import asyncio
import websockets
import json
import os
import time
import logging
import csv
import signal
import sys
from pathlib import Path
from typing import Any, Optional

_APP_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _APP_DIR.parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from schema.lean_event import BAR_INTERVAL_MS  # noqa: E402
from schema.spread_event import lean_schema_enabled, tick_schema_mode  # noqa: E402
from storage.mount_state import MountFailureState
from storage.paths import (
    assert_storage_root_writable,
    bars_parquet_root,
    resolve_bars_root,
    resolve_failed_batches_log_path,
    resolve_gaps_root,
    resolve_parquet_root,
    resolve_runtime_log_path,
)
from storage.recovery import SpoolRecoveryWorker
from storage.spool import DurableSpool, resolve_spool_root
from storage.writer import (
    ParquetPublisher,
    collect_bars_enabled,
)
from utils.tick_validity import (  # noqa: E402
    TickValidityGate,
    book_l1_complete,
)
from utils.ws_gap_journal import WsGapJournal  # noqa: E402
from utils.ws_reconnect import (  # noqa: E402
    BOOK_CONNECT_PRIORITY,
    CANDLE_CONNECT_PRIORITY,
    ExchangeConnectScheduler,
    ReconnectController,
    connect_per_sec,
    reconnect_v2_enabled,
    subscribe_batch_size,
)


RUNTIME_LOG_PATH = resolve_runtime_log_path()
FAILED_BATCHES_LOG_PATH = resolve_failed_batches_log_path()
PARQUET_ROOT = resolve_parquet_root()
BARS_ROOT = resolve_bars_root()
BARS_PARQUET_ROOT = bars_parquet_root(BARS_ROOT)
GAPS_ROOT = resolve_gaps_root()

