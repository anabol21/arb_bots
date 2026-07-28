import asyncio
import websockets
import json
import time
import logging
import csv
import signal
import sys
import threading
from pathlib import Path
from typing import Optional

_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from storage.mount_state import MountFailureState
from storage.paths import (
    assert_storage_mount_writable,
    resolve_failed_batches_log_path,
    resolve_parquet_root,
    resolve_runtime_log_path,
)
from storage.recovery import SpoolRecoveryWorker
from storage.spool import DurableSpool
from storage.writer import ParquetPublisher


RUNTIME_LOG_PATH = resolve_runtime_log_path()
FAILED_BATCHES_LOG_PATH = resolve_failed_batches_log_path()
PARQUET_ROOT = resolve_parquet_root()

runtime_logger = logging.getLogger("runtime")
runtime_logger.setLevel(logging.INFO)

if not runtime_logger.handlers:
    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%y-%m-%d %H:%M:%S"
    )

    RUNTIME_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    runtime_file_handler = logging.FileHandler(RUNTIME_LOG_PATH)
    runtime_file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    runtime_logger.addHandler(runtime_file_handler)
    runtime_logger.addHandler(console_handler)


failed_batches_logger = logging.getLogger("failed_batches")
failed_batches_logger.setLevel(logging.ERROR)
failed_batches_logger.propagate = False

if not failed_batches_logger.handlers:
    FAILED_BATCHES_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    failed_batches_handler = logging.FileHandler(FAILED_BATCHES_LOG_PATH)
    failed_batches_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s",
            datefmt="%y-%m-%d %H:%M:%S",
        )
    )
    failed_batches_logger.addHandler(failed_batches_handler)


UNIVERSE_PATH = "bybit_okx_universe.csv"
ROW_START = 0
ROW_END = 337

PERSIST_EVERY_N = 100_000
SUBSCRIBE_BATCH_SIZE = 30
SUBSCRIBE_BATCH_PAUSE_SEC = 3
PUBLISHER_MAX_QUEUE = 4
MOUNT_PROBE_TIMEOUT_SEC = 5.0

opportunities_buffer = []
publisher: Optional[ParquetPublisher] = None
spool: Optional[DurableSpool] = None
recovery_worker: Optional[SpoolRecoveryWorker] = None
mount_failure_state = MountFailureState()


def load_pairs_from_csv(path, row_start=0, row_end=10):
    pairs = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    subset = rows[row_start:row_end]

    for row in subset:
        base_coin = row["base_coin"].strip()
        okx_symbol = row["okx_symbol"].strip()
        bybit_symbol = row["bybit_symbol"].strip()

        pairs.append({
            "base_coin": base_coin,
            "okx_symbol": okx_symbol,
            "bybit_symbol": bybit_symbol,
        })

    return pairs


pairs = load_pairs_from_csv(UNIVERSE_PATH, ROW_START, ROW_END)

quotes = {
    row["base_coin"]: {
        "okx_symbol": row["okx_symbol"],
        "bybit_symbol": row["bybit_symbol"],
        "okx": {
            "bid_price": None,
            "bid_size": None,
            "ask_price": None,
            "ask_size": None,
            "ts_exchange": None,
            "local_recv_ts_ms": None,
            "delivery_latency_ms": None,
        },
        "bybit": {
            "bid_price": None,
            "bid_size": None,
            "ask_price": None,
            "ask_size": None,
            "ts_exchange": None,
            "cts_exchange": None,
            "local_recv_ts_ms": None,
            "delivery_latency_ms": None,
        },
    }
    for row in pairs
}


def persist_opportunities() -> None:
    """Non-blocking retain-in-buffer backpressure contract.

    A snapshot is removed from ``opportunities_buffer`` only after the
    publisher confirms ``put_nowait`` succeeded. If the bounded queue is full,
    the entire snapshot remains buffered and ``backpressure_hit`` is logged;
    no implicit recovery or fallback path is used.
    """
    global publisher

    if not opportunities_buffer:
        return
    if mount_failure_state.is_dead():
        runtime_logger.error(
            "enqueue_rejected | reason=mount_dead | buffer_size=%s",
            len(opportunities_buffer),
        )
        return

    if publisher is None:
        runtime_logger.error(
            "failed | reason=publisher_missing | rows=%s",
            len(opportunities_buffer),
        )
        return

    record_count = len(opportunities_buffer)
    if not publisher.ready_for_enqueue(record_count):
        return

    records = list(opportunities_buffer)
    ok = publisher.enqueue_records(records)
    if not ok:
        return

    del opportunities_buffer[:record_count]


