"""Time-weighted quantiles / hist helpers for piecewise-constant L1 series."""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np

# Default quantiles stored on each 5m bucket (hold→next TW).
TW_QUANTILE_LEVELS: tuple[float, ...] = (0.01, 0.05, 0.25, 0.50, 0.95, 0.99)
TW_QUANTILE_NAMES: tuple[str, ...] = (
    "tw_p01",
    "tw_p05",
    "tw_p25",
    "tw_p50",
    "tw_p95",
    "tw_p99",
)
# Original 6th-row series — do not add p01/p05 there (floor panel owns those).
TW_ROW6_NAMES: tuple[str, ...] = ("tw_p25", "tw_p50", "tw_p95", "tw_p99")

TW_NAME_MAP: dict[float, str] = {
    0.01: "tw_p01",
    0.05: "tw_p05",
    0.25: "tw_p25",
    0.50: "tw_p50",
    0.95: "tw_p95",
    0.99: "tw_p99",
}

# Inspect-panel hist: robust axis + percentiles shown on the UI.
INSPECT_RANGE_LEVELS: tuple[float, ...] = (0.01, 0.50, 0.95, 0.99)
INSPECT_PERCENTILE_KEYS: tuple[str, ...] = ("p50", "p95", "p99")


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


def _quantile_name_map(levels: Sequence[float]) -> dict[float, str]:
    known = {
        0.01: "tw_p01",
        0.05: "tw_p05",
        0.25: "tw_p25",
        0.50: "tw_p50",
        0.95: "tw_p95",
        0.99: "tw_p99",
    }
    return {
        float(q): known.get(float(q), f"tw_p{int(round(100 * float(q)))}")
        for q in levels
    }


def time_weighted_quantiles(
    values: np.ndarray,
    weights_ms: np.ndarray,
    levels: Sequence[float] = TW_QUANTILE_LEVELS,
) -> dict[str, float]:
    """Empirical time-weighted quantiles; NaN if total weight is 0."""
    y = np.asarray(values, dtype="float64")
    w = np.asarray(weights_ms, dtype="float64")
    name_map = _quantile_name_map(levels)
    out = {name_map[float(q)]: float("nan") for q in levels}
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
        key = name_map[float(q)]
        target = float(q) * total
        # First index where cumulative weight >= target.
        j = int(np.searchsorted(cw, target, side="left"))
        j = min(max(j, 0), y.size - 1)
        out[key] = float(y[j])
    return out


def time_weighted_mean(
    values: np.ndarray,
    weights_ms: np.ndarray,
) -> float:
    """Hold-weighted mean; NaN if total weight is 0."""
    y = np.asarray(values, dtype="float64")
    w = np.asarray(weights_ms, dtype="float64")
    if y.size == 0 or w.size != y.size:
        return float("nan")
    finite = np.isfinite(y) & np.isfinite(w) & (w > 0)
    if not np.any(finite):
        return float("nan")
    ww = w[finite]
    total = float(np.sum(ww))
    if total <= 0:
        return float("nan")
    return float(np.dot(y[finite], ww) / total)


def time_weighted_histogram(
    values: np.ndarray,
    weights_ms: np.ndarray,
    *,
    n_bins: int,
    lo: float | None = None,
    hi: float | None = None,
) -> tuple[np.ndarray, float, float]:
    """Accumulate hold weights into equal-width bins over ``[lo, hi]``.

    Values outside the range are clipped into the edge bins so total TW mass
    is conserved. Returns ``(mass_per_bin, lo, hi)``.
    """
    y = np.asarray(values, dtype="float64")
    w = np.asarray(weights_ms, dtype="float64")
    nb = int(n_bins)
    if nb <= 0 or y.size == 0 or w.size != y.size:
        return np.zeros(max(nb, 0), dtype="float64"), float("nan"), float("nan")
    finite = np.isfinite(y) & np.isfinite(w) & (w > 0)
    if not np.any(finite):
        return np.zeros(nb, dtype="float64"), float("nan"), float("nan")
    y = y[finite]
    w = w[finite]
    if lo is None or hi is None or not np.isfinite(lo) or not np.isfinite(hi):
        lo = float(np.min(y))
        hi = float(np.max(y))
    if hi < lo:
        lo, hi = hi, lo
    if lo == hi:
        pad = max(abs(lo) * 1e-6, 1e-6)
        lo -= pad
        hi += pad
    # Clip into [lo, hi] then digitize into nb equal-width bins.
    # Right edge inclusive for the last bin.
    width = (hi - lo) / float(nb)
    idx = np.floor((y - lo) / width).astype("int64")
    idx = np.clip(idx, 0, nb - 1)
    mass = np.zeros(nb, dtype="float64")
    np.add.at(mass, idx, w)
    return mass, float(lo), float(hi)


