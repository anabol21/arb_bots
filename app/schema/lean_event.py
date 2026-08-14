"""Lean tick/bar schemas (model-oriented body).

Production writer uses these when ``SPREAD_LEAN_SCHEMA=1`` / ``schema_mode`` is
``lean`` or ``bar_5m``. Default canary path stays on ``SPREAD_EVENT_BODY_COLS``.

Unit / lot / contract metadata lives in ``bybit_okx_universe.csv`` and is
joined at analysis time — never written into lean parquet rows.
"""

from __future__ import annotations

# Target lean tick body from docs/data-format-ingest-gap.md §4.1.
# No spread_*, max_*, freshness, event_dt, latency (derive at read).
LEAN_TICK_BODY_COLS: tuple[str, ...] = (
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
)

LEAN_TICK_BOOK_COLS: tuple[str, ...] = (
    "okx_bid_price",
    "okx_bid_size",
    "okx_ask_price",
    "okx_ask_size",
    "bybit_bid_price",
    "bybit_bid_size",
    "bybit_ask_price",
    "bybit_ask_size",
)

# bars_5m v0 — closed candle volume only. Semantics fixed by channel choice
# in docs/local-lean-collector.md (OKX volCcy = base coin for SWAP).
LEAN_BAR_5M_BODY_COLS: tuple[str, ...] = (
    "bar_start_ts_ms",
    "bar_end_ts_ms",
    "base_coin",
    "ref_exchange",
    "volume",
)

BAR_INTERVAL_MS = 300_000
"""Fixed 5-minute window length in milliseconds."""
