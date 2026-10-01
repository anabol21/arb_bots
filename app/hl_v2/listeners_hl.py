"""Sharded Hyperliquid l2Book asyncio listeners."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import websockets

from app.hl_v2.l2book import HL_WS_URL, l2book_subscribe_payload, parse_l2book_message
from app.hl_v2.staff import QuoteBook
from app.utils.ws_reconnect import (
    BOOK_CONNECT_PRIORITY,
    ExchangeConnectScheduler,
    ReconnectController,
    planned_backoff_sec,
)


@dataclass
class ShardStatus:
    shard_id: int
    coins: tuple[str, ...]
    ws_subscribe_ok: bool = False
    active_subs: int = 0
    last_msg_mono: float | None = None
    reconnect_count: int = 0
    frames_total: int = 0
    parsed_total: int = 0
    incomplete_total: int = 0

    def last_msg_age_ms(self, now: float | None = None) -> float | None:
        if self.last_msg_mono is None:
            return None
        ts = time.monotonic() if now is None else now
        return max(0.0, (ts - self.last_msg_mono) * 1000.0)

    def heartbeat_fields(self) -> dict[str, Any]:
        return {
            "shard_id": self.shard_id,
            "ws_subscribe_ok": self.ws_subscribe_ok,
            "active_subs": self.active_subs,
            "last_msg_age_ms": (
                None
                if self.last_msg_age_ms() is None
                else int(round(self.last_msg_age_ms() or 0.0))
            ),
            "reconnect_count": self.reconnect_count,
            "frames_total": self.frames_total,
            "parsed_total": self.parsed_total,
            "incomplete_total": self.incomplete_total,
            "coin_count": len(self.coins),
        }


@dataclass
class HlShardFleet:
    statuses: list[ShardStatus] = field(default_factory=list)

    def heartbeat_fields(self) -> list[dict[str, Any]]:
        return [status.heartbeat_fields() for status in self.statuses]


async def _wait_hl_reconnect(
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
                "reconnect_budget_exceeded | exchange=hyperliquid | "
                "channel=l2Book | shard=%s | attempt=%s | wait_ms=%s",
                session.base_coin,
                session.attempt,
                int(round(wait_sec * 1000)),
            )
            await asyncio.sleep(wait_sec)
    await connect_scheduler.acquire(
        "hyperliquid",
        priority=BOOK_CONNECT_PRIORITY,
        coin=session.base_coin,
    )
    ctrl.mark_connect_slot("hyperliquid")
    if disconnect.get("planned", True):
        ctrl.record_planned(session.key)


async def hl_shard_listener(
    *,
    shard_id: int,
    coins: tuple[str, ...],
    book: QuoteBook,
    status: ShardStatus,
    ws_reconnect: ReconnectController,
    connect_scheduler: ExchangeConnectScheduler,
    logger: logging.Logger,
    url: str = HL_WS_URL,
) -> None:
    """One HL websocket covering ``coins``. Closes old socket before reconnect."""
    if not coins:
        logger.warning("hl_shard_empty | shard_id=%s", shard_id)
        return
    coin_set = set(coins)
    # Session key uses shard label as "coin" so budget is per-socket.
    session = ws_reconnect.session("hyperliquid", "l2Book", f"shard{shard_id}")
    pending_disconnect: Optional[dict[str, Any]] = None
    ws = None

    while True:
        try:
            if pending_disconnect is not None:
                status.reconnect_count += 1
                status.ws_subscribe_ok = False
                status.active_subs = 0
                await _wait_hl_reconnect(
                    session,
                    pending_disconnect,
                    connect_scheduler=connect_scheduler,
                    logger=logger,
                )
                pending_disconnect = None
            else:
                await connect_scheduler.acquire(
                    "hyperliquid",
                    priority=BOOK_CONNECT_PRIORITY,
                    coin=f"shard{shard_id}",
                )

            # Explicit close-before-open: leave prior async-with before reconnect.
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=2,
                max_size=8 * 1024 * 1024,
            ) as ws:
                for coin in coins:
                    await ws.send(json.dumps(l2book_subscribe_payload(coin)))
                ok = session.mark_subscribe_ok()
                status.ws_subscribe_ok = True
                status.active_subs = len(coins)
                logger.info(
                    "ws_subscribe_ok | exchange=hyperliquid | channel=l2Book | "
                    "shard_id=%s | active_subs=%s | attempt=%s",
                    shard_id,
                    status.active_subs,
                    ok.get("attempt"),
                )
                async for message in ws:
                    status.frames_total += 1
                    status.last_msg_mono = time.monotonic()
                    local_recv_ts_ms = time.time() * 1000
                    parsed = parse_l2book_message(message)
                    if parsed.kind == "l2Book":
                        status.parsed_total += 1
                        coin = parsed.coin or ""
                        if coin not in coin_set:
                            continue
                        book.update_hl(
                            base_coin=coin,
                            bid_price=float(parsed.bid_price),
                            bid_size=float(parsed.bid_size),
                            ask_price=float(parsed.ask_price),
                            ask_size=float(parsed.ask_size),
                            ts_exchange=float(parsed.ts_ms),
                            local_recv_ts_ms=local_recv_ts_ms,
                        )
                    elif parsed.kind == "incomplete":
                        status.incomplete_total += 1
                    elif parsed.kind == "invalid":
                        logger.warning(
                            "hl_l2book_invalid | shard_id=%s | message=%s",
                            shard_id,
                            str(message)[:200],
                        )
        except asyncio.CancelledError:
            status.ws_subscribe_ok = False
            status.active_subs = 0
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
            raise
        except Exception as exc:
            # Ensure socket is closed before planning reconnect.
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
                ws = None
            disconnect = session.on_disconnect(exc, ws)
            # Ensure backoff_sec present even if helper shape changes.
            if "backoff_sec" not in disconnect:
                disconnect["backoff_sec"] = planned_backoff_sec(
                    int(disconnect.get("attempt", 1))
                )
            pending_disconnect = disconnect
            status.ws_subscribe_ok = False
            status.active_subs = 0
            logger.warning(
                "ws_disconnect | exchange=hyperliquid | channel=l2Book | "
                "shard_id=%s | close_code=%s | reason_class=%s | attempt=%s | "
                "backoff_ms=%s",
                shard_id,
                disconnect.get("close_code"),
                disconnect.get("reason_class"),
                disconnect.get("attempt"),
                disconnect.get("backoff_ms"),
            )
            unrecovered = session.unrecovered_event(disconnect)
            if unrecovered is not None:
                logger.error(
                    "ws_unrecovered | exchange=hyperliquid | channel=l2Book | "
                    "shard_id=%s | attempt=%s",
                    shard_id,
                    unrecovered.get("attempt"),
                )
