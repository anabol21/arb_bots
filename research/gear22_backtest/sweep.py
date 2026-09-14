"""Gear-2.2 parameter sweep harness. Observation only, not a PnL claim.

`replay.py` stays the reference implementation. This module is a faster,
event-driven scan of the *same* rules so a local stability probe over the
parameter grid is affordable: `replay_frame` walks every row in Python
(~145 s per combo on the 18-day IS window), which makes a few hundred combos
impractical.

Equivalence is a hard requirement, not a hope. `assert_parity` replays the
same frame through `replay_frame` and through this scan and compares trades
field by field; `tests/test_gear22_backtest_sweep.py` locks it on synthetic
frames that cover the NaN precedence, same-second close-then-open and
`hold_open_overlap` branches.

Why an event scan is legitimate here: under `slot_mode="global"` with K=1,
while flat only the first qualifying row in `(ts_s, coin)` order matters, and
while in position only the held coin's rows matter (`_replay_global_timestamp`
returns early for every other coin). Open gates are elementwise threshold
tests, so they vectorize per combo; the sequential part then jumps event to
event instead of visiting all 46.6M rows.

Accounting (chosen so the objective is not the censored one): a run's PnL is

    total_pp = sum(closed potential_pp) + mtm_pp

where `mtm_pp` marks the still-open position to market at the last row of the
held coin with a finite opposite `spread_last`. Without that term, raising
`min_profit_pp` looks better simply by pushing losses into `open_positions`,
because a closed trade satisfies `potential_pp >= min_profit_pp` by
construction.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd

from research.gear22_backtest.policy import PolicyParams
from research.gear22_backtest.replay import ClosedTrade, replay_frame

# Side status codes, mirroring policy._open_side_status return values.
_NOT_USABLE = np.int8(0)
_NAN = np.int8(1)
_BELOW = np.int8(2)
_QUALIFY = np.int8(3)

# Open outcome codes, mirroring policy.decide_open.
_OPEN_NONE = np.int8(0)
_OPEN_LONG = np.int8(1)
_OPEN_SHORT = np.int8(2)

STORE_COLUMNS: tuple[str, ...] = (
    "ts_s",
    "coin",
    "p50_1m_long",
    "p50_1m_short",
    "theta_1m_long",
    "theta_1m_short",
    "spread_last_long",
    "spread_last_short",
    "usable_long",
    "usable_short",
)

_DEFAULT_PART = "part-000.parquet"


@dataclass(frozen=True)
class Store:
    """Feature rows as numpy arrays in `(ts_s, coin)` clock order.

    `coin_code` is the rank of the symbol in sorted order, so ordering by
    code reproduces the lexicographic tie-break of `_clock_order`.
    """

    ts: np.ndarray
    coin_code: np.ndarray
    coins: list[str]
    p50_long: np.ndarray
    p50_short: np.ndarray
    theta_long: np.ndarray
    theta_short: np.ndarray
    spread_long: np.ndarray
    spread_short: np.ndarray
    usable_long: np.ndarray
    usable_short: np.ndarray
    group_start: np.ndarray  # index of the first row of each ts_s group
    coin_rows: list[np.ndarray] = field(repr=False, default_factory=list)

    @property
    def n_rows(self) -> int:
        return int(self.ts.shape[0])

    @property
    def span_s(self) -> int:
        """Distinct seconds covered (one group per second)."""
        return int(self.group_start.shape[0])


@dataclass(frozen=True)
class RunResult:
    trades: list[ClosedTrade]
    open_coin: Optional[str]
    open_side: Optional[str]
    open_ts: Optional[int]
    mtm_pp: float
    mtm_ts: Optional[int]
    exposure_s: int

    @property
    def n_closed(self) -> int:
        return len(self.trades)

    @property
    def sum_closed_pp(self) -> float:
        return float(sum(t.potential_pp for t in self.trades))

    @property
    def total_pp(self) -> float:
        """Closed round-trips plus mark-to-market of the still-open position."""
        return self.sum_closed_pp + self.mtm_pp


def store_from_frame(df: pd.DataFrame) -> Store:
    """Build a Store from an in-memory feature frame (same input as replay)."""
    missing = [c for c in STORE_COLUMNS if c not in df.columns]
    if missing:
        raise KeyError(f"store_from_frame missing columns: {missing}")
    coin_str = df["coin"].astype(str).to_numpy()
    coins = sorted(set(coin_str.tolist()))
    lookup = {c: i for i, c in enumerate(coins)}
    coin_code = np.array([lookup[c] for c in coin_str], dtype=np.int32)
    ts = df["ts_s"].to_numpy(dtype=np.int64)
    arrays = {
        name: df[name].to_numpy(dtype=np.float32)
        for name in (
            "p50_1m_long",
            "p50_1m_short",
            "theta_1m_long",
            "theta_1m_short",
            "spread_last_long",
            "spread_last_short",
        )
    }
    usable = {
        name: _to_bool(df[name].to_numpy()) for name in ("usable_long", "usable_short")
    }
    return _finish_store(ts, coin_code, coins, arrays, usable)


def store_from_hive(
    hive_root: Path | str,
    *,
    dates: Sequence[str] | None = None,
    coins: Sequence[str] | None = None,
    part_name: str = _DEFAULT_PART,
) -> Store:
    """Read the by_date hive into a Store without building a pandas frame."""
    import pyarrow.parquet as pq

    root = Path(hive_root)
    if dates is None:
        paths = sorted(p for p in root.glob(f"event_date=*/{part_name}") if p.is_file())
    else:
        paths = [root / f"event_date={d}" / part_name for d in sorted(set(dates))]
        absent = [str(p) for p in paths if not p.is_file()]
        if absent:
            raise FileNotFoundError(f"missing parts: {absent}")
    if not paths:
        raise FileNotFoundError(f"no {part_name} under {root}")

    wanted = None if coins is None else set(str(c) for c in coins)
    ts_parts: list[np.ndarray] = []
    code_parts: list[np.ndarray] = []
    float_parts: dict[str, list[np.ndarray]] = {
        name: []
        for name in (
            "p50_1m_long",
            "p50_1m_short",
            "theta_1m_long",
            "theta_1m_short",
            "spread_last_long",
            "spread_last_short",
        )
    }
    bool_parts: dict[str, list[np.ndarray]] = {"usable_long": [], "usable_short": []}
    all_coins: set[str] = set()
    local: list[tuple[np.ndarray, list[str]]] = []

    for path in paths:
        table = pq.read_table(path, columns=list(STORE_COLUMNS))
        names, codes = _coin_codes(table.column("coin"))
        all_coins.update(names)
        local.append((codes, names))
        ts_parts.append(table.column("ts_s").to_numpy(zero_copy_only=False))
        for name in float_parts:
            float_parts[name].append(
                table.column(name).to_numpy(zero_copy_only=False).astype(np.float32)
            )
        for name in bool_parts:
            bool_parts[name].append(
                _to_bool(table.column(name).to_numpy(zero_copy_only=False))
            )

    coin_list = sorted(all_coins if wanted is None else (all_coins & wanted))
    global_idx = {c: i for i, c in enumerate(coin_list)}
    for codes, names in local:
        remap = np.array(
            [global_idx.get(n, -1) for n in names],
            dtype=np.int32,
        )
        code_parts.append(remap[codes])

    ts = np.concatenate(ts_parts).astype(np.int64, copy=False)
    coin_code = np.concatenate(code_parts)
    arrays = {name: np.concatenate(parts) for name, parts in float_parts.items()}
    usable = {name: np.concatenate(parts) for name, parts in bool_parts.items()}

    if (coin_code < 0).any():
        keep = coin_code >= 0
        ts = ts[keep]
        coin_code = coin_code[keep]
        arrays = {k: v[keep] for k, v in arrays.items()}
        usable = {k: v[keep] for k, v in usable.items()}
    return _finish_store(ts, coin_code, coin_list, arrays, usable)


def _coin_codes(column) -> tuple[list[str], np.ndarray]:
    """Local dictionary names plus per-row codes for a parquet coin column."""
    import pyarrow as pa

    combined = column.combine_chunks() if hasattr(column, "combine_chunks") else column
    if isinstance(combined, pa.ChunkedArray):
        combined = combined.chunk(0) if combined.num_chunks == 1 else combined.cast(
            pa.string()
        )
    if pa.types.is_dictionary(combined.type):
        names = [str(v) for v in combined.dictionary.to_pylist()]
        codes = combined.indices.to_numpy(zero_copy_only=False).astype(np.int32)
        return names, codes
    values = [str(v) for v in combined.to_pylist()]
    names = sorted(set(values))
    lookup = {c: i for i, c in enumerate(names)}
    return names, np.array([lookup[v] for v in values], dtype=np.int32)


def _to_bool(values: np.ndarray) -> np.ndarray:
    """Match replay._as_bool: None / NaN / non-bool falsy → False."""
    if values.dtype == bool:
        return values
    if values.dtype == object:
        return np.array(
            [bool(v) if v is not None and v is not pd.NA else False for v in values],
            dtype=bool,
        )
    if np.issubdtype(values.dtype, np.floating):
        return np.isfinite(values) & (values != 0.0)
    return values.astype(bool)


def _finish_store(
    ts: np.ndarray,
    coin_code: np.ndarray,
    coins: list[str],
    arrays: dict[str, np.ndarray],
    usable: dict[str, np.ndarray],
) -> Store:
    order = np.lexsort((coin_code, ts))
    ts = ts[order]
    coin_code = coin_code[order]
    arrays = {k: v[order] for k, v in arrays.items()}
    usable = {k: v[order] for k, v in usable.items()}

    if ts.shape[0] == 0:
        group_start = np.zeros(0, dtype=np.int64)
    else:
        boundary = np.flatnonzero(np.diff(ts) != 0) + 1
        group_start = np.concatenate(([0], boundary)).astype(np.int64)

    coin_rows = [
        np.flatnonzero(coin_code == code).astype(np.int64)
        for code in range(len(coins))
    ]
    return Store(
        ts=ts,
        coin_code=coin_code,
        coins=coins,
        p50_long=arrays["p50_1m_long"],
        p50_short=arrays["p50_1m_short"],
        theta_long=arrays["theta_1m_long"],
        theta_short=arrays["theta_1m_short"],
        spread_long=arrays["spread_last_long"],
        spread_short=arrays["spread_last_short"],
        usable_long=usable["usable_long"],
        usable_short=usable["usable_short"],
        group_start=group_start,
        coin_rows=coin_rows,
    )


def _side_status(
    usable: np.ndarray,
    theta: np.ndarray,
    p50: np.ndarray,
    spread_last: np.ndarray,
    theta_open: Optional[float],
    p50_open: Optional[float],
    min_spread_open: Optional[float],
) -> np.ndarray:
    """Vectorized policy._open_side_status. Gate order is load-bearing.

    The first failing gate decides the status, so a NaN theta reports `nan`
    even when p50 is also below its threshold.

    Thresholds are wrapped in `np.float64` on purpose. The feature arrays are
    float32 (the on-disk dtype), and NEP 50 weak promotion would cast a plain
    Python threshold *down* to float32, whereas `replay._as_float` widens each
    value to float64 and compares there. The two disagree exactly on the
    boundary: `p50_1m` of float32(0.6) is 0.60000002384 in float64, which
    passes a strict `> 0.60` gate, but equals float32(0.60) and fails it.
    """
    status = np.full(usable.shape[0], _QUALIFY, dtype=np.int8)
    status[~usable] = _NOT_USABLE
    pending = usable.copy()
    for value, threshold, strict in (
        (theta, theta_open, True),
        (p50, p50_open, True),
        (spread_last, min_spread_open, False),
    ):
        if threshold is None:
            continue
        bad = pending & ~np.isfinite(value)
        status[bad] = _NAN
        pending &= ~bad
        limit = np.float64(threshold)
        passes = value > limit if strict else value >= limit
        bad = pending & ~passes
        status[bad] = _BELOW
        pending &= ~bad
    return status


def _open_outcome(
    status_long: np.ndarray,
    status_short: np.ndarray,
    spread_long: np.ndarray,
    spread_short: np.ndarray,
) -> np.ndarray:
    """Vectorized policy.decide_open: prefer long, and long NaN blocks short."""
    out = np.zeros(status_long.shape[0], dtype=np.int8)
    long_qualify = status_long == _QUALIFY
    out[long_qualify & np.isfinite(spread_long)] = _OPEN_LONG
    # A long-side `nan` or a qualifying long returns before the short side is
    # evaluated, so those rows can never open short.
    blocked = long_qualify | (status_long == _NAN)
    short_qualify = (status_short == _QUALIFY) & ~blocked
    out[short_qualify & np.isfinite(spread_short)] = _OPEN_SHORT
    return out


def _close_base(
    usable_close: np.ndarray,
    spread_close: np.ndarray,
    theta_close: np.ndarray,
    min_theta_close: Optional[float],
    qualify_position_side: np.ndarray,
) -> np.ndarray:
    """Close-side gates that do not depend on the fill price."""
    ok = usable_close & np.isfinite(spread_close)
    if min_theta_close is not None:
        # np.float64: see the promotion note in _side_status.
        ok = ok & np.isfinite(theta_close) & (theta_close > np.float64(min_theta_close))
    return ok & ~qualify_position_side


def run_combo(store: Store, params: PolicyParams) -> RunResult:
    """Event-driven global-K=1 scan of `store` under `params`."""
    status_long = _side_status(
        store.usable_long,
        store.theta_long,
        store.p50_long,
        store.spread_long,
        params.theta_open,
        params.p50_open,
        params.min_spread_open,
    )
    status_short = _side_status(
        store.usable_short,
        store.theta_short,
        store.p50_short,
        store.spread_short,
        params.theta_open,
        params.p50_open,
        params.min_spread_open,
    )
    open_outcome = _open_outcome(
        status_long, status_short, store.spread_long, store.spread_short
    )
    open_any = open_outcome != _OPEN_NONE
    qualify_long = status_long == _QUALIFY
    qualify_short = status_short == _QUALIFY
    # Holding long unwinds the short book, and the overlap rule tests the
    # *position* side's open gates.
    close_base_long = _close_base(
        store.usable_short,
        store.spread_short,
        store.theta_short,
        params.min_theta_close,
        qualify_long,
    )
    close_base_short = _close_base(
        store.usable_long,
        store.spread_long,
        store.theta_long,
        params.min_theta_close,
        qualify_short,
    )

    fee = float(params.fee_round_trip_pp)
    min_profit = params.min_profit_pp
    trades: list[ClosedTrade] = []
    exposure_s = 0
    n_rows = store.n_rows

    idx = 0
    skip_code = -1
    skip_ts = -1
    while idx < n_rows:
        found = _next_open(store, open_any, idx, skip_code, skip_ts)
        if found < 0:
            break
        skip_code = -1
        skip_ts = -1
        side_code = open_outcome[found]
        held_code = int(store.coin_code[found])
        ts_open = int(store.ts[found])
        if side_code == _OPEN_LONG:
            side = "long"
            fill = float(store.spread_long[found])
            base = close_base_long
            exit_arr = store.spread_short
        else:
            side = "short"
            fill = float(store.spread_short[found])
            base = close_base_short
            exit_arr = store.spread_long

        closed_at = _next_close(
            store, held_code, found, base, exit_arr, fill, fee, min_profit
        )
        if closed_at < 0:
            mtm_pp, mtm_ts = _mark_to_market(
                store, held_code, found, exit_arr, fill, fee
            )
            if mtm_ts is not None:
                exposure_s += int(mtm_ts) - ts_open
            return RunResult(
                trades=trades,
                open_coin=store.coins[held_code],
                open_side=side,
                open_ts=ts_open,
                mtm_pp=mtm_pp,
                mtm_ts=mtm_ts,
                exposure_s=exposure_s,
            )

        exit_spread = float(exit_arr[closed_at])
        ts_close = int(store.ts[closed_at])
        trades.append(
            ClosedTrade(
                coin=store.coins[held_code],
                side=side,
                ts_open=ts_open,
                ts_close=ts_close,
                fill_spread_pp=fill,
                exit_spread_pp=exit_spread,
                potential_pp=fill + exit_spread - fee,
                reason_open=f"open_{side}",
                reason_close="close_min_profit",
            )
        )
        exposure_s += ts_close - ts_open
        # The whole second is rescanned after a close, minus the coin that
        # just closed: a lexicographically earlier coin may still open.
        idx = int(store.group_start[_group_of(store, closed_at)])
        skip_code = held_code
        skip_ts = ts_close

    return RunResult(
        trades=trades,
        open_coin=None,
        open_side=None,
        open_ts=None,
        mtm_pp=0.0,
        mtm_ts=None,
        exposure_s=exposure_s,
    )


def _group_of(store: Store, row: int) -> int:
    return int(np.searchsorted(store.group_start, row, side="right") - 1)


def _next_open(
    store: Store,
    open_any: np.ndarray,
    start: int,
    skip_code: int,
    skip_ts: int,
) -> int:
    """First row at or after `start` that opens, honouring the just-closed skip."""
    n_rows = store.n_rows
    if start >= n_rows:
        return -1
    if skip_code >= 0:
        group = _group_of(store, start)
        end = (
            int(store.group_start[group + 1])
            if group + 1 < store.group_start.shape[0]
            else n_rows
        )
        window = open_any[start:end] & (store.coin_code[start:end] != skip_code)
        hit = np.flatnonzero(window)
        if hit.size:
            return start + int(hit[0])
        start = end
        if start >= n_rows:
            return -1
    hit = np.flatnonzero(open_any[start:])
    if not hit.size:
        return -1
    return start + int(hit[0])


def _next_close(
    store: Store,
    held_code: int,
    open_row: int,
    base: np.ndarray,
    exit_arr: np.ndarray,
    fill: float,
    fee: float,
    min_profit: Optional[float],
) -> int:
    """First held-coin row strictly after the open second that closes."""
    rows = store.coin_rows[held_code]
    pos = int(np.searchsorted(rows, open_row, side="right"))
    if pos >= rows.shape[0]:
        return -1
    candidates = rows[pos:]
    ok = base[candidates]
    if min_profit is not None:
        if not np.isfinite(fill) or not np.isfinite(fee):
            return -1
        # Same expression and same precision as policy.potential_profit_pp:
        # rearranging to a threshold on the exit spread, or letting NEP 50
        # cast the bound to float32, both shift borderline closes by a row.
        potential = (
            np.float64(fill)
            + exit_arr[candidates].astype(np.float64)
            - np.float64(fee)
        )
        ok = ok & (potential >= np.float64(min_profit))
    hit = np.flatnonzero(ok)
    if not hit.size:
        return -1
    return int(candidates[hit[0]])


def _mark_to_market(
    store: Store,
    held_code: int,
    open_row: int,
    exit_arr: np.ndarray,
    fill: float,
    fee: float,
) -> tuple[float, Optional[int]]:
    """Value the still-open position at the last finite opposite spread."""
    rows = store.coin_rows[held_code]
    pos = int(np.searchsorted(rows, open_row, side="right"))
    candidates = rows[pos:]
    if candidates.shape[0] == 0:
        return 0.0, None
    finite = np.flatnonzero(np.isfinite(exit_arr[candidates]))
    if not finite.size:
        return 0.0, None
    row = int(candidates[finite[-1]])
    return float(fill + float(exit_arr[row]) - fee), int(store.ts[row])


def metrics_row(params: PolicyParams, result: RunResult, span_s: int) -> dict:
    """One flat record per parameter combo."""
    n_closed = result.n_closed
    return {
        "theta_open": params.theta_open,
        "p50_open": params.p50_open,
        "min_profit_pp": params.min_profit_pp,
        "min_theta_close": params.min_theta_close,
        "min_spread_open": params.min_spread_open,
        "n_closed": n_closed,
        "n_open_end": 0 if result.open_coin is None else 1,
        "sum_closed_pp": result.sum_closed_pp,
        "mtm_pp": result.mtm_pp,
        "total_pp": result.total_pp,
        "exposure_s": result.exposure_s,
        "duty_cycle": (result.exposure_s / span_s) if span_s else float("nan"),
        "mean_hold_s": (result.exposure_s / n_closed) if n_closed else float("nan"),
        "open_coin": result.open_coin,
    }


def sweep(
    store: Store,
    *,
    theta_open: Iterable[Optional[float]],
    p50_open: Iterable[Optional[float]],
    min_profit_pp: Iterable[Optional[float]],
    min_theta_close: Iterable[Optional[float]],
    min_spread_open: Iterable[Optional[float]] = (None,),
    fee_round_trip_pp: float = 0.30,
    progress: bool = False,
) -> pd.DataFrame:
    """Cartesian product over the knobs; one row of metrics per combo."""
    combos = list(
        itertools.product(
            list(theta_open),
            list(p50_open),
            list(min_profit_pp),
            list(min_theta_close),
            list(min_spread_open),
        )
    )
    rows = []
    for n, (th, p50, prof, theta_cl, spread_open) in enumerate(combos, start=1):
        params = PolicyParams(
            theta_open=th,
            p50_open=p50,
            min_profit_pp=prof,
            fee_round_trip_pp=fee_round_trip_pp,
            min_spread_open=spread_open,
            min_theta_close=theta_cl,
        )
        result = run_combo(store, params)
        rows.append(metrics_row(params, result, store.span_s))
        if progress and (n % 25 == 0 or n == len(combos)):
            print(f"  combo {n}/{len(combos)}", flush=True)
    return pd.DataFrame(rows)


def assert_parity(
    df: pd.DataFrame, params: PolicyParams, *, float_tol: float = 1e-6
) -> int:
    """Fail loudly if the fast scan disagrees with `replay_frame` on `df`.

    Returns the number of closed trades compared.

    Prices are compared with a tolerance, not bit for bit: the store keeps
    float32 because that is the on-disk dtype of the feature table, while
    `replay_frame` preserves whatever the frame holds — float64 for a
    hand-built synthetic frame. Trade identity (coin, side, timestamps) is
    still compared exactly, so a genuine divergence cannot hide behind the
    tolerance.
    """
    reference = replay_frame(df, params, slot_mode="global")
    fast = run_combo(store_from_frame(df), params)
    ref_trades = [_trade_key(t) for t in reference.closed]
    fast_trades = [_trade_key(t) for t in fast.trades]
    mismatch = len(reference.closed) != len(fast.trades) or any(
        not _trades_match(a, b, float_tol)
        for a, b in zip(reference.closed, fast.trades)
    )
    if mismatch:
        raise AssertionError(
            "closed trades differ\n"
            f"  replay_frame: {ref_trades}\n"
            f"  sweep       : {fast_trades}"
        )
    ref_open = [(p.coin, p.side, p.ts_open) for p in reference.open_positions]
    fast_open = (
        []
        if fast.open_coin is None
        else [(fast.open_coin, fast.open_side, fast.open_ts)]
    )
    if ref_open != fast_open:
        raise AssertionError(
            f"open positions differ\n  replay_frame: {ref_open}\n  sweep: {fast_open}"
        )
    return len(ref_trades)


def _trade_key(trade: ClosedTrade) -> tuple:
    return (
        trade.coin,
        trade.side,
        int(trade.ts_open),
        int(trade.ts_close),
        round(float(trade.fill_spread_pp), 9),
        round(float(trade.exit_spread_pp), 9),
        round(float(trade.potential_pp), 9),
    )


def _main(argv: Sequence[str] | None = None) -> int:
    """Run the frozen set (or an explicit one) over a window and print metrics."""
    import argparse

    from research.gear22_backtest.params_frozen import FROZEN, IS_DATES, OOS_DATES

    parser = argparse.ArgumentParser(
        description="Gear-2.2 frozen-parameter observation run (not a PnL claim)."
    )
    parser.add_argument("--hive", type=Path, required=True, help="by_date hive root")
    parser.add_argument(
        "--window",
        choices=("is", "oos", "all"),
        default="all",
        help="is / oos as frozen in params_frozen, or all partitions on disk",
    )
    parser.add_argument(
        "--date", action="append", dest="dates", default=None, help="repeatable UTC date"
    )
    parser.add_argument("--theta-open", type=float, default=None)
    parser.add_argument("--p50-open", type=float, default=None)
    parser.add_argument("--min-profit-pp", type=float, default=None)
    parser.add_argument("--min-theta-close", type=float, default=None)
    args = parser.parse_args(argv)

    if args.dates:
        dates: Optional[Sequence[str]] = args.dates
    elif args.window == "is":
        dates = list(IS_DATES)
    elif args.window == "oos":
        dates = list(OOS_DATES)
    else:
        dates = None

    overrides = {
        name: value
        for name, value in (
            ("theta_open", args.theta_open),
            ("p50_open", args.p50_open),
            ("min_profit_pp", args.min_profit_pp),
            ("min_theta_close", args.min_theta_close),
        )
        if value is not None
    }
    params = PolicyParams(**{**FROZEN.__dict__, **overrides})

    store = store_from_hive(args.hive, dates=dates)
    result = run_combo(store, params)
    row = metrics_row(params, result, store.span_s)
    print(f"rows={store.n_rows} coins={len(store.coins)} span_s={store.span_s}")
    print(f"params={params}")
    for key in (
        "n_closed",
        "n_open_end",
        "sum_closed_pp",
        "mtm_pp",
        "total_pp",
        "exposure_s",
        "duty_cycle",
        "mean_hold_s",
        "open_coin",
    ):
        print(f"  {key}={row[key]}")
    print("observation only: not a PnL claim, not live-ready")
    return 0


def _trades_match(a: ClosedTrade, b: ClosedTrade, float_tol: float) -> bool:
    if (a.coin, a.side, int(a.ts_open), int(a.ts_close)) != (
        b.coin,
        b.side,
        int(b.ts_open),
        int(b.ts_close),
    ):
        return False
    return all(
        math.isclose(
            float(getattr(a, name)),
            float(getattr(b, name)),
            rel_tol=float_tol,
            abs_tol=float_tol,
        )
        for name in ("fill_spread_pp", "exit_spread_pp", "potential_pp")
    )


if __name__ == "__main__":
    raise SystemExit(_main())