# Option B: default OFF — deploying code must not change canary v1 output.
LEAN_SCHEMA = lean_schema_enabled()
COLLECT_BARS = collect_bars_enabled()
COLLECT_BYBIT_BARS = os.environ.get("SPREAD_COLLECT_BYBIT_BARS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
BAR_PERSIST_EVERY_N = int(os.environ.get("SPREAD_BAR_PERSIST_EVERY", "500"))

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


ws_gap_journal = WsGapJournal(GAPS_ROOT, logger=runtime_logger)

UNIVERSE_PATH = os.environ.get("SPREAD_UNIVERSE", "bybit_okx_universe.csv")
ROW_START = int(os.environ.get("SPREAD_ROW_START", "0"))
ROW_END = int(os.environ.get("SPREAD_ROW_END", "337"))

# Default keeps canary-scale batching; lower via env for soak / disk pressure.
PERSIST_EVERY_N = int(os.environ.get("SPREAD_PERSIST_EVERY", "100000"))
SUBSCRIBE_BATCH_SIZE = subscribe_batch_size(v2=reconnect_v2_enabled())
SUBSCRIBE_BATCH_PAUSE_SEC = float(os.environ.get("SPREAD_SUBSCRIBE_BATCH_PAUSE_SEC", "3"))
PUBLISHER_MAX_QUEUE = 4

opportunities_buffer: list[dict[str, Any]] = []
bar_buffer: list[dict[str, Any]] = []
seen_bar_keys: set[tuple[str, str, int]] = set()
publisher: Optional[ParquetPublisher] = None
bars_publisher: Optional[ParquetPublisher] = None
spool: Optional[DurableSpool] = None
bars_spool: Optional[DurableSpool] = None
recovery_worker: Optional[SpoolRecoveryWorker] = None
bars_recovery_worker: Optional[SpoolRecoveryWorker] = None
mount_failure_state = MountFailureState()
ws_reconnect = ReconnectController()
tick_validity = TickValidityGate()
connect_scheduler = ExchangeConnectScheduler(connects_per_sec=connect_per_sec())


def _ms_int(value: Any) -> int:
    """Store timestamps as int64 ms (avoid float display confusion)."""
    return int(round(float(value)))


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


def _persist_buffer(
    buffer: list[dict[str, Any]],
    pub: Optional[ParquetPublisher],
    *,
    kind: str,
) -> None:
    """Non-blocking retain-in-buffer backpressure contract for one buffer."""
    if not buffer:
        return
    if mount_failure_state.is_dead():
        runtime_logger.error(
            "enqueue_rejected | kind=%s | reason=mount_dead | buffer_size=%s",
            kind,
            len(buffer),
        )
        return
    if pub is None:
        runtime_logger.error(
            "failed | kind=%s | reason=publisher_missing | rows=%s",
            kind,
            len(buffer),
        )
        return

    record_count = len(buffer)
    if not pub.ready_for_enqueue(record_count):
        return

    records = list(buffer)
    if not pub.enqueue_records(records):
        return
    del buffer[:record_count]


def persist_opportunities() -> None:
    """Non-blocking retain-in-buffer backpressure contract.

    A snapshot is removed from ``opportunities_buffer`` only after the
    publisher confirms ``put_nowait`` succeeded. If the bounded queue is full,
    the entire snapshot remains buffered and ``backpressure_hit`` is logged;
    no implicit recovery or fallback path is used.
    """
    _persist_buffer(opportunities_buffer, publisher, kind="ticks")


def persist_bars() -> None:
    _persist_buffer(bar_buffer, bars_publisher, kind="bars")


def _spool_buffer(
    buffer: list[dict[str, Any]],
    pub: Optional[ParquetPublisher],
    *,
    reason: str,
    kind: str,
) -> bool:
    if not buffer:
        return True
    if pub is None:
        runtime_logger.critical(
            "buffer_spool_failed | kind=%s | reason=publisher_missing | rows=%s",
            kind,
            len(buffer),
        )
        return False

    records = list(buffer)
    if not pub.durably_spool_records(records, reason=reason):
        runtime_logger.critical(
            "buffer_spool_failed | kind=%s | reason=%s | rows=%s",
            kind,
            reason,
            len(records),
        )
        return False
    del buffer[: len(records)]
    runtime_logger.warning(
        "buffer_locally_spooled | kind=%s | reason=%s | rows=%s",
        kind,
        reason,
        len(records),
    )
    return True


def spool_opportunities_buffer(*, reason: str) -> bool:
    """Synchronously make the current raw buffer locally durable."""
    return _spool_buffer(
        opportunities_buffer, publisher, reason=reason, kind="ticks"
    )


def spool_bars_buffer(*, reason: str) -> bool:
    return _spool_buffer(bar_buffer, bars_publisher, reason=reason, kind="bars")


def flush_opportunities_for_shutdown() -> bool:
    """Enqueue once, then spool any raw batch retained by backpressure."""
    persist_opportunities()
    if not opportunities_buffer:
        return True
    runtime_logger.warning(
        "shutdown_backpressure | kind=ticks | buffer_size=%s | action=local_spool",
        len(opportunities_buffer),
    )
    return spool_opportunities_buffer(reason="shutdown_backpressure")


def flush_bars_for_shutdown() -> bool:
    persist_bars()
    if not bar_buffer:
        return True
    runtime_logger.warning(
        "shutdown_backpressure | kind=bars | buffer_size=%s | action=local_spool",
        len(bar_buffer),
    )
    return spool_bars_buffer(reason="shutdown_backpressure")


def store_closed_bar(
    *,
    base_coin: str,
    ref_exchange: str,
    bar_start_ts_ms: int,
    volume: float,
) -> None:
    key = (base_coin, ref_exchange, int(bar_start_ts_ms))
    if key in seen_bar_keys:
        return
    seen_bar_keys.add(key)
    bar_buffer.append(
        {
            "bar_start_ts_ms": int(bar_start_ts_ms),
            "bar_end_ts_ms": int(bar_start_ts_ms) + BAR_INTERVAL_MS,
            "base_coin": base_coin,
            "ref_exchange": ref_exchange,
            "volume": float(volume),
        }
    )
    if len(bar_buffer) >= BAR_PERSIST_EVERY_N:
        persist_bars()


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
    # Re-read flag so tests can toggle env without reimporting module constants.
    lean = lean_schema_enabled()
    event_local_ts_ms = (
        bybit_local_recv_ts_ms if trigger_exchange == "bybit" else okx_local_recv_ts_ms
    )
    if lean:
        record = {
            "event_local_ts_ms": _ms_int(event_local_ts_ms),
            "base_coin": base_coin,
            "trigger": trigger_exchange,
            "calc_local_ts_ms": _ms_int(calc_local_ts_ms),
            "okx_local_recv_ts_ms": _ms_int(okx_local_recv_ts_ms),
            "okx_ts_ms": _ms_int(okx_ts_ms),
            "bybit_local_recv_ts_ms": _ms_int(bybit_local_recv_ts_ms),
            "bybit_ts_ms": _ms_int(bybit_ts_ms),
            "okx_bid_price": okx_bid_price,
            "okx_bid_size": okx_bid_size,
            "okx_ask_price": okx_ask_price,
            "okx_ask_size": okx_ask_size,
            "bybit_bid_price": bybit_bid_price,
            "bybit_bid_size": bybit_bid_size,
            "bybit_ask_price": bybit_ask_price,
            "bybit_ask_size": bybit_ask_size,
        }
    else:
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

    lean = lean_schema_enabled()
    ready = (
        okx["bid_price"] is not None
        and okx["ask_price"] is not None
        and bybit["bid_price"] is not None
        and bybit["ask_price"] is not None
        and okx["ts_exchange"] is not None
        and bybit["ts_exchange"] is not None
        and okx["local_recv_ts_ms"] is not None
        and bybit["local_recv_ts_ms"] is not None
    )
    if not lean:
        ready = ready and (
            okx["delivery_latency_ms"] is not None
            and bybit["delivery_latency_ms"] is not None
        )
    if not ready:
        return

    calc_local_ts_ms = time.time() * 1000
    suppress = tick_validity.evaluate(base_coin, okx, bybit, calc_local_ts_ms)
    if suppress is not None:
        return

    # Still computed in-process for v1 body; lean omits from parquet (derive at read).
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


def _log_ws_event(fields: dict[str, Any], *, level: int = logging.INFO) -> None:
    parts = [str(fields.get("event", "ws_event"))]
    for key in (
        "exchange",
        "channel",
        "coin",
        "close_code",
        "reason_class",
        "attempt",
        "backoff_ms",
        "wave_60s",
        "exception_class",
        "exception",
        "unrecovered",
    ):
        if key in fields and fields[key] is not None:
            parts.append(f"{key}={fields[key]}")
    runtime_logger.log(level, " | ".join(parts))


async def _wait_reconnect_gates(session, disconnect: dict[str, Any]) -> None:
    await asyncio.sleep(float(disconnect["backoff_sec"]))
    ctrl = session.controller
    if ctrl.budget_exceeded(session.key):
        ctrl.counters["budget_exceeded_total"] += 1
        while ctrl.budget_exceeded(session.key):
            wait_sec = min(max(ctrl.time_until_budget_slot(session.key), 1.0), 60.0)
            _log_ws_event(
                {
                    "event": "reconnect_budget_exceeded",
                    "exchange": session.exchange,
                    "channel": session.channel,
                    "coin": session.base_coin,
                    "attempt": session.attempt,
                    "backoff_ms": int(round(wait_sec * 1000)),
                    "wave_60s": ctrl.wave_60s(session.exchange),
                },
                level=logging.WARNING,
            )
            await asyncio.sleep(wait_sec)
    priority = (
        CANDLE_CONNECT_PRIORITY
        if session.channel in {"candle5m", "kline.5"}
        else BOOK_CONNECT_PRIORITY
    )
    await connect_scheduler.acquire(
        session.exchange, priority=priority, coin=session.base_coin
    )
    ctrl.mark_connect_slot(session.exchange)
    if disconnect.get("planned", True):
        ctrl.record_planned(session.key)


async def _ws_listen_loop_v2(
    *,
    exchange: str,
    channel: str,
    base_coin: str,
    url: str,
    subscribe_payload: dict[str, Any],
    subscribe_ok_message: str,
    on_message,
    book_exchange: Optional[str] = None,
    connect_priority: int = BOOK_CONNECT_PRIORITY,
) -> None:
    """Planned reconnect wrapper. on_message is the frozen parse/store callback."""
    session = ws_reconnect.session(exchange, channel, base_coin)
    pending_disconnect: Optional[dict[str, Any]] = None
    ws = None
    while True:
        try:
            if pending_disconnect is not None:
                await _wait_reconnect_gates(session, pending_disconnect)
                pending_disconnect = None
            else:
                await connect_scheduler.acquire(
                    exchange, priority=connect_priority, coin=base_coin
                )
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=2,
            ) as ws:
                await ws.send(json.dumps(subscribe_payload))
                _log_ws_event(session.mark_subscribe_ok())
                ws_gap_journal.note_subscribe_ok(
                    exchange=exchange,
                    channel=channel,
                    base_coin=base_coin,
                )
                tick_validity.note_subscribe_ok(base_coin, channel)
                runtime_logger.info(subscribe_ok_message)
                async for message in ws:
                    on_message(message)
        except asyncio.CancelledError:
            runtime_logger.info("%s | %s listener cancelled", base_coin, exchange)
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
            raise
        except Exception as exc:
            disconnect = session.on_disconnect(
                exc,
                ws,
                quotes=quotes,
                book_exchange=book_exchange,
            )
            pending_disconnect = disconnect
            if book_exchange is not None:
                tick_validity.note_disconnect(base_coin, book_exchange)
            level = (
                logging.ERROR
                if disconnect["reason_class"] == "protocol_error"
                else logging.WARNING
            )
            _log_ws_event(disconnect, level=level)
            ws_gap_journal.note_disconnect(
                exchange=exchange,
                channel=channel,
                base_coin=base_coin,
                close_code=disconnect.get("close_code"),
            )
            unrecovered = session.unrecovered_event(disconnect)
            if unrecovered is not None:
                _log_ws_event(unrecovered, level=logging.ERROR)
            _log_ws_event(session.plan_reconnect(disconnect), level=level)


