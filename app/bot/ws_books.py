"""Public book WebSockets with simple reconnect. Never import app.utils.ws_reconnect."""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any, Callable, Optional

import websockets

from app.utils.tick_validity import book_l1_complete

OKX_PUBLIC_WS = "wss://ws.okx.com:8443/ws/v5/public"
BYBIT_LINEAR_WS = "wss://stream.bybit.com/v5/public/linear"

# Bounded exponential backoff for reconnect (not a reconnect-v2 import).
_RECONNECT_BASE_SEC = 5.0
_RECONNECT_CAP_SEC = 60.0
_RECONNECT_JITTER_FRAC = 0.20  # 0–20% additive jitter

OnBook = Callable[[str, str, dict[str, Any]], None]
OnLifecycle = Callable[[str, str, str], None]  # coin, exchange, event


def _reconnect_sleep_sec(attempt: int, *, base: float = _RECONNECT_BASE_SEC, cap: float = _RECONNECT_CAP_SEC) -> float:
    """base * 2^n capped, plus 0–20% jitter. attempt starts at 0 after first failure."""
    n = max(0, int(attempt))
    delay = min(cap, float(base) * (2 ** n))
    jitter = delay * _RECONNECT_JITTER_FRAC * random.random()
    return delay + jitter


def empty_book() -> dict[str, Any]:
    return {
        "bid_price": None,
        "bid_size": None,
        "ask_price": None,
        "ask_size": None,
        "ts_exchange": None,
        "local_recv_ts_ms": None,
        "delivery_latency_ms": None,
        "cts_exchange": None,
    }


def parse_bybit_orderbook_message(message: str | bytes) -> Optional[dict[str, Any]]:
    """Copy of collector Bybit L1 parse — local only."""
    local_recv_ts_ms = time.time() * 1000
    data = json.loads(message)
    if "data" not in data:
        return None
    payload = data["data"]
    if not isinstance(payload, dict):
        return None

    book = empty_book()
    bids = payload.get("b", [])
    asks = payload.get("a", [])
    if bids and len(bids[0]) >= 2:
        book["bid_price"] = float(bids[0][0])
        book["bid_size"] = float(bids[0][1])
    if asks and len(asks[0]) >= 2:
        book["ask_price"] = float(asks[0][0])
        book["ask_size"] = float(asks[0][1])

    exchange_ts_ms = data.get("ts")
    cts_exchange_ms = data.get("cts")
    book["ts_exchange"] = float(exchange_ts_ms) if exchange_ts_ms is not None else None
    book["cts_exchange"] = float(cts_exchange_ms) if cts_exchange_ms is not None else None
    book["local_recv_ts_ms"] = local_recv_ts_ms
    if exchange_ts_ms is not None:
        book["delivery_latency_ms"] = local_recv_ts_ms - float(exchange_ts_ms)
    return book


def parse_okx_books5_message(message: str | bytes) -> Optional[dict[str, Any]]:
    """Copy of collector OKX books5 L1 parse — local only."""
    local_recv_ts_ms = time.time() * 1000
    data = json.loads(message)
    if "data" not in data or not data["data"]:
        return None
    payload = data["data"][0]
    book = empty_book()
    bids = payload.get("bids", [])
    asks = payload.get("asks", [])
    if bids and len(bids[0]) >= 2:
        book["bid_price"] = float(bids[0][0])
        book["bid_size"] = float(bids[0][1])
    if asks and len(asks[0]) >= 2:
        book["ask_price"] = float(asks[0][0])
        book["ask_size"] = float(asks[0][1])

    exchange_ts_ms = payload.get("ts")
    book["ts_exchange"] = float(exchange_ts_ms) if exchange_ts_ms is not None else None
    book["local_recv_ts_ms"] = local_recv_ts_ms
    if exchange_ts_ms is not None:
        book["delivery_latency_ms"] = local_recv_ts_ms - float(exchange_ts_ms)
    return book