def spool_opportunities_buffer(*, reason: str) -> bool:
    """Synchronously make the current raw buffer locally durable."""
    if not opportunities_buffer:
        return True
    if publisher is None:
        runtime_logger.critical(
            "buffer_spool_failed | reason=publisher_missing | rows=%s",
            len(opportunities_buffer),
        )
        return False

    records = list(opportunities_buffer)
    if not publisher.durably_spool_records(records, reason=reason):
        runtime_logger.critical(
            "buffer_spool_failed | reason=%s | rows=%s",
            reason,
            len(records),
        )
        return False
    del opportunities_buffer[:len(records)]
    runtime_logger.warning(
        "buffer_locally_spooled | reason=%s | rows=%s",
        reason,
        len(records),
    )
    return True


def flush_opportunities_for_shutdown() -> bool:
    """Enqueue once, then spool any raw batch retained by backpressure."""
    persist_opportunities()
    if not opportunities_buffer:
        return True
    runtime_logger.warning(
        "shutdown_backpressure | buffer_size=%s | action=local_spool",
        len(opportunities_buffer),
    )
    return spool_opportunities_buffer(reason="shutdown_backpressure")


def write_spread_record(
    base_coin,
    trigger_exchange,
    spread_long,
    spread_short,
    okx_latency_ms,
    bybit_latency_ms,
    calc_local_ts_ms,
    okx_local_recv_ts_ms,
    okx_ts_ms,
    bybit_local_recv_ts_ms,
    bybit_ts_ms,
    okx_bid_price,
    okx_bid_size,
    okx_ask_price,
    okx_ask_size,
    bybit_bid_price,
    bybit_bid_size,
    bybit_ask_price,
    bybit_ask_size,
):
    record = {
        "base_coin": base_coin,
        "trigger": trigger_exchange,
        "spread_long": spread_long,
        "spread_short": spread_short,
        "okx_latency_ms": okx_latency_ms,
        "bybit_latency_ms": bybit_latency_ms,
        "calc_local_ts_ms": calc_local_ts_ms,
        "okx_local_recv_ts_ms": okx_local_recv_ts_ms,
        "okx_ts_ms": okx_ts_ms,
        "bybit_local_recv_ts_ms": bybit_local_recv_ts_ms,
        "bybit_ts_ms": bybit_ts_ms,
        "okx_bid_price": okx_bid_price,
        "okx_bid_size": okx_bid_size,
        "okx_ask_price": okx_ask_price,
        "okx_ask_size": okx_ask_size,
        "bybit_bid_price": bybit_bid_price,
        "bybit_bid_size": bybit_bid_size,
        "bybit_ask_price": bybit_ask_price,
        "bybit_ask_size": bybit_ask_size,
    }

    opportunities_buffer.append(record)

    if len(opportunities_buffer) >= PERSIST_EVERY_N:
        persist_opportunities()


def calc_and_store_spread(base_coin, trigger_exchange):
    state = quotes[base_coin]
    okx = state["okx"]
    bybit = state["bybit"]

    if not (
        okx["bid_price"] is not None and okx["ask_price"] is not None and
        bybit["bid_price"] is not None and bybit["ask_price"] is not None and
        okx["ts_exchange"] is not None and bybit["ts_exchange"] is not None and
        okx["local_recv_ts_ms"] is not None and bybit["local_recv_ts_ms"] is not None and
        okx["delivery_latency_ms"] is not None and bybit["delivery_latency_ms"] is not None
    ):
        return

    calc_local_ts_ms = time.time() * 1000

    spread_long = (bybit["bid_price"] - okx["ask_price"]) * 100 / bybit["bid_price"]
    spread_short = (okx["bid_price"] - bybit["ask_price"]) * 100 / okx["bid_price"]

    write_spread_record(
        base_coin=base_coin,
        trigger_exchange=trigger_exchange,
        spread_long=spread_long,
        spread_short=spread_short,
        okx_latency_ms=okx["delivery_latency_ms"],
        bybit_latency_ms=bybit["delivery_latency_ms"],
        calc_local_ts_ms=calc_local_ts_ms,
        okx_local_recv_ts_ms=okx["local_recv_ts_ms"],
        okx_ts_ms=float(okx["ts_exchange"]),
        bybit_local_recv_ts_ms=bybit["local_recv_ts_ms"],
        bybit_ts_ms=float(bybit["ts_exchange"]),
        okx_bid_price=okx["bid_price"],
        okx_bid_size=okx["bid_size"],
        okx_ask_price=okx["ask_price"],
        okx_ask_size=okx["ask_size"],
        bybit_bid_price=bybit["bid_price"],
        bybit_bid_size=bybit["bid_size"],
        bybit_ask_price=bybit["ask_price"],
        bybit_ask_size=bybit["ask_size"],
    )


