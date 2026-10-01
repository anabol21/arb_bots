"""Hyperliquid v2 canary tick schema (Bybit + OKX lean staff + HL L1).

Separate dataset from lean Bybit/OKX ticks and from the HL-only ``hl_l1`` draft.
No spread columns — post-process spreads/matrix offline.

Producer: ``python -m app.hl_v2``.
Writer: ``ParquetPublisher`` with ``schema_mode="hl_v2"``.
Root default: ``/data/live_hl_v2``. Never ``/data/live``.
"""

from __future__ import annotations

from typing import Sequence

HL_V2_SCHEMA_NAME = "hl_v2"

# Lean Bybit/OKX staff columns (prod-next lean body) plus HL L1 fields.
HL_V2_BODY_COLS: tuple[str, ...] = (
    "event_local_ts_ms",
    "base_coin",
    "trigger",
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
    "hl_local_recv_ts_ms",
    "hl_ts_ms",
    "hl_bid_price",
    "hl_bid_size",
    "hl_ask_price",
    "hl_ask_size",
)

HL_V2_TS_COLS: tuple[str, ...] = (
    "event_local_ts_ms",
    "calc_local_ts_ms",
    "okx_local_recv_ts_ms",
    "okx_ts_ms",
    "bybit_local_recv_ts_ms",
    "bybit_ts_ms",
    "hl_local_recv_ts_ms",
    "hl_ts_ms",
)

HL_V2_BOOK_COLS: tuple[str, ...] = (
    "okx_bid_price",
    "okx_bid_size",
    "okx_ask_price",
    "okx_ask_size",
    "bybit_bid_price",
    "bybit_bid_size",
    "bybit_ask_price",
    "bybit_ask_size",
    "hl_bid_price",
    "hl_bid_size",
    "hl_ask_price",
    "hl_ask_size",
)

# Must never appear in published hl_v2 body.
HL_V2_EXCLUDED_FROM_BODY: frozenset[str] = frozenset(
    {
        "event_date",
        "event_dt",
        "spread_long",
        "spread_short",
        "okx_latency_ms",
        "bybit_latency_ms",
        "okx_freshness_ms",
        "bybit_freshness_ms",
        "max_freshness_ms",
        "max_latency_ms",
    }
)


def hl_v2_body_is_exact(column_names: Sequence[str]) -> bool:
    """True when parquet body is exactly HL_V2_BODY_COLS in order."""
    names = tuple(column_names)
    if names != HL_V2_BODY_COLS:
        return False
    return HL_V2_EXCLUDED_FROM_BODY.isdisjoint(names)