async def bybit_listener(base_coin, bybit_symbol):
    def handle_message(message):
        local_recv_ts_ms = time.time() * 1000
        data = json.loads(message)

        if "data" not in data:
            return

        payload = data["data"]
        if not isinstance(payload, dict):
            return

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

        tick_validity.note_book_update(
            base_coin, "bybit", complete_l1=book_l1_complete(quotes[base_coin]["bybit"])
        )
        calc_and_store_spread(base_coin, "bybit")

    sub_msg = {
        "op": "subscribe",
        "args": [f"orderbook.1.{bybit_symbol}"],
    }
    if reconnect_v2_enabled():
        await _ws_listen_loop_v2(
            exchange="bybit",
            channel="orderbook.1",
            base_coin=base_coin,
            url="wss://stream.bybit.com/v5/public/linear",
            subscribe_payload=sub_msg,
            subscribe_ok_message=f"{base_coin} | Bybit subscribed: orderbook.1.{bybit_symbol}",
            on_message=handle_message,
            book_exchange="bybit",
        )
        return

    ws = None
    while True:
        try:
            async with websockets.connect(
                "wss://stream.bybit.com/v5/public/linear",
                ping_interval=20,
                ping_timeout=20,
                close_timeout=2,
            ) as ws:
                await ws.send(json.dumps(sub_msg))
                runtime_logger.info(f"{base_coin} | Bybit subscribed: orderbook.1.{bybit_symbol}")
                async for message in ws:
                    handle_message(message)

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
    def handle_message(message):
        local_recv_ts_ms = time.time() * 1000
        data = json.loads(message)

        if "data" not in data or not data["data"]:
            return

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

        tick_validity.note_book_update(
            base_coin, "okx", complete_l1=book_l1_complete(quotes[base_coin]["okx"])
        )
        calc_and_store_spread(base_coin, "okx")

    sub_msg = {
        "op": "subscribe",
        "args": [{"channel": "books5", "instId": okx_symbol}],
    }
    if reconnect_v2_enabled():
        await _ws_listen_loop_v2(
            exchange="okx",
            channel="books5",
            base_coin=base_coin,
            url="wss://ws.okx.com:8443/ws/v5/public",
            subscribe_payload=sub_msg,
            subscribe_ok_message=f"{base_coin} | OKX subscribed: books5 {okx_symbol}",
            on_message=handle_message,
            book_exchange="okx",
        )
        return

    ws = None
    while True:
        try:
            async with websockets.connect(
                "wss://ws.okx.com:8443/ws/v5/public",
                ping_interval=20,
                ping_timeout=20,
                close_timeout=2,
            ) as ws:
                await ws.send(json.dumps(sub_msg))
                runtime_logger.info(f"{base_coin} | OKX subscribed: books5 {okx_symbol}")
                async for message in ws:
                    handle_message(message)

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