async def bybit_listener(base_coin, bybit_symbol):
    ws = None

    while True:
        try:
            async with websockets.connect(
                "wss://stream.bybit.com/v5/public/linear",
                ping_interval=20,
                ping_timeout=20,
                close_timeout=2,
            ) as ws:
                sub_msg = {
                    "op": "subscribe",
                    "args": [f"orderbook.1.{bybit_symbol}"]
                }
                await ws.send(json.dumps(sub_msg))
                runtime_logger.info(f"{base_coin} | Bybit subscribed: orderbook.1.{bybit_symbol}")

                async for message in ws:
                    local_recv_ts_ms = time.time() * 1000
                    data = json.loads(message)

                    if "data" not in data:
                        continue

                    payload = data["data"]
                    if not isinstance(payload, dict):
                        continue

                    bids = payload.get("b", [])
                    asks = payload.get("a", [])

                    if bids and len(bids[0]) >= 2:
                        quotes[base_coin]["bybit"]["bid_price"] = float(bids[0][0])
                        quotes[base_coin]["bybit"]["bid_size"] = float(bids[0][1])

                    if asks and len(asks[0]) >= 2:
                        quotes[base_coin]["bybit"]["ask_price"] = float(asks[0][0])
                        quotes[base_coin]["bybit"]["ask_size"] = float(asks[0][1])

                    exchange_ts_ms = data.get("ts")
                    cts_exchange_ms = data.get("cts")

                    quotes[base_coin]["bybit"]["ts_exchange"] = float(exchange_ts_ms) if exchange_ts_ms is not None else None
                    quotes[base_coin]["bybit"]["cts_exchange"] = float(cts_exchange_ms) if cts_exchange_ms is not None else None
                    quotes[base_coin]["bybit"]["local_recv_ts_ms"] = local_recv_ts_ms

                    if exchange_ts_ms is not None:
                        quotes[base_coin]["bybit"]["delivery_latency_ms"] = local_recv_ts_ms - float(exchange_ts_ms)

                    calc_and_store_spread(base_coin, "bybit")

        except asyncio.CancelledError:
            runtime_logger.info(f"{base_coin} | Bybit listener cancelled")
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
            raise

        except Exception as e:
            runtime_logger.error(f"{base_coin} | Bybit error: {e}")
            await asyncio.sleep(10)


async def okx_listener(base_coin, okx_symbol):
    ws = None

    while True:
        try:
            async with websockets.connect(
                "wss://ws.okx.com:8443/ws/v5/public",
                ping_interval=20,
                ping_timeout=20,
                close_timeout=2,
            ) as ws:
                sub_msg = {
                    "op": "subscribe",
                    "args": [{"channel": "books5", "instId": okx_symbol}]
                }
                await ws.send(json.dumps(sub_msg))
                runtime_logger.info(f"{base_coin} | OKX subscribed: books5 {okx_symbol}")

                async for message in ws:
                    local_recv_ts_ms = time.time() * 1000
                    data = json.loads(message)

                    if "data" not in data or not data["data"]:
                        continue

                    payload = data["data"][0]
                    bids = payload.get("bids", [])
                    asks = payload.get("asks", [])

                    if bids and len(bids[0]) >= 2:
                        quotes[base_coin]["okx"]["bid_price"] = float(bids[0][0])
                        quotes[base_coin]["okx"]["bid_size"] = float(bids[0][1])

                    if asks and len(asks[0]) >= 2:
                        quotes[base_coin]["okx"]["ask_price"] = float(asks[0][0])
                        quotes[base_coin]["okx"]["ask_size"] = float(asks[0][1])

                    exchange_ts_ms = payload.get("ts")

                    quotes[base_coin]["okx"]["ts_exchange"] = float(exchange_ts_ms) if exchange_ts_ms is not None else None
                    quotes[base_coin]["okx"]["local_recv_ts_ms"] = local_recv_ts_ms

                    if exchange_ts_ms is not None:
                        quotes[base_coin]["okx"]["delivery_latency_ms"] = local_recv_ts_ms - float(exchange_ts_ms)

                    calc_and_store_spread(base_coin, "okx")

        except asyncio.CancelledError:
            runtime_logger.info(f"{base_coin} | OKX listener cancelled")
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
            raise

        except Exception as e:
            runtime_logger.error(f"{base_coin} | OKX error: {e}")
            await asyncio.sleep(10)


