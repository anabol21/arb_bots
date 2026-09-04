"""Gear 2.2 quiet-regime research visualizer (observation tool, not a threshold).

Builds one Plotly HTML page per coin from gappy L1 ticks: dual long/short
5m candles, intra-bucket stats, time-weighted quantiles, window histograms,
causal MAs, sparse ticks, red gap regions, and click-to-inspect in-bar
panels (TW-mass hist + TW p50/p95/p99 + equal-time temporal means).

This module does **not** invent quiet-regime floors or change live bot params.
Extension hooks for future integral metrics live in ``metrics_ext``.
"""

from __future__ import annotations

from research.gear22_quiet_regime_viz.candles import (
    BAR_MS,
    DEFAULT_CANDLE_BINS,
    SPREAD_LONG_COL,
    SPREAD_SHORT_COL,
    build_5m_bucket_stats,
    build_bar_inspect_payloads,
)
from research.gear22_quiet_regime_viz.gaps import detect_gap_intervals
from research.gear22_quiet_regime_viz.load import (
    DEFAULT_SINCE_UTC,
    derive_research_series,
    load_ticks,
    parse_since_ms,
)
from research.gear22_quiet_regime_viz.plot import write_coin_html
from research.gear22_quiet_regime_viz.quantiles import time_weighted_quantiles

__all__ = [
    "BAR_MS",
    "DEFAULT_CANDLE_BINS",
    "DEFAULT_SINCE_UTC",
    "SPREAD_LONG_COL",
    "SPREAD_SHORT_COL",
    "build_5m_bucket_stats",
    "build_bar_inspect_payloads",
    "detect_gap_intervals",
    "derive_research_series",
    "load_ticks",
    "parse_since_ms",
    "time_weighted_quantiles",
    "write_coin_html",
]