def merge_book_update(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for key in (
        "bid_price",
        "bid_size",
        "ask_price",
        "ask_size",
        "ts_exchange",
        "local_recv_ts_ms",
        "delivery_latency_ms",
        "cts_exchange",
    ):
        if key in src and src[key] is not None:
            dst[key] = src[key]
        elif key in ("ts_exchange", "local_recv_ts_ms", "delivery_latency_ms", "cts_exchange"):
            if key in src:
                dst[key] = src[key]


async def _listen_loop(
    *,
    exchange: str,
    channel: str,
    base_coin: str,
    url: str,
    subscribe_payload: dict[str, Any],
    parse_message: Callable[[str | bytes], Optional[dict[str, Any]]],
    book_store: dict[str, Any],
    on_book: OnBook,
    on_lifecycle: Optional[OnLifecycle] = None,
    reconnect_base_sec: float = _RECONNECT_BASE_SEC,
    reconnect_cap_sec: float = _RECONNECT_CAP_SEC,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """Simple reconnect loop with bounded backoff. Only sends public subscribe payloads."""

    def _life(event: str) -> None:
        if on_lifecycle is not None:
            on_lifecycle(base_coin, exchange, event)

    fail_attempt = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        ws = None
        try:
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=2,
            ) as ws:
                # Public subscribe only — never private / order URLs.
                await ws.send(json.dumps(subscribe_payload))
                _life("subscribe_ok")
                fail_attempt = 0  # reset backoff after successful subscribe
                async for message in ws:
                    if stop_event is not None and stop_event.is_set():
                        return
                    parsed = parse_message(message)
                    if parsed is None:
                        continue
                    merge_book_update(book_store, parsed)
                    on_book(base_coin, exchange, book_store)
        except asyncio.CancelledError:
            _life("cancelled")
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
            raise
        except Exception:
            _life("disconnect")
            # Clear incomplete markers so validity fails closed until fresh L1.
            for k in ("bid_price", "ask_price", "ts_exchange", "local_recv_ts_ms"):
                book_store[k] = None
            if stop_event is not None and stop_event.is_set():
                return
            sleep_sec = _reconnect_sleep_sec(
                fail_attempt, base=reconnect_base_sec, cap=reconnect_cap_sec
            )
            fail_attempt += 1
            await asyncio.sleep(sleep_sec)


async def run_okx_books5(
    *,
    base_coin: str,
    okx_symbol: str,
    book_store: dict[str, Any],
    on_book: OnBook,
    on_lifecycle: Optional[OnLifecycle] = None,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    sub = {"op": "subscribe", "args": [{"channel": "books5", "instId": okx_symbol}]}
    await _listen_loop(
        exchange="okx",
        channel="books5",
        base_coin=base_coin,
        url=OKX_PUBLIC_WS,
        subscribe_payload=sub,
        parse_message=parse_okx_books5_message,
        book_store=book_store,
        on_book=on_book,
        on_lifecycle=on_lifecycle,
        stop_event=stop_event,
    )


async def run_bybit_orderbook1(
    *,
    base_coin: str,
    bybit_symbol: str,
    book_store: dict[str, Any],
    on_book: OnBook,
    on_lifecycle: Optional[OnLifecycle] = None,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    sub = {"op": "subscribe", "args": [f"orderbook.1.{bybit_symbol}"]}
    await _listen_loop(
        exchange="bybit",
        channel="orderbook.1",
        base_coin=base_coin,
        url=BYBIT_LINEAR_WS,
        subscribe_payload=sub,
        parse_message=parse_bybit_orderbook_message,
        book_store=book_store,
        on_book=on_book,
        on_lifecycle=on_lifecycle,
        stop_event=stop_event,
    )


def compute_spreads(okx: dict[str, Any], bybit: dict[str, Any]) -> tuple[float, float]:
    """long (bybit_bid-okx_ask)/bybit_bid*100; short (okx_bid-bybit_ask)/okx_bid*100."""
    spread_long = (bybit["bid_price"] - okx["ask_price"]) * 100.0 / bybit["bid_price"]
    spread_short = (okx["bid_price"] - bybit["ask_price"]) * 100.0 / okx["bid_price"]
    return float(spread_long), float(spread_short)


def books_ready(okx: dict[str, Any], bybit: dict[str, Any]) -> bool:
    return book_l1_complete(okx) and book_l1_complete(bybit)
