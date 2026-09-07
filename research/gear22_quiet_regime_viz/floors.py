"""Causal SMA-12 floor estimators for gear 2.2 quiet-regime HTML pages.

Inner path (already on the candle row)::

    s_t = SMA_12(close)_t     # W1 = 12 × 5m bars; existing causal_sma

Chosen floor (one line on the corridor panel)::

    tf_select_25 = min(trim_3h_α25, trim_12h_α25) of s

Holes: non-finite ``s`` are excluded from the window sample (no interpolation).
Warm-up / sparse window: NaN when finite count < max(5, ceil(0.20 × W2)).
"""

from __future__ import annotations

import math
from typing import Callable, Mapping

import numpy as np

# W1 is the existing causal SMA on 5m closes (do not change candle-row SMA-12).
W1_BARS = 12

# W2 memory in 5m bars.
W2_HOURS: tuple[int, ...] = (3, 6, 12)
W2_BARS: tuple[int, ...] = (36, 72, 144)
W2_HOUR_BY_BARS: dict[int, int] = {36: 3, 72: 6, 144: 12}

TRIM_ALPHA = 0.10  # 10% each tail (default / existing trim10)
TRIM_ALPHA_25 = 0.25  # analogue traces on the same panel

# Same window → same color on median / mean / trim10 graphs.
W2_COLORS: dict[int, str] = {
    36: "#1f77b4",  # 3h
    72: "#2ca02c",  # 6h
    144: "#d62728",  # 12h
}

FLOOR_ESTIMATORS: tuple[str, ...] = ("median", "mean", "trim10")

# Chosen floor: tf-select on 25% trimmed means of SMA-12 (3h vs 12h).
W2_TRIM3_BARS = 36
W2_TRIM12_BARS = 144
TF_SELECT_NAME = "tf-select"
TRIM3_NAME = "trim 3h"
TRIM12_NAME = "trim 12h"
SMA12_NAME = "SMA-12"
SMA3_NAME = "SMA-3"
TRIM3_25_NAME = "trim 3h α25"
TRIM12_25_NAME = "trim 12h α25"
TF_SELECT_25_NAME = "tf-select α25"
MED3_NAME = "median 3h"
MED12_NAME = "median 12h"
TF_SELECT_MED_NAME = "tf-select med"
TF_SELECT_COLOR = "#9467bd"
SMA12_COLOR = "#ff7f0e"
TRIM3_COLOR = "#1f77b4"
TRIM12_COLOR = "#d62728"

# Single-panel styles (corridor display SMA-3 + chosen floor from SMA-12).
FLOOR_PANEL_STYLES: dict[str, dict[str, float | str]] = {
    SMA3_NAME: dict(width=1.7, color=SMA12_COLOR),
    TF_SELECT_25_NAME: dict(width=2.6, color=TF_SELECT_COLOR),
}

INNER_BAND_FILL = "rgba(140, 170, 200, 0.28)"
INNER_BAND_EDGE = "rgba(110, 140, 170, 0.55)"
OUTER_BAND_EDGE = "rgba(70, 80, 100, 0.72)"

# Kept for unit tests of the α=10%/25% and median selectors (not plotted).
TF_COMPARE_STYLES: dict[str, dict[str, float | str]] = {
    SMA12_NAME: dict(width=1.5, color=SMA12_COLOR),
    TRIM3_NAME: dict(width=1.6, color=TRIM3_COLOR),
    TRIM12_NAME: dict(width=1.6, color=TRIM12_COLOR),
    TF_SELECT_NAME: dict(width=2.4, color=TF_SELECT_COLOR),
    TRIM3_25_NAME: dict(width=1.2, color=TRIM3_COLOR, dash="dash"),
    TRIM12_25_NAME: dict(width=1.2, color=TRIM12_COLOR, dash="dash"),
    TF_SELECT_25_NAME: dict(width=1.8, color=TF_SELECT_COLOR, dash="dash"),
}

TF_COMPARE_NAMES: tuple[str, ...] = (
    SMA12_NAME,
    TRIM3_NAME,
    TRIM12_NAME,
    TF_SELECT_NAME,
    TRIM3_25_NAME,
    TRIM12_25_NAME,
    TF_SELECT_25_NAME,
)

