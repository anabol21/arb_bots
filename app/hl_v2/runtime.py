"""Process wiring for the HL v2 canary contour.

Bybit + OKX staff listeners + sharded HL l2Book → lean+HL parquet under
``/data/live_hl_v2``. Does not write ``/data/live``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional

from app.hl_v2.buffer import HlV2RecordBuffer
from app.hl_v2.l2book import HL_WS_URL, channels_from_env
from app.hl_v2.listeners_cex import bybit_listener, okx_listener
from app.hl_v2.listeners_hl import HlShardFleet, ShardStatus, hl_shard_listener
from app.hl_v2.paths import (
    DEFAULT_HL_V2_FAILED_BATCHES_LOG,
    DEFAULT_HL_V2_RUNTIME_LOG,
    assert_distinct_roots,
    resolve_gaps_root,
    resolve_log_path,
    resolve_parquet_root,
    resolve_spool_root,
)
from app.hl_v2.sharding import shard_count_from_env, split_round_robin
from app.hl_v2.staff import QuoteBook
from app.hl_v2.universe import (
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
from app.utils.ws_reconnect import ExchangeConnectScheduler, ReconnectController, connect_per_sec


def _configure_logger(name: str, log_path: Path | None) -> logging.Logger:
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


def _positive_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got: {value}")
    return value


def build_publisher(
    *,
    parquet_root: Path,
    spool_root: Path,
    logger: logging.Logger,
    failed_logger: logging.Logger,
) -> tuple[MountFailureState, DurableSpool, ParquetPublisher, SpoolRecoveryWorker]:
    assert_distinct_roots(parquet_root, spool_root)
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
        schema_mode="hl_v2",
        name="hl-v2-publisher",
    )
    recovery = SpoolRecoveryWorker(
        spool=spool,
        parquet_root=parquet_root,
        logger=logger,
        mount_failure_state=mount_state,
    )
    return mount_state, spool, publisher, recovery


async def _heartbeat(
    *,
    logger: logging.Logger,
    fleet: HlShardFleet,
    buffer: HlV2RecordBuffer,
    publisher: ParquetPublisher,
    pair_count: int,
    socket_count: int,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=30.0)
            return
        except asyncio.TimeoutError:
            pass
        snap = publisher.metrics_snapshot()
        shard_parts = []
        for fields in fleet.heartbeat_fields():
            shard_parts.append(
                "shard{shard_id}=ok:{ws_subscribe_ok}/subs:{active_subs}/"
                "age_ms:{last_msg_age_ms}/reconn:{reconnect_count}".format(**fields)
            )
        logger.info(
            "heartbeat | pairs=%s | hl_sockets=%s | offered=%s | dropped=%s | "
            "flushed=%s | published_files=%s | published_rows=%s | "
            "spooled_jobs=%s | %s",
            pair_count,
            socket_count,
            buffer.offered_total,
            buffer.dropped_total,
            buffer.flushed_total,
            snap["published_files_total"],
            snap["published_rows_total"],
            snap["spooled_jobs_total"],
            " | ".join(shard_parts) if shard_parts else "shards=none",
        )


async def _run_async(
    *,
    pairs,
    parquet_root: Path,
    spool_root: Path,
    gaps_root: Path,
    logger: logging.Logger,
    failed_logger: logging.Logger,
    ws_url: str,
    socket_count: int,
) -> int:
    del gaps_root  # reserved for optional gap journal; not required for first slice
    _mount, _spool, publisher, recovery = build_publisher(
        parquet_root=parquet_root,
        spool_root=spool_root,
        logger=logger,
        failed_logger=failed_logger,
    )
    publisher.start()
    recovery.start()
    buffer = HlV2RecordBuffer(publisher, logger)
    buffer.start()
    book = QuoteBook(buffer.offer)
    for pair in pairs:
        book.register(pair.base_coin, pair.okx_symbol, pair.bybit_symbol)

    ws_reconnect = ReconnectController()
    connect_scheduler = ExchangeConnectScheduler(connects_per_sec=connect_per_sec())
    coins = tuple(pair.base_coin for pair in pairs)
    shards = split_round_robin(coins, socket_count)
    fleet = HlShardFleet()
    for shard_id, shard_coins in enumerate(shards):
        fleet.statuses.append(ShardStatus(shard_id=shard_id, coins=shard_coins))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _handle_signal() -> None:
        logger.info("hl_v2_signal | stopping")
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            signal.signal(sig, lambda *_args: _handle_signal())

    tasks: list[asyncio.Task] = [
        asyncio.create_task(
            _heartbeat(
                logger=logger,
                fleet=fleet,
                buffer=buffer,
                publisher=publisher,
                pair_count=len(pairs),
                socket_count=socket_count,
                stop=stop,
            ),
            name="hl-v2-heartbeat",
        )
    ]
    for pair in pairs:
        tasks.append(
            asyncio.create_task(
                bybit_listener(
                    pair.base_coin,
                    pair.bybit_symbol,
                    book,
                    ws_reconnect=ws_reconnect,
                    connect_scheduler=connect_scheduler,
                    logger=logger,
                ),
                name=f"bybit-{pair.base_coin}",
            )
        )
        tasks.append(
            asyncio.create_task(
                okx_listener(
                    pair.base_coin,
                    pair.okx_symbol,
                    book,
                    ws_reconnect=ws_reconnect,
                    connect_scheduler=connect_scheduler,
                    logger=logger,
                ),
                name=f"okx-{pair.base_coin}",
            )
        )
    for status in fleet.statuses:
        tasks.append(
            asyncio.create_task(
                hl_shard_listener(
                    shard_id=status.shard_id,
                    coins=status.coins,
                    book=book,
                    status=status,
                    ws_reconnect=ws_reconnect,
                    connect_scheduler=connect_scheduler,
                    logger=logger,
                    url=ws_url,
                ),
                name=f"hl-shard-{status.shard_id}",
            )
        )

    await stop.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    buffer.stop_flush_thread()
    drain_outcome = buffer.drain()
    publisher.shutdown()
    recovery.shutdown()
    snap = publisher.metrics_snapshot()
    logger.info(
        "hl_v2_stop | drain=%s | offered=%s | dropped=%s | published_files=%s | "
        "published_rows=%s | spooled_jobs=%s | failed_jobs=%s",
        drain_outcome,
        buffer.offered_total,
        buffer.dropped_total,
        snap["published_files_total"],
        snap["published_rows_total"],
        snap["spooled_jobs_total"],
        snap["failed_jobs_total"],
    )
    if drain_outcome == "failed" or snap["failed_jobs_total"]:
        return 1
    return 0


def run(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "HL v2 canary: Bybit+OKX staff + sharded HL l2Book → /data/live_hl_v2. "
            "Do not point at /data/live."
        )
    )
    parser.add_argument("--universe", default="")
    parser.add_argument("--parquet-root", default="")
    parser.add_argument("--spool-root", default="")
    parser.add_argument("--gaps-root", default="")
    parser.add_argument("--log", default="")
    parser.add_argument("--failed-batches-log", default="")
    parser.add_argument("--info-url", default=os.environ.get("HL_INFO_URL") or HL_INFO_URL)
    parser.add_argument("--ws-url", default=os.environ.get("HL_WS_URL") or HL_WS_URL)
    parser.add_argument(
        "--socket-count",
        default="",
        help="HL_SOCKET_COUNT override (default 1)",
    )
    args = parser.parse_args(argv)

    # Refuse accidental prod root even before path resolve helpers run.
    for candidate in (
        args.parquet_root,
        os.environ.get("SPREAD_PARQUET_ROOT", ""),
        os.environ.get("HL_V2_PARQUET_ROOT", ""),
    ):
        if str(candidate).rstrip("/") == "/data/live":
            print(
                "hl_v2_fatal | refused parquet root /data/live "
                "(use /data/live_hl_v2)",
                file=sys.stderr,
            )
            return 2

    universe_path = Path(args.universe) if args.universe else default_universe_path()
    parquet_root = resolve_parquet_root(args.parquet_root or None)
    spool_root = resolve_spool_root(args.spool_root or None)
    gaps_root = resolve_gaps_root(args.gaps_root or None)
    assert_distinct_roots(parquet_root, spool_root)
    log_path = resolve_log_path(
        args.log or None,
        env_name="HL_V2_RUNTIME_LOG",
        default=DEFAULT_HL_V2_RUNTIME_LOG,
    )
    failed_path = resolve_log_path(
        args.failed_batches_log or None,
        env_name="HL_V2_FAILED_BATCHES_LOG",
        default=DEFAULT_HL_V2_FAILED_BATCHES_LOG,
    )
    socket_count = shard_count_from_env(
        args.socket_count or os.environ.get("HL_SOCKET_COUNT"),
        default=1,
    )
    channels = channels_from_env(os.environ.get("HL_CHANNELS"))

    logger = _configure_logger("hl-v2", log_path)
    failed_logger = _configure_logger("hl-v2-failed", failed_path)
    logger.info(
        "hl_v2_start | universe=%s | parquet_root=%s | spool_root=%s | "
        "gaps_root=%s | ws=%s | hl_sockets=%s | channels=%s | schema_mode=hl_v2",
        universe_path,
        parquet_root,
        spool_root,
        gaps_root,
        args.ws_url,
        socket_count,
        ",".join(channels),
    )

    try:
        meta = fetch_perp_meta(
            url=args.info_url,
            timeout_sec=_positive_float_env("HL_INFO_TIMEOUT_SEC", 20.0),
        )
        hl_names = perp_names_from_meta(meta)
        selection = load_and_select(universe_path, hl_names)
    except Exception as exc:
        logger.error("hl_v2_universe_failed | error=%r", exc)
        return 1

    logger.info(
        "hl_v2_universe_matched | count=%s | coins=%s",
        len(selection.matched),
        ",".join(pair.base_coin for pair in selection.matched),
    )
    logger.warning(
        "hl_v2_universe_unmatched | count=%s | policy=exact_name_only | coins=%s",
        len(selection.unmatched_coins),
        ",".join(selection.unmatched_coins),
    )
    if not selection.matched:
        logger.error("hl_v2_universe_empty | reason=no_exact_name_match")
        return 2

    return asyncio.run(
        _run_async(
            pairs=selection.matched,
            parquet_root=parquet_root,
            spool_root=spool_root,
            gaps_root=gaps_root,
            logger=logger,
            failed_logger=failed_logger,
            ws_url=args.ws_url,
            socket_count=socket_count,
        )
    )


def main(argv: Optional[list[str]] = None) -> int:
    try:
        return run(argv)
    except Exception as exc:
        print(f"hl_v2_fatal | error={exc!r}", file=sys.stderr)
        return 1
