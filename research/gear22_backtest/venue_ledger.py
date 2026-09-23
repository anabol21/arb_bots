"""Two-venue cash ledger on top of a frozen gear-2.2 trade list.

Observation accounting only. ``policy.decide`` is not called here and its
gates are not retuned. This is not gear 2.5, not a profitability claim, and
not live.

The replay still fills at 1 Hz ``spread_last``. This module turns each
round-trip into a Bybit cash change and an OKX cash change using the L1
prices of that same last tick:

- open long: sell Bybit at bid, buy OKX at ask
- close long: buy Bybit at ask, sell OKX at bid
- open short: buy Bybit at ask, sell OKX at bid
- close short: sell Bybit at bid, buy OKX at ask

Each leg is sized in dollars (``qty = notional / entry_price``), not in a
shared coin quantity. The wallet is a perp wallet: notional is not debited,
only taker fees and price PnL. There is no liquidation.

Fees are taker on each of the four fills, on that fill's notional
(``rate * qty * fill_price``): Bybit 0.10%, OKX 0.05%. The policy close gate
still subtracts ``fee_round_trip_pp = 0.30`` from the spread. These rates do
not change that gate.

Modes
-----
``fixed100``
    Every leg is $100. No transfer. A leg still opens at $100 when cash is
    below $100.
``fixed100_equalize``
    Same $100 leg. After each close, both cashes become their average.
    The transfer is free and immediate.
``balance_equalize``
    Same free equalize. The next leg's notional is that common cash. If the
    sum of the two cashes is <= 0, the round is skipped
    (``skipped_nonpositive``). The policy still "saw" the trade.

A round with no L1 book (``skipped_hole``) or whose book fails the spread
match (``price_mismatch``) is not booked and is not replaced with
``potential_pp``. The cash path continues: later rounds still apply when
their books are present and match.
"""

from __future__ import annotations

import csv
import html
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal, Optional, Sequence

import numpy as np

from research.gear22_backtest.params_frozen import FROZEN
from research.gear22_backtest.policy import PolicyParams, Side

Mode = Literal["fixed100", "fixed100_equalize", "balance_equalize"]
MODES: tuple[Mode, ...] = ("fixed100", "fixed100_equalize", "balance_equalize")

START_CASH = 100.0
FIXED_NOTIONAL = 100.0
BYBIT_TAKER_FEE = 0.001
OKX_TAKER_FEE = 0.0005
SPREAD_ATOL_PP = 1e-3
# Observation mark only: spread_open + opposite spread(t) minus 0.15 pp in
# and 0.15 pp out. Same 0.30 pp round trip as the frozen policy gate. This is
# not the ledger's Bybit 0.10% / OKX 0.05% taker split.
APPROX_FEE_ENTRY_PP = 0.15
APPROX_FEE_EXIT_PP = 0.15
LOOKBACK_MS = 120_000
# Compacted names are mtime windows and can lag event time by ~40s, so the
# last tick at a second boundary often sits in the next named file. The
# feature loader pads one 5-minute slot on each side (``load_slim``). The
# row rule stays ``event_local_ts_ms <= ts_s * 1000`` inside ``LOOKBACK_MS``.
FILE_LABEL_PAD_MS = 300_000

_EQUALIZE_MODES = frozenset({"fixed100_equalize", "balance_equalize"})


@dataclass(frozen=True)
class Book:
    bybit_bid: float
    bybit_ask: float
    okx_bid: float
    okx_ask: float


@dataclass(frozen=True)
class PricedRound:
    coin: str
    side: Side
    ts_open: int
    ts_close: int
    open_book: Book
    close_book: Book
    potential_pp: Optional[float] = None
    prices_match: bool = True
    # True when open or close L1 was absent (NaN book), not a spread disagree.
    is_hole: bool = False


@dataclass(frozen=True)
class PricedOpen:
    """Still-open position. Entry fees hit cash; unrealized does not."""

    coin: str
    side: Side
    ts_open: int
    open_book: Book
    mark_book: Optional[Book] = None
    mark_ts: Optional[int] = None
    potential_pp: Optional[float] = None
    prices_match: bool = True
    mark_prices_match: bool = False
    is_hole: bool = False


@dataclass(frozen=True)
class LedgerStep:
    status: str
    coin: str
    side: str
    ts_open: Optional[int]
    ts_close: Optional[int]
    notional: Optional[float]
    potential_pp: Optional[float]
    cash_bybit_after_open: Optional[float]
    cash_okx_after_open: Optional[float]
    cash_bybit_after_close: Optional[float]
    cash_okx_after_close: Optional[float]
    cash_bybit: float
    cash_okx: float
    equity_bybit: Optional[float]
    equity_okx: Optional[float]
    pnl_bybit: Optional[float]
    pnl_okx: Optional[float]
    fee_bybit: Optional[float]
    fee_okx: Optional[float]


@dataclass
class LedgerResult:
    mode: Mode
    steps: list[LedgerStep] = field(default_factory=list)
    cash_bybit: float = START_CASH
    cash_okx: float = START_CASH
    min_bybit: float = START_CASH
    min_okx: float = START_CASH
    n_applied: int = 0
    n_skipped_nonpositive: int = 0
    n_skipped_hole: int = 0
    n_price_mismatch: int = 0
    n_not_applied: int = 0
    n_negative_rounds: int = 0
    n_open_end: int = 0
    complete: bool = True

    @property
    def equity_bybit(self) -> float:
        if not self.steps:
            return self.cash_bybit
        last = self.steps[-1].equity_bybit
        return self.cash_bybit if last is None else last

    @property
    def equity_okx(self) -> float:
        if not self.steps:
            return self.cash_okx
        last = self.steps[-1].equity_okx
        return self.cash_okx if last is None else last


def spread_pp(book: Book, which: str) -> float:
    """Venue spread in the same percentage points as ``spread_last_*``."""
    if which == "long":
        if not _positive(book.bybit_bid):
            return float("nan")
        return (book.bybit_bid - book.okx_ask) / book.bybit_bid * 100.0
    if which == "short":
        if not _positive(book.okx_bid):
            return float("nan")
        return (book.okx_bid - book.bybit_ask) / book.okx_bid * 100.0
    raise ValueError(f"unknown spread side {which!r}")


def approx_potential_pp(side: str, open_book: Book, mark_book: Book) -> Optional[float]:
    """``spread_open + spread_opposite(mark) - 0.15 - 0.15``, in percentage points.

    Opposite is the close side: short while a long is open, long while a short
    is open. ``None`` when either book cannot form that spread.
    """
    if book_is_missing(open_book) or book_is_missing(mark_book):
        return None
    opened = spread_pp(open_book, open_book_side(side))
    opposite = spread_pp(mark_book, close_book_side(side))
    if not math.isfinite(opened) or not math.isfinite(opposite):
        return None
    return opened + opposite - APPROX_FEE_ENTRY_PP - APPROX_FEE_EXIT_PP


def approx_usd(notional: float, potential_pp: float) -> float:
    """Map the pp mark onto the round's dollar notional: ``N * pp / 100``."""
    return float(notional) * float(potential_pp) / 100.0


def book_is_missing(book: Book) -> bool:
    """True when no usable L1 was attached (NaN / non-finite prices)."""
    return not (
        math.isfinite(book.bybit_bid)
        and math.isfinite(book.bybit_ask)
        and math.isfinite(book.okx_bid)
        and math.isfinite(book.okx_ask)
    )


def book_matches_spread(
    book: Book,
    which: str,
    stored_pp: float,
    *,
    atol: float = SPREAD_ATOL_PP,
) -> bool:
    """True when recomputed spread matches the feature value at float32 scale.

    A miss means these prices are not the tick ``spread_last`` was taken
    from. Callers must not substitute ``potential_pp`` for cash.
    """
    got = spread_pp(book, which)
    if not math.isfinite(got) or not math.isfinite(stored_pp):
        return False
    return abs(float(np.float32(got)) - float(np.float32(stored_pp))) <= atol


def open_book_side(position_side: str) -> str:
    if position_side == "long":
        return "long"
    if position_side == "short":
        return "short"
    raise ValueError(f"unknown position side {position_side!r}")


def close_book_side(position_side: str) -> str:
    if position_side == "long":
        return "short"
    if position_side == "short":
        return "long"
    raise ValueError(f"unknown position side {position_side!r}")


def one_step_neighbors(
    frozen: PolicyParams = FROZEN,
) -> list[tuple[str, PolicyParams]]:
    """Frozen cell plus the one-step neighbours documented in params_frozen.

    Each neighbour moves a single knob. This is not a search for a new set.
    """
    specs: tuple[tuple[str, dict], ...] = (
        ("frozen", {}),
        ("theta_open=0.40", {"theta_open": 0.40}),
        ("theta_open=0.60", {"theta_open": 0.60}),
        ("p50_open=0.50", {"p50_open": 0.50}),
        ("p50_open=0.70", {"p50_open": 0.70}),
        ("min_profit_pp=0.15", {"min_profit_pp": 0.15}),
        ("min_profit_pp=0.25", {"min_profit_pp": 0.25}),
        ("min_theta_close=0", {"min_theta_close": 0.0}),
        ("min_theta_close=0.10", {"min_theta_close": 0.10}),
    )
    return [(label, replace(frozen, **overrides)) for label, overrides in specs]


