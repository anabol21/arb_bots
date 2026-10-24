"""Experiment 3. Local shifts of the frozen trade clock.

Each trade keeps its coin, side, and duration. The open second moves by
an integer δ drawn uniformly from the feasible part of [-R, +R]. If that
interval meets the edge of the coin's funding history, the draw is uniform
on the seconds that remain. Deltas are not clipped onto the boundary.

Overlaps are allowed. Spread PnL is not recomputed. Funding uses the
experiment-1 hold rule.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from research.gear23_funding.exp1_funding import FundingBook, funding_for_hold

EXP3_SEED = 20260810
RADII_H = (72, 24, 6, 1)


@dataclass(frozen=True)
class CoinWindow:
    """Inclusive seconds a hold may occupy. Both venues have prints on this span."""

    open_lo: int
    close_hi: int


def local_support(ts_open: int, radius_s: int, open_lo: int, open_hi: int) -> tuple[int, int]:
    """Inclusive open seconds: ``[ts_open - R, ts_open + R]`` cut by history.

    ``open_lo`` and ``open_hi`` are already the opens that keep the whole
    hold inside the coin's funding history. The result is the intersection,
    not a clipped draw.
    """
    if radius_s < 0:
        raise ValueError(f"negative radius {radius_s}")
    if open_lo > open_hi:
        raise ValueError(f"history allows no open in [{open_lo}, {open_hi}]")
    lo = max(int(ts_open) - int(radius_s), int(open_lo))
    hi = min(int(ts_open) + int(radius_s), int(open_hi))
    if lo > hi:
        raise ValueError(
            f"empty local support around {ts_open} radius {radius_s} "
            f"inside [{open_lo}, {open_hi}]"
        )
    return lo, hi


def sample_local_opens(
    ts_open: np.ndarray,
    radius_s: int,
    open_lo: np.ndarray,
    open_hi: np.ndarray,
    rng: np.random.Generator,
) -> np.ndarray:
    """One local open per trade. Independent across trades."""
    opens = np.empty(ts_open.shape[0], dtype=np.int64)
    for i in range(ts_open.shape[0]):
        lo, hi = local_support(int(ts_open[i]), radius_s, int(open_lo[i]), int(open_hi[i]))
        opens[i] = int(rng.integers(lo, hi + 1))
    return opens


def _edge_limit(print_ts: int, max_gap_s: int, horizon: int, *, at_start: bool) -> int:
    """Use the scanned horizon when the series is still running into it.

    A quiet tail shorter than the series' own maximum gap contains no
    hidden print. A series that started or stopped farther away ends at
    its own first or last print.
    """
    if max_gap_s < 0:
        raise ValueError(f"negative gap {max_gap_s}")
    gap = horizon - print_ts if at_start is False else print_ts - horizon
    if gap < 0:
        raise ValueError(f"print {print_ts} is outside horizon {horizon}")
    if gap <= max_gap_s:
        return horizon
    return print_ts


def history_bounds(
    book: FundingBook,
    coin: str,
    horizon_lo: int,
    horizon_hi: int,
) -> CoinWindow:
    """Seconds a hold may occupy: both venues, including a known empty tail."""
    bybit = book.bybit.get(coin)
    okx = book.okx.get(coin)
    if bybit is None or okx is None or bybit.ts_s.size == 0 or okx.ts_s.size == 0:
        raise ValueError(f"no funding series for {coin}")
    limits = []
    for series in (bybit, okx):
        gaps = np.diff(series.ts_s.astype(np.int64))
        max_gap = int(gaps.max()) if gaps.size else 0
        limits.append(
            (
                _edge_limit(int(series.ts_s[0]), max_gap, horizon_lo, at_start=True),
                _edge_limit(int(series.ts_s[-1]), max_gap, horizon_hi, at_start=False),
            )
        )
    return CoinWindow(
        open_lo=max(item[0] for item in limits),
        close_hi=min(item[1] for item in limits),
    )


def event_key(book: FundingBook, coin: str, ts_open: int, ts_close: int) -> tuple[int, int, int, int]:
    """Index span of included prints. Same key means the same settlement set."""
    bybit = book.bybit[coin].ts_s
    okx = book.okx[coin].ts_s
    return (
        int(np.searchsorted(bybit, np.int64(ts_open), side="right")),
        int(np.searchsorted(bybit, np.int64(ts_close), side="right")),
        int(np.searchsorted(okx, np.int64(ts_open), side="right")),
        int(np.searchsorted(okx, np.int64(ts_close), side="right")),
    )


def _span_varies(ts: np.ndarray, duration: int, open_lo: int, open_hi: int) -> bool:
    """True when some print is inside some opens in the support and outside others."""
    if ts.size == 0 or open_lo == open_hi:
        return False
    full = open_hi - open_lo + 1
    left = int(np.searchsorted(ts, np.int64(open_lo + 1), side="left"))
    right = int(np.searchsorted(ts, np.int64(open_hi + duration), side="right"))
    for raw in ts[left:right]:
        t = int(raw)
        a = max(t - duration, open_lo)
        b = min(t - 1, open_hi)
        if a > b:
            continue
        if b - a + 1 != full:
            return True
    return False


def event_set_constant(
    book: FundingBook,
    coin: str,
    duration: int,
    open_lo: int,
    open_hi: int,
) -> bool:
    """True when every open in ``[open_lo, open_hi]`` meets the same prints."""
    if open_lo > open_hi:
        raise ValueError("empty support")
    return not _span_varies(book.bybit[coin].ts_s, duration, open_lo, open_hi) and not _span_varies(
        book.okx[coin].ts_s, duration, open_lo, open_hi
    )


def funding_for_opens(
    book: FundingBook,
    coin: str,
    side: str,
    duration: int,
    opens: np.ndarray,
) -> np.ndarray:
    """Funding for many opens of one hold. Same prints and sign as ``funding_for_hold``."""
    if side not in ("long", "short"):
        raise ValueError(f"unknown side {side!r}")
    bybit = book.bybit[coin]
    okx = book.okx[coin]
    bybit_pp = _slice_sums(bybit.ts_s, bybit.rate_pct, opens, duration)
    okx_pp = _slice_sums(okx.ts_s, okx.rate_pct, opens, duration)
    if side == "long":
        return bybit_pp - okx_pp
    return -bybit_pp + okx_pp


def _slice_sums(
    ts: np.ndarray,
    rates: np.ndarray,
    opens: np.ndarray,
    duration: int,
) -> np.ndarray:
    """Sum of ``rate_pct`` on ``open < t <= open + duration`` for each open.

    The prefix difference matches a short sequential slice sum closely
    enough for rate data. Callers that need the scalar rule exactly should
    still check ``funding_for_hold`` on the observed opens.
    """
    lo = np.searchsorted(ts, opens, side="right")
    hi = np.searchsorted(ts, opens + np.int64(duration), side="right")
    csum = np.empty(rates.shape[0] + 1, dtype=np.float64)
    csum[0] = 0.0
    if rates.size:
        csum[1:] = np.cumsum(rates, dtype=np.float64)
    return csum[hi] - csum[lo]


def funding_matches_hold(
    book: FundingBook,
    coin: str,
    side: str,
    duration: int,
    opens: np.ndarray,
    values: np.ndarray,
    atol: float = 1e-9,
) -> None:
    """Raise if the vector path disagrees with the scalar hold rule."""
    for open_ts, value in zip(opens, values):
        got = funding_for_hold(book, coin, side, int(open_ts), int(open_ts) + duration)
        if got.missing or got.funding_pp is None:
            raise RuntimeError(f"missing series for {coin}")
        if abs(float(value) - float(got.funding_pp)) > atol:
            raise RuntimeError(
                f"{coin} open {int(open_ts)} vector {value} != hold {got.funding_pp}"
            )


def local_summary(samples: np.ndarray, actual: float) -> dict:
    """Sample quantiles and tail share. The tail share is not a p-value."""
    m = int(samples.shape[0])
    count = int(np.count_nonzero(samples <= actual))
    exact = int(np.count_nonzero(np.abs(samples - actual) <= 1e-9))
    quantiles = np.quantile(samples, [0.025, 0.05, 0.5, 0.95, 0.975], method="linear")
    median = float(np.median(samples))
    return {
        "m": m,
        "mean": float(np.mean(samples)),
        "median": median,
        "std": float(np.std(samples, ddof=1)) if m > 1 else 0.0,
        "q025": float(quantiles[0]),
        "q05": float(quantiles[1]),
        "q50": float(quantiles[2]),
        "q95": float(quantiles[3]),
        "q975": float(quantiles[4]),
        "min": float(samples.min()),
        "max": float(samples.max()),
        "actual": float(actual),
        "delta_actual_minus_median": float(actual) - median,
        "n_le_actual": count,
        "share_le_actual": count / m,
        "share_negative": float(np.mean(samples < 0.0)),
        "n_exact": exact,
        "share_exact": exact / m,
    }


def radius_streams(seed: int = EXP3_SEED) -> dict[int, np.random.Generator]:
    """One independent stream per radius, in the order 72, 24, 6, 1 hour."""
    children = np.random.SeedSequence(seed).spawn(len(RADII_H))
    return {
        hours: np.random.Generator(np.random.PCG64(child))
        for hours, child in zip(RADII_H, children)
    }