def inspect_equal_weight_summary(values: np.ndarray) -> dict[str, Any]:
    """Equal-weight mean + p01/p50/p95/p99 (same compact keys as TW summary).

    Used for venue-scoped latency inspect. Not time-weighted.
    """
    y = np.asarray(values, dtype="float64")
    y = y[np.isfinite(y)]
    out: dict[str, Any] = {
        "mean": None,
        "p01": None,
        "p50": None,
        "p95": None,
        "p99": None,
    }
    if y.size == 0:
        return out
    out["mean"] = round(float(np.mean(y)), 5)
    qs = np.percentile(y, [1, 50, 95, 99])
    for key, val in zip(("p01", "p50", "p95", "p99"), qs):
        out[key] = round(float(val), 5)
    return out


def inspect_tw_summary(
    values: np.ndarray,
    weights_ms: np.ndarray,
) -> dict[str, Any]:
    """TW mean + p01/p50/p95/p99 for inspect payloads (compact keys)."""
    q = time_weighted_quantiles(values, weights_ms, levels=INSPECT_RANGE_LEVELS)
    mean = time_weighted_mean(values, weights_ms)
    out: dict[str, Any] = {
        "mean": None if not np.isfinite(mean) else round(float(mean), 5),
        "p01": None,
        "p50": None,
        "p95": None,
        "p99": None,
    }
    key_map = {
        "tw_p01": "p01",
        "tw_p50": "p50",
        "tw_p95": "p95",
        "tw_p99": "p99",
    }
    for src, dst in key_map.items():
        v = q.get(src, float("nan"))
        out[dst] = None if not np.isfinite(v) else round(float(v), 5)
    return out


def hist_cdf_quantile(
    lo: float | None,
    hi: float | None,
    counts: Sequence[float] | None,
    q: float,
) -> float:
    """Approximate a quantile from an equal-width histogram CDF.

    Used when a true TW series (``tw_p05`` / ``tw_p01``) is missing from an
    already-built HTML page. Interpolates linearly inside the bin that
    crosses ``q * total_mass``. Not a substitute for ``time_weighted_quantiles``.
    """
    if lo is None or hi is None or counts is None:
        return float("nan")
    try:
        left = float(lo)
        right = float(hi)
    except (TypeError, ValueError):
        return float("nan")
    if not np.isfinite(left) or not np.isfinite(right):
        return float("nan")
    c = np.asarray(list(counts), dtype="float64")
    if c.size == 0 or not (0.0 < float(q) < 1.0):
        return float("nan")
    mass = np.where(np.isfinite(c), np.clip(c, 0.0, None), 0.0)
    total = float(mass.sum())
    if total <= 0:
        return float("nan")
    width = (right - left) / float(c.size)
    target = float(q) * total
    acc = 0.0
    for i, w in enumerate(mass):
        ww = float(w)
        if acc + ww >= target:
            frac = 0.0 if ww <= 0 else (target - acc) / ww
            return float(left + (i + frac) * width)
        acc += ww
    return float(right)


def window_hold_weights_ms(
    ts_ms: np.ndarray,
    *,
    window_end_ms: int,
) -> np.ndarray:
    """Same hold rule over a whole analysis window (last tick → window_end)."""
    return tick_hold_weights_ms(ts_ms, last_end_ms=window_end_ms)