def run_ledger(
    rounds: Sequence[PricedRound],
    mode: Mode,
    *,
    open_leg: Optional[PricedOpen] = None,
    start_bybit: float = START_CASH,
    start_okx: float = START_CASH,
    fixed_notional: float = FIXED_NOTIONAL,
    bybit_fee: float = BYBIT_TAKER_FEE,
    okx_fee: float = OKX_TAKER_FEE,
) -> LedgerResult:
    """Apply ``rounds`` in order, then an optional still-open leg."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    result = LedgerResult(
        mode=mode,
        cash_bybit=float(start_bybit),
        cash_okx=float(start_okx),
        min_bybit=float(start_bybit),
        min_okx=float(start_okx),
    )
    for rnd in rounds:
        if not rnd.prices_match:
            # Hole or mismatch: do not book; do not freeze later rounds.
            if rnd.is_hole:
                result.n_skipped_hole += 1
                result.steps.append(_unchanged_step(result, rnd, "skipped_hole"))
            else:
                result.n_price_mismatch += 1
                result.steps.append(_unchanged_step(result, rnd, "price_mismatch"))
            continue
        notional = _notional(result, mode, fixed_notional)
        if notional is None:
            result.n_skipped_nonpositive += 1
            result.steps.append(_unchanged_step(result, rnd, "skipped_nonpositive"))
            continue
        _apply_round(result, rnd, notional, bybit_fee, okx_fee, mode)

    if open_leg is not None:
        _apply_open_leg(result, open_leg, mode, fixed_notional, bybit_fee, okx_fee)
    return result


def _notional(result: LedgerResult, mode: Mode, fixed_notional: float) -> Optional[float]:
    if mode == "balance_equalize":
        total = result.cash_bybit + result.cash_okx
        if total <= 0.0:
            return None
        return total / 2.0
    if fixed_notional <= 0.0:
        raise ValueError("fixed_notional must be positive")
    return float(fixed_notional)


def _apply_round(
    result: LedgerResult,
    rnd: PricedRound,
    notional: float,
    bybit_fee: float,
    okx_fee: float,
    mode: Mode,
) -> None:
    entry = _entry_exit(rnd.side, rnd.open_book, rnd.close_book)
    if entry is None:
        result.n_price_mismatch += 1
        result.steps.append(_unchanged_step(result, rnd, "price_mismatch"))
        return
    bybit_entry, bybit_exit, okx_entry, okx_exit, bybit_dir, okx_dir = entry
    qty_bybit = notional / bybit_entry
    qty_okx = notional / okx_entry
    fee_bybit_open = bybit_fee * qty_bybit * bybit_entry
    fee_okx_open = okx_fee * qty_okx * okx_entry
    result.cash_bybit -= fee_bybit_open
    result.cash_okx -= fee_okx_open
    _touch_min(result)
    after_open_bybit = result.cash_bybit
    after_open_okx = result.cash_okx

    pnl_bybit = _leg_pnl(bybit_dir, qty_bybit, bybit_entry, bybit_exit)
    pnl_okx = _leg_pnl(okx_dir, qty_okx, okx_entry, okx_exit)
    fee_bybit_close = bybit_fee * qty_bybit * bybit_exit
    fee_okx_close = okx_fee * qty_okx * okx_exit
    result.cash_bybit += pnl_bybit - fee_bybit_close
    result.cash_okx += pnl_okx - fee_okx_close
    _touch_min(result)
    after_close_bybit = result.cash_bybit
    after_close_okx = result.cash_okx
    if after_open_bybit < 0.0 or after_open_okx < 0.0 or after_close_bybit < 0.0 or after_close_okx < 0.0:
        result.n_negative_rounds += 1
    if mode in _EQUALIZE_MODES:
        _equalize(result)
    result.n_applied += 1
    result.steps.append(
        LedgerStep(
            status="closed",
            coin=rnd.coin,
            side=rnd.side,
            ts_open=rnd.ts_open,
            ts_close=rnd.ts_close,
            notional=notional,
            potential_pp=rnd.potential_pp,
            cash_bybit_after_open=after_open_bybit,
            cash_okx_after_open=after_open_okx,
            cash_bybit_after_close=after_close_bybit,
            cash_okx_after_close=after_close_okx,
            cash_bybit=result.cash_bybit,
            cash_okx=result.cash_okx,
            equity_bybit=result.cash_bybit,
            equity_okx=result.cash_okx,
            pnl_bybit=pnl_bybit,
            pnl_okx=pnl_okx,
            fee_bybit=fee_bybit_open + fee_bybit_close,
            fee_okx=fee_okx_open + fee_okx_close,
        )
    )


def _apply_open_leg(
    result: LedgerResult,
    leg: PricedOpen,
    mode: Mode,
    fixed_notional: float,
    bybit_fee: float,
    okx_fee: float,
) -> None:
    if not leg.prices_match:
        if leg.is_hole:
            result.n_skipped_hole += 1
            status = "skipped_hole"
        else:
            result.n_price_mismatch += 1
            status = "price_mismatch"
        result.steps.append(
            LedgerStep(
                status=status,
                coin=leg.coin,
                side=leg.side,
                ts_open=leg.ts_open,
                ts_close=None,
                notional=None,
                potential_pp=leg.potential_pp,
                cash_bybit_after_open=None,
                cash_okx_after_open=None,
                cash_bybit_after_close=None,
                cash_okx_after_close=None,
                cash_bybit=result.cash_bybit,
                cash_okx=result.cash_okx,
                equity_bybit=None,
                equity_okx=None,
                pnl_bybit=None,
                pnl_okx=None,
                fee_bybit=None,
                fee_okx=None,
            )
        )
        return
    notional = _notional(result, mode, fixed_notional)
    if notional is None:
        result.n_skipped_nonpositive += 1
        result.steps.append(
            LedgerStep(
                status="skipped_nonpositive",
                coin=leg.coin,
                side=leg.side,
                ts_open=leg.ts_open,
                ts_close=None,
                notional=None,
                potential_pp=leg.potential_pp,
                cash_bybit_after_open=None,
                cash_okx_after_open=None,
                cash_bybit_after_close=None,
                cash_okx_after_close=None,
                cash_bybit=result.cash_bybit,
                cash_okx=result.cash_okx,
                equity_bybit=result.cash_bybit,
                equity_okx=result.cash_okx,
                pnl_bybit=None,
                pnl_okx=None,
                fee_bybit=None,
                fee_okx=None,
            )
        )
        return
    # Mark book is only the exit side of the still-open position.
    mark = leg.mark_book if leg.mark_prices_match and leg.mark_book is not None else leg.open_book
    entry = _entry_exit(leg.side, leg.open_book, mark)
    if entry is None:
        result.n_price_mismatch += 1
        return
    bybit_entry, bybit_exit, okx_entry, okx_exit, bybit_dir, okx_dir = entry
    qty_bybit = notional / bybit_entry
    qty_okx = notional / okx_entry
    fee_bybit = bybit_fee * qty_bybit * bybit_entry
    fee_okx = okx_fee * qty_okx * okx_entry
    result.cash_bybit -= fee_bybit
    result.cash_okx -= fee_okx
    _touch_min(result)
    if result.cash_bybit < 0.0 or result.cash_okx < 0.0:
        result.n_negative_rounds += 1
    unreal_bybit: Optional[float] = None
    unreal_okx: Optional[float] = None
    if leg.mark_prices_match and leg.mark_book is not None:
        unreal_bybit = _leg_pnl(bybit_dir, qty_bybit, bybit_entry, bybit_exit)
        unreal_okx = _leg_pnl(okx_dir, qty_okx, okx_entry, okx_exit)
    result.n_open_end = 1
    result.steps.append(
        LedgerStep(
            status="open",
            coin=leg.coin,
            side=leg.side,
            ts_open=leg.ts_open,
            ts_close=leg.mark_ts,
            notional=notional,
            potential_pp=leg.potential_pp,
            cash_bybit_after_open=result.cash_bybit,
            cash_okx_after_open=result.cash_okx,
            cash_bybit_after_close=None,
            cash_okx_after_close=None,
            cash_bybit=result.cash_bybit,
            cash_okx=result.cash_okx,
            equity_bybit=None if unreal_bybit is None else result.cash_bybit + unreal_bybit,
            equity_okx=None if unreal_okx is None else result.cash_okx + unreal_okx,
            pnl_bybit=unreal_bybit,
            pnl_okx=unreal_okx,
            fee_bybit=fee_bybit,
            fee_okx=fee_okx,
        )
    )


def _entry_exit(
    side: str, open_book: Book, close_book: Book
) -> Optional[tuple[float, float, float, float, str, str]]:
    if side == "long":
        bybit_entry, bybit_exit = open_book.bybit_bid, close_book.bybit_ask
        okx_entry, okx_exit = open_book.okx_ask, close_book.okx_bid
        bybit_dir, okx_dir = "short", "long"
    elif side == "short":
        bybit_entry, bybit_exit = open_book.bybit_ask, close_book.bybit_bid
        okx_entry, okx_exit = open_book.okx_bid, close_book.okx_ask
        bybit_dir, okx_dir = "long", "short"
    else:
        raise ValueError(f"unknown position side {side!r}")
    if not all(_positive(px) for px in (bybit_entry, bybit_exit, okx_entry, okx_exit)):
        return None
    return bybit_entry, bybit_exit, okx_entry, okx_exit, bybit_dir, okx_dir


def _leg_pnl(direction: str, qty: float, entry: float, exit_px: float) -> float:
    if direction == "long":
        return qty * (exit_px - entry)
    if direction == "short":
        return qty * (entry - exit_px)
    raise ValueError(direction)


def _equalize(result: LedgerResult) -> None:
    mid = (result.cash_bybit + result.cash_okx) / 2.0
    result.cash_bybit = mid
    result.cash_okx = mid
    _touch_min(result)


def _touch_min(result: LedgerResult) -> None:
    if result.cash_bybit < result.min_bybit:
        result.min_bybit = result.cash_bybit
    if result.cash_okx < result.min_okx:
        result.min_okx = result.cash_okx


def _unchanged_step(result: LedgerResult, rnd: PricedRound, status: str) -> LedgerStep:
    return LedgerStep(
        status=status,
        coin=rnd.coin,
        side=rnd.side,
        ts_open=rnd.ts_open,
        ts_close=None if rnd.ts_close < 0 else rnd.ts_close,
        notional=None,
        potential_pp=rnd.potential_pp,
        cash_bybit_after_open=None,
        cash_okx_after_open=None,
        cash_bybit_after_close=None,
        cash_okx_after_close=None,
        cash_bybit=result.cash_bybit,
        cash_okx=result.cash_okx,
        equity_bybit=result.cash_bybit,
        equity_okx=result.cash_okx,
        pnl_bybit=None,
        pnl_okx=None,
        fee_bybit=None,
        fee_okx=None,
    )


def _positive(value: float) -> bool:
    return math.isfinite(value) and value > 0.0


def _parse_books_1hz_part_start_ms(path: Path) -> Optional[int]:
    """UTC start ms from ``part-YYYYMMDDTHHMMSSZ.parquet``, else ``None``."""
    name = path.name
    if not (name.startswith("part-") and name.endswith(".parquet")):
        return None
    if ".tmp" in name:
        return None
    stamp = name[len("part-") : -len(".parquet")]
    try:
        dt = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(dt.timestamp() * 1000)


def _merge_books_from_table(
    table,
    file_keys: list[tuple[str, int]],
    *,
    lookback_ms: int,
    best_ts: dict[tuple[str, int], int],
    best_book: dict[tuple[str, int], Book],
) -> None:
    """Update ``best_*`` with last L1 at or before each key from one table."""
    import pyarrow as pa
    import pyarrow.compute as pc

    if table.num_rows == 0:
        return
    coins = sorted({coin for coin, _ in file_keys})
    t_lo = min(ts * 1000 - lookback_ms for _, ts in file_keys)
    t_hi = max(ts * 1000 for _, ts in file_keys)
    coin_col = table.column("base_coin")
    if pa.types.is_dictionary(coin_col.type):
        coin_col = pc.dictionary_decode(coin_col)
    ts = table.column("event_local_ts_ms").to_numpy(zero_copy_only=False)
    names = coin_col.to_pylist()
    bid_b = table.column("bybit_bid_price").to_numpy(zero_copy_only=False)
    ask_b = table.column("bybit_ask_price").to_numpy(zero_copy_only=False)
    bid_o = table.column("okx_bid_price").to_numpy(zero_copy_only=False)
    ask_o = table.column("okx_ask_price").to_numpy(zero_copy_only=False)
    mask = (ts >= t_lo) & (ts <= t_hi)
    if not mask.any():
        return
    idx = np.flatnonzero(mask)
    order = idx[np.argsort(ts[idx], kind="mergesort")]
    ts_sorted = ts[order]
    name_s = [str(names[i]).upper() for i in order]
    bid_b_s = bid_b[order]
    ask_b_s = ask_b[order]
    bid_o_s = bid_o[order]
    ask_o_s = ask_o[order]
    by_coin: dict[str, list[int]] = {}
    for i, name in enumerate(name_s):
        if name in coins:
            by_coin.setdefault(name, []).append(i)
    keys_by_coin: dict[str, list[int]] = {}
    for coin, ts_key in file_keys:
        keys_by_coin.setdefault(coin, []).append(ts_key)
    for coin, positions in by_coin.items():
        pos = np.asarray(positions, dtype=np.int64)
        ts_c = ts_sorted[pos]
        for ts_key in keys_by_coin.get(coin, []):
            t_ms = ts_key * 1000
            hi = int(np.searchsorted(ts_c, t_ms, side="right"))
            lo = int(np.searchsorted(ts_c, t_ms - lookback_ms, side="right"))
            if hi <= lo:
                continue
            j = int(pos[hi - 1])
            stamp = int(ts_sorted[j])
            key = (coin, ts_key)
            prev = best_ts.get(key)
            if prev is not None and stamp <= prev:
                continue
            best_ts[key] = stamp
            best_book[key] = Book(
                bybit_bid=float(bid_b_s[j]),
                bybit_ask=float(ask_b_s[j]),
                okx_bid=float(bid_o_s[j]),
                okx_ask=float(ask_o_s[j]),
            )


def _files_covering_keys(
    index: list[tuple[int, int, Path]],
    wanted: set[tuple[str, int]],
    *,
    lookback_ms: int,
) -> dict[Path, list[tuple[str, int]]]:
    """Map each key to files whose name overlaps the padded lookup window.

    The pad is only for the filename label. ``_merge_books_from_table`` still
    keeps the last row with ``event_local_ts_ms <= ts_s * 1000`` inside the
    lookback, so a tick after the second boundary is not a book.
    """
    by_file: dict[Path, list[tuple[str, int]]] = {}
    pad = FILE_LABEL_PAD_MS
    for coin, ts_s in wanted:
        t_ms = ts_s * 1000
        start = t_ms - int(lookback_ms) - pad
        end = t_ms + 1 + pad
        for a, b, path in index:
            if a < end and b > start:
                by_file.setdefault(path, []).append((coin, ts_s))
    return by_file


def load_last_books_1hz(
    books_dir: Path | str,
    keys: Iterable[tuple[str, int]],
    *,
    lookback_ms: int = LOOKBACK_MS,
    part_span_ms: int = 3_600_000,
) -> dict[tuple[str, int], Book]:
    """Last row with ``event_local_ts_ms <= ts_s * 1000`` from ``part-*.parquet``.

    Sidecar product from ``research/gear22_books_1hz_reduce.py`` (canary coins,
    last tick of each UTC second). Same lookback bound as lean ticks. Part
    names encode the batch start; each part's end is the next part's start
    (or ``start + part_span_ms`` for the last). A part can contain ticks
    earlier than that stamp, so the file list uses ``FILE_LABEL_PAD_MS``.
    Partial ``.tmp`` files are ignored.
    """
    import pyarrow.parquet as pq

    root = Path(books_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"1 Hz books dir not found: {root}")
    wanted = {(str(coin).upper(), int(ts_s)) for coin, ts_s in keys}
    if not wanted:
        return {}

    starts: list[tuple[int, Path]] = []
    for path in sorted(root.glob("part-*.parquet")):
        if ".tmp" in path.name:
            continue
        start_ms = _parse_books_1hz_part_start_ms(path)
        if start_ms is None:
            continue
        starts.append((start_ms, path))
    starts.sort()
    index: list[tuple[int, int, Path]] = []
    for i, (start_ms, path) in enumerate(starts):
        if i + 1 < len(starts):
            end_ms = starts[i + 1][0]
        else:
            end_ms = start_ms + int(part_span_ms)
        index.append((start_ms, end_ms, path))

    by_file = _files_covering_keys(index, wanted, lookback_ms=lookback_ms)

    cols = [
        "event_local_ts_ms",
        "base_coin",
        "bybit_bid_price",
        "bybit_ask_price",
        "okx_bid_price",
        "okx_ask_price",
    ]
    best_ts: dict[tuple[str, int], int] = {}
    best_book: dict[tuple[str, int], Book] = {}
    for path, file_keys in by_file.items():
        table = pq.read_table(path, columns=cols)
        _merge_books_from_table(
            table,
            file_keys,
            lookback_ms=lookback_ms,
            best_ts=best_ts,
            best_book=best_book,
        )
    return best_book


def load_last_books(
    tick_dir: Path | str,
    keys: Iterable[tuple[str, int]],
    *,
    lookback_ms: int = LOOKBACK_MS,
    books_1hz_dir: Path | str | None = None,
) -> dict[tuple[str, int], Book]:
    """Last lean tick at or before each ``ts_s`` for that coin.

    The feature ``spread_last`` is the last tick with ``event_local_ts_ms <=
    ts_s * 1000``. Search is limited to ``lookback_ms`` so a missing tick
    stays missing instead of borrowing an old book. File names are padded by
    ``FILE_LABEL_PAD_MS`` because the label lags the rows. Prices are not invented.

    When ``books_1hz_dir`` is set, keys still missing after lean lookup are
    filled from finished ``part-*.parquet`` (lean wins on overlap).
    """
    import pyarrow.parquet as pq

    from research.lean_ticks_io import parse_lean_file_window

    root = Path(tick_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"lean tick dir not found: {root}")
    wanted = {(str(coin).upper(), int(ts_s)) for coin, ts_s in keys}
    if not wanted:
        return {}

    index: list[tuple[int, int, Path]] = []
    for path in sorted(root.glob("spread_*.parquet")):
        win = parse_lean_file_window(path)
        if win is not None:
            index.append((win[0], win[1], path))
    index.sort()

    by_file = _files_covering_keys(index, wanted, lookback_ms=lookback_ms)

    cols = [
        "event_local_ts_ms",
        "base_coin",
        "bybit_bid_price",
        "bybit_ask_price",
        "okx_bid_price",
        "okx_ask_price",
    ]
    best_ts: dict[tuple[str, int], int] = {}
    best_book: dict[tuple[str, int], Book] = {}
    for path, file_keys in by_file.items():
        table = pq.read_table(path, columns=cols)
        _merge_books_from_table(
            table,
            file_keys,
            lookback_ms=lookback_ms,
            best_ts=best_ts,
            best_book=best_book,
        )

    if books_1hz_dir is not None:
        missing = wanted - set(best_book.keys())
        if missing:
            filled = load_last_books_1hz(
                books_1hz_dir, missing, lookback_ms=lookback_ms
            )
            best_book.update(filled)
    return best_book


def write_steps_csv(path: Path, rows: Sequence[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "window",
        "label",
        "mode",
        "status",
        "coin",
        "side",
        "ts_open",
        "ts_close",
        "notional",
        "potential_pp",
        "cash_bybit_after_open",
        "cash_okx_after_open",
        "cash_bybit_after_close",
        "cash_okx_after_close",
        "cash_bybit",
        "cash_okx",
        "equity_bybit",
        "equity_okx",
        "pnl_bybit",
        "pnl_okx",
        "fee_bybit",
        "fee_okx",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_summary_csv(path: Path, rows: Sequence[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "window",
        "label",
        "theta_open",
        "p50_open",
        "min_profit_pp",
        "min_theta_close",
        "mode",
        "status",
        "n_policy_closed",
        "n_applied",
        "n_skipped_nonpositive",
        "n_skipped_hole",
        "n_price_mismatch",
        "n_not_applied",
        "n_negative_rounds",
        "n_open_end",
        "final_bybit",
        "final_okx",
        "min_bybit",
        "min_okx",
        "policy_sum_potential_pp",
        "complete",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_balance_html(
    path: Path,
    *,
    charts: Sequence[tuple[str, list[tuple[float, float]]]],
    summary_rows: Sequence[dict],
    notes: Sequence[str],
) -> None:
    """Cash paths for the frozen cell and a neighbour table.

    ``charts`` entries are ``(title, [(bybit, okx), ...])`` including the
    starting cash as the first point. ``potential_pp`` is not drawn as dollars.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    note_html = "".join(f"<p class='note'>{html.escape(n)}</p>" for n in notes)
    chart_html = "\n".join(_chart_block(title, series) for title, series in charts)
    body_rows = "\n".join(_summary_tr(row) for row in summary_rows)
    if not body_rows:
        body_rows = "<tr><td colspan='14'><em>no rows</em></td></tr>"
    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Bybit and OKX cash ledger</title>