MED_COMPARE_STYLES: dict[str, dict[str, float | str]] = {
    SMA12_NAME: dict(width=1.5, color=SMA12_COLOR),
    MED3_NAME: dict(width=1.6, color=TRIM3_COLOR),
    MED12_NAME: dict(width=1.6, color=TRIM12_COLOR),
    TF_SELECT_MED_NAME: dict(width=2.4, color=TF_SELECT_COLOR),
}

MED_COMPARE_NAMES: tuple[str, ...] = (
    SMA12_NAME,
    MED3_NAME,
    MED12_NAME,
    TF_SELECT_MED_NAME,
)


def min_finite_count(window: int) -> int:
    """Minimum finite ``s`` samples required to emit a floor value."""
    w = int(window)
    if w <= 0:
        return 1
    return max(5, int(math.ceil(0.20 * w)))


def trim_mean_alpha(values: np.ndarray, alpha: float = TRIM_ALPHA) -> float:
    """Trimmed mean: drop ``alpha`` from each tail (scipy when present).

    Matches ``scipy.stats.trim_mean``: ``k = int(n * alpha)`` cut from each
    end after an optional sort/partition. Empty → NaN. If ``k`` would empty
    the middle, returns NaN rather than raising.
    """
    a = np.asarray(values, dtype="float64")
    a = a[np.isfinite(a)]
    n = int(a.size)
    if n == 0:
        return float("nan")
    prop = float(alpha)
    if prop < 0.0 or prop >= 0.5:
        return float("nan")
    try:
        from scipy.stats import trim_mean

        return float(trim_mean(a, prop))
    except ImportError:
        return float(_trim_mean_impl(a, prop))


def _trim_mean_impl(values: np.ndarray, alpha: float) -> float:
    """Scipy-compatible trimmed mean (no scipy). ``values`` already finite."""
    a = np.asarray(values, dtype="float64")
    n = int(a.size)
    if n == 0:
        return float("nan")
    lowercut = int(n * float(alpha))
    uppercut = n - lowercut
    if lowercut >= uppercut:
        return float("nan")
    part = np.partition(a, (lowercut, uppercut - 1))
    return float(np.mean(part[lowercut:uppercut]))


def _stat_median(finite: np.ndarray) -> float:
    return float(np.median(finite))


def _stat_mean(finite: np.ndarray) -> float:
    return float(np.mean(finite))


def _stat_trim10(finite: np.ndarray) -> float:
    return trim_mean_alpha(finite, TRIM_ALPHA)


_ESTIMATOR_FN: Mapping[str, Callable[[np.ndarray], float]] = {
    "median": _stat_median,
    "mean": _stat_mean,
    "trim10": _stat_trim10,
}


def causal_floor(
    s: np.ndarray,
    window: int,
    *,
    estimator: str = "median",
    min_finite: int | None = None,
    alpha: float | None = None,
) -> np.ndarray:
    """Causal floor of ``s`` over the last ``window`` bars.

    Uses only finite values in ``{s}_{t-window+1..t}``. Does not look at
    ``s[t+1:]``. Partial windows at the start are allowed once ``min_finite``
    finite points exist. Empty/NaN bars are skipped; they do not interpolate.

    ``estimator="trim10"`` (or ``"trim"``) uses ``trim_mean_alpha``. Default
    ``alpha`` is 0.10 so existing trim10 callers stay unchanged. Pass
    ``alpha=0.25`` for the 25% each-tail analogue.
    """
    x = np.asarray(s, dtype="float64")
    out = np.full(x.shape, np.nan, dtype="float64")
    w = int(window)
    if w <= 0 or x.size == 0:
        return out
    key = str(estimator)
    if key in ("trim10", "trim"):
        prop = TRIM_ALPHA if alpha is None else float(alpha)
        fn: Callable[[np.ndarray], float] = lambda finite, p=prop: trim_mean_alpha(
            finite, p
        )
    elif key not in _ESTIMATOR_FN:
        raise ValueError(f"unknown floor estimator {estimator!r}")
    else:
        fn = _ESTIMATOR_FN[key]
    need = int(min_finite) if min_finite is not None else min_finite_count(w)
    if need <= 0:
        need = 1
    for t in range(x.size):
        start = t - w + 1
        if start < 0:
            start = 0
        sl = x[start : t + 1]
        finite = sl[np.isfinite(sl)]
        if finite.size < need:
            continue
        out[t] = fn(finite)
    return out