async def okx_candle5m_listener(base_coin: str, okx_symbol: str) -> None:
    """OKX business WS candle5m; persist closed bars with volCcy (base coin)."""

    def handle_message(message):
        data = json.loads(message)
        rows = data.get("data")
        if not rows:
            return
        for row in rows:
            # [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
            if not isinstance(row, (list, tuple)) or len(row) < 9:
                continue
            if str(row[8]) != "1":
                continue
            try:
                bar_start = int(float(row[0]))
                volume = float(row[6])  # volCcy = base currency for SWAP
            except (TypeError, ValueError):
                continue
            store_closed_bar(
                base_coin=base_coin,
                ref_exchange="okx",
                bar_start_ts_ms=bar_start,
                volume=volume,
            )

    sub_msg = {
        "op": "subscribe",
        "args": [{"channel": "candle5m", "instId": okx_symbol}],
    }
    if reconnect_v2_enabled():
        await _ws_listen_loop_v2(
            exchange="okx",
            channel="candle5m",
            base_coin=base_coin,
            url="wss://ws.okx.com:8443/ws/v5/business",
            subscribe_payload=sub_msg,
            subscribe_ok_message=(
                f"{base_coin} | OKX subscribed candle5m {okx_symbol} (business WS)"
            ),
            on_message=handle_message,
            connect_priority=CANDLE_CONNECT_PRIORITY,
        )
        return

    ws = None
    while True:
        try:
            async with websockets.connect(
                "wss://ws.okx.com:8443/ws/v5/business",
                ping_interval=20,
                ping_timeout=20,
                close_timeout=2,
            ) as ws:
                await ws.send(json.dumps(sub_msg))
                runtime_logger.info(
                    "%s | OKX subscribed candle5m %s (business WS)",
                    base_coin,
                    okx_symbol,
                )
                async for message in ws:
                    handle_message(message)
        except asyncio.CancelledError:
            runtime_logger.info("%s | OKX candle5m cancelled", base_coin)
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
            raise
        except Exception as exc:
            runtime_logger.error("%s | OKX candle5m error: %s", base_coin, exc)
            await asyncio.sleep(10)