<style>
  body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 24px; color: #111; max-width: 1200px; }}
  h1 {{ font-size: 1.4rem; }}
  h2 {{ font-size: 1.05rem; margin-top: 1.6rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.78rem; }}
  th, td {{ border-bottom: 1px solid #eee; padding: 4px 6px; text-align: left; }}
  th {{ background: #f6f6f6; }}
  .note {{ color: #333; font-size: 0.9rem; line-height: 1.45; }}
  .warn {{ background: #fff8e6; border: 1px solid #e6d08a; padding: 10px 12px; }}
  svg {{ max-width: 100%; height: auto; }}
</style>
</head>
<body>
<h1>Bybit and OKX cash, frozen gear-2.2 policy</h1>
<div class="warn note">
This is a perp-wallet ledger on the 1 Hz last tick. It is not a profitability
claim and not live. Policy gates are unchanged, including
<code>fee_round_trip_pp = 0.30</code>. Ledger taker fees are Bybit 0.10% and
OKX 0.05% on each fill. <code>potential_pp</code> is listed in the CSV and is
not added into these balances.
</div>
{note_html}
{chart_html}
<h2>One-step neighbours</h2>
<p class="note">Each row moves one frozen knob. <code>p50_open=0.50</code> is the known occupancy cliff. A higher cash there is not a better policy.</p>
<table>
<thead><tr>
<th>window</th><th>label</th><th>mode</th><th>status</th>
<th>applied</th><th>neg rounds</th><th>skipped</th><th>holes</th><th>mismatch</th>
<th>final Bybit</th><th>final OKX</th><th>min Bybit</th><th>min OKX</th>
<th>policy sum pp</th>
</tr></thead>
<tbody>
{body_rows}
</tbody>
</table>
</body>
</html>
"""
    path.write_text(doc, encoding="utf-8")


def _chart_block(title: str, series: Sequence[tuple[float, float]]) -> str:
    return f"<h2>{html.escape(title)}</h2>\n{_line_svg(series, title)}"


def _line_svg(series: Sequence[tuple[float, float]], title: str) -> str:
    if len(series) < 2:
        return f"<p class='note'><em>No cash path for {html.escape(title)}</em></p>"
    width, height = 1040, 240
    left, right, bottom, top = 56, 16, 36, 28
    inner_w = width - left - right
    inner_h = height - top - bottom
    values = [v for pair in series for v in pair]
    ymin = min(values)
    ymax = max(values)
    if ymax == ymin:
        ymax = ymin + 1.0
    n = len(series)

    def xy(i: int, value: float) -> tuple[float, float]:
        x = left + (inner_w * i / (n - 1))
        y = top + inner_h * (1.0 - (value - ymin) / (ymax - ymin))
        return x, y

    bybit = " ".join(f"{xy(i, pair[0])[0]:.1f},{xy(i, pair[0])[1]:.1f}" for i, pair in enumerate(series))
    okx = " ".join(f"{xy(i, pair[1])[0]:.1f},{xy(i, pair[1])[1]:.1f}" for i, pair in enumerate(series))
    ticks = []
    for frac in (0.0, 0.5, 1.0):
        val = ymin + (ymax - ymin) * frac
        y = top + inner_h * (1.0 - frac)
        ticks.append(
            f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end" font-size="10" fill="#444">{val:.1f}</text>'
        )
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">',
            f'<text x="{left}" y="16" font-size="12" font-family="sans-serif">{html.escape(title)}</text>',
            f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + inner_h}" stroke="#ccc"/>',
            f'<line x1="{left}" y1="{top + inner_h}" x2="{width - right}" y2="{top + inner_h}" stroke="#ccc"/>',
            *ticks,
            f'<polyline fill="none" stroke="#4c6ef5" stroke-width="2" points="{bybit}"/>',
            f'<polyline fill="none" stroke="#c92a2a" stroke-width="2" points="{okx}"/>',
            '<text x="280" y="16" font-size="11" fill="#4c6ef5" font-family="sans-serif">Bybit cash</text>',
            '<text x="380" y="16" font-size="11" fill="#c92a2a" font-family="sans-serif">OKX cash</text>',
            "</svg>",
        ]
    )


def _summary_tr(row: dict) -> str:
    cells = [
        row.get("window", ""),
        row.get("label", ""),
        row.get("mode", ""),
        row.get("status", ""),
        row.get("n_applied", ""),
        row.get("n_negative_rounds", ""),
        row.get("n_skipped_nonpositive", ""),
        row.get("n_skipped_hole", ""),
        row.get("n_price_mismatch", ""),
        _fmt_num(row.get("final_bybit")),
        _fmt_num(row.get("final_okx")),
        _fmt_num(row.get("min_bybit")),
        _fmt_num(row.get("min_okx")),
        _fmt_num(row.get("policy_sum_potential_pp")),
    ]
    return "<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in cells) + "</tr>"


def _fmt_num(value: object) -> str:
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return ""
    return f"{number:.4f}"


def cash_path(result: LedgerResult, *, start_bybit: float = START_CASH, start_okx: float = START_CASH) -> list[tuple[float, float]]:
    """Start cash, then cash after each step that changed or marked a wallet."""
    path = [(start_bybit, start_okx)]
    for step in result.steps:
        if step.status in ("closed", "open"):
            path.append((step.cash_bybit, step.cash_okx))
    return path


def mark_open_equity(
    side: str,
    open_book: Book,
    mark_book: Book,
    notional: float,
    cash_bybit: float,
    cash_okx: float,
) -> Optional[tuple[float, float]]:
    """Realized cash after open fees plus unrealized leg PnL at close-side marks.

    Long marks Bybit at ask / OKX at bid; short marks Bybit at bid / OKX at ask.
    Open fees stay in ``cash_*``; exit fees are not charged again. Returns
    ``None`` when prices are not usable.
    """
    if notional <= 0.0 or book_is_missing(open_book) or book_is_missing(mark_book):
        return None
    entry = _entry_exit(side, open_book, mark_book)
    if entry is None:
        return None
    bybit_entry, bybit_exit, okx_entry, okx_exit, bybit_dir, okx_dir = entry
    qty_bybit = notional / bybit_entry
    qty_okx = notional / okx_entry
    unreal_bybit = _leg_pnl(bybit_dir, qty_bybit, bybit_entry, bybit_exit)
    unreal_okx = _leg_pnl(okx_dir, qty_okx, okx_entry, okx_exit)
    return cash_bybit + unreal_bybit, cash_okx + unreal_okx


BUCKET_5M_S = 300


def hour_mark_ts(hour_start_s: int) -> int:
    """Last UTC second of the hour that starts at ``hour_start_s`` (xx:59:59)."""
    return int(hour_start_s) + 3599


def bucket_5m_mark_ts(bucket_start_s: int) -> int:
    """Last UTC second of the 5-minute bucket (xx:04:59, xx:09:59, …)."""
    return int(bucket_start_s) + BUCKET_5M_S - 1


def iter_hour_starts(first_hour_s: int, last_hour_s: int) -> list[int]:
    """Inclusive UTC hour-start seconds from ``first_hour_s`` through ``last_hour_s``."""
    return _iter_bucket_starts(first_hour_s, last_hour_s, 3600)


def iter_bucket_5m_starts(first_s: int, last_s: int) -> list[int]:
    """Inclusive UTC 5-minute bucket starts from ``first_s`` through ``last_s``."""
    return _iter_bucket_starts(first_s, last_s, BUCKET_5M_S)


def _iter_bucket_starts(first_s: int, last_s: int, bucket_s: int) -> list[int]:
    if last_s < first_s:
        return []
    out: list[int] = []
    t = int(first_s)
    end = int(last_s)
    step = int(bucket_s)
    while t <= end:
        out.append(t)
        t += step
    return out


def align_bucket_5m_start(ts_s: int) -> int:
    """UTC 5-minute bucket start containing ``ts_s`` (floor to ``BUCKET_5M_S``)."""
    t = int(ts_s)
    return t - (t % BUCKET_5M_S)


def step_open_at(step: LedgerStep, ts_mark: int) -> bool:
    """True when ``step`` still has an open leg at ``ts_mark`` (before close)."""
    if step.ts_open is None or step.notional is None:
        return False
    if int(step.ts_open) > ts_mark:
        return False
    if step.status == "open":
        # Open-at-end: ts_close on the step is an mtm stamp, not a close fill.
        return True
    if step.status != "closed" or step.ts_close is None:
        return False
    return ts_mark < int(step.ts_close)


def _flat_cash_at(
    steps: Sequence[LedgerStep],
    ts_mark: int,
    *,
    start_bybit: float = START_CASH,
    start_okx: float = START_CASH,
) -> tuple[float, float]:
    """Post-fill realized cash when flat at ``ts_mark`` (after equalize when used)."""
    cash_b, cash_o = float(start_bybit), float(start_okx)
    for step in steps:
        if step.status == "closed" and step.ts_close is not None and int(step.ts_close) <= ts_mark:
            cash_b = float(step.cash_bybit)
            cash_o = float(step.cash_okx)
    return cash_b, cash_o


def _equity_series_at(
    steps: Sequence[LedgerStep],
    books: dict[tuple[str, int], Book],
    bucket_starts: Sequence[int],
    mark_ts_fn,
    *,
    start_bybit: float = START_CASH,
    start_okx: float = START_CASH,
) -> list[Optional[tuple[float, float]]]:
    """One equity point per bucket; ``mark_ts_fn(bucket_start)`` is the sample second.

    Flat buckets use the last realized cash at or before the mark (post-close and
    post-equalize when applicable). Buckets inside an open leg use
    ``cash_after_open + unrealized`` at close-side L1. A missing book is a gap
    (``None``); prices are not invented and ``potential_pp`` is not used.
    Flat buckets do not require a book lookup.
    """
    applied = [s for s in steps if s.status in ("closed", "open") and s.notional is not None]
    series: list[Optional[tuple[float, float]]] = []
    for bucket_start in bucket_starts:
        ts_mark = int(mark_ts_fn(int(bucket_start)))
        open_step: Optional[LedgerStep] = None
        for step in applied:
            if step_open_at(step, ts_mark):
                open_step = step
                break
        if open_step is None:
            series.append(
                _flat_cash_at(
                    applied,
                    ts_mark,
                    start_bybit=start_bybit,
                    start_okx=start_okx,
                )
            )
            continue
        assert open_step.ts_open is not None and open_step.notional is not None
        assert open_step.cash_bybit_after_open is not None
        assert open_step.cash_okx_after_open is not None
        coin = str(open_step.coin).upper()
        open_book = books.get((coin, int(open_step.ts_open)))
        mark_book = books.get((coin, ts_mark))
        if open_book is None or mark_book is None:
            series.append(None)
            continue
        marked = mark_open_equity(
            open_step.side,
            open_book,
            mark_book,
            float(open_step.notional),
            float(open_step.cash_bybit_after_open),
            float(open_step.cash_okx_after_open),
        )
        series.append(marked)
    return series


def _book_keys_at(
    steps: Sequence[LedgerStep],
    bucket_starts: Sequence[int],
    mark_ts_fn,
) -> set[tuple[str, int]]:
    """``(COIN, ts_s)`` keys for open fills + marks while a position is open."""
    applied = [s for s in steps if s.status in ("closed", "open") and s.notional is not None]
    keys: set[tuple[str, int]] = set()
    for step in applied:
        if step.ts_open is None:
            continue
        keys.add((str(step.coin).upper(), int(step.ts_open)))
        if step.status == "closed" and step.ts_close is not None:
            keys.add((str(step.coin).upper(), int(step.ts_close)))
    for bucket_start in bucket_starts:
        ts_mark = int(mark_ts_fn(int(bucket_start)))
        for step in applied:
            if step_open_at(step, ts_mark):
                keys.add((str(step.coin).upper(), ts_mark))
                break
    return keys


def _coverage_counts_at(
    steps: Sequence[LedgerStep],
    bucket_starts: Sequence[int],
    points: Sequence[Optional[tuple[float, float]]],
    mark_ts_fn,
) -> tuple[int, int, int]:
    """``(n_marked, n_gap, n_flat)`` for one sampled series."""
    applied = [s for s in steps if s.status in ("closed", "open") and s.notional is not None]
    n_marked = n_gap = n_flat = 0
    for bucket_start, pt in zip(bucket_starts, points):
        ts_mark = int(mark_ts_fn(int(bucket_start)))
        in_pos = any(step_open_at(s, ts_mark) for s in applied)
        if pt is None:
            n_gap += 1
            continue
        if in_pos:
            n_marked += 1
        else:
            n_flat += 1
    return n_marked, n_gap, n_flat


def hourly_equity_series(
    steps: Sequence[LedgerStep],
    books: dict[tuple[str, int], Book],
    hour_starts: Sequence[int],
    *,
    start_bybit: float = START_CASH,
    start_okx: float = START_CASH,
) -> list[Optional[tuple[float, float]]]:
    """One equity point per UTC hour (mark at hour-end second)."""
    return _equity_series_at(
        steps,
        books,
        hour_starts,
        hour_mark_ts,
        start_bybit=start_bybit,
        start_okx=start_okx,
    )


def _closed_approx_usd(
    step: LedgerStep,
    books: dict[tuple[str, int], Book],
) -> Optional[float]:
    """Dollar mark of one closed round: ``N * (spread_open + spread_close - 0.30) / 100``.

    Prefers the open and close L1 books. Falls back to the stored policy
    ``potential_pp`` when a close book is missing.
    """
    if step.status != "closed" or step.ts_close is None or step.notional is None:
        return None
    if step.ts_open is None:
        return None
    coin = str(step.coin).upper()
    open_book = books.get((coin, int(step.ts_open)))
    close_book = books.get((coin, int(step.ts_close)))
    if open_book is not None and close_book is not None:
        pp = approx_potential_pp(step.side, open_book, close_book)
        if pp is not None:
            return approx_usd(float(step.notional), pp)
    if step.potential_pp is not None and math.isfinite(step.potential_pp):
        return approx_usd(float(step.notional), float(step.potential_pp))
    return None


def approx_vs_cash_usd_5m(
    steps: Sequence[LedgerStep],
    books: dict[tuple[str, int], Book],
    bucket_starts: Sequence[int],
    equity: Sequence[Optional[tuple[float, float]]] | None = None,
) -> list[Optional[tuple[float, float]]]:
    """``(approx_usd, true_usd)`` on the 5-minute grid.

    ``true_usd`` is the sum of the two venue equities minus the $100+$100 start.
    ``approx_usd`` adds ``N * potential_pp / 100`` for every round already closed,
    plus the same formula on the live opposite spread while a leg is open.
    A missing mark book is a gap (``None``).
    """
    if equity is None:
        equity = equity_series_5m(steps, books, bucket_starts)
    applied = [s for s in steps if s.status in ("closed", "open") and s.notional is not None]
    closed: list[tuple[int, float]] = []
    for step in applied:
        if step.status != "closed" or step.ts_close is None:
            continue
        usd = _closed_approx_usd(step, books)
        if usd is None:
            continue
        closed.append((int(step.ts_close), usd))
    closed.sort(key=lambda item: item[0])

    out: list[Optional[tuple[float, float]]] = []
    cursor = 0
    realized = 0.0
    for bucket_start, eq in zip(bucket_starts, equity):
        ts_mark = bucket_5m_mark_ts(int(bucket_start))
        while cursor < len(closed) and closed[cursor][0] <= ts_mark:
            realized += closed[cursor][1]
            cursor += 1
        if eq is None:
            out.append(None)
            continue
        true_usd = float(eq[0]) + float(eq[1]) - 2.0 * START_CASH
        open_step: Optional[LedgerStep] = None
        for step in applied:
            if step_open_at(step, ts_mark):
                open_step = step
                break
        extra = 0.0
        if open_step is not None:
            assert open_step.ts_open is not None and open_step.notional is not None
            coin = str(open_step.coin).upper()
            open_book = books.get((coin, int(open_step.ts_open)))
            mark_book = books.get((coin, ts_mark))
            if open_book is None or mark_book is None:
                out.append(None)
                continue
            pp = approx_potential_pp(open_step.side, open_book, mark_book)
            if pp is None:
                out.append(None)
                continue
            extra = approx_usd(float(open_step.notional), pp)
        out.append((realized + extra, true_usd))
    return out


def equity_series_5m(
    steps: Sequence[LedgerStep],
    books: dict[tuple[str, int], Book],
    bucket_starts: Sequence[int],
    *,
    start_bybit: float = START_CASH,
    start_okx: float = START_CASH,
) -> list[Optional[tuple[float, float]]]:
    """One equity point per UTC 5-minute bucket (mark at xx:04:59, …)."""
    return _equity_series_at(
        steps,
        books,
        bucket_starts,
        bucket_5m_mark_ts,
        start_bybit=start_bybit,
        start_okx=start_okx,
    )


def hourly_book_keys(
    steps: Sequence[LedgerStep],
    hour_starts: Sequence[int],
) -> set[tuple[str, int]]:
    """``(COIN, ts_s)`` keys needed for ``hourly_equity_series`` (open + marks)."""
    return _book_keys_at(steps, hour_starts, hour_mark_ts)


def book_keys_5m(
    steps: Sequence[LedgerStep],
    bucket_starts: Sequence[int],
) -> set[tuple[str, int]]:
    """``(COIN, ts_s)`` keys needed for ``equity_series_5m`` (open + marks)."""
    return _book_keys_at(steps, bucket_starts, bucket_5m_mark_ts)


def hourly_coverage_counts(
    steps: Sequence[LedgerStep],
    hour_starts: Sequence[int],
    points: Sequence[Optional[tuple[float, float]]],
) -> tuple[int, int, int]:
    """``(n_marked, n_gap, n_flat)`` for one hourly series."""
    return _coverage_counts_at(steps, hour_starts, points, hour_mark_ts)


def coverage_counts_5m(
    steps: Sequence[LedgerStep],
    bucket_starts: Sequence[int],
    points: Sequence[Optional[tuple[float, float]]],
) -> tuple[int, int, int]:
    """``(n_marked, n_gap, n_flat)`` for one 5-minute series."""
    return _coverage_counts_at(steps, bucket_starts, points, bucket_5m_mark_ts)


def write_hourly_html(
    path: Path,
    *,
    mode_series: Sequence[tuple[str, Sequence[int], Sequence[Optional[tuple[float, float]]]]],
    notes: Sequence[str],
    first_hour_s: int,
    last_hour_s: int,
    gap_counts: Sequence[tuple[str, int, int, int]] | None = None,
) -> None:
    """Write the UTC-hourly Bybit/OKX equity monitoring HTML.

    Each ``mode_series`` entry is ``(mode, hour_starts, points)`` where a point
    is ``(bybit, okx)`` or ``None`` (gap). Gaps break the polyline so a missing
    book does not draw a flat rectangle to a stale extreme.
    """
    _write_equity_monitor_html(
        path,
        mode_series=mode_series,
        notes=notes,
        first_s=first_hour_s,
        last_s=last_hour_s,
        gap_counts=gap_counts,
        title="Gear 2.2 — почасовой equity (наблюдение)",
        heading="Почасовой equity Bybit / OKX — наблюдение, не прибыль",
        warn=(
            "Это <strong>observation accounting</strong> по frozen gear&nbsp;2.2 cash-ledger за окно "
            "<strong>2026-08-10 … 2026-09-13</strong> (UTC). Старт cash $100 / $100. "
            "Один пункт на UTC-час: метка часа — начало часа, цена стакана — на <strong>последней "
            "секунде часа</strong> (xx:59:59). Внутри открытой позиции equity = "
            "realized cash после open-fee + unrealized PnL по close-side L1. На часе закрытия "
            "(после close и equalize) точка = post-close cash. Пропуск книги — разрыв линии. "
            "Это не claim о прибыльности и не live-ready. Политика не перенастраивалась."
        ),
        meta_label="час",
        x_axis_label="час (UTC)",
    )


def write_balance_5m_html(
    path: Path,
    *,
    mode_series: Sequence[tuple[str, Sequence[int], Sequence[Optional[tuple[float, float]]]]],
    notes: Sequence[str],
    first_s: int,
    last_s: int,
    gap_counts: Sequence[tuple[str, int, int, int]] | None = None,
    profit_series: Sequence[
        tuple[str, Sequence[int], Sequence[Optional[tuple[float, float]]]]
    ]
    | None = None,
) -> None:
    """Write the UTC 5-minute Bybit/OKX equity monitoring HTML.

    ``profit_series`` points are ``(approx_usd, true_usd)`` on the same grid.
    """
    _write_equity_monitor_html(
        path,
        mode_series=mode_series,
        notes=notes,
        first_s=first_s,
        last_s=last_s,
        gap_counts=gap_counts,
        profit_by_mode={mode: (starts, points) for mode, starts, points in (profit_series or ())},
        title="Gear 2.2 — equity каждые 5 минут (наблюдение)",
        heading="Equity Bybit / OKX каждые 5 минут — наблюдение, не прибыль",
        warn=(
            "Это <strong>observation accounting</strong> по frozen gear&nbsp;2.2 cash-ledger с "
            "<strong>2026-08-10</strong> и до последнего дня hive (UTC). Старт cash $100 / $100. "
            "Один пункт на UTC 5-минутный бакет: метка оси — начало бакета, цена стакана — на "
            "<strong>последней секунде бакета</strong> (xx:04:59, xx:09:59, …). "
            "Внутри открытой позиции equity = realized cash после open-fee + unrealized PnL "
            "по close-side L1. На бакете закрытия (после close и equalize) точка = post-close cash. "
            "Плоские бакеты — carry-forward realized cash без lookup книги. "
            "Пропуск книги при открытой позиции — разрыв линии. "
            "Ниже equity — прибыль в долларах: истина (сумма двух кошельков минус $200) и приближение "
            "<code>N × (spread_open + spread_opposite(t) − 0.15 − 0.15) / 100</code>. "
            "Третий график — приближение минус истина. "
            "Это не claim о прибыльности и не live-ready. Политика не перенастраивалась."
        ),
        meta_label="5 мин",
        x_axis_label="5 мин (UTC)",
    )


def _write_equity_monitor_html(
    path: Path,
    *,
    mode_series: Sequence[tuple[str, Sequence[int], Sequence[Optional[tuple[float, float]]]]],
    notes: Sequence[str],
    first_s: int,
    last_s: int,
    gap_counts: Sequence[tuple[str, int, int, int]] | None,
    title: str,
    heading: str,
    warn: str,
    meta_label: str,
    x_axis_label: str,
    profit_by_mode: dict[str, tuple[Sequence[int], Sequence[Optional[tuple[float, float]]]]]
    | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    note_html = "".join(f"<p class='note'>{html.escape(n)}</p>" for n in notes)
    gap_html = ""
    if gap_counts:
        items = "".join(
            (
                f"<li><strong>{html.escape(mode)}</strong>: "
                f"marked {n_marked}, gap {n_gap}, flat {n_flat}</li>"
            )
            for mode, n_marked, n_gap, n_flat in gap_counts
        )
        gap_html = f"<ul>{items}</ul>"
    chart_html = "\n".join(
        _equity_chart_block(
            mode,
            starts,
            points,
            x_axis_label=x_axis_label,
            profit=None if profit_by_mode is None else profit_by_mode.get(mode),
        )
        for mode, starts, points in mode_series
    )
    first_lbl = datetime.fromtimestamp(first_s, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )
    last_lbl = datetime.fromtimestamp(last_s, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )
    n_pts = len(mode_series[0][1]) if mode_series else 0
    doc = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{html.escape(title)}</title>
<style>
  body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 24px; color: #111; max-width: 1160px; }}
  h1 {{ font-size: 1.35rem; margin-bottom: 0.4rem; }}
  h2 {{ font-size: 1.05rem; margin: 1.4rem 0 0.5rem; }}
  .warn {{ background: #fff8e6; border: 1px solid #e6d08a; padding: 10px 12px; line-height: 1.45; }}
  .note {{ color: #333; font-size: 0.92rem; line-height: 1.45; }}
  .meta {{ font-size: 0.9rem; color: #444; }}
  svg {{ max-width: 100%; height: auto; display: block; border: 1px solid #eee; background: #fff; }}
  ul {{ font-size: 0.9rem; }}
</style>
</head>
<body>
<h1>{heading}</h1>
<div class="warn note">
{warn}
</div>
<p class="meta">
Точек: <strong>{n_pts}</strong> · первый {html.escape(meta_label)}: <strong>{html.escape(first_lbl)}</strong> ·
последний {html.escape(meta_label)}: <strong>{html.escape(last_lbl)}</strong> ·
источник шагов: <code>frozen_steps.csv</code> · L1: lean_ticks + gear22_books_1hz.
</p>
{note_html}
{gap_html}
{chart_html}
</body>
</html>
"""
    path.write_text(doc, encoding="utf-8")


def _equity_chart_block(
    mode: str,
    bucket_starts: Sequence[int],
    points: Sequence[Optional[tuple[float, float]]],
    *,
    x_axis_label: str,
    profit: tuple[Sequence[int], Sequence[Optional[tuple[float, float]]]] | None = None,
) -> str:
    title = f"режим: {mode}"
    finite = [p for p in points if p is not None]
    if not finite:
        return (
            f"<section><h2>{html.escape(title)}</h2>"
            f"<p class='note'><em>Нет точек для {html.escape(mode)}</em></p></section>"
        )
    last = next((p for p in reversed(points) if p is not None), None)
    end_note = ""
    if last is not None:
        end_note = f"конец ряда: Bybit {last[0]:.2f} · OKX {last[1]:.2f}"
    profit_html = ""
    if profit is not None:
        p_starts, p_points = profit
        finite = [p for p in p_points if p is not None]
        if finite:
            last_p = next((p for p in reversed(p_points) if p is not None), None)
            p_note = ""
            if last_p is not None:
                p_note = (
                    f"конец: приближение {last_p[0]:.2f} $ · истина {last_p[1]:.2f} $ · "
                    f"разница {last_p[0] - last_p[1]:.2f} $"
                )
            diffs: list[Optional[tuple[float, float]]] = [
                None if p is None else (p[0] - p[1], p[0] - p[1]) for p in p_points
            ]
            profit_html = (
                f"<h3>прибыль: приближение и истина</h3>\n"
                f"{_equity_line_svg(title + ' прибыль', p_starts, p_points, p_note, x_axis_label=x_axis_label, legends=(('приближение', '#2b8a3e'), ('истина', '#111')))}\n"
                f"<h3>расхождение: приближение − истина</h3>\n"
                f"{_equity_line_svg(title + ' расхождение', p_starts, diffs, '', x_axis_label=x_axis_label, legends=(('расхождение', '#e67700'), ('', '#e67700')), draw_second=False)}\n"
            )
    return (
        f"<section>\n<h2>{html.escape(title)}</h2>\n"
        f"{_equity_line_svg(title, bucket_starts, points, end_note, x_axis_label=x_axis_label)}\n"
        f"{profit_html}"
        f"</section>"
    )


def _hourly_chart_block(
    mode: str,
    hour_starts: Sequence[int],
    points: Sequence[Optional[tuple[float, float]]],
) -> str:
    return _equity_chart_block(mode, hour_starts, points, x_axis_label="час (UTC)")


def _equity_line_svg(
    title: str,
    bucket_starts: Sequence[int],
    points: Sequence[Optional[tuple[float, float]]],
    end_note: str,
    *,
    x_axis_label: str,
    legends: tuple[tuple[str, str], tuple[str, str]] = (
        ("Bybit", "#4c6ef5"),
        ("OKX", "#c92a2a"),
    ),
    draw_second: bool = True,
) -> str:
    width, height = 1100, 320
    left, right, bottom, top = 64, 24, 52, 36
    inner_w = width - left - right
    inner_h = height - top - bottom
    values = [v for p in points if p is not None for v in p]
    ymin = min(values)
    ymax = max(values)
    if ymax == ymin:
        ymax = ymin + 1.0
    n = len(points)
    if n < 2:
        return f"<p class='note'><em>No path for {html.escape(title)}</em></p>"

    def xy(i: int, value: float) -> tuple[float, float]:
        x = left + (inner_w * i / (n - 1))
        y = top + inner_h * (1.0 - (value - ymin) / (ymax - ymin))
        return x, y

    def segments(which: int) -> list[str]:
        out: list[str] = []
        buf: list[str] = []
        for i, pair in enumerate(points):
            if pair is None:
                if len(buf) >= 2:
                    out.append(" ".join(buf))
                buf = []
                continue
            x, y = xy(i, pair[which])
            buf.append(f"{x:.2f},{y:.2f}")
        if len(buf) >= 2:
            out.append(" ".join(buf))
        elif len(buf) == 1:
            # Single point: draw a degenerate segment so it remains visible.
            out.append(buf[0] + " " + buf[0])
        return out

    first_polylines = "".join(
        f'<polyline fill="none" stroke="{legends[0][1]}" stroke-width="1.8" points="{seg}"/>'
        for seg in segments(0)
    )
    second_polylines = (
        "".join(
            f'<polyline fill="none" stroke="{legends[1][1]}" stroke-width="1.8" points="{seg}"/>'
            for seg in segments(1)
        )
        if draw_second
        else ""
    )
    ticks = []
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        val = ymin + (ymax - ymin) * frac
        y = top + inner_h * (1.0 - frac)
        ticks.append(
            f'<line x1="{left}" y1="{y:.2f}" x2="{width - right}" y2="{y:.2f}" stroke="#eee"/>'
        )
        ticks.append(
            f'<text x="{left - 8}" y="{y + 4:.2f}" text-anchor="end" font-size="11" fill="#444">{val:.1f}</text>'
        )
    x_labels = []
    for frac in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
        i = int(round(frac * (n - 1)))
        x = left + (inner_w * i / (n - 1))
        lbl = datetime.fromtimestamp(bucket_starts[i], tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M"
        )
        x_labels.append(
            f'<text x="{x:.2f}" y="{height - 14}" text-anchor="middle" font-size="10" fill="#444">{lbl}</text>'
        )
    end_text = (
        f'<text x="{width - right}" y="20" text-anchor="end" font-size="11" fill="#555" '
        f'font-family="ui-sans-serif, system-ui, sans-serif">{html.escape(end_note)}</text>'
        if end_note
        else ""
    )
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}" role="img" aria-label="{html.escape(title)}">',
            f'<text x="{left}" y="20" font-size="14" font-family="ui-sans-serif, system-ui, sans-serif" fill="#111">{html.escape(title)}</text>',
            end_text,
            f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + inner_h}" stroke="#bbb"/>',
            f'<line x1="{left}" y1="{top + inner_h}" x2="{width - right}" y2="{top + inner_h}" stroke="#bbb"/>',
            *ticks,
            first_polylines,
            second_polylines,
            f'<text x="{left + 8}" y="{top + 14}" font-size="12" fill="{legends[0][1]}" font-family="ui-sans-serif, system-ui, sans-serif">{html.escape(legends[0][0])}</text>',
            (
                f'<text x="{left + 140}" y="{top + 14}" font-size="12" fill="{legends[1][1]}" font-family="ui-sans-serif, system-ui, sans-serif">{html.escape(legends[1][0])}</text>'
                if draw_second and legends[1][0]
                else ""
            ),
            f'<text x="{(left + width - right) / 2:.1f}" y="{height - 2}" text-anchor="middle" font-size="11" fill="#333" font-family="ui-sans-serif, system-ui, sans-serif">{html.escape(x_axis_label)}</text>',
            *x_labels,
            "</svg>",
        ]
    )


def _hourly_line_svg(
    title: str,
    hour_starts: Sequence[int],
    points: Sequence[Optional[tuple[float, float]]],
    end_note: str,
) -> str:
    return _equity_line_svg(
        title, hour_starts, points, end_note, x_axis_label="час (UTC)"
    )


def steps_from_frozen_csv(
    path: Path | str,
    *,
    mode: Mode,
    window: str | None = "all",
    label: str = "frozen",
) -> list[LedgerStep]:
    """Load ``LedgerStep`` rows for one mode from ``frozen_steps.csv``."""

    def _opt_float(raw: str) -> Optional[float]:
        if raw is None or raw == "":
            return None
        return float(raw)

    def _opt_int(raw: str) -> Optional[int]:
        if raw is None or raw == "":
            return None
        return int(float(raw))

    out: list[LedgerStep] = []
    with Path(path).open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("mode") != mode:
                continue
            if label and row.get("label") != label:
                continue
            if window is not None and row.get("window") != window:
                continue
            out.append(
                LedgerStep(
                    status=str(row["status"]),
                    coin=str(row["coin"]),
                    side=str(row["side"]),
                    ts_open=_opt_int(row.get("ts_open") or ""),
                    ts_close=_opt_int(row.get("ts_close") or ""),
                    notional=_opt_float(row.get("notional") or ""),
                    potential_pp=_opt_float(row.get("potential_pp") or ""),
                    cash_bybit_after_open=_opt_float(row.get("cash_bybit_after_open") or ""),
                    cash_okx_after_open=_opt_float(row.get("cash_okx_after_open") or ""),
                    cash_bybit_after_close=_opt_float(row.get("cash_bybit_after_close") or ""),
                    cash_okx_after_close=_opt_float(row.get("cash_okx_after_close") or ""),
                    cash_bybit=float(row["cash_bybit"]),
                    cash_okx=float(row["cash_okx"]),
                    equity_bybit=_opt_float(row.get("equity_bybit") or ""),
                    equity_okx=_opt_float(row.get("equity_okx") or ""),
                    pnl_bybit=_opt_float(row.get("pnl_bybit") or ""),
                    pnl_okx=_opt_float(row.get("pnl_okx") or ""),
                    fee_bybit=_opt_float(row.get("fee_bybit") or ""),
                    fee_okx=_opt_float(row.get("fee_okx") or ""),
                )
            )
    return out


def step_row(window: str, label: str, mode: str, step: LedgerStep) -> dict:
    return {
        "window": window,
        "label": label,
        "mode": mode,
        "status": step.status,
        "coin": step.coin,
        "side": step.side,
        "ts_open": step.ts_open,
        "ts_close": step.ts_close,
        "notional": step.notional,
        "potential_pp": step.potential_pp,
        "cash_bybit_after_open": step.cash_bybit_after_open,
        "cash_okx_after_open": step.cash_okx_after_open,
        "cash_bybit_after_close": step.cash_bybit_after_close,
        "cash_okx_after_close": step.cash_okx_after_close,
        "cash_bybit": step.cash_bybit,
        "cash_okx": step.cash_okx,
        "equity_bybit": step.equity_bybit,
        "equity_okx": step.equity_okx,
        "pnl_bybit": step.pnl_bybit,
        "pnl_okx": step.pnl_okx,
        "fee_bybit": step.fee_bybit,
        "fee_okx": step.fee_okx,
    }


def summary_row(
    *,
    window: str,
    label: str,
    params: PolicyParams,
    mode: str,
    status: str,
    result: Optional[LedgerResult],
    n_policy_closed: int,
    policy_sum_potential_pp: float,
) -> dict:
    return {
        "window": window,
        "label": label,
        "theta_open": params.theta_open,
        "p50_open": params.p50_open,
        "min_profit_pp": params.min_profit_pp,
        "min_theta_close": params.min_theta_close,
        "mode": mode,
        "status": status,
        "n_policy_closed": n_policy_closed,
        "n_applied": "" if result is None else result.n_applied,
        "n_skipped_nonpositive": "" if result is None else result.n_skipped_nonpositive,
        "n_skipped_hole": "" if result is None else result.n_skipped_hole,
        "n_price_mismatch": "" if result is None else result.n_price_mismatch,
        "n_not_applied": "" if result is None else result.n_not_applied,
        "n_negative_rounds": "" if result is None else result.n_negative_rounds,
        "n_open_end": "" if result is None else result.n_open_end,
        "final_bybit": "" if result is None else result.cash_bybit,
        "final_okx": "" if result is None else result.cash_okx,
        "min_bybit": "" if result is None else result.min_bybit,
        "min_okx": "" if result is None else result.min_okx,
        "policy_sum_potential_pp": policy_sum_potential_pp,
        "complete": "" if result is None else result.complete,
    }


def tick_coverage(tick_dir: Path) -> Optional[tuple[datetime, datetime]]:
    """UTC ``[start, end)`` covered by ``spread_*.parquet`` names, if any."""
    from research.lean_ticks_io import parse_lean_file_window

    starts: list[int] = []
    ends: list[int] = []
    if not tick_dir.is_dir():
        return None
    for path in tick_dir.glob("spread_*.parquet"):
        win = parse_lean_file_window(path)
        if win is None:
            continue
        starts.append(win[0])
        ends.append(win[1])
    if not starts:
        return None
    return (
        datetime.fromtimestamp(min(starts) / 1000.0, tz=timezone.utc),
        datetime.fromtimestamp(max(ends) / 1000.0, tz=timezone.utc),
    )


_MISSING_BOOK = Book(
    bybit_bid=float("nan"),
    bybit_ask=float("nan"),
    okx_bid=float("nan"),
    okx_ask=float("nan"),
)


def timestamp_keys_for_run(run) -> set[tuple[str, int]]:
    """``(COIN, ts_s)`` pairs needed to price a ``sweep.RunResult`` trade list."""
    keys: set[tuple[str, int]] = set()
    for trade in run.trades:
        keys.add((str(trade.coin).upper(), int(trade.ts_open)))
        keys.add((str(trade.coin).upper(), int(trade.ts_close)))
    if run.open_coin is not None and run.open_ts is not None:
        keys.add((str(run.open_coin).upper(), int(run.open_ts)))
        if run.mtm_ts is not None:
            keys.add((str(run.open_coin).upper(), int(run.mtm_ts)))
    return keys


def store_row_at(store, coin: str, ts_s: int) -> Optional[int]:
    """Index of ``(coin, ts_s)`` in a sweep ``Store``, or ``None``."""
    coin_u = str(coin)
    try:
        code = store.coins.index(coin_u)
    except ValueError:
        coin_u = coin_u.upper()
        try:
            code = next(i for i, c in enumerate(store.coins) if str(c).upper() == coin_u)
        except StopIteration:
            return None
    rows = store.coin_rows[code]
    if rows.shape[0] == 0:
        return None
    ts_coin = store.ts[rows]
    pos = int(np.searchsorted(ts_coin, int(ts_s), side="left"))
    if pos >= ts_coin.shape[0] or int(ts_coin[pos]) != int(ts_s):
        return None
    return int(rows[pos])


def open_fill_exit_pp(store, run) -> tuple[Optional[float], Optional[float]]:
    """Fill and mark exit spreads for a still-open ``RunResult`` position.

    ``RunResult`` does not carry fill/exit spreads; look them up on the Store
    arrays at ``open_ts`` / ``mtm_ts``.
    """
    if run.open_coin is None or run.open_side is None or run.open_ts is None:
        return None, None
    open_row = store_row_at(store, run.open_coin, run.open_ts)
    if open_row is None:
        return None, None
    if run.open_side == "long":
        fill = float(store.spread_long[open_row])
        exit_arr = store.spread_short
    elif run.open_side == "short":
        fill = float(store.spread_short[open_row])
        exit_arr = store.spread_long
    else:
        raise ValueError(f"unknown open side {run.open_side!r}")
    if run.mtm_ts is None:
        return fill, None
    mark_row = store_row_at(store, run.open_coin, run.mtm_ts)
    if mark_row is None:
        return fill, None
    return fill, float(exit_arr[mark_row])


def price_run(
    run,
    books: dict[tuple[str, int], Book],
    *,
    open_fill_pp: Optional[float] = None,
    open_exit_pp: Optional[float] = None,
) -> tuple[list[PricedRound], Optional[PricedOpen]]:
    """Attach L1 books to a trade list and flag holes / spread mismatches.

    Missing books set ``is_hole=True``. Float32 disagreement with stored
    ``spread_last`` sets ``prices_match=False`` without a hole. Callers must
    not substitute ``potential_pp``.
    """
    rounds: list[PricedRound] = []
    for trade in run.trades:
        coin = str(trade.coin).upper()
        open_b = books.get((coin, int(trade.ts_open)), _MISSING_BOOK)
        close_b = books.get((coin, int(trade.ts_close)), _MISSING_BOOK)
        hole = book_is_missing(open_b) or book_is_missing(close_b)
        if hole:
            open_ok = close_ok = False
        else:
            open_ok = book_matches_spread(
                open_b, open_book_side(trade.side), float(trade.fill_spread_pp)
            )
            close_ok = book_matches_spread(
                close_b, close_book_side(trade.side), float(trade.exit_spread_pp)
            )
        rounds.append(
            PricedRound(
                coin=coin,
                side=trade.side,
                ts_open=int(trade.ts_open),
                ts_close=int(trade.ts_close),
                open_book=open_b,
                close_book=close_b,
                potential_pp=float(trade.potential_pp),
                prices_match=bool(open_ok and close_ok),
                is_hole=bool(hole),
            )
        )

    open_leg: Optional[PricedOpen] = None
    if run.open_coin is not None and run.open_side is not None and run.open_ts is not None:
        coin = str(run.open_coin).upper()
        open_b = books.get((coin, int(run.open_ts)), _MISSING_BOOK)
        fill_pp = open_fill_pp
        if fill_pp is None:
            fill_pp = float("nan")
        hole = book_is_missing(open_b)
        if hole:
            open_ok = False
        else:
            open_ok = book_matches_spread(open_b, open_book_side(run.open_side), float(fill_pp))
        mark_book: Optional[Book] = None
        mark_ok = False
        if run.mtm_ts is not None:
            mark_book = books.get((coin, int(run.mtm_ts)), _MISSING_BOOK)
            if open_exit_pp is not None and math.isfinite(float(open_exit_pp)):
                if not book_is_missing(mark_book):
                    mark_ok = book_matches_spread(
                        mark_book, close_book_side(run.open_side), float(open_exit_pp)
                    )
        open_leg = PricedOpen(
            coin=coin,
            side=run.open_side,
            ts_open=int(run.open_ts),
            open_book=open_b,
            mark_book=mark_book,
            mark_ts=None if run.mtm_ts is None else int(run.mtm_ts),
            potential_pp=None if run.mtm_pp is None else float(run.mtm_pp),
            prices_match=bool(open_ok),
            mark_prices_match=bool(mark_ok),
            is_hole=bool(hole),
        )
    return rounds, open_leg


def ledger_status(result: LedgerResult) -> str:
    """Compact path status for CSV/HTML summaries."""
    if result.n_not_applied:
        return "incomplete"
    if not result.complete:
        return "incomplete"
    if result.n_skipped_nonpositive and result.n_applied == 0:
        return "skipped_nonpositive"
    if result.n_skipped_hole or result.n_price_mismatch:
        # Path ran to the end; holes/mismatches were skipped, not freezing.
        return "ok_with_skips"
    return "ok"
