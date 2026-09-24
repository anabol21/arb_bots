"""Process wiring: universe screen, publisher, spool, listener.

Publish and spool use ``ParquetPublisher`` / ``DurableSpool``. This module
does not write ``/data/live`` or ``/data/spool``.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

from app.hl.bbo import HL_WS_URL
from app.hl.buffer import HlRecordBuffer
from app.hl.listener import HlBboListener
from app.hl.paths import (
    assert_distinct_hl_roots,
    resolve_hl_log_path,
    resolve_hl_parquet_root,
    resolve_hl_spool_root,
)
from app.hl.universe import (
    HL_INFO_URL,
    default_universe_path,
    fetch_perp_meta,
    load_and_select,
    perp_names_from_meta,
)
from app.storage.mount_state import MountFailureState
from app.storage.recovery import SpoolRecoveryWorker
from app.storage.spool import DurableSpool
from app.storage.writer import ParquetPublisher


def _configure_logger(
    name: str,
    log_path: Path | None,
) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = logging.FileHandler(log_path)
        handle.setFormatter(formatter)
        logger.addHandler(handle)
    return logger


def build_publisher(
    *,
    parquet_root: Path,
    spool_root: Path,
    logger: logging.Logger,
    failed_logger: logging.Logger,
) -> tuple[MountFailureState, DurableSpool, ParquetPublisher, SpoolRecoveryWorker]:
    assert_distinct_hl_roots(parquet_root, spool_root)
    mount_state = MountFailureState()
    spool = DurableSpool(
        logger=logger,
        mount_failure_state=mount_state,
        root=spool_root,
    )
    publisher = ParquetPublisher(
        parquet_root=parquet_root,
        logger=logger,
        failed_batches_logger=failed_logger,
        mount_failure_state=mount_state,
        spool=spool,
        max_queue=8,
        schema_mode="hl_l1",
        name="hl-l1-publisher",
    )
    recovery = SpoolRecoveryWorker(
        spool=spool,
        parquet_root=parquet_root,
        logger=logger,
        mount_failure_state=mount_state,
    )
    return mount_state, spool, publisher, recovery


def _positive_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got: {value}")
    return value


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Hyperliquid L1 bbo collector (separate from the Bybit/OKX collector)",
    )
    parser.add_argument("--universe", default=os.environ.get("HL_UNIVERSE") or "")
    parser.add_argument("--parquet-root", default="")
    parser.add_argument("--spool-root", default="")
    parser.add_argument("--log", default="")
    parser.add_argument("--failed-batches-log", default="")
    parser.add_argument("--info-url", default=os.environ.get("HL_INFO_URL") or HL_INFO_URL)
    parser.add_argument("--ws-url", default=os.environ.get("HL_WS_URL") or HL_WS_URL)
    args = parser.parse_args(argv)

    universe_path = Path(args.universe) if args.universe else default_universe_path()
    parquet_root = resolve_hl_parquet_root(args.parquet_root or None)
    spool_root = resolve_hl_spool_root(args.spool_root or None)
    assert_distinct_hl_roots(parquet_root, spool_root)
    log_path = resolve_hl_log_path(
        args.log or None,
        env_name="HL_RUNTIME_LOG",
        default=None,
    )
    failed_path = resolve_hl_log_path(
        args.failed_batches_log or None,
        env_name="HL_FAILED_BATCHES_LOG",
        default=None,
    )

    logger = _configure_logger("hl-l1", log_path)
    failed_logger = _configure_logger("hl-l1-failed", failed_path)
    logger.info(
        "hl_l1_start | universe=%s | parquet_root=%s | spool_root=%s | "
        "ws=%s | schema_mode=hl_l1",
        universe_path,
        parquet_root,
        spool_root,
        args.ws_url,
    )

    try:
        meta = fetch_perp_meta(
            url=args.info_url,
            timeout_sec=_positive_float_env("HL_INFO_TIMEOUT_SEC", 20.0),
        )
        hl_names = perp_names_from_meta(meta)
        selection = load_and_select(universe_path, hl_names)
    except Exception as exc:
        logger.error("hl_universe_failed | error=%r", exc)
        return 1

    logger.info(
        "hl_universe_matched | count=%s | coins=%s",
        len(selection.matched),
        ",".join(selection.matched),
    )
    logger.warning(
        "hl_universe_unmatched | count=%s | policy=exact_name_only | coins=%s",
        len(selection.unmatched),
        ",".join(selection.unmatched),
    )
    if not selection.matched:
        logger.error("hl_universe_empty | reason=no_exact_name_match")
        return 2

    _mount, _spool, publisher, recovery = build_publisher(
        parquet_root=parquet_root,
        spool_root=spool_root,
        logger=logger,
        failed_logger=failed_logger,
    )
    publisher.start()
    recovery.start()
    buffer = HlRecordBuffer(publisher, logger)
    listener = HlBboListener(
        selection.matched,
        buffer.offer,
        logger,
        url=args.ws_url,
    )
    buffer.start()
    listener.start()

    stop = threading.Event()

    def _handle_signal(signum: int, _frame: object) -> None:
        logger.info("hl_l1_signal | signum=%s", signum)
        stop.set()
        listener.close()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    stop.wait()

    listener.close()
    listener.join(timeout_sec=5.0)
    buffer.stop_flush_thread()
    drain_outcome = buffer.drain()
    publisher.shutdown()
    recovery.shutdown()
    snap = publisher.metrics_snapshot()
    logger.info(
        "hl_l1_stop | drain=%s | offered=%s | dropped=%s | frames=%s | "
        "parsed=%s | incomplete=%s | unexpected_coin=%s | "
        "published_files=%s | published_rows=%s | spooled_jobs=%s | "
        "quarantined_jobs=%s | failed_jobs=%s",
        drain_outcome,
        buffer.offered_total,
        buffer.dropped_total,
        listener.frames_total,
        listener.parsed_total,
        listener.incomplete_total,
        listener.unexpected_coin_total,
        snap["published_files_total"],
        snap["published_rows_total"],
        snap["spooled_jobs_total"],
        snap["quarantined_jobs_total"],
        snap["failed_jobs_total"],
    )
    if drain_outcome == "failed" or snap["failed_jobs_total"]:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(argv)
    except Exception as exc:
        print(f"hl_l1_fatal | error={exc!r}", file=sys.stderr)
        return 1
