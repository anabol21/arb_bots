"""Time-weighted quantiles for piecewise-constant L1 series."""

from __future__ import annotations

from typing import NamedTuple, Sequence

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
    name_map = dict(TW_NAME_MAP)
    out = {
        name_map.get(float(q), f"tw_p{int(round(100 * float(q)))}"): float("nan")
        for q in levels
    }
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
