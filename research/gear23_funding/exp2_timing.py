"""Experiment 2. Funding of a fixed trade list under random clocks.

The trade list is frozen: coin, side, and duration stay put. Only the
open second moves. Spread PnL is not recomputed.

Mode A draws each open independently and uniformly inside the window.
Overlaps are allowed, so one settlement can land in several holds.

Mode B draws a schedule uniformly from the integer-second placements of
all trades into ``[t0, t1]`` with no interior overlap. Adjacent trades
may share the boundary second (close of one equals open of the next):
the funding rule then assigns that second to the trade that closes.

The uniform draw is exact, not a greedy packing and not MCMC.

1. Pick a trade order uniformly.
2. Let slack ``S = (t1 - t0) - sum(durations)`` seconds.
3. Split ``S`` into ``n + 1`` non-negative integer gaps uniformly
   (stars and bars). The gaps sit before the first trade, between
   trades, and after the last.
4. Place the ordered trades with those gaps.

Every order has the same number of gap tuples, and every feasible
placement has one order, so the result is uniform on the feasible set.
Every draw is inside the window and non-overlapping. Nothing is rejected.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from research.gear23_funding.exp1_funding import FundingBook, funding_for_hold

EXP2_SEED = 20260810


@dataclass(frozen=True)
class TradeClock:
    trade_id: int
    coin: str
    side: str
    duration_s: int
    ts_open: int
    ts_close: int
    funding_pp: float


def assert_trade_invariants(trades: list[TradeClock], t0: int, t1: int) -> None:
    """The observed list is a feasible K=1 schedule inside the window."""
    if not trades:
        raise ValueError("empty trade list")
    coins = [t.coin for t in trades]
    if "ICX" in coins:
        raise ValueError("ICX is in the trade list")
    ids = [t.trade_id for t in trades]
    if ids != list(range(len(trades))):
        raise ValueError("trade ids are not 0..n-1 in order")
    for trade in trades:
        if trade.side not in ("long", "short"):
            raise ValueError(f"bad side {trade.side}")
        if trade.duration_s <= 0:
            raise ValueError(f"non-positive duration on {trade.coin}")
        if trade.ts_close - trade.ts_open != trade.duration_s:
            raise ValueError(f"duration mismatch on trade {trade.trade_id}")
        if trade.ts_open < t0 or trade.ts_close > t1:
            raise ValueError(f"trade {trade.trade_id} leaves the window")
    opens = np.array([t.ts_open for t in trades], dtype=np.int64)
    durations = np.array([t.duration_s for t in trades], dtype=np.int64)
    if overlaps(opens, durations):
        raise ValueError("observed trades overlap")


def overlaps(opens: np.ndarray, durations: np.ndarray) -> bool:
    """True when two holds share an interior second. A shared boundary is allowed."""
    order = np.argsort(opens, kind="mergesort")
    start = opens[order]
    end = start + durations[order]
    if start.shape[0] < 2:
        return False
    return bool(np.any(end[:-1] > start[1:]))


def sample_gaps(total: int, k: int, rng: np.random.Generator) -> np.ndarray:
    """Uniform non-negative integers ``x_0 + ... + x_{k-1} = total``."""
    if k <= 0 or total < 0:
        raise ValueError(f"bad composition total={total} k={k}")
    if k == 1:
        return np.array([total], dtype=np.int64)
    population = total + k - 1
    cuts = np.sort(_distinct_ints(population, k - 1, rng))
    gaps = np.empty(k, dtype=np.int64)
    prev = -1
    for i, cut in enumerate(cuts):
        gaps[i] = int(cut) - prev - 1
        prev = int(cut)
    gaps[-1] = population - prev - 1
    return gaps


def _distinct_ints(population: int, size: int, rng: np.random.Generator) -> np.ndarray:
    """Uniform sample of ``size`` distinct values in ``0 .. population-1``."""
    if size > population:
        raise ValueError(f"cannot draw {size} from {population}")
    chosen: set[int] = set()
    while len(chosen) < size:
        chosen.add(int(rng.integers(0, population)))
    return np.fromiter(chosen, dtype=np.int64, count=size)


def sample_independent(
    durations: np.ndarray,
    t0: int,
    t1: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """Each open is uniform on the seconds that keep that hold inside the window."""
    opens = np.empty(durations.shape[0], dtype=np.int64)
    for i, duration in enumerate(durations):
        last_open = t1 - int(duration)
        if last_open < t0:
            raise ValueError(f"duration {duration} does not fit in [{t0}, {t1}]")
        opens[i] = int(rng.integers(t0, last_open + 1))
    return opens


def sample_k1(
    durations: np.ndarray,
    t0: int,
    t1: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """One uniform feasible K=1 placement. Raises if the durations do not fit."""
    n = int(durations.shape[0])
    slack = (t1 - t0) - int(durations.sum())
    if slack < 0:
        raise RuntimeError(
            f"durations sum to more than the window ({int(durations.sum())} > {t1 - t0})"
        )
    order = rng.permutation(n)
    gaps = sample_gaps(slack, n + 1, rng)
    opens = np.empty(n, dtype=np.int64)
    cursor = t0 + int(gaps[0])
    for step, idx in enumerate(order):
        opens[int(idx)] = cursor
        cursor += int(durations[int(idx)]) + int(gaps[step + 1])
    if cursor != t1:
        raise RuntimeError(f"placement cursor {cursor} != window end {t1}")
    return opens


def schedule_funding(
    book: FundingBook,
    trades: list[TradeClock],
    opens: np.ndarray,
) -> tuple[float, list[float]]:
    """Sum of per-trade funding. A missing venue series is an error, not a zero."""
    parts: list[float] = []
    for trade, open_ts in zip(trades, opens):
        close_ts = int(open_ts) + trade.duration_s
        got = funding_for_hold(book, trade.coin, trade.side, int(open_ts), close_ts)
        if got.missing or got.funding_pp is None:
            raise RuntimeError(f"no funding series for {trade.coin}")
        parts.append(float(got.funding_pp))
    return float(sum(parts)), parts


def manual_funding_pp(
    book: FundingBook,
    coin: str,
    side: str,
    ts_open: int,
    ts_close: int,
) -> float:
    """Same sign rule, summed by walking the prints. Used to check searchsorted."""
    bybit = book.bybit[coin]
    okx = book.okx[coin]
    bybit_pp = 0.0
    okx_pp = 0.0
    for ts_s, rate in zip(bybit.ts_s, bybit.rate_pct):
        if ts_open < int(ts_s) <= ts_close:
            bybit_pp += float(rate)
    for ts_s, rate in zip(okx.ts_s, okx.rate_pct):
        if ts_open < int(ts_s) <= ts_close:
            okx_pp += float(rate)
    if side == "long":
        return bybit_pp - okx_pp
    if side == "short":
        return -bybit_pp + okx_pp
    raise ValueError(side)


def mc_summary(samples: np.ndarray, actual: float) -> dict:
    """Tail probability uses the add-one count. Quantiles are of the sample, not of the mean."""
    m = int(samples.shape[0])
    count = int(np.count_nonzero(samples <= actual))
    phat = count / m
    se = math.sqrt(phat * (1.0 - phat) / m)
    quantiles = np.quantile(samples, [0.025, 0.05, 0.5, 0.95, 0.975], method="linear")
    median = float(np.median(samples))
    return {
        "m": m,
        "mean": float(np.mean(samples)),
        "median": median,
        "std": float(np.std(samples, ddof=1)),
        "q025": float(quantiles[0]),
        "q05": float(quantiles[1]),
        "q50": float(quantiles[2]),
        "q95": float(quantiles[3]),
        "q975": float(quantiles[4]),
        "actual": float(actual),
        "delta_actual_minus_median": float(actual) - median,
        "n_le_actual": count,
        "p_add_one": (1 + count) / (m + 1),
        "tail_frequency": phat,
        "tail_frequency_se": se,
        "share_negative": float(np.mean(samples < 0)),
    }


def streams(seed: int = EXP2_SEED) -> tuple[np.random.Generator, np.random.Generator]:
    """Independent streams for modes A and B from one master seed."""
    children = np.random.SeedSequence(seed).spawn(2)
    return (
        np.random.Generator(np.random.PCG64(children[0])),
        np.random.Generator(np.random.PCG64(children[1])),
    )
