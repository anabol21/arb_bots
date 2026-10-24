"""Post-hoc funding on a gear-2.2 round trip. Does not change decide().

Long spread is short Bybit and long OKX. A positive settlement rate means
the long leg pays and the short leg receives. Cash is the sum of each
venue's own prints, in the same percentage points as ``potential_pp``
(``rate_pct`` = fraction × 100). Notionals are assumed equal, so the
print is the rate itself, not mark × size.

A print at second ``t`` belongs to a hold when ``ts_open < t <= ts_close``.
The open second is excluded. The close second is included.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class VenueSeries:
    """Settlement seconds and rate_pct, sorted by second, one coin, one venue."""

    ts_s: np.ndarray
    rate_pct: np.ndarray


@dataclass(frozen=True)
class FundingBook:
    bybit: dict[str, VenueSeries]
    okx: dict[str, VenueSeries]


@dataclass(frozen=True)
class FundingResult:
    funding_pp: float | None
    n_bybit: int
    n_okx: int
    missing: bool


def sum_rates(series: VenueSeries, ts_open: int, ts_close: int) -> tuple[float, int]:
    """Sum rate_pct for prints with ts_open < t <= ts_close."""
    if ts_close < ts_open:
        raise ValueError(f"ts_close {ts_close} < ts_open {ts_open}")
    lo = int(np.searchsorted(series.ts_s, np.int64(ts_open), side="right"))
    hi = int(np.searchsorted(series.ts_s, np.int64(ts_close), side="right"))
    return float(series.rate_pct[lo:hi].sum()), int(hi - lo)


def funding_for_hold(
    book: FundingBook,
    coin: str,
    side: str,
    ts_open: int,
    ts_close: int,
) -> FundingResult:
    """Signed funding in percentage points, or missing if either venue has no series."""
    bybit = book.bybit.get(coin)
    okx = book.okx.get(coin)
    if bybit is None or okx is None:
        return FundingResult(funding_pp=None, n_bybit=0, n_okx=0, missing=True)
    bybit_pp, n_bybit = sum_rates(bybit, ts_open, ts_close)
    okx_pp, n_okx = sum_rates(okx, ts_open, ts_close)
    if side == "long":
        net = bybit_pp - okx_pp
    elif side == "short":
        net = -bybit_pp + okx_pp
    else:
        raise ValueError(f"unknown side {side!r}")
    return FundingResult(
        funding_pp=net,
        n_bybit=n_bybit,
        n_okx=n_okx,
        missing=False,
    )
