"""Extension traces: one corridor panel + SMA-12 floor per side.

Gear 2.2 observation tool only — these series are plotted for visual
inspection, not as live bot thresholds.

One graph per side (appended after the 6-row stack)::

    inner band  p5–p95 of the 5m candle (TW hold→next)
    outer band  p1–p99 dashed
    SMA-3       display midline inside the inner band
    tf-select α25 = min(trim_3h_α25, trim_12h_α25) of SMA-12
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from research.gear22_quiet_regime_viz.candles import causal_sma
from research.gear22_quiet_regime_viz.floors import (
    SMA3_NAME,
    TF_SELECT_25_NAME,
    W1_BARS,
    compute_chosen_floor,
)

# Standalone appended figure (not extra rows on the 6-row stack).
FLOOR_PANELS: tuple[str, ...] = ("floor",)


@dataclass(frozen=True)
class MetricTrace:
    """One optional overlay for a spread-block figure.

    ``panel`` selects a subplot id understood by ``plot.build_spread_block_figure``:
    ``candles`` | ``tick_count`` | ``gap_fraction`` | ``mean_std`` | ``range_iqr`` |
    ``tw_quantiles`` | ``floor``.

    Prefix with ``long_`` / ``short_`` to target only one side
    (e.g. ``long_floor``). Unprefixed traces are offered to both blocks;
    the writer filters by side prefix when present.
    """

    name: str
    panel: str
    x: Sequence[Any]
    y: Sequence[float]
    mode: str = "lines"
    line: Mapping[str, Any] = field(default_factory=dict)
    meta: Mapping[str, Any] = field(default_factory=dict)
    showlegend: bool = True
    legendgroup: str | None = None


def _sma_from_buckets(buckets: pd.DataFrame, window: int) -> np.ndarray:
    if buckets is None or buckets.empty:
        return np.asarray([], dtype="float64")
    col = f"ma_{int(window)}"
    if col in buckets.columns:
        return buckets[col].to_numpy(dtype="float64")
    if "close" not in buckets.columns:
        return np.asarray([], dtype="float64")
    return causal_sma(buckets["close"].to_numpy(dtype="float64"), int(window))


def _sma12_from_buckets(buckets: pd.DataFrame) -> np.ndarray:
    return _sma_from_buckets(buckets, W1_BARS)


def _floor_traces_for_side(
    buckets: pd.DataFrame,
    *,
    side: str,
) -> list[MetricTrace]:
    if buckets is None or buckets.empty or "bar_start_dt" not in buckets.columns:
        return []
    sma12 = _sma12_from_buckets(buckets)
    if sma12.size == 0:
        return []
    sma3 = _sma_from_buckets(buckets, 3)
    x = list(buckets["bar_start_dt"])
    chosen = compute_chosen_floor(sma12)
    traces: list[MetricTrace] = []
    if sma3.size:
        traces.append(
            MetricTrace(
                name=SMA3_NAME,
                panel=f"{side}_floor",
                x=x,
                y=list(sma3),
                mode="lines",
                line={},
                meta={"series": SMA3_NAME, "w1_bars": 3},
                legendgroup=SMA3_NAME,
            )
        )
    traces.append(
        MetricTrace(
            name=TF_SELECT_25_NAME,
            panel=f"{side}_floor",
            x=x,
            y=list(chosen[TF_SELECT_25_NAME]),
            mode="lines",
            line={},
            meta={"series": TF_SELECT_25_NAME, "w1_bars": W1_BARS},
        )
    )
    return traces


def collect_extension_traces(
    ticks: pd.DataFrame,
    buckets: pd.DataFrame,
    *,
    coin: str,
    buckets_short: Optional[pd.DataFrame] = None,
    floors: bool = True,
) -> list[MetricTrace]:
    """Return chosen-floor traces (one panel per side).

    ``floors=False`` keeps the historical empty-hook behavior. Default is on.
    The HTML writer draws a standalone corridor figure rather than extra
    rows on the 6-row stack.
    """
    _ = (ticks, coin)
    if not floors:
        return []
    traces = _floor_traces_for_side(buckets, side="long")
    if buckets_short is not None:
        traces.extend(_floor_traces_for_side(buckets_short, side="short"))
    return traces


def extension_help_html() -> str:
    """HTML blurb embedded in each page describing the floor corridor."""
    return (
        "<section class='ext-help'>"
        "<h2>Floor panel — corridor + tf-select α25</h2>"
        "<p>After each 6-row stack: <strong>one</strong> graph per side.</p>"
        "<p><strong>Inner corridor</strong> (filled): time-weighted "
        "p5–p95 of the intra-bar spread (hold→next, last tick → bar end). "
        "<strong>Outer corridor</strong> (dashed): p1–p99, same convention. "
        "On already-built pages a missing p5/p1 is reconstructed from the "
        "candle inspect histogram and labeled as reconstructed — never "
        "silently replaced by min/max.</p>"
        "<p><strong>SMA-3</strong> (orange) sits inside the inner band. "
        "<strong>tf-select α25</strong> (purple) is the chosen floor: "
        "min of the 3h and 12h 25%-trimmed means of SMA-12. Observation "
        "only — not a live threshold.</p>"
        "</section>"
    )
