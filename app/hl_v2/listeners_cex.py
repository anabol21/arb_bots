"""Bybit + OKX asyncio listeners (staff mode, same channels as prod-next)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

import websockets

from app.hl_v2.staff import QuoteBook
from app.utils.ws_reconnect import (
    BOOK_CONNECT_PRIORITY,
    ExchangeConnectScheduler,
    ReconnectController,
)


async def _wait_reconnect_gates(
    session: Any,
    disconnect: dict[str, Any],
    *,
    connect_scheduler: ExchangeConnectScheduler,
    logger: logging.Logger,
) -> None:
    await asyncio.sleep(float(disconnect["backoff_sec"]))
    ctrl = session.controller
    if ctrl.budget_exceeded(session.key):
        ctrl.counters["budget_exceeded_total"] += 1
        while ctrl.budget_exceeded(session.key):
            wait_sec = min(max(ctrl.time_until_budget_slot(session.key), 1.0), 60.0)
            logger.warning(
                "reconnect_budget_exceeded | exchange=%s | channel=%s | coin=%s | "
                "attempt=%s | wait_ms=%s",
                session.exchange,
                session.channel,
                session.base_coin,
                session.attempt,
                int(round(wait_sec * 1000)),
            )
            await asyncio.sleep(wait_sec)
    await connect_scheduler.acquire(
        session.exchange,
        priority=BOOK_CONNECT_PRIORITY,
        coin=session.base_coin,
    )
    ctrl.mark_connect_slot(session.exchange)
    if disconnect.get("planned", True):
        ctrl.record_planned(session.key)


async def _cex_listen_loop(
    *,
    exchange: str,
    channel: str,
    base_coin: str,
    url: str,
    subscribe_payload: dict[str, Any],
    on_message,
    ws_reconnect: ReconnectController,
    connect_scheduler: ExchangeConnectScheduler,
    logger: logging.Logger,
) -> None:
    """Reconnect loop. Closes the old socket (async with exit) before a new connect."""
    session = ws_reconnect.session(exchange, channel, base_coin)
    pending_disconnect: Optional[dict[str, Any]] = None
    ws = None
    while True:
        try:
            if pending_disconnect is not None:
                await _wait_reconnect_gates(
                    session,
                    pending_disconnect,
                    connect_scheduler=connect_scheduler,
                    logger=logger,
                )
                pending_disconnect = None
            else:
                await connect_scheduler.acquire(
                    exchange,
                    priority=BOOK_CONNECT_PRIORITY,
                    coin=base_coin,
                )
            # Previous socket is closed before this connect (async-with exit).
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=2,
            ) as ws:
                await ws.send(json.dumps(subscribe_payload))
                ok = session.mark_subscribe_ok()
                logger.info(
                    "ws_subscribe_ok | exchange=%s | channel=%s | coin=%s",
                    ok["exchange"],
                    ok["channel"],
                    ok["coin"],
                )
                async for message in ws:
                    on_message(message)
        except asyncio.CancelledError:
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
            raise
        except Exception as exc:
            disconnect = session.on_disconnect(exc, ws)
            pending_disconnect = disconnect
            logger.warning(
                "ws_disconnect | exchange=%s | channel=%s | coin=%s | "
                "close_code=%s | reason_class=%s | attempt=%s | backoff_ms=%s",
                disconnect["exchange"],
                disconnect["channel"],
                disconnect["coin"],
                disconnect.get("close_code"),
                disconnect.get("reason_class"),
                disconnect["attempt"],
                disconnect.get("backoff_ms"),
            )
            unrecovered = session.unrecovered_event(disconnect)
            if unrecovered is not None:
                logger.error(
                    "ws_unrecovered | exchange=%s | channel=%s | coin=%s | attempt=%s",
                    unrecovered["exchange"],
                    unrecovered["channel"],
                    unrecovered["coin"],
                    unrecovered["attempt"],
                )


async def bybit_listener(
    base_coin: str,
    bybit_symbol: str,
    book: QuoteBook,
    *,
    ws_reconnect: ReconnectController,
    connect_scheduler: ExchangeConnectScheduler,
    logger: logging.Logger,
) -> None:
    def handle_message(message: str | bytes) -> None:
        local_recv_ts_ms = time.time() * 1000
        data = json.loads(message)
        if "data" not in data:
            return
        payload = data["data"]
        if not isinstance(payload, dict):
            return
        bids = payload.get("b", [])
        asks = payload.get("a", [])
        if not bids or not asks or len(bids[0]) < 2 or len(asks[0]) < 2:
            return
        exchange_ts_ms = data.get("ts")
        if exchange_ts_ms is None:
            return
        book.update_cex(
            base_coin=base_coin,
            exchange="bybit",
            bid_price=float(bids[0][0]),
            bid_size=float(bids[0][1]),
            ask_price=float(asks[0][0]),
            ask_size=float(asks[0][1]),
            ts_exchange=float(exchange_ts_ms),
            local_recv_ts_ms=local_recv_ts_ms,
        )

    sub_msg = {"op": "subscribe", "args": [f"orderbook.1.{bybit_symbol}"]}
    await _cex_listen_loop(
        exchange="bybit",
        channel="orderbook.1",
        base_coin=base_coin,
        url="wss://stream.bybit.com/v5/public/linear",
        subscribe_payload=sub_msg,
        on_message=handle_message,
        ws_reconnect=ws_reconnect,
        connect_scheduler=connect_scheduler,
        logger=logger,
    )


async def okx_listener(
    base_coin: str,
    okx_symbol: str,
    book: QuoteBook,
    *,
    ws_reconnect: ReconnectController,
    connect_scheduler: ExchangeConnectScheduler,
    logger: logging.Logger,
) -> None:
    def handle_message(message: str | bytes) -> None:
        local_recv_ts_ms = time.time() * 1000
        data = json.loads(message)
        if "data" not in data or not data["data"]:
            return
        payload = data["data"][0]
        bids = payload.get("bids", [])
        asks = payload.get("asks", [])
        if not bids or not asks or len(bids[0]) < 2 or len(asks[0]) < 2:
            return
        exchange_ts_ms = payload.get("ts")
        if exchange_ts_ms is None:
            return
        book.update_cex(
            base_coin=base_coin,
            exchange="okx",
            bid_price=float(bids[0][0]),
            bid_size=float(bids[0][1]),
            ask_price=float(asks[0][0]),
            ask_size=float(asks[0][1]),
            ts_exchange=float(exchange_ts_ms),
            local_recv_ts_ms=local_recv_ts_ms,
        )

    sub_msg = {
        "op": "subscribe",
        "args": [{"channel": "books5", "instId": okx_symbol}],
    }
    await _cex_listen_loop(
        exchange="okx",
        channel="books5",
        base_coin=base_coin,
        url="wss://ws.okx.com:8443/ws/v5/public",
        subscribe_payload=sub_msg,
        on_message=handle_message,
        ws_reconnect=ws_reconnect,
        connect_scheduler=connect_scheduler,
        logger=logger,
    )
