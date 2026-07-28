"""Crash-safe local parquet spool used when mounted storage is unavailable."""

from __future__ import annotations

import logging
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .mount_state import MountFailureState

DEFAULT_SPOOL_ROOT = Path("/root/spool")
DEFAULT_SPOOL_MAX_BYTES = 20 * 1024 * 1024 * 1024
DEFAULT_SPOOL_MAX_FILES = 100_000
DEFAULT_SPOOL_TTL_HOURS = 6.0


class SpoolQuotaExceeded(RuntimeError):
    """Writing another durable spool file would exceed configured limits."""


def _positive_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    value = default if raw is None else int(raw)
    if value < 1:
        raise ValueError(f"{name} must be >= 1, got: {value}")
    return value


def _positive_env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    value = default if raw is None else float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got: {value}")
    return value


class DurableSpool:
    """Atomic local spool with explicit quota, TTL, and recovery metrics."""

    def __init__(
        self,
        logger: logging.Logger,
        mount_failure_state: MountFailureState,
        *,
        root: Path = DEFAULT_SPOOL_ROOT,
    ) -> None:
        if not root.is_absolute():
            raise ValueError(f"spool root must be absolute, got: {root}")
        self.root = root
        self.logger = logger
        self.mount_failure_state = mount_failure_state
        self.max_bytes = _positive_env_int(
            "SPREAD_SPOOL_MAX_BYTES",
            DEFAULT_SPOOL_MAX_BYTES,
        )
        self.max_files = _positive_env_int(
            "SPREAD_SPOOL_MAX_FILES",
            DEFAULT_SPOOL_MAX_FILES,
        )
        self.ttl_hours = _positive_env_float(
            "SPREAD_SPOOL_TTL_HOURS",
            DEFAULT_SPOOL_TTL_HOURS,
        )
        self._lock = threading.RLock()
        self._spool_files_count = 0
        self._spool_bytes_total = 0
        self._spool_recovered_total = 0
        self._spool_recovery_failed_total = 0
        self._stale_alerted: set[Path] = set()

        self.root.mkdir(parents=True, exist_ok=True)
        self.refresh_inventory_and_monitor()

    def iter_spool_files(self) -> list[Path]:
        with self._lock:
            return sorted(self.root.glob("*/*/batch_*.parquet"))

    def _inventory_files(self) -> list[Path]:
        return sorted(path for path in self.root.rglob("*") if path.is_file())

    def write_partition(
        self,
        *,
        df: pd.DataFrame,
        base_coin: str,
        event_date: str,
        batch_id: str,
    ) -> Path:
        """Write one partition atomically and return its durable spool path."""
        final_dir = self.root / base_coin / event_date
        final_path = final_dir / f"batch_{batch_id}.parquet"
        tmp_path = final_dir / f".batch_{batch_id}.{uuid.uuid4().hex}.parquet.tmp"
        rows = int(len(df))

        with self._lock:
            if final_path.exists():
                self._validate_rows(final_path, rows)
                return final_path
            self._assert_file_quota(additional_files=1)
            final_dir.mkdir(parents=True, exist_ok=True)

            try:
                table = pa.Table.from_pandas(
                    df.drop(columns=["event_date"], errors="ignore"),
                    preserve_index=False,
                )
                pq.write_table(table, tmp_path, compression="zstd")
                self._fsync_file(tmp_path)
                self._validate_rows(tmp_path, rows)
                file_bytes = tmp_path.stat().st_size
                self._assert_byte_quota(additional_bytes=file_bytes)
                os.replace(tmp_path, final_path)
                self._fsync_dir(final_dir)
            except Exception:
                self._safe_unlink(tmp_path)
                raise

            self._spool_files_count += 1
            self._spool_bytes_total += file_bytes
            self.logger.warning(
                "spool_written | batch_id=%s | rows=%s | bytes=%s | path=%s",
                batch_id,
                rows,
                file_bytes,
                final_path,
            )
            return final_path

    def write_quarantine(
        self,
        *,
        records: list[dict[str, Any]],
        batch_id: str,
        reason: str,
    ) -> Path:
        """Atomically persist rejected raw records outside recovery partitions."""
        final_dir = self.root / "_quarantine"
        final_path = final_dir / f"quarantine_{batch_id}.json"
        tmp_path = final_dir / f".quarantine_{batch_id}.{uuid.uuid4().hex}.tmp"
        payload = {
            "batch_id": batch_id,
            "reason": reason,
            "record_count": len(records),
            "records": records,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=repr,
        ).encode("utf-8")

        with self._lock:
            if final_path.exists():
                return final_path
            self._assert_file_quota(additional_files=1)
            self._assert_byte_quota(additional_bytes=len(encoded))
            final_dir.mkdir(parents=True, exist_ok=True)
            try:
                with tmp_path.open("xb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, final_path)
                self._fsync_dir(final_dir)
            except Exception:
                self._safe_unlink(tmp_path)
                raise

            self._spool_files_count += 1
            self._spool_bytes_total += len(encoded)
            self.logger.error(
                "quarantine_written | batch_id=%s | records=%s | bytes=%s | "
                "reason=%s | path=%s",
                batch_id,
                len(records),
                len(encoded),
                reason,
                final_path,
            )
            return final_path

    def mark_recovered(self, path: Path) -> None:
        """Delete a confirmed-published spool file and update metrics."""
        with self._lock:
            file_bytes = path.stat().st_size
            path.unlink()
            self._fsync_dir(path.parent)
            self._spool_files_count = max(0, self._spool_files_count - 1)
            self._spool_bytes_total = max(
                0,
                self._spool_bytes_total - file_bytes,
            )
            self._spool_recovered_total += 1
            self._stale_alerted.discard(path)
            self._remove_empty_partition_dirs(path.parent)

    def mark_recovery_failed(self) -> None:
        with self._lock:
            self._spool_recovery_failed_total += 1

    def refresh_inventory_and_monitor(self) -> dict[str, Any]:
        """Refresh current usage, enforce quota, and emit one TTL alert per file."""
        with self._lock:
            files = self._inventory_files()
            sizes = [(path, path.stat().st_size) for path in files]
            self._spool_files_count = len(sizes)
            self._spool_bytes_total = sum(size for _, size in sizes)

            if (
                self._spool_files_count > self.max_files
                or self._spool_bytes_total > self.max_bytes
            ):
                self._raise_quota_exceeded(
                    files=self._spool_files_count,
                    bytes_total=self._spool_bytes_total,
                )

            stale_before = time.time() - (self.ttl_hours * 3600.0)
            for path, _ in sizes:
                if path.stat().st_mtime < stale_before and path not in self._stale_alerted:
                    age_hours = (time.time() - path.stat().st_mtime) / 3600.0
                    self.logger.error(
                        "spool_stale_alert | path=%s | age_hours=%.3f | ttl_hours=%.3f",
                        path,
                        age_hours,
                        self.ttl_hours,
                    )
                    self._stale_alerted.add(path)
            self._stale_alerted.intersection_update(path for path, _ in sizes)
            return self.metrics_snapshot()

    def metrics_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "spool_files_count": self._spool_files_count,
                "spool_bytes_total": self._spool_bytes_total,
                "spool_recovered_total": self._spool_recovered_total,
                "spool_recovery_failed_total": self._spool_recovery_failed_total,
                "spool_max_bytes": self.max_bytes,
                "spool_max_files": self.max_files,
                "spool_ttl_hours": self.ttl_hours,
            }

    def _assert_file_quota(self, *, additional_files: int) -> None:
        projected = self._spool_files_count + additional_files
        if projected > self.max_files:
            self._raise_quota_exceeded(
                files=projected,
                bytes_total=self._spool_bytes_total,
            )

    def _assert_byte_quota(self, *, additional_bytes: int) -> None:
        projected = self._spool_bytes_total + additional_bytes
        if projected > self.max_bytes:
            self._raise_quota_exceeded(
                files=self._spool_files_count + 1,
                bytes_total=projected,
            )

    def _raise_quota_exceeded(self, *, files: int, bytes_total: int) -> None:
        reason = (
            f"files={files}/{self.max_files},"
            f"bytes={bytes_total}/{self.max_bytes}"
        )
        self.logger.critical("spool_quota_exceeded | %s", reason)
        self.mount_failure_state.mark_dead(
            source="spool_quota",
            reason=reason,
        )
        raise SpoolQuotaExceeded(reason)

    @staticmethod
    def _validate_rows(path: Path, expected_rows: int) -> None:
        actual_rows = pq.ParquetFile(path).metadata.num_rows
        if actual_rows != expected_rows:
            raise ValueError(
                f"spool row mismatch: expected={expected_rows} got={actual_rows}"
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
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _remove_empty_partition_dirs(self, event_date_dir: Path) -> None:
        for path in (event_date_dir, event_date_dir.parent):
            try:
                path.rmdir()
                self._fsync_dir(path.parent)
            except OSError:
                break
