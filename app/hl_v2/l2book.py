"""Parse Hyperliquid ``l2Book`` frames; map top-of-book to bid/ask L1."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

HL_WS_URL = "wss://api.hyperliquid.xyz/ws"
DEFAULT_HL_CHANNELS = ("l2Book",)


@dataclass(frozen=True)
class L2BookParse:
    kind: str
    coin: str | None = None
    ts_ms: int | None = None
    bid_price: float | None = None
    bid_size: float | None = None
    ask_price: float | None = None
    ask_size: float | None = None


def l2book_subscribe_payload(coin: str) -> dict[str, Any]:
    return {
        "method": "subscribe",
        "subscription": {"type": "l2Book", "coin": coin},
    }


def ping_payload() -> dict[str, str]:
    return {"method": "ping"}


def channels_from_env(raw: str | None) -> tuple[str, ...]:
    """HL_CHANNELS. Only ``l2Book`` is supported in this contour."""
    if raw is None or not str(raw).strip():
        return DEFAULT_HL_CHANNELS
    parts = tuple(part.strip() for part in str(raw).split(",") if part.strip())
    if not parts:
        return DEFAULT_HL_CHANNELS
    unsupported = [part for part in parts if part != "l2Book"]
    if unsupported:
        raise ValueError(
            "HL_CHANNELS supports only l2Book in this contour; "
            f"got unsupported={unsupported}"
        )
    return parts


def _level_px_sz(level: object) -> tuple[float, float] | None:
    if not isinstance(level, dict):
        return None
    px = level.get("px")
    sz = level.get("sz")
    if px is None or sz is None:
        return None
    if isinstance(px, bool) or isinstance(sz, bool):
        return None
    try:
        price = float(px)
        size = float(sz)
    except (TypeError, ValueError):
        return None
    if price <= 0 or size < 0:
        return None
    return price, size


def parse_l2book_message(message: str | bytes | dict[str, Any]) -> L2BookParse:
    """Return top-of-book L1 from an l2Book snapshot. Other frames are ignored."""
    if isinstance(message, dict):
        payload: object = message
    else:
        text = message.decode("utf-8") if isinstance(message, bytes) else message
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return L2BookParse("invalid")
    if not isinstance(payload, dict):
        return L2BookParse("invalid")
    channel = payload.get("channel")
    if channel != "l2Book":
        return L2BookParse("ignore")
    data = payload.get("data")
    if not isinstance(data, dict):
        return L2BookParse("incomplete")
    coin_raw = data.get("coin")
    if not isinstance(coin_raw, str) or not coin_raw.strip():
        return L2BookParse("incomplete")
    hl_time = data.get("time")
    if isinstance(hl_time, bool) or not isinstance(hl_time, (int, float)):
        return L2BookParse("incomplete")
    levels = data.get("levels")
    if not isinstance(levels, list) or len(levels) < 2:
        return L2BookParse("incomplete")
    bids = levels[0]
    asks = levels[1]
    if not isinstance(bids, list) or not isinstance(asks, list):
        return L2BookParse("incomplete")
    if not bids or not asks:
        return L2BookParse("incomplete")
    bid = _level_px_sz(bids[0])
    ask = _level_px_sz(asks[0])
    if bid is None or ask is None:
        return L2BookParse("incomplete")
    return L2BookParse(
        "l2Book",
        coin=coin_raw.strip(),
        ts_ms=int(hl_time),
        bid_price=bid[0],
        bid_size=bid[1],
        ask_price=ask[0],
        ask_size=ask[1],
    )
