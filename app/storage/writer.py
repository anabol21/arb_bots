"""Background parquet publisher for the VPS-local live dataset.

Hot path only enqueues raw record batches. Normalization, partitioning, disk
I/O, read-back validation, and atomic rename run in a dedicated worker thread.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from app.schema.lean_event import LEAN_BAR_5M_BODY_COLS, LEAN_TICK_BODY_COLS
from app.schema.parquet_layout import PARTITION_DATE_COL
from app.schema.spread_event import SPREAD_EVENT_BODY_COLS, lean_schema_enabled

from .mount_state import MountFailureState
from .paths import (
    assert_storage_root_writable,
    ensure_storage_dirs,
    is_mount_failure_error,
    partition_dir,
    tmp_dir,
)
from .spool import DurableSpool

_SENTINEL = object()

# Tick schemas: "v1" (canary default) | "lean". Bars: "bar_5m".
SchemaMode = str

_LEAN_TS_COLS: tuple[str, ...] = (
    "event_local_ts_ms",
    "calc_local_ts_ms",
    "okx_local_recv_ts_ms",
    "okx_ts_ms",
    "bybit_local_recv_ts_ms",
    "bybit_ts_ms",
)
_BAR_TS_COLS: tuple[str, ...] = ("bar_start_ts_ms", "bar_end_ts_ms")


@dataclass
class PublisherMetrics:
    published_files_total: int = 0
    published_rows_total: int = 0
    bytes_written_total: int = 0
    failures_total: int = 0
    orphan_tmp_cleaned: int = 0
    last_write_latency_ms: float | None = None
    last_failure_reason: str | None = None
    backpressure_hits_total: int = 0
    enqueued_jobs_total: int = 0
    published_jobs_total: int = 0
    spooled_jobs_total: int = 0
    quarantined_jobs_total: int = 0
    failed_jobs_total: int = 0
    accepted_records_total: int = 0
    rejected_records_total: int = 0
    quarantined_records_total: int = 0


@dataclass
class NormalizedBatch:
    dataframe: pd.DataFrame
    rejected: list[dict[str, Any]]


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_tick_schema_mode() -> SchemaMode:
    """Env-driven tick mode; default ``v1`` for canary continuity."""
    return "lean" if lean_schema_enabled() else "v1"


def collect_bars_enabled() -> bool:
    """Bar channel collection; default OFF (independent of lean tick schema)."""
    return _truthy_env("SPREAD_COLLECT_BARS")


def _finalize_normalized(
    records: list[dict[str, Any]],
    accepted: pd.DataFrame,
    reasons: list[list[str]],
    body_cols: tuple[str, ...],
) -> NormalizedBatch:
    keep_cols = [PARTITION_DATE_COL, *body_cols]
    if not accepted.empty:
        accepted = accepted[[col for col in keep_cols if col in accepted.columns]]
    else:
        accepted = pd.DataFrame(columns=list(keep_cols))
    rejected = [
        {"record": records[index], "reasons": row_reasons}
        for index, row_reasons in enumerate(reasons)
        if row_reasons
    ]
    if len(accepted) + len(rejected) != len(records):
        raise AssertionError(
            "normalization accounting mismatch: "
            f"raw={len(records)} accepted={len(accepted)} rejected={len(rejected)}"
        )
    return NormalizedBatch(accepted, rejected)


def _cast_int64_ms(df: pd.DataFrame, cols: tuple[str, ...]) -> pd.DataFrame:
    for col in cols:
        if col not in df.columns:
            continue
        numeric = pd.to_numeric(df[col], errors="coerce")
        # nullable Int64 avoids float display noise for model consumers
        df[col] = numeric.round().astype("Int64")
    return df


def normalize_v1_records(records: list[dict[str, Any]]) -> NormalizedBatch:
    """Full canary v1 body (spread_*, freshness, event_dt, latencies)."""
    if not records:
        return NormalizedBatch(pd.DataFrame(), [])

    df = pd.DataFrame(records)
    if df.empty:
        return NormalizedBatch(pd.DataFrame(), [])

    reasons: list[list[str]] = [[] for _ in records]
    num_cols = [
        "spread_long",
        "spread_short",
        "okx_latency_ms",
        "bybit_latency_ms",
        "calc_local_ts_ms",
        "okx_local_recv_ts_ms",
        "okx_ts_ms",
        "bybit_local_recv_ts_ms",
        "bybit_ts_ms",
        "okx_bid_price",
        "okx_bid_size",
        "okx_ask_price",
        "okx_ask_size",
        "bybit_bid_price",
        "bybit_bid_size",
        "bybit_ask_price",
        "bybit_ask_size",
    ]
    for col in num_cols:
        if col not in df.columns:
            df[col] = None
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if "base_coin" not in df.columns:
        df["base_coin"] = None
    valid_base_coin = df["base_coin"].notna() & df["base_coin"].astype(str).str.strip().ne("")
    for index in df.index[~valid_base_coin]:
        reasons[int(index)].append("invalid_base_coin")
    df["base_coin"] = df["base_coin"].astype(str).str.strip()

    df["okx_freshness_ms"] = df["calc_local_ts_ms"] - df["okx_local_recv_ts_ms"]
    df["bybit_freshness_ms"] = df["calc_local_ts_ms"] - df["bybit_local_recv_ts_ms"]
    df["event_local_ts_ms"] = df["okx_local_recv_ts_ms"]
    if "trigger" in df.columns:
        mask_bybit = df["trigger"].eq("bybit")
        df.loc[mask_bybit, "event_local_ts_ms"] = df.loc[
            mask_bybit, "bybit_local_recv_ts_ms"
        ]

    df["event_dt"] = pd.to_datetime(
        df["event_local_ts_ms"],
        unit="ms",
        errors="coerce",
    )
    valid_event_dt = df["event_dt"].notna()
    for index in df.index[~valid_event_dt]:
        reasons[int(index)].append("invalid_event_dt")

    accepted_mask = valid_base_coin & valid_event_dt
    accepted = df.loc[accepted_mask].copy()
    if not accepted.empty:
        accepted["event_date"] = accepted["event_dt"].dt.strftime("%Y-%m-%d")
        accepted["max_freshness_ms"] = accepted[
            ["okx_freshness_ms", "bybit_freshness_ms"]
        ].max(axis=1)
        accepted["max_latency_ms"] = accepted[
            ["okx_latency_ms", "bybit_latency_ms"]
        ].max(axis=1)

    return _finalize_normalized(records, accepted, reasons, SPREAD_EVENT_BODY_COLS)


def normalize_lean_tick_records(records: list[dict[str, Any]]) -> NormalizedBatch:
    """Lean 16-column tick body; derive event_date from ms stamps (no event_dt)."""
    if not records:
        return NormalizedBatch(pd.DataFrame(), [])

    df = pd.DataFrame(records)
    if df.empty:
        return NormalizedBatch(pd.DataFrame(), [])

    reasons: list[list[str]] = [[] for _ in records]
    num_cols = [
        "event_local_ts_ms",
        "calc_local_ts_ms",
        "okx_local_recv_ts_ms",
        "okx_ts_ms",
        "bybit_local_recv_ts_ms",
        "bybit_ts_ms",
        "okx_bid_price",
        "okx_bid_size",
        "okx_ask_price",
        "okx_ask_size",
        "bybit_bid_price",
        "bybit_bid_size",
        "bybit_ask_price",
        "bybit_ask_size",
    ]
    for col in num_cols:
        if col not in df.columns:
            df[col] = None
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if "base_coin" not in df.columns:
        df["base_coin"] = None
    valid_base_coin = df["base_coin"].notna() & df["base_coin"].astype(str).str.strip().ne("")
    for index in df.index[~valid_base_coin]:
        reasons[int(index)].append("invalid_base_coin")
    df["base_coin"] = df["base_coin"].astype(str).str.strip()

    if "trigger" not in df.columns:
        df["trigger"] = None
    df["trigger"] = df["trigger"].astype(str)

    # Prefer caller event_local_ts_ms; fall back to trigger recv like v1.
    if "event_local_ts_ms" not in df.columns or df["event_local_ts_ms"].isna().all():
        df["event_local_ts_ms"] = df["okx_local_recv_ts_ms"]
        mask_bybit = df["trigger"].eq("bybit")
        df.loc[mask_bybit, "event_local_ts_ms"] = df.loc[
            mask_bybit, "bybit_local_recv_ts_ms"
        ]
    else:
        missing = df["event_local_ts_ms"].isna()
        df.loc[missing, "event_local_ts_ms"] = df.loc[missing, "okx_local_recv_ts_ms"]
        mask_bybit = missing & df["trigger"].eq("bybit")
        df.loc[mask_bybit, "event_local_ts_ms"] = df.loc[
            mask_bybit, "bybit_local_recv_ts_ms"
        ]

    event_dt = pd.to_datetime(df["event_local_ts_ms"], unit="ms", errors="coerce")
    valid_ts = event_dt.notna()
    for index in df.index[~valid_ts]:
        reasons[int(index)].append("invalid_event_local_ts_ms")

    accepted_mask = valid_base_coin & valid_ts
    accepted = df.loc[accepted_mask].copy()
    if not accepted.empty:
        accepted["event_date"] = event_dt.loc[accepted_mask].dt.strftime("%Y-%m-%d")
        accepted = _cast_int64_ms(accepted, _LEAN_TS_COLS)

    return _finalize_normalized(records, accepted, reasons, LEAN_TICK_BODY_COLS)


def normalize_bar_records(records: list[dict[str, Any]]) -> NormalizedBatch:
    """Closed 5m bar volume rows (LEAN_BAR_5M_BODY_COLS)."""
    if not records:
        return NormalizedBatch(pd.DataFrame(), [])

    df = pd.DataFrame(records)
    if df.empty:
        return NormalizedBatch(pd.DataFrame(), [])

    reasons: list[list[str]] = [[] for _ in records]
    for col in ("bar_start_ts_ms", "bar_end_ts_ms", "volume"):
        if col not in df.columns:
            df[col] = None
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if "base_coin" not in df.columns:
        df["base_coin"] = None
    valid_base_coin = df["base_coin"].notna() & df["base_coin"].astype(str).str.strip().ne("")
    for index in df.index[~valid_base_coin]:
        reasons[int(index)].append("invalid_base_coin")
    df["base_coin"] = df["base_coin"].astype(str).str.strip()

    if "ref_exchange" not in df.columns:
        df["ref_exchange"] = None
    valid_ref = df["ref_exchange"].notna() & df["ref_exchange"].astype(str).str.strip().ne("")
    for index in df.index[~valid_ref]:
        reasons[int(index)].append("invalid_ref_exchange")
    df["ref_exchange"] = df["ref_exchange"].astype(str).str.strip()

    valid_start = df["bar_start_ts_ms"].notna()
    for index in df.index[~valid_start]:
        reasons[int(index)].append("invalid_bar_start_ts_ms")
    valid_volume = df["volume"].notna()
    for index in df.index[~valid_volume]:
        reasons[int(index)].append("invalid_volume")

    accepted_mask = valid_base_coin & valid_ref & valid_start & valid_volume
    accepted = df.loc[accepted_mask].copy()
    if not accepted.empty:
        starts = pd.to_datetime(accepted["bar_start_ts_ms"], unit="ms", errors="coerce")
        accepted["event_date"] = starts.dt.strftime("%Y-%m-%d")
        accepted = _cast_int64_ms(accepted, _BAR_TS_COLS)

    return _finalize_normalized(records, accepted, reasons, LEAN_BAR_5M_BODY_COLS)


def normalize_records(
    records: list[dict[str, Any]],
    *,
    schema_mode: SchemaMode | None = None,
) -> NormalizedBatch:
    """Normalize raw records while preserving every rejected source record.

    ``schema_mode``:
    - ``None`` / omitted: env-driven tick mode (``v1`` default, ``lean`` if flagged)
    - ``v1`` / ``lean``: tick bodies
    - ``bar_5m``: closed candle volume rows
    """
    mode = resolve_tick_schema_mode() if schema_mode is None else schema_mode
    if mode == "bar_5m":
        return normalize_bar_records(records)
    if mode == "lean":
        return normalize_lean_tick_records(records)
    if mode == "v1":
        return normalize_v1_records(records)
    raise ValueError(f"unsupported schema_mode: {mode!r}")


def _sort_column_for_mode(schema_mode: SchemaMode) -> str:
    if schema_mode == "bar_5m":
        return "bar_start_ts_ms"
    if schema_mode == "lean":
        return "event_local_ts_ms"
    return "event_dt"


class ParquetPublisher:
    """Bounded raw queue with explicit published/spooled/quarantined outcomes."""

    def __init__(
        self,
        parquet_root: Path,
        logger: logging.Logger,
        failed_batches_logger: logging.Logger,
        mount_failure_state: MountFailureState,
        spool: DurableSpool,
        *,
        max_queue: int = 4,
        shutdown_timeout_sec: float = 120.0,
        schema_mode: SchemaMode | None = None,
        name: str = "parquet-publisher",
    ) -> None:
        if not parquet_root.is_absolute():
            raise ValueError(f"parquet_root must be absolute, got: {parquet_root}")
        if max_queue < 1:
            raise ValueError("max_queue must be >= 1")
        if schema_mode is not None and schema_mode not in {"v1", "lean", "bar_5m"}:
            raise ValueError(f"unsupported schema_mode: {schema_mode!r}")

        self.parquet_root = parquet_root
        self.logger = logger
        self.failed_batches_logger = failed_batches_logger
        self.mount_failure_state = mount_failure_state
        self.spool = spool
        self.shutdown_timeout_sec = shutdown_timeout_sec
        # None => resolve tick mode from env at each normalize (Option B).
        self.schema_mode = schema_mode
        self.name = name

        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max_queue)
        self._metrics = PublisherMetrics()
        self._metrics_lock = threading.Lock()
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._started = False
        self._closed = False
        self._retained_failed_jobs: list[dict[str, Any]] = []
        self._backpressure_active = False

    def effective_schema_mode(self) -> SchemaMode:
        if self.schema_mode is not None:
            return self.schema_mode
        return resolve_tick_schema_mode()

    def start(self) -> None:
        if self._started:
            return
        ensure_storage_dirs(self.parquet_root)
        cleaned = self._cleanup_orphan_tmps()
        with self._metrics_lock:
            self._metrics.orphan_tmp_cleaned = cleaned
        self._thread = threading.Thread(
            target=self._worker_loop,
            name=self.name,
            daemon=True,
        )
        self._thread.start()
        self._started = True
        self.logger.info(
            "publisher_started | name=%s | schema_mode=%s | root=%s | tmp=%s | "
            "max_queue=%s | orphan_tmp_cleaned=%s",
            self.name,
            self.effective_schema_mode(),
            self.parquet_root,
            tmp_dir(self.parquet_root),
            self._queue.maxsize,
            cleaned,
        )

    def queue_depth(self) -> int:
        return self._queue.qsize()

    def metrics_snapshot(self) -> dict[str, Any]:
        with self._metrics_lock:
            m = self._metrics
            return {
                "queue_depth": self.queue_depth(),
                "published_files_total": m.published_files_total,
                "published_rows_total": m.published_rows_total,
                "bytes_written_total": m.bytes_written_total,
                "failures_total": m.failures_total,
                "backpressure_hits_total": m.backpressure_hits_total,
                "enqueued_jobs_total": m.enqueued_jobs_total,
                "published_jobs_total": m.published_jobs_total,
                "spooled_jobs_total": m.spooled_jobs_total,
                "quarantined_jobs_total": m.quarantined_jobs_total,
                "failed_jobs_total": m.failed_jobs_total,
                "accepted_records_total": m.accepted_records_total,
                "rejected_records_total": m.rejected_records_total,
                "quarantined_records_total": m.quarantined_records_total,
                "last_write_latency_ms": m.last_write_latency_ms,
                "last_failure_reason": m.last_failure_reason,
                "orphan_tmp_cleaned": m.orphan_tmp_cleaned,
            }

    def ready_for_enqueue(self, rows: int) -> bool:
        """Cheap hot-path gate that suppresses repeated queue-full work and logs."""
        if not self._started or self._closed or self.mount_failure_state.is_dead():
            return False
        if not self._queue.full():
            return True
        if not self._backpressure_active:
            self._backpressure_active = True
            with self._metrics_lock:
                self._metrics.backpressure_hits_total += 1
            self.logger.warning(
                "backpressure_hit | policy=retain_raw_in_buffer | rows=%s | "
                "queue_depth=%s",
                rows,
                self.queue_depth(),
            )
        return False

    def enqueue_records(self, records: list[dict[str, Any]]) -> bool:
        """Try a non-blocking raw enqueue; False requires caller buffer retention."""
        if self.mount_failure_state.is_dead():
            self.logger.error(
                "enqueue_rejected | reason=mount_dead | rows=%s",
                len(records),
            )
            return False
        if not self._started or self._closed:
            self._record_failure("publisher_not_running")
            self.logger.error(
                "failed | reason=publisher_not_running | rows=%s",
                len(records),
            )
            return False
        if not records:
            return True

        job = {
            "job_id": self._next_batch_id(),
            "records": records,
            "rows": len(records),
            "enqueued_at": time.monotonic(),
        }
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            self.ready_for_enqueue(job["rows"])
            return False
        with self._metrics_lock:
            self._metrics.enqueued_jobs_total += 1
        self._backpressure_active = False
        return True

    def durably_spool_records(
        self,
        records: list[dict[str, Any]],
        *,
        reason: str,
    ) -> bool:
        """Synchronously account a raw shutdown batch using local durability."""
        if not records:
            return True
        job = {
            "job_id": self._next_batch_id(),
            "records": records,
            "rows": len(records),
            "enqueued_at": time.monotonic(),
        }
        outcome = self._store_job(job, force_spool=True, reason=reason)
        return outcome in {"locally_spooled", "durably_quarantined"}

    def shutdown(self) -> None:
        if not self._started:
            return
        if self._closed:
            self._join_worker()
            return
        self._closed = True
        self.logger.info(
            "publisher_shutdown_begin | queue_depth=%s | mount_dead=%s",
            self.queue_depth(),
            self.mount_failure_state.is_dead(),
        )
        if self.mount_failure_state.is_dead():
            self._join_worker(timeout_sec=self.shutdown_timeout_sec)
        else:
            try:
                self._queue.put(_SENTINEL, timeout=self.shutdown_timeout_sec)
            except queue.Full:
                self._record_failure("shutdown_enqueue_sentinel_timeout")
                self.logger.error(
                    "failed | reason=shutdown_enqueue_sentinel_timeout | queue_depth=%s",
                    self.queue_depth(),
                )
            self._join_worker(timeout_sec=self.shutdown_timeout_sec)

        snap = self.metrics_snapshot()
        worker_alive = self._thread is not None and self._thread.is_alive()
        fully_published = (
            not worker_alive
            and snap["queue_depth"] == 0
            and snap["enqueued_jobs_total"] == snap["published_jobs_total"]
            and snap["failed_jobs_total"] == 0
        )
        fully_spooled_or_published = (
            not worker_alive
            and snap["queue_depth"] == 0
            and snap["enqueued_jobs_total"]
            == (
                snap["published_jobs_total"]
                + snap["spooled_jobs_total"]
                + snap["quarantined_jobs_total"]
            )
            and snap["failed_jobs_total"] == 0
        )
        if fully_published:
            self.logger.info(
                "shutdown_flush_done | published_files=%s | published_rows=%s | "
                "bytes_written=%s | published_jobs=%s",
                snap["published_files_total"],
                snap["published_rows_total"],
                snap["bytes_written_total"],
                snap["published_jobs_total"],
            )
        elif fully_spooled_or_published:
            self.logger.warning(
                "shutdown_spool_done | published_jobs=%s | spooled_jobs=%s | "
                "quarantined_jobs=%s | spool_files_count=%s | "
                "spool_bytes_total=%s",
                snap["published_jobs_total"],
                snap["spooled_jobs_total"],
                snap["quarantined_jobs_total"],
                self.spool.metrics_snapshot()["spool_files_count"],
                self.spool.metrics_snapshot()["spool_bytes_total"],
            )
        else:
            self.logger.error(
                "shutdown_flush_incomplete | queue_depth=%s | enqueued_jobs=%s | "
                "published_jobs=%s | spooled_jobs=%s | quarantined_jobs=%s | "
                "failed_jobs=%s | worker_alive=%s | mount_dead=%s",
                snap["queue_depth"],
                snap["enqueued_jobs_total"],
                snap["published_jobs_total"],
                snap["spooled_jobs_total"],
                snap["quarantined_jobs_total"],
                snap["failed_jobs_total"],
                worker_alive,
                self.mount_failure_state.is_dead(),
            )

    def _join_worker(self, *, timeout_sec: float | None = None) -> None:
        if self._thread is not None and self._thread.is_alive():
            timeout = self.shutdown_timeout_sec if timeout_sec is None else timeout_sec
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                self._record_failure("shutdown_worker_join_timeout")
                self.logger.error(
                    "failed | reason=shutdown_worker_join_timeout | queue_depth=%s",
                    self.queue_depth(),
                )

    def _cleanup_orphan_tmps(self) -> int:
        staging = tmp_dir(self.parquet_root)
        staging.mkdir(parents=True, exist_ok=True)
        cleaned = 0
        for path in staging.glob("*.parquet.tmp"):
            try:
                path.unlink()
                cleaned += 1
                self.logger.warning("orphan_tmp_cleaned | path=%s", path)
            except OSError as exc:
                self.logger.error(
                    "failed | reason=orphan_tmp_cleanup_error | path=%s | error=%r",
                    path,
                    exc,
                )
        return cleaned

    def _next_batch_id(self) -> str:
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        utc_ts_ms = int(time.time() * 1000)
        return f"{utc_ts_ms}_{os.getpid()}_{seq:06d}_{uuid.uuid4().hex[:8]}"

    def _record_failure(self, reason: str) -> None:
        with self._metrics_lock:
            self._metrics.failures_total += 1
            self._metrics.last_failure_reason = reason

    def _worker_loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._closed:
                    return
                continue
            if item is _SENTINEL:
                self._queue.task_done()
                return

            outcome = self._publish_job(item)
            if outcome == "published":
                with self._metrics_lock:
                    self._metrics.published_jobs_total += 1
                self._queue.task_done()
                continue
            if outcome == "locally_spooled":
                with self._metrics_lock:
                    self._metrics.spooled_jobs_total += 1
                self._queue.task_done()
                continue
            if outcome == "durably_quarantined":
                with self._metrics_lock:
                    self._metrics.quarantined_jobs_total += 1
                self._queue.task_done()
                continue

            with self._metrics_lock:
                self._metrics.failed_jobs_total += 1
            if self.mount_failure_state.is_dead():
                self._retained_failed_jobs.append(item)
                return
            self._queue.task_done()

    def _publish_job(self, job: dict[str, Any]) -> str:
        try:
            return self._store_job(
                job,
                force_spool=False,
                reason="writer_processing",
            )
        except Exception as exc:
            self._record_failure(f"writer_job_error:{type(exc).__name__}")
            self.logger.exception(
                "writer_job_error | job_id=%s | rows=%s",
                job["job_id"],
                job["rows"],
            )
            return self._quarantine_records(
                job_id=str(job["job_id"]),
                records=[
                    {"record": record, "reasons": ["writer_job_error"]}
                    for record in job["records"]
                ],
                reason=f"writer_job_error:{type(exc).__name__}",
            )

    def _store_job(
        self,
        job: dict[str, Any],
        *,
        force_spool: bool,
        reason: str,
    ) -> str:
        records = job["records"]
        raw_count = len(records)
        mode = self.effective_schema_mode()
        try:
            normalized = normalize_records(records, schema_mode=mode)
        except Exception as exc:
            self._record_failure(f"normalization_error:{type(exc).__name__}")
            return self._quarantine_records(
                job_id=str(job["job_id"]),
                records=[{"record": record, "reasons": ["normalization_error"]} for record in records],
                reason=f"normalization_error:{type(exc).__name__}",
            )

        df = normalized.dataframe
        rejected_count = len(normalized.rejected)
        accepted_count = len(df)
        with self._metrics_lock:
            self._metrics.accepted_records_total += accepted_count
            self._metrics.rejected_records_total += rejected_count

        rejected_outcome = "none"
        if normalized.rejected:
            rejected_outcome = self._quarantine_records(
                job_id=str(job["job_id"]),
                records=normalized.rejected,
                reason="dataframe_normalization_rejected",
            )
            if rejected_outcome == "failed":
                return "failed"

        if df.empty:
            self._log_job_accounting(
                job_id=str(job["job_id"]),
                raw_count=raw_count,
                accepted_count=0,
                rejected_count=rejected_count,
                accepted_outcome="none",
                rejected_outcome=rejected_outcome,
            )
            return "durably_quarantined"

        sort_col = _sort_column_for_mode(mode)
        if sort_col not in df.columns:
            raise AssertionError(
                f"normalized batch missing sort column {sort_col!r} for mode={mode}"
            )

        outcome = "published"
        for (base_coin, event_date), sub in df.groupby(
            ["base_coin", "event_date"], sort=False
        ):
            sub = sub.sort_values(sort_col).reset_index(drop=True)
            batch_id = self._next_batch_id()
            if force_spool or self.mount_failure_state.is_dead():
                partition_outcome = self._spool_partition(
                    job_id=str(job["job_id"]),
                    batch_id=batch_id,
                    base_coin=str(base_coin),
                    event_date=str(event_date),
                    sub=sub,
                    reason=reason if force_spool else "mount_already_dead",
                )
            else:
                partition_outcome = self._publish_partition(
                    batch_id=batch_id,
                    job_id=str(job["job_id"]),
                    base_coin=str(base_coin),
                    event_date=str(event_date),
                    sub=sub,
                )
            if partition_outcome == "failed":
                quarantine_outcome = self._quarantine_records(
                    job_id=str(job["job_id"]),
                    records=sub.to_dict(orient="records"),
                    reason="accepted_partition_storage_failure",
                )
                if quarantine_outcome == "failed":
                    return "failed"
                outcome = "durably_quarantined"
                continue
            if (
                partition_outcome == "locally_spooled"
                and outcome != "durably_quarantined"
            ):
                outcome = "locally_spooled"
        self._log_job_accounting(
            job_id=str(job["job_id"]),
            raw_count=raw_count,
            accepted_count=accepted_count,
            rejected_count=rejected_count,
            accepted_outcome=outcome,
            rejected_outcome=rejected_outcome,
        )
        return outcome

    def _quarantine_records(
        self,
        *,
        job_id: str,
        records: list[dict[str, Any]],
        reason: str,
    ) -> str:
        try:
            path = self.spool.write_quarantine(
                records=records,
                batch_id=self._next_batch_id(),
                reason=reason,
            )
        except Exception as exc:
            self.logger.critical(
                "quarantine_write_failed | job_id=%s | records=%s | "
                "reason=%s | error=%r",
                job_id,
                len(records),
                reason,
                exc,
            )
            return "failed"
        with self._metrics_lock:
            self._metrics.quarantined_records_total += len(records)
        self.logger.error(
            "durably_quarantined | job_id=%s | records=%s | reason=%s | path=%s",
            job_id,
            len(records),
            reason,
            path,
        )
        return "durably_quarantined"

    def _log_job_accounting(
        self,
        *,
        job_id: str,
        raw_count: int,
        accepted_count: int,
        rejected_count: int,
        accepted_outcome: str,
        rejected_outcome: str,
    ) -> None:
        accounting_ok = raw_count == accepted_count + rejected_count
        self.logger.info(
            "job_accounted | job_id=%s | raw_records=%s | accepted_records=%s | "
            "rejected_records=%s | accepted_outcome=%s | rejected_outcome=%s | "
            "accounting_ok=%s",
            job_id,
            raw_count,
            accepted_count,
            rejected_count,
            accepted_outcome,
            rejected_outcome,
            str(accounting_ok).lower(),
        )
        if not accounting_ok:
            raise AssertionError(f"job accounting mismatch: job_id={job_id}")

    def _spool_partition(
        self,
        *,
        job_id: str,
        batch_id: str,
        base_coin: str,
        event_date: str,
        sub: pd.DataFrame,
        reason: str,
    ) -> str:
        rows = int(len(sub))
        try:
            spool_path = self.spool.write_partition(
                df=sub,
                base_coin=base_coin,
                event_date=event_date,
                batch_id=batch_id,
            )
        except Exception as exc:
            self.logger.critical(
                "spool_write_failed | batch_id=%s | job_id=%s | rows=%s | "
                "reason=%s | error=%r",
                batch_id,
                job_id,
                rows,
                reason,
                exc,
            )
            self.failed_batches_logger.error(
                "failed_batch | source=spool | batch_id=%s | job_id=%s | "
                "rows=%s | base_coin=%s | event_date=%s | durability=none | error=%r",
                batch_id,
                job_id,
                rows,
                base_coin,
                event_date,
                exc,
            )
            return "failed"

        self.failed_batches_logger.error(
            "failed_batch | source=writer | batch_id=%s | job_id=%s | "
            "rows=%s | base_coin=%s | event_date=%s | remote_published=false | "
            "durability=local_spool | spool_path=%s | reason=%s",
            batch_id,
            job_id,
            rows,
            base_coin,
            event_date,
            spool_path,
            reason,
        )
        return "locally_spooled"

    def _publish_partition(
        self,
        *,
        batch_id: str,
        job_id: str,
        base_coin: str,
        event_date: str,
        sub: pd.DataFrame,
    ) -> str:
        rows = int(len(sub))
        started = time.monotonic()
        tmp_path = tmp_dir(self.parquet_root) / f"batch_{batch_id}.parquet.tmp"
        final_path = (
            partition_dir(self.parquet_root, base_coin, event_date)
            / f"batch_{batch_id}.parquet"
        )

        self.logger.info(
            "write_started | batch_id=%s | rows=%s | base_coin=%s | "
            "event_date=%s | tmp=%s",
            batch_id,
            rows,
            base_coin,
            event_date,
            tmp_path,
        )

        try:
            assert_storage_root_writable(self.parquet_root, probe_write=False)
            part_dir = final_path.parent
            part_dir.mkdir(parents=True, exist_ok=True)
            if final_path.exists():
                raise FileExistsError(f"final path already exists: {final_path}")

            table = pa.Table.from_pandas(
                sub.drop(columns=["event_date"]),
                preserve_index=False,
            )
            pq.write_table(table, tmp_path, compression="zstd")
            self._fsync_file(tmp_path)

            read_back = pq.ParquetFile(tmp_path)
            if read_back.metadata.num_rows != rows:
                raise ValueError(
                    f"read_back row mismatch: expected={rows} "
                    f"got={read_back.metadata.num_rows}"
                )

            os.replace(tmp_path, final_path)
            self._fsync_dir(part_dir)

            bytes_written = final_path.stat().st_size
            latency_ms = (time.monotonic() - started) * 1000.0
            with self._metrics_lock:
                self._metrics.published_files_total += 1
                self._metrics.published_rows_total += rows
                self._metrics.bytes_written_total += bytes_written
                self._metrics.last_write_latency_ms = round(latency_ms, 3)

            self.logger.info(
                "published | batch_id=%s | rows=%s | bytes=%s | "
                "write_latency_ms=%.1f | path=%s | queue_depth=%s",
                batch_id,
                rows,
                bytes_written,
                latency_ms,
                final_path,
                self.queue_depth(),
            )
            return "published"
        except Exception as exc:
            reason = f"publish_error:{type(exc).__name__}"
            self._record_failure(reason)
            storage_failure = is_mount_failure_error(exc)
            if not storage_failure:
                try:
                    assert_storage_root_writable(
                        self.parquet_root,
                        probe_write=False,
                    )
                except Exception:
                    storage_failure = True

            if storage_failure:
                self.logger.critical(
                    "primary_storage_lost | source=writer | batch_id=%s | job_id=%s | "
                    "error=%r | tmp_cleanup=skipped",
                    batch_id,
                    job_id,
                    exc,
                )
                self.mount_failure_state.mark_dead(
                    source="writer",
                    reason=repr(exc),
                    batch_id=batch_id,
                )
                return self._spool_partition(
                    job_id=job_id,
                    batch_id=batch_id,
                    base_coin=base_coin,
                    event_date=event_date,
                    sub=sub,
                    reason=f"primary_publish_error:{type(exc).__name__}",
                )

            self.logger.error(
                "failed | batch_id=%s | reason=%s | error=%r | tmp=%s | final=%s",
                batch_id,
                reason,
                exc,
                tmp_path,
                final_path,
            )
            self._safe_unlink(tmp_path)
            return self._spool_partition(
                job_id=job_id,
                batch_id=batch_id,
                base_coin=base_coin,
                event_date=event_date,
                sub=sub,
                reason=reason,
            )

    @staticmethod
    def _fsync_file(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