async def bybit_kline5m_listener(base_coin: str, bybit_symbol: str) -> None:
    """Optional Bybit 5m kline (off by default; model canon is OKX)."""

    def handle_message(message):
        data = json.loads(message)
        rows = data.get("data")
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("confirm") is not True:
                continue
            try:
                store_closed_bar(
                    base_coin=base_coin,
                    ref_exchange="bybit",
                    bar_start_ts_ms=int(row["start"]),
                    volume=float(row["volume"]),
                )
            except (KeyError, TypeError, ValueError):
                continue

    sub_msg = {"op": "subscribe", "args": [f"kline.5.{bybit_symbol}"]}
    if reconnect_v2_enabled():
        await _ws_listen_loop_v2(
            exchange="bybit",
            channel="kline.5",
            base_coin=base_coin,
            url="wss://stream.bybit.com/v5/public/linear",
            subscribe_payload=sub_msg,
            subscribe_ok_message=f"{base_coin} | Bybit subscribed kline.5.{bybit_symbol}",
            on_message=handle_message,
            connect_priority=CANDLE_CONNECT_PRIORITY,
        )
        return

    ws = None
    while True:
        try:
            async with websockets.connect(
                "wss://stream.bybit.com/v5/public/linear",
                ping_interval=20,
                ping_timeout=20,
                close_timeout=2,
            ) as ws:
                await ws.send(json.dumps(sub_msg))
                runtime_logger.info(
                    "%s | Bybit subscribed kline.5.%s", base_coin, bybit_symbol
                )
                async for message in ws:
                    handle_message(message)
        except asyncio.CancelledError:
            runtime_logger.info("%s | Bybit kline5m cancelled", base_coin)
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
            raise
        except Exception as exc:
            runtime_logger.error("%s | Bybit kline5m error: %s", base_coin, exc)
            await asyncio.sleep(10)


