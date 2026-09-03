"""Gear 2.2 quiet-regime research visualizer (observation tool, not a threshold).

Builds one Plotly HTML page per coin from gappy L1 ticks: 5m edge candles,
intra-bucket stats, causal MAs, sparse ticks, and red gap regions.

This module does **not** invent quiet-regime floors or change live bot params.
Extension hooks for future integral metrics live in ``metrics_ext``.
"""

from __future__ import annotations

from research.gear22_quiet_regime_viz.candles import BAR_MS, build_5m_bucket_stats
from research.gear22_quiet_regime_viz.gaps import detect_gap_intervals
from research.gear22_quiet_regime_viz.load import (
    DEFAULT_SINCE_UTC,
    derive_research_series,
    load_ticks,
    parse_since_ms,
)
from research.gear22_quiet_regime_viz.plot import write_coin_html

__all__ = [
    "BAR_MS",
    "DEFAULT_SINCE_UTC",
    "build_5m_bucket_stats",
    "detect_gap_intervals",
    "derive_research_series",
    "load_ticks",
    "parse_since_ms",
    "write_coin_html",
]
