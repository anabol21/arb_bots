"""Gear 2.2 quiet-regime research visualizer (observation tool, not a threshold).

Builds one Plotly HTML page per coin from gappy L1 ticks: dual long/short
5m candles, intra-bucket stats, time-weighted quantiles, window histograms,
causal MAs, sparse ticks, red gap regions, and click-to-inspect in-bar
distribution panels (compact hist + equal-time means).

One corridor panel per side (p1–p99 dashed, p5–p95 fill, SMA-3,
tf-select α25 of SMA-12) is plotted for observation only; it does not change live
bot params. See ``floors`` / ``metrics_ext``.
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
from research.gear22_quiet_regime_viz.quantiles import (
    rolling_tw_p50,
    time_weighted_quantiles,
    tw_p50,
)

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
    "rolling_tw_p50",
    "time_weighted_quantiles",
    "tw_p50",
    "write_coin_html",
]
