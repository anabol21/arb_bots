"""Time-weighted statistics.

All distributions in the anomaly-onset detector are weighted by *time* (a tick's
dwell duration), never by tick count. These helpers are the single source of
truth for that convention.

A weighted quantile ``Q_p`` of values ``v`` with non-negative weights ``w`` is
defined via the weighted empirical CDF: sort by value, accumulate weight, and
locate the smallest value whose cumulative weight fraction reaches ``p``. We use
midpoint cumulative fractions and linear interpolation so the estimate is
continuous in ``p`` and matches ``numpy.quantile`` when all weights are equal.
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "weighted_quantile",
    "weighted_median",
    "weighted_mad",
    "weighted_sigma",
]


def weighted_quantile(
    values: np.ndarray,
    weights: np.ndarray,
    q,
    *,
    already_sorted: bool = False,
):
    """Time-weighted quantile(s).

    Parameters
    ----------
    values : array of observations (e.g. spread or a metric at each tick).
    weights : non-negative dwell weights aligned to ``values``.
    q : scalar or array-like of probabilities in [0, 1].
    already_sorted : set True when ``values`` is ascending and ``weights`` is
        aligned to it (skips the internal argsort on hot paths).

    Returns
    -------
    float when ``q`` is scalar, else an ``np.ndarray`` aligned to ``q``.
    ``nan`` when there is no positive weight.
    """
    v = np.asarray(values, dtype=np.float64)
    w = np.asarray(weights, dtype=np.float64)
    scalar = np.isscalar(q)
    qs = np.atleast_1d(np.asarray(q, dtype=np.float64))

    finite = np.isfinite(v) & np.isfinite(w) & (w > 0.0)
    if not finite.any():
        out = np.full(qs.shape, np.nan)
        return float(out[0]) if scalar else out
    v = v[finite]
    w = w[finite]

    if not already_sorted:
        order = np.argsort(v, kind="stable")
        v = v[order]
        w = w[order]

    cw = np.cumsum(w)
    total = cw[-1]
    # Midpoint cumulative fraction of each sample (Hazen-style plotting position).
    p = (cw - 0.5 * w) / total
    out = np.interp(qs, p, v, left=v[0], right=v[-1])
    return float(out[0]) if scalar else out


def weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    """Time-weighted median (== ``weighted_quantile(.., 0.5)``)."""
    return weighted_quantile(values, weights, 0.5)


def weighted_mad(values: np.ndarray, weights: np.ndarray, *, center=None) -> float:
    """Time-weighted median absolute deviation about the weighted median."""
    v = np.asarray(values, dtype=np.float64)
    w = np.asarray(weights, dtype=np.float64)
    c = weighted_median(v, w) if center is None else float(center)
    if not np.isfinite(c):
        return np.nan
    return weighted_quantile(np.abs(v - c), w, 0.5)


def weighted_sigma(values: np.ndarray, weights: np.ndarray, *, center=None) -> float:
    """Robust scale ``1.4826 * wMAD`` (normal-consistent)."""
    mad = weighted_mad(values, weights, center=center)
    return 1.4826 * mad if np.isfinite(mad) else np.nan
