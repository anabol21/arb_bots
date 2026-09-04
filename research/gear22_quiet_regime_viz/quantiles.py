"""Time-weighted quantiles for piecewise-constant L1 series."""

from __future__ import annotations

from typing import Sequence

import numpy as np

# Default quantiles plotted as series on each spread block.
TW_QUANTILE_LEVELS: tuple[float, ...] = (0.25, 0.50, 0.95, 0.99)
TW_QUANTILE_NAMES: tuple[str, ...] = ("tw_p25", "tw_p50", "tw_p95", "tw_p99")


def tick_hold_weights_ms(
    ts_ms: np.ndarray,
    *,
    last_end_ms: int,
) -> np.ndarray:
    """Holding time (ms) for each tick until the next tick (last → ``last_end_ms``).

    Convention (documented in README):
    - ticks sorted ascending in time;
    - weight_i = t_{i+1} - t_i for i < n-1;
    - weight_{n-1} = last_end_ms - t_{n-1} (clamped to ≥ 0);
    - leading gap before the first tick is **unobserved** (no mass).
    """
    ts = np.asarray(ts_ms, dtype="int64")
    n = int(ts.size)
    if n == 0:
        return np.asarray([], dtype="float64")
    w = np.empty(n, dtype="float64")
    if n >= 2:
        w[:-1] = (ts[1:] - ts[:-1]).astype("float64")
    w[-1] = float(max(0, int(last_end_ms) - int(ts[-1])))
    # Zero / negative holds (duplicate timestamps) get zero mass.
    w[w < 0] = 0.0
    return w


def time_weighted_quantiles(
    values: np.ndarray,
    weights_ms: np.ndarray,
    levels: Sequence[float] = TW_QUANTILE_LEVELS,
) -> dict[str, float]:
    """Empirical time-weighted quantiles; NaN if total weight is 0."""
    y = np.asarray(values, dtype="float64")
    w = np.asarray(weights_ms, dtype="float64")
    out = {f"tw_p{int(round(100 * float(q)))}": float("nan") for q in levels}
    # Normalize names to tw_p25 style for 0.25 etc.
    name_map = {
        0.25: "tw_p25",
        0.50: "tw_p50",
        0.95: "tw_p95",
        0.99: "tw_p99",
    }
    out = {name_map.get(float(q), f"tw_p{int(round(100 * float(q)))}"): float("nan") for q in levels}
    if y.size == 0 or w.size != y.size:
        return out
    finite = np.isfinite(y) & np.isfinite(w) & (w > 0)
    if not np.any(finite):
        return out
    y = y[finite]
    w = w[finite]
    order = np.argsort(y, kind="mergesort")
    y = y[order]
    w = w[order]
    cw = np.cumsum(w)
    total = float(cw[-1])
    if total <= 0:
        return out
    # Hyndman-Fan type 7 analogue on the weight CDF: target = q * total.
    for q in levels:
        key = name_map.get(float(q), f"tw_p{int(round(100 * float(q)))}")
        target = float(q) * total
        # First index where cumulative weight >= target.
        j = int(np.searchsorted(cw, target, side="left"))
        j = min(max(j, 0), y.size - 1)
        out[key] = float(y[j])
    return out


def window_hold_weights_ms(
    ts_ms: np.ndarray,
    *,
    window_end_ms: int,
) -> np.ndarray:
    """Same hold rule over a whole analysis window (last tick → window_end)."""
    return tick_hold_weights_ms(ts_ms, last_end_ms=window_end_ms)
