"""Parse one Hyperliquid ``bbo`` websocket frame.

Wire shape (public market data)::

    {"channel": "bbo", "data": {"coin": "BTC", "time": <ms>,
     "bbo": [{"px": "...", "sz": "...", "n": N}, {"px": "...", "sz": "...", "n": N}]}}

Index 0 is bid, index 1 is ask. A null side is incomplete and is not a record.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

HL_WS_URL = "wss://api.hyperliquid.xyz/ws"


@dataclass(frozen=True)
class BboParse:
    kind: str
    record: dict[str, Any] | None = None


def bbo_subscribe_payload(coin: str) -> dict[str, Any]:
    return {
        "method": "subscribe",
        "subscription": {"type": "bbo", "coin": coin},
    }


def ping_payload() -> dict[str, str]:
    return {"method": "ping"}


def _level(level: object) -> tuple[str, str] | None:
    if not isinstance(level, dict):
        return None
    px = level.get("px")
    sz = level.get("sz")
    if px is None or sz is None:
        return None
    if isinstance(px, bool) or isinstance(sz, bool):
        return None
    if not isinstance(px, (str, int, float)) or not isinstance(sz, (str, int, float)):
        return None
    return (str(px), str(sz))


def parse_bbo_message(message: str | bytes | dict[str, Any], *, recv_ts_ms: int) -> BboParse:
    """Return a record only for a complete two-sided bbo. Other frames are ignored."""
    if isinstance(message, dict):
        payload: object = message
    else:
        text = message.decode("utf-8") if isinstance(message, bytes) else message
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return BboParse("invalid")
    if not isinstance(payload, dict):
        return BboParse("invalid")
    if payload.get("channel") != "bbo":
        return BboParse("ignore")
    data = payload.get("data")
    if not isinstance(data, dict):
        return BboParse("incomplete")
    coin = data.get("coin")
    if not isinstance(coin, str) or not coin.strip():
        return BboParse("incomplete")
    hl_time = data.get("time")
    if isinstance(hl_time, bool) or not isinstance(hl_time, (int, float)):
        return BboParse("incomplete")
    bbo = data.get("bbo")
    if not isinstance(bbo, list) or len(bbo) < 2:
        return BboParse("incomplete")
    bid = _level(bbo[0])
    ask = _level(bbo[1])
    if bid is None or ask is None:
        return BboParse("incomplete")
    local_ms = int(recv_ts_ms)
    return BboParse(
        "bbo",
        {
            "base_coin": coin.strip(),
            "event_local_ts_ms": local_ms,
            "hl_local_recv_ts_ms": local_ms,
            "hl_ts_ms": int(hl_time),
            "hl_bid_price": bid[0],
            "hl_bid_size": bid[1],
            "hl_ask_price": ask[0],
            "hl_ask_size": ask[1],
        },
    )