def causal_trim_floor(
    s: np.ndarray,
    window: int,
    *,
    alpha: float,
    min_finite: int | None = None,
) -> np.ndarray:
    """Causal trimmed-mean floor; ``alpha`` is the each-tail fraction."""
    return causal_floor(
        s, window, estimator="trim", min_finite=min_finite, alpha=alpha
    )


def tf_select_floor(trim_short: np.ndarray, trim_long: np.ndarray) -> np.ndarray:
    """Sign-of-divergence selector: ``min(trim_3h, trim_12h)`` where both finite.

    - short TF above long TF (spike / upward wander) → hold the 12h trim
    - short TF below long TF (return toward a quieter level) → follow the 3h trim
    - either side NaN → NaN (no interpolation)
    """
    a = np.asarray(trim_short, dtype="float64")
    b = np.asarray(trim_long, dtype="float64")
    if a.shape != b.shape:
        raise ValueError(
            f"tf_select_floor length mismatch short={a.shape} long={b.shape}"
        )
    out = np.full(a.shape, np.nan, dtype="float64")
    ok = np.isfinite(a) & np.isfinite(b)
    out[ok] = np.minimum(a[ok], b[ok])
    return out


def compute_chosen_floor(
    sma12: np.ndarray,
) -> dict[str, np.ndarray]:
    """SMA-12 plus the chosen floor: tf-select at α=25%.

    ``tf_select_25 = min(trim_3h_α25, trim_12h_α25)``. The 3h/12h trim
    series are computed but not returned — they are not plotted.
    """
    s = np.asarray(sma12, dtype="float64")
    trim3_25 = causal_trim_floor(s, W2_TRIM3_BARS, alpha=TRIM_ALPHA_25)
    trim12_25 = causal_trim_floor(s, W2_TRIM12_BARS, alpha=TRIM_ALPHA_25)
    return {
        SMA12_NAME: s,
        TF_SELECT_25_NAME: tf_select_floor(trim3_25, trim12_25),
    }


def compute_tf_compare(
    sma12: np.ndarray,
) -> dict[str, np.ndarray]:
    """SMA-12 plus trim 3h / 12h / tf-select at α=10% and α=25%."""
    s = np.asarray(sma12, dtype="float64")
    trim3 = causal_trim_floor(s, W2_TRIM3_BARS, alpha=TRIM_ALPHA)
    trim12 = causal_trim_floor(s, W2_TRIM12_BARS, alpha=TRIM_ALPHA)
    trim3_25 = causal_trim_floor(s, W2_TRIM3_BARS, alpha=TRIM_ALPHA_25)
    trim12_25 = causal_trim_floor(s, W2_TRIM12_BARS, alpha=TRIM_ALPHA_25)
    return {
        SMA12_NAME: s,
        TRIM3_NAME: trim3,
        TRIM12_NAME: trim12,
        TF_SELECT_NAME: tf_select_floor(trim3, trim12),
        TRIM3_25_NAME: trim3_25,
        TRIM12_25_NAME: trim12_25,
        TF_SELECT_25_NAME: tf_select_floor(trim3_25, trim12_25),
    }


def compute_median_compare(
    sma12: np.ndarray,
) -> dict[str, np.ndarray]:
    """SMA-12 plus median 3h / 12h / tf-select med (no α)."""
    s = np.asarray(sma12, dtype="float64")
    med3 = causal_floor(s, W2_TRIM3_BARS, estimator="median")
    med12 = causal_floor(s, W2_TRIM12_BARS, estimator="median")
    return {
        SMA12_NAME: s,
        MED3_NAME: med3,
        MED12_NAME: med12,
        TF_SELECT_MED_NAME: tf_select_floor(med3, med12),
    }


def compute_floor_bundle(
    sma12: np.ndarray,
    *,
    windows: tuple[int, ...] = W2_BARS,
) -> dict[str, dict[int, np.ndarray]]:
    """All estimators × W2 windows for one side's SMA-12 series."""
    s = np.asarray(sma12, dtype="float64")
    bundle: dict[str, dict[int, np.ndarray]] = {name: {} for name in FLOOR_ESTIMATORS}
    for w in windows:
        ww = int(w)
        for name in FLOOR_ESTIMATORS:
            bundle[name][ww] = causal_floor(s, ww, estimator=name)
    return bundle


def legend_name(estimator: str, window_bars: int) -> str:
    hours = W2_HOUR_BY_BARS.get(int(window_bars))
    if hours is None:
        return f"{estimator} {int(window_bars)}×5m"
    return f"{estimator} {hours}h"
