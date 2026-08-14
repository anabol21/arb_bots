"""Канон колонок события спреда (тело parquet, без hive-партиций)."""

from __future__ import annotations

import os

from .lean_event import LEAN_TICK_BODY_COLS

# Полный контракт v1. Согласован с app/storage/writer.py и docs/storage-contract.md.
# event_date — только партиция на диске, в body не пишется.
SPREAD_EVENT_BODY_COLS: tuple[str, ...] = (
    "event_dt",
    "event_local_ts_ms",
    "base_coin",
    "trigger",
    "spread_long",
    "spread_short",
    "okx_latency_ms",
    "bybit_latency_ms",
    "okx_freshness_ms",
    "bybit_freshness_ms",
    "max_freshness_ms",
    "max_latency_ms",
    "calc_local_ts_ms",
    "okx_local_recv_ts_ms",
    "okx_ts_ms",
    "bybit_local_recv_ts_ms",
    "bybit_ts_ms",
    "okx_bid_price",
    "okx_bid_size",
    "okx_ask_price",
    "okx_ask_size",
    "bybit_bid_price",
    "bybit_bid_size",
    "bybit_ask_price",
    "bybit_ask_size",
)

# Восемь полей L1; отсутствие = legacy-партиция.
SPREAD_EVENT_BOOK_COLS: tuple[str, ...] = (
    "okx_bid_price",
    "okx_bid_size",
    "okx_ask_price",
    "okx_ask_size",
    "bybit_bid_price",
    "bybit_bid_size",
    "bybit_ask_price",
    "bybit_ask_size",
)

# Option B: keep v1 canary output until explicitly enabled.
_ENV_LEAN_SCHEMA = "SPREAD_LEAN_SCHEMA"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def lean_schema_enabled() -> bool:
    """Return True when new tick writes should use the lean 16-column body.

    Default OFF so a code deploy does not change a running VPS canary's schema.
    Enable after canary: ``SPREAD_LEAN_SCHEMA=1``.
    """
    return os.environ.get(_ENV_LEAN_SCHEMA, "").strip().lower() in _TRUTHY


def active_tick_body_cols() -> tuple[str, ...]:
    """Body columns for the currently selected tick schema (v1 or lean)."""
    if lean_schema_enabled():
        return LEAN_TICK_BODY_COLS
    return SPREAD_EVENT_BODY_COLS


def tick_schema_mode() -> str:
    """``lean`` or ``v1`` for the production tick writer."""
    return "lean" if lean_schema_enabled() else "v1"