async def heartbeat():
    while True:
        snap = publisher.metrics_snapshot() if publisher is not None else {}
        spool_snap = spool.metrics_snapshot() if spool is not None else {}
        bar_snap = bars_publisher.metrics_snapshot() if bars_publisher is not None else {}
        reconnect_bits = ""
        if reconnect_v2_enabled():
            ws_snap = ws_reconnect.heartbeat_fields()
            reconnect_bits = (
                " | reconnect_mode=%s | ws_disconnects=%s | ws_reconnect_planned=%s | "
                "ws_reconnect_unplanned=%s | ws_subscribe_ok=%s | ws_unrecovered=%s | "
                "ws_unrecovered_active=%s | ws_budget_exceeded=%s | ws_protocol_errors=%s | "
                "ws_wave_okx_60s=%s | ws_wave_bybit_60s=%s"
            )
            reconnect_args = (
                ws_snap["reconnect_mode"],
                ws_snap["ws_disconnects"],
                ws_snap["ws_reconnect_planned"],
                ws_snap["ws_reconnect_unplanned"],
                ws_snap["ws_subscribe_ok"],
                ws_snap["ws_unrecovered"],
                ws_snap["ws_unrecovered_active"],
                ws_snap["ws_budget_exceeded"],
                ws_snap["ws_protocol_errors"],
                ws_snap["ws_wave_okx_60s"],
                ws_snap["ws_wave_bybit_60s"],
            )
        else:
            reconnect_args = ("v1",)
            reconnect_bits = " | reconnect_mode=%s"
        valid_snap = tick_validity.heartbeat_fields()
        reconnect_bits += (
            " | ticks_suppressed_stale=%s | ticks_suppressed_generation=%s | "
            "ticks_accepted=%s"
        )
        reconnect_args = reconnect_args + (
            valid_snap["ticks_suppressed_stale"],
            valid_snap["ticks_suppressed_generation"],
            valid_snap["ticks_accepted"],
        )
        runtime_logger.info(
            "heartbeat | pairs=%s | schema_mode=%s | collect_bars=%s | "
            "buffer_size=%s | bar_buffer_size=%s | queue_depth=%s | "
            "published_rows=%s | published_files=%s | failures=%s | "
            "accepted_records=%s | rejected_records=%s | "
            "quarantined_records=%s | "
            "bytes_written=%s | last_write_latency_ms=%s | "
            "bar_published_rows=%s | "
            "spool_files_count=%s | spool_bytes_total=%s | "
            "spool_recovered_total=%s | spool_recovery_failed_total=%s"
            + reconnect_bits,
            len(pairs),
            tick_schema_mode(),
            str(collect_bars_enabled()).lower(),
            len(opportunities_buffer),
            len(bar_buffer),
            snap.get("queue_depth", 0),
            snap.get("published_rows_total", 0),
            snap.get("published_files_total", 0),
            snap.get("failures_total", 0),
            snap.get("accepted_records_total", 0),
            snap.get("rejected_records_total", 0),
            snap.get("quarantined_records_total", 0),
            snap.get("bytes_written_total", 0),
            snap.get("last_write_latency_ms"),
            bar_snap.get("published_rows_total", 0),
            spool_snap.get("spool_files_count", 0),
            spool_snap.get("spool_bytes_total", 0),
            spool_snap.get("spool_recovered_total", 0),
            spool_snap.get("spool_recovery_failed_total", 0),
            *reconnect_args,
        )
        await asyncio.sleep(30)


async def monitor_primary_storage_failure(main_task: asyncio.Task) -> None:
    while not mount_failure_state.is_dead():
        await asyncio.sleep(0.1)

    failure = mount_failure_state.failure()
    runtime_logger.critical(
        "primary_storage_failure_detected | source=%s | batch_id=%s | reason=%s",
        failure.source if failure is not None else "unknown",
        failure.batch_id if failure is not None else None,
        failure.reason if failure is not None else "unknown",
    )
    main_task.cancel()


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