async def _run_mount_probe() -> None:
    """Run a probe in a daemon thread so a stuck fsync cannot hold shutdown."""
    loop = asyncio.get_running_loop()
    result = loop.create_future()

    def complete(error):
        if result.done():
            return
        if error is None:
            result.set_result(None)
        else:
            result.set_exception(error)

    def probe():
        error = None
        try:
            assert_storage_mount_writable()
        except BaseException as exc:
            error = exc
        try:
            loop.call_soon_threadsafe(complete, error)
        except RuntimeError:
            pass

    threading.Thread(
        target=probe,
        name="mount-liveness-probe",
        daemon=True,
    ).start()
    await asyncio.wait_for(result, timeout=MOUNT_PROBE_TIMEOUT_SEC)


async def heartbeat():
    while True:
        try:
            await _run_mount_probe()
        except asyncio.TimeoutError:
            reason = f"probe_timeout_sec={MOUNT_PROBE_TIMEOUT_SEC}"
            mount_failure_state.mark_dead(source="heartbeat", reason=reason)
            runtime_logger.critical(
                "mount_lost | source=heartbeat | reason=%s",
                reason,
            )
            return
        except Exception as exc:
            mount_failure_state.mark_dead(
                source="heartbeat",
                reason=repr(exc),
            )
            runtime_logger.critical(
                "mount_lost | source=heartbeat | mount=/mnt/storage | error=%r",
                exc,
            )
            return

        snap = publisher.metrics_snapshot() if publisher is not None else {}
        spool_snap = spool.metrics_snapshot() if spool is not None else {}
        runtime_logger.info(
            "heartbeat | pairs=%s | buffer_size=%s | queue_depth=%s | "
            "published_rows=%s | published_files=%s | failures=%s | "
            "accepted_records=%s | rejected_records=%s | "
            "quarantined_records=%s | "
            "bytes_written=%s | last_write_latency_ms=%s | "
            "spool_files_count=%s | spool_bytes_total=%s | "
            "spool_recovered_total=%s | spool_recovery_failed_total=%s",
            len(pairs),
            len(opportunities_buffer),
            snap.get("queue_depth", 0),
            snap.get("published_rows_total", 0),
            snap.get("published_files_total", 0),
            snap.get("failures_total", 0),
            snap.get("accepted_records_total", 0),
            snap.get("rejected_records_total", 0),
            snap.get("quarantined_records_total", 0),
            snap.get("bytes_written_total", 0),
            snap.get("last_write_latency_ms"),
            spool_snap.get("spool_files_count", 0),
            spool_snap.get("spool_bytes_total", 0),
            spool_snap.get("spool_recovered_total", 0),
            spool_snap.get("spool_recovery_failed_total", 0),
        )
        await asyncio.sleep(30)


async def monitor_mount_failure(main_task: asyncio.Task) -> None:
    while not mount_failure_state.is_dead():
        await asyncio.sleep(0.1)

    failure = mount_failure_state.failure()
    runtime_logger.critical(
        "mount_failure_detected | source=%s | batch_id=%s | reason=%s",
        failure.source if failure is not None else "unknown",
        failure.batch_id if failure is not None else None,
        failure.reason if failure is not None else "unknown",
    )
    main_task.cancel()


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


