"""Hyperliquid L1 book schema.

Separate dataset from lean Bybit/OKX ticks and from ``bar_5m``.
The body has no spread columns. ``event_date`` is a hive partition only.

Producer: ``python -m app.hl``.
Writer: ``ParquetPublisher`` with ``schema_mode="hl_l1"``.
Root: ``/data/live-hl`` (``HL_PARQUET_ROOT``). Not ``/data/live``.
"""

from __future__ import annotations

from typing import Sequence

HL_L1_SCHEMA_NAME = "hl_l1"

# Body order. event_date is not included.
HL_L1_BODY_COLS: tuple[str, ...] = (
    "event_local_ts_ms",
    "base_coin",
    "hl_local_recv_ts_ms",
    "hl_ts_ms",
    "hl_bid_price",
    "hl_bid_size",
    "hl_ask_price",
    "hl_ask_size",
)

HL_L1_TS_COLS: tuple[str, ...] = (
    "event_local_ts_ms",
    "hl_local_recv_ts_ms",
    "hl_ts_ms",
)

HL_L1_BOOK_COLS: tuple[str, ...] = (
    "hl_bid_price",
    "hl_bid_size",
    "hl_ask_price",
    "hl_ask_size",
)

# Columns that belong to other datasets. A published HL file must not contain them.
HL_L1_EXCLUDED_FROM_BODY: frozenset[str] = frozenset(
    {
        "event_date",
        "event_dt",
        "trigger",
        "spread_long",
        "spread_short",
        "calc_local_ts_ms",
        "okx_latency_ms",
        "bybit_latency_ms",
        "okx_freshness_ms",
        "bybit_freshness_ms",
        "max_freshness_ms",
        "max_latency_ms",
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
    }
)


def hl_l1_body_is_exact(column_names: Sequence[str]) -> bool:
    """True when the parquet body is exactly the HL L1 columns, in order."""
    names = tuple(column_names)
    if names != HL_L1_BODY_COLS:
        return False
    return HL_L1_EXCLUDED_FROM_BODY.isdisjoint(names)