async def main():
    global publisher, bars_publisher, recovery_worker, bars_recovery_worker
    global spool, bars_spool

    # Refresh path/flag module views for test env overrides at main() time.
    parquet_root = resolve_parquet_root()
    bars_root = resolve_bars_root()
    bars_pq_root = bars_parquet_root(bars_root)
    do_bars = collect_bars_enabled()

    parquet_root.mkdir(parents=True, exist_ok=True)
    assert_storage_root_writable(parquet_root)
    gaps_root = resolve_gaps_root()
    ws_gap_journal.root = gaps_root
    try:
        gaps_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        runtime_logger.warning(
            "main | gaps_root_mkdir_failed | path=%s | %s", gaps_root, exc
        )
    spool = DurableSpool(
        logger=runtime_logger,
        mount_failure_state=mount_failure_state,
    )
    publisher = ParquetPublisher(
        parquet_root=parquet_root,
        logger=runtime_logger,
        failed_batches_logger=failed_batches_logger,
        mount_failure_state=mount_failure_state,
        spool=spool,
        max_queue=PUBLISHER_MAX_QUEUE,
        name="tick-publisher",
    )
    publisher.start()
    recovery_worker = SpoolRecoveryWorker(
        spool=spool,
        parquet_root=parquet_root,
        logger=runtime_logger,
        mount_failure_state=mount_failure_state,
    )
    recovery_worker.start()

    if do_bars:
        bars_pq_root.mkdir(parents=True, exist_ok=True)
        assert_storage_root_writable(bars_pq_root)
        bars_spool = DurableSpool(
            logger=runtime_logger,
            mount_failure_state=mount_failure_state,
            root=resolve_spool_root() / "bars",
        )
        bars_publisher = ParquetPublisher(
            parquet_root=bars_pq_root,
            logger=runtime_logger,
            failed_batches_logger=failed_batches_logger,
            mount_failure_state=mount_failure_state,
            spool=bars_spool,
            max_queue=PUBLISHER_MAX_QUEUE,
            schema_mode="bar_5m",
            name="bars-publisher",
        )
        bars_publisher.start()
        bars_recovery_worker = SpoolRecoveryWorker(
            spool=bars_spool,
            parquet_root=bars_pq_root,
            logger=runtime_logger,
            mount_failure_state=mount_failure_state,
        )
        bars_recovery_worker.start()

    runtime_logger.info(
        "runtime_paths | parquet_root=%s | bars_root=%s | bars_parquet_root=%s | "
        "gaps_root=%s | runtime_log=%s | failed_batches_log=%s | spool_root=%s | "
        "schema_mode=%s | collect_bars=%s | collect_bybit_bars=%s | "
        "reconnect_mode=%s | connect_per_sec=%s | subscribe_batch_size=%s",
        parquet_root,
        bars_root,
        bars_pq_root,
        ws_gap_journal.root,
        RUNTIME_LOG_PATH,
        FAILED_BATCHES_LOG_PATH,
        spool.root,
        tick_schema_mode(),
        str(do_bars).lower(),
        str(COLLECT_BYBIT_BARS).lower(),
        "v2" if reconnect_v2_enabled() else "v1",
        connect_per_sec(),
        subscribe_batch_size(v2=reconnect_v2_enabled()),
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
            monitor_primary_storage_failure(main_task),
            name="primary-storage-failure-monitor",
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
        batch_size = subscribe_batch_size(v2=reconnect_v2_enabled())
        if batch_size <= 0:
            runtime_logger.info(
                "Starting subscriptions via connect scheduler | "
                "connect_per_sec=%s | coins=%s | books_first=true",
                connect_per_sec(),
                len(pairs),
            )
            for row in pairs:
                base_coin = row["base_coin"]
                okx_symbol = row["okx_symbol"]
                bybit_symbol = row["bybit_symbol"]
                tasks.append(
                    asyncio.create_task(okx_listener(base_coin, okx_symbol), name=f"okx:{base_coin}")
                )
                tasks.append(
                    asyncio.create_task(
                        bybit_listener(base_coin, bybit_symbol), name=f"bybit:{base_coin}"
                    )
                )
            if do_bars:
                for row in pairs:
                    tasks.append(
                        asyncio.create_task(
                            okx_candle5m_listener(row["base_coin"], row["okx_symbol"]),
                            name=f"okx-candle:{row['base_coin']}",
                        )
                    )
                    if COLLECT_BYBIT_BARS:
                        tasks.append(
                            asyncio.create_task(
                                bybit_kline5m_listener(
                                    row["base_coin"], row["bybit_symbol"]
                                ),
                                name=f"bybit-kline:{row['base_coin']}",
                            )
                        )
        else:
            pair_batches = list(chunked(pairs, batch_size))
            runtime_logger.info(
                f"Starting subscriptions in batches | batch_size={batch_size} | "
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
                    if do_bars:
                        tasks.append(
                            asyncio.create_task(
                                okx_candle5m_listener(base_coin, okx_symbol),
                                name=f"okx-candle:{base_coin}",
                            )
                        )
                        if COLLECT_BYBIT_BARS:
                            tasks.append(
                                asyncio.create_task(
                                    bybit_kline5m_listener(base_coin, bybit_symbol),
                                    name=f"bybit-kline:{base_coin}",
                                )
                            )

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
                "main | primary storage failure shutdown | source=%s | batch_id=%s",
                mount_failure.source,
                mount_failure.batch_id,
            )

        for task in tasks:
            task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0.25)
        if mount_failure is not None:
            raise RuntimeError(
                f"primary storage failure detected by {mount_failure.source}: "
                f"{mount_failure.reason}"
            )
        # Expected signal shutdown: do not re-raise CancelledError so the
        # process can exit 0 after a successful finally flush/drain.
    finally:
        shutdown_error: Exception | None = None
        try:
            if mount_failure_state.is_dead():
                runtime_logger.warning(
                    "main | spooling_buffer_after_primary_failure | "
                    "tick_buffer=%s | bar_buffer=%s",
                    len(opportunities_buffer),
                    len(bar_buffer),
                )
                if not spool_opportunities_buffer(
                    reason="primary_storage_failure_shutdown"
                ):
                    shutdown_error = RuntimeError(
                        "primary storage failure shutdown could not spool buffer"
                    )
                if bars_publisher is not None and not spool_bars_buffer(
                    reason="primary_storage_failure_shutdown"
                ):
                    shutdown_error = RuntimeError(
                        "primary storage failure shutdown could not spool bars buffer"
                    )
            else:
                runtime_logger.info(
                    "main | flushing opportunities buffer before exit"
                )
                if not flush_opportunities_for_shutdown():
                    shutdown_error = RuntimeError(
                        "shutdown flush failed to enqueue or spool remaining buffer"
                    )
                if bars_publisher is not None and not flush_bars_for_shutdown():
                    shutdown_error = RuntimeError(
                        "shutdown flush failed to enqueue or spool remaining bars"
                    )
            if bars_recovery_worker is not None:
                bars_recovery_worker.shutdown()
            if recovery_worker is not None:
                recovery_worker.shutdown()
            if bars_publisher is not None:
                bars_publisher.shutdown()
                bar_snap = bars_publisher.metrics_snapshot()
                bar_accounted = (
                    bar_snap.get("published_jobs_total", 0)
                    + bar_snap.get("spooled_jobs_total", 0)
                    + bar_snap.get("quarantined_jobs_total", 0)
                )
                if bar_snap.get("enqueued_jobs_total", 0) > bar_accounted:
                    shutdown_error = RuntimeError(
                        "bars publisher shutdown incomplete: "
                        "enqueued jobs exceed durable outcomes"
                    )
            if publisher is not None:
                publisher.shutdown()
                snap = publisher.metrics_snapshot()
                accounted = (
                    snap.get("published_jobs_total", 0)
                    + snap.get("spooled_jobs_total", 0)
                    + snap.get("quarantined_jobs_total", 0)
                )
                if snap.get("enqueued_jobs_total", 0) > accounted:
                    shutdown_error = RuntimeError(
                        "publisher shutdown incomplete: enqueued jobs exceed durable outcomes"
                    )
        except Exception as exc:
            shutdown_error = exc
        if shutdown_error is not None:
            raise shutdown_error


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
