"""Time-weighted quantiles / hist helpers for piecewise-constant L1 series."""

from __future__ import annotations

from typing import Any, NamedTuple, Sequence

import numpy as np

# Default quantiles plotted as series on each spread block.
TW_QUANTILE_LEVELS: tuple[float, ...] = (0.25, 0.50, 0.95, 0.99)
TW_QUANTILE_NAMES: tuple[str, ...] = ("tw_p25", "tw_p50", "tw_p95", "tw_p99")

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


def window_hold_weights_ms(
    ts_ms: np.ndarray,
    *,
    window_end_ms: int,
) -> np.ndarray:
    """Same hold rule over a whole analysis window (last tick → window_end)."""
    return tick_hold_weights_ms(ts_ms, last_end_ms=window_end_ms)


# Locked gear-2.2 "current spread" p50 windows (docs/gear22-state-p50.md).
WINDOW_1M_MS = 60_000
WINDOW_5M_MS = 300_000
# NaN unless TW mass covers this fraction of W and enough positive-hold ticks.
ROLL_P50_MIN_MASS_FRAC = 0.20
ROLL_P50_MIN_TICKS = 2


def tw_p50(values: np.ndarray, weights_ms: np.ndarray) -> float:
    """Time-weighted median; same CDF rule as ``time_weighted_quantiles`` p50."""
    return float(time_weighted_quantiles(values, weights_ms, levels=(0.50,))["tw_p50"])


class RollingTwWindowStats(NamedTuple):
    """One-pass rolling TW diagnostics on ``[t - W, t]``.

    ``p50`` follows the locked NaN rule (min ticks / min mass). ``cov`` is
    always ``mass / W`` (0 when the window is empty). ``occ`` is NaN when
    there is no positive hold mass or the threshold is non-finite.
    """

    p50: np.ndarray
    cov: np.ndarray
    n_ticks: np.ndarray
    occ: np.ndarray


def rolling_tw_window_stats(
    ts_ms: np.ndarray,
    values: np.ndarray,
    *,
    window_ms: int,
    eval_ts_ms: np.ndarray | None = None,
    min_mass_frac: float = ROLL_P50_MIN_MASS_FRAC,
    min_ticks: int = ROLL_P50_MIN_TICKS,
    occ_threshold: np.ndarray | None = None,
) -> RollingTwWindowStats:
    """Causal rolling TW-p50 plus coverage / occupancy on ``[t - W, t]``.

    Same window, hold→next, and p50 NaN rule as ``rolling_tw_p50``. Extra
    outputs (same length as ``eval_ts_ms``):

    - ``cov``: positive-hold mass / ``window_ms`` (0 if empty, never filled);
    - ``n_ticks``: raw tick count in ``[t-W, t]`` (int32);
    - ``occ``: hold mass with ``y > occ_threshold`` / ``window_ms``.
      NaN if mass is 0 or the threshold is non-finite. ``occ_threshold`` is
      aligned with ``eval_ts_ms`` (one value per eval). Omit to get all-NaN
      occupancy.
    """
    ts = np.asarray(ts_ms, dtype="int64")
    y = np.asarray(values, dtype="float64")
    if ts.size != y.size:
        raise ValueError("ts_ms and values must have the same length")
    w_ms = int(window_ms)
    if w_ms <= 0:
        raise ValueError("window_ms must be positive")
    n = int(ts.size)

    if eval_ts_ms is None:
        evals_in_raw = None
    else:
        evals_in_raw = np.asarray(eval_ts_ms, dtype="int64")

    if n == 0:
        if evals_in_raw is None:
            empty = np.asarray([], dtype="float64")
            return RollingTwWindowStats(
                empty,
                np.asarray([], dtype="float64"),
                np.asarray([], dtype="int32"),
                np.asarray([], dtype="float64"),
            )
        n_eval = int(evals_in_raw.size)
        return RollingTwWindowStats(
            np.full(n_eval, np.nan, dtype="float64"),
            np.zeros(n_eval, dtype="float64"),
            np.zeros(n_eval, dtype="int32"),
            np.full(n_eval, np.nan, dtype="float64"),
        )

    tick_order = np.argsort(ts, kind="mergesort")
    ts = ts[tick_order]
    y = y[tick_order]

    if evals_in_raw is None:
        evals_in = ts
        eval_order = None
        evals = ts
        thr_sorted = (
            None
            if occ_threshold is None
            else np.asarray(occ_threshold, dtype="float64")[tick_order]
        )
    else:
        evals_in = evals_in_raw
        eval_order = np.argsort(evals_in, kind="mergesort")
        evals = evals_in[eval_order]
        thr_sorted = (
            None
            if occ_threshold is None
            else np.asarray(occ_threshold, dtype="float64")[eval_order]
        )

    n_eval = int(evals.size)
    p50_sorted = np.full(n_eval, np.nan, dtype="float64")
    cov_sorted = np.zeros(n_eval, dtype="float64")
    ntick_sorted = np.zeros(n_eval, dtype="int32")
    occ_sorted = np.full(n_eval, np.nan, dtype="float64")
    if n_eval == 0:
        return RollingTwWindowStats(p50_sorted, cov_sorted, ntick_sorted, occ_sorted)
    min_mass = float(min_mass_frac) * float(w_ms)
    min_k = int(min_ticks)
    left = 0
    right = 0
    for i, t_raw in enumerate(evals):
        t = int(t_raw)
        lo = t - w_ms
        while left < n and int(ts[left]) < lo:
            left += 1
        while right < n and int(ts[right]) <= t:
            right += 1
        k = right - left
        ntick_sorted[i] = k
        if k <= 0:
            continue
        sl = slice(left, right)
        weights = tick_hold_weights_ms(ts[sl], last_end_ms=t)
        y_sl = y[sl]
        finite = np.isfinite(y_sl) & np.isfinite(weights) & (weights > 0)
        n_fin = int(np.count_nonzero(finite))
        if n_fin <= 0:
            continue
        mass = float(weights[finite].sum())
        cov_sorted[i] = mass / float(w_ms)
        if thr_sorted is not None:
            thr = float(thr_sorted[i])
            if np.isfinite(thr) and mass > 0:
                above = finite & (y_sl > thr)
                occ_sorted[i] = float(weights[above].sum()) / float(w_ms)
        if n_fin < min_k or mass < min_mass:
            continue
        p50_sorted[i] = tw_p50(y_sl, weights)

    return _unsort_window_stats(
        p50_sorted, cov_sorted, ntick_sorted, occ_sorted, eval_order
    )