async def main():
    global publisher, recovery_worker, spool

    assert_storage_mount_writable()
    spool = DurableSpool(
        logger=runtime_logger,
        mount_failure_state=mount_failure_state,
    )
    publisher = ParquetPublisher(
        parquet_root=PARQUET_ROOT,
        logger=runtime_logger,
        failed_batches_logger=failed_batches_logger,
        mount_failure_state=mount_failure_state,
        spool=spool,
        max_queue=PUBLISHER_MAX_QUEUE,
    )
    publisher.start()
    recovery_worker = SpoolRecoveryWorker(
        spool=spool,
        parquet_root=PARQUET_ROOT,
        logger=runtime_logger,
        mount_failure_state=mount_failure_state,
    )
    recovery_worker.start()

    runtime_logger.info(
        "runtime_paths | parquet_root=%s | runtime_log=%s | "
        "failed_batches_log=%s | spool_root=%s",
        PARQUET_ROOT,
        RUNTIME_LOG_PATH,
        FAILED_BATCHES_LOG_PATH,
        spool.root,
    )
    runtime_logger.info(f"Loaded pairs: {len(pairs)}")
    for row in pairs:
        runtime_logger.info(
            f"PAIR | base_coin={row['base_coin']} | "
            f"okx_symbol={row['okx_symbol']} | bybit_symbol={row['bybit_symbol']}"
        )

    main_task = asyncio.current_task()
    if main_task is None:
        raise RuntimeError("main task is unavailable")
    tasks = [
        asyncio.create_task(heartbeat(), name="heartbeat"),
        asyncio.create_task(
            monitor_mount_failure(main_task),
            name="mount-failure-monitor",
        ),
    ]

    loop = asyncio.get_running_loop()

    def request_shutdown():
        runtime_logger.info("shutdown signal received, cancelling tasks")
        for task in tasks:
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except NotImplementedError:
            signal.signal(sig, lambda *_: request_shutdown())

    try:
        pair_batches = list(chunked(pairs, SUBSCRIBE_BATCH_SIZE))
        runtime_logger.info(
            f"Starting subscriptions in batches | batch_size={SUBSCRIBE_BATCH_SIZE} | "
            f"pause_sec={SUBSCRIBE_BATCH_PAUSE_SEC} | batches={len(pair_batches)}"
        )

        for batch_idx, pair_batch in enumerate(pair_batches, start=1):
            runtime_logger.info(
                f"Subscription batch {batch_idx}/{len(pair_batches)} | "
                f"coins_in_batch={len(pair_batch)}"
            )

            for row in pair_batch:
                base_coin = row["base_coin"]
                okx_symbol = row["okx_symbol"]
                bybit_symbol = row["bybit_symbol"]

                tasks.append(asyncio.create_task(okx_listener(base_coin, okx_symbol), name=f"okx:{base_coin}"))
                tasks.append(asyncio.create_task(bybit_listener(base_coin, bybit_symbol), name=f"bybit:{base_coin}"))

            if batch_idx < len(pair_batches):
                runtime_logger.info(
                    f"Subscription batch {batch_idx} scheduled | sleeping {SUBSCRIBE_BATCH_PAUSE_SEC}s before next batch"
                )
                await asyncio.sleep(SUBSCRIBE_BATCH_PAUSE_SEC)

        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        mount_failure = mount_failure_state.failure()
        if mount_failure is None:
            runtime_logger.info("main | cancellation received, shutting down tasks")
        else:
            runtime_logger.critical(
                "main | mount failure shutdown | source=%s | batch_id=%s",
                mount_failure.source,
                mount_failure.batch_id,
            )

        for task in tasks:
            task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0.25)
        if mount_failure is not None:
            raise RuntimeError(
                f"mount failure detected by {mount_failure.source}: "
                f"{mount_failure.reason}"
            )
        raise
    finally:
        if mount_failure_state.is_dead():
            runtime_logger.warning(
                "main | spooling_buffer_after_mount_failure | buffer_size=%s",
                len(opportunities_buffer),
            )
            spool_opportunities_buffer(reason="mount_failure_shutdown")
        else:
            runtime_logger.info("main | flushing opportunities buffer before exit")
            flush_opportunities_for_shutdown()
        if recovery_worker is not None:
            recovery_worker.shutdown()
        if publisher is not None:
            publisher.shutdown()


if __name__ == "__main__":
    try:
        import uvloop
        uvloop.install()
    except Exception:
        pass

    if not Path(UNIVERSE_PATH).exists():
        raise FileNotFoundError(f"Universe file not found: {UNIVERSE_PATH}")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        runtime_logger.info("KeyboardInterrupt received, process stopped by user")