def _unsort_window_stats(
    p50_sorted: np.ndarray,
    cov_sorted: np.ndarray,
    ntick_sorted: np.ndarray,
    occ_sorted: np.ndarray,
    eval_order: np.ndarray | None,
) -> RollingTwWindowStats:
    if eval_order is None:
        return RollingTwWindowStats(p50_sorted, cov_sorted, ntick_sorted, occ_sorted)
    p50 = np.empty_like(p50_sorted)
    cov = np.empty_like(cov_sorted)
    ntick = np.empty_like(ntick_sorted)
    occ = np.empty_like(occ_sorted)
    p50[eval_order] = p50_sorted
    cov[eval_order] = cov_sorted
    ntick[eval_order] = ntick_sorted
    occ[eval_order] = occ_sorted
    return RollingTwWindowStats(p50, cov, ntick, occ)


def rolling_tw_p50(
    ts_ms: np.ndarray,
    values: np.ndarray,
    *,
    window_ms: int,
    eval_ts_ms: np.ndarray | None = None,
    min_mass_frac: float = ROLL_P50_MIN_MASS_FRAC,
    min_ticks: int = ROLL_P50_MIN_TICKS,
) -> np.ndarray:
    """Causal rolling TW-p50 on ``[t - window_ms, t]``.

    For each evaluation time ``t``:

    - ticks with ``ts`` in ``[t - W, t]`` (inclusive), ``ts ≤ t`` (causal);
    - hold→next weights; last tick holds to ``t`` (not to a UTC bar close);
    - leading gap before the first in-window tick is unobserved (no mass);
    - holes are **not** linearly interpolated; a long inter-tick gap still
      assigns hold mass to the previous tick (same as closed-bar ``tw_p50``);
    - NaN when finite ticks with positive hold < ``min_ticks`` **or**
      total positive hold mass < ``min_mass_frac * W``.

    ``eval_ts_ms`` defaults to each tick timestamp. For HTML / batch jobs a
    1s grid is allowed; that is a cadence choice, not a 5m-close substitute.
    """
    return rolling_tw_window_stats(
        ts_ms,
        values,
        window_ms=window_ms,
        eval_ts_ms=eval_ts_ms,
        min_mass_frac=min_mass_frac,
        min_ticks=min_ticks,
    ).p50


def eval_grid_ms(start_ms: int, end_ms: int, step_ms: int) -> np.ndarray:
    """Half-open UTC grid ``[start_ms, end_ms)`` with step ``step_ms``."""
    start = int(start_ms)
    end = int(end_ms)
    step = int(step_ms)
    if step <= 0:
        raise ValueError("step_ms must be positive")
    if end <= start:
        return np.asarray([], dtype="int64")
    return np.arange(start, end, step, dtype="int64")
