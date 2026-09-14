"""Gear-2.2 observation replay: I/O, clock, K=1 slots. No trading rules.

Trading rules stay in ``policy.decide``. This module sorts ``FeatureSnapshot``
rows by ``(ts_s, coin)``, calls ``decide``, and applies the returned action to
slot state. Until gear 2.5, the live contour is **global K=1**: at most one
open position across all coins (sequential trades).

``slot_mode``:

- ``global`` (default) — one position worldwide. Matches would_send K=1.
  Same-second: at most one open; lexicographic ``coin`` wins. While in
  position, the held coin's row is evaluated for close before any other
  coin can open. If that coin is missing at the timestamp, hold.
- ``per_coin`` — independent K=1 slot per coin. Explicit opt-in only;
  research / gear-2.5-ish. **Not** the live contour: canary-30 independent
  slots is wrong for gear-2 K=1.

Pass-1: no Trade_Lat delay; fill = that second's ``spread_last`` of the opened
side. Do not interpolate NaN. Do not read ticks. Open-at-end is returned as
``ReplayResult.open_positions``, not as a ``ClosedTrade``.
``ReplayResult.to_trades_frame()`` concatenates closed round-trips and still-open
rows (``status`` / ``reason_close="unclosed"`` / null ``ts_close``).
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal, Optional, Sequence, Union

import pandas as pd

from research.gear22_backtest.policy import (
    DummyParams,
    FeatureSnapshot,
    PolicyState,
    Side,
    decide,
    potential_profit_pp,
)

SlotMode = Literal["per_coin", "global"]
DEFAULT_SLOT_MODE: SlotMode = "global"
CoinFilter = Union[str, Sequence[str], None]

SNAPSHOT_COLUMNS: tuple[str, ...] = (
    "ts_s",
    "coin",
    "p50_1m_long",
    "p50_1m_short",
    "floor_long",
    "floor_short",
    "theta_1m_long",
    "theta_1m_short",
    "spread_last_long",
    "spread_last_short",
    "usable_long",
    "usable_short",
)

_GLOBAL_KEY = "__global__"
_DEFAULT_PART = "part-000.parquet"
UNCLOSED_REASON = "unclosed"
TRADE_FRAME_COLUMNS: tuple[str, ...] = (
    "coin",
    "side",
    "ts_open",
    "ts_close",
    "fill_spread_pp",
    "exit_spread_pp",
    "potential_pp",
    "reason_open",
    "reason_close",
    "status",
)


@dataclass(frozen=True)
class ClosedTrade:
    coin: str
    side: Side
    ts_open: int
    ts_close: int
    fill_spread_pp: float
    exit_spread_pp: float  # opposite spread_last at close
    potential_pp: float  # at close; not a PnL claim
    reason_open: str
    reason_close: str


@dataclass(frozen=True)
class OpenPosition:
    """In-slot position still open at end of scan (not a trade)."""

    coin: str
    side: Side
    ts_open: int
    fill_spread_pp: float
    reason_open: str
    last_ts_s: Optional[int] = None
    last_potential_pp: Optional[float] = None
    last_exit_spread_pp: Optional[float] = None


@dataclass(frozen=True)
class ReplayResult:
    closed: list[ClosedTrade]
    open_positions: list[OpenPosition]

    def to_trades_frame(self) -> pd.DataFrame:
        """One table: closed round-trips plus still-open rows.

        Open rows use the ClosedTrade columns with ``ts_close`` null,
        ``exit_spread_pp`` / ``potential_pp`` from last seen (if recorded),
        ``reason_close="unclosed"``, and ``status="open"``. Does not change
        policy or the potential-profit formula.
        """
        rows: list[dict[str, object]] = []
        for trade in self.closed:
            rows.append(
                {
                    "coin": trade.coin,
                    "side": trade.side,
                    "ts_open": trade.ts_open,
                    "ts_close": trade.ts_close,
                    "fill_spread_pp": trade.fill_spread_pp,
                    "exit_spread_pp": trade.exit_spread_pp,
                    "potential_pp": trade.potential_pp,
                    "reason_open": trade.reason_open,
                    "reason_close": trade.reason_close,
                    "status": "closed",
                }
            )
        for pos in self.open_positions:
            rows.append(
                {
                    "coin": pos.coin,
                    "side": pos.side,
                    "ts_open": pos.ts_open,
                    "ts_close": None,
                    "fill_spread_pp": pos.fill_spread_pp,
                    "exit_spread_pp": pos.last_exit_spread_pp,
                    "potential_pp": pos.last_potential_pp,
                    "reason_open": pos.reason_open,
                    "reason_close": UNCLOSED_REASON,
                    "status": "open",
                }
            )
        frame = pd.DataFrame(rows, columns=list(TRADE_FRAME_COLUMNS))
        for col in ("ts_close", "exit_spread_pp", "potential_pp"):
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        return frame


@dataclass
class _Slot:
    state: PolicyState = field(default_factory=PolicyState)
    reason_open: Optional[str] = None
    last_ts_s: Optional[int] = None
    last_potential_pp: Optional[float] = None
    last_exit_spread_pp: Optional[float] = None


def replay_frame(
    df: pd.DataFrame,
    params: DummyParams | None = None,
    slot_mode: SlotMode = DEFAULT_SLOT_MODE,
) -> ReplayResult:
    """Replay an in-memory feature table in time-major clock order.

    Always sorts by ``(ts_s, coin)`` before scanning. A coin-major concat
    (all of A, then all of B) cannot hold A for a whole day then open B
    under ``global``. Same-second tie-break is lexicographic ``coin``.
    """
    if params is None:
        params = DummyParams()
    _check_slot_mode(slot_mode)
    missing = [c for c in SNAPSHOT_COLUMNS if c not in df.columns]
    if missing:
        raise KeyError(f"replay_frame missing columns: {missing}")
    return _replay_rows(_iter_snapshots(_clock_order(df)), params, slot_mode)


def replay_hive(
    hive_root: Path | str,
    *,
    coins: CoinFilter = None,
    dates: Sequence[str] | None = None,
    params: DummyParams | None = None,
    slot_mode: SlotMode = DEFAULT_SLOT_MODE,
    part_name: str = _DEFAULT_PART,
) -> ReplayResult:
    """Read selected ``event_date=*/part-000.parquet`` parts and replay.

    Concatenates named parts then ``replay_frame`` sorts by ``(ts_s, coin)``
    so day/coin concat order is not load-bearing. ``dates=None`` — all
    partitions. Missing named dates raise. Optional ``coins`` filters after
    read (string compare; a bare ``str`` is one coin, not characters).
    One ``replay_frame``: slots persist across days.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    root = Path(hive_root)
    if dates is None:
        paths = _part_paths(root, None, part_name)
        missing: list[str] = []
    else:
        paths = []
        missing = []
        for day in sorted({str(d) for d in dates}):
            found = _part_paths(root, day, part_name)
            if found:
                paths.extend(found)
            else:
                missing.append(day)
    if missing:
        raise FileNotFoundError(
            f"no {part_name} under {hive_root} event_date={missing}"
        )
    if not paths:
        raise FileNotFoundError(f"no {part_name} under {hive_root}")
    tables = [pq.read_table(p, columns=list(SNAPSHOT_COLUMNS)) for p in paths]
    table = tables[0] if len(tables) == 1 else pa.concat_tables(tables)
    df = table.to_pandas()
    wanted = _normalize_coins(coins)
    if wanted:
        df = df.loc[df["coin"].astype(str).isin(wanted)].reset_index(drop=True)
    return replay_frame(df, params, slot_mode)


def replay_path(
    hive_root: Path | str,
    *,
    event_date: str | None = None,
    coins: CoinFilter = None,
    params: DummyParams | None = None,
    slot_mode: SlotMode = DEFAULT_SLOT_MODE,
    part_name: str = _DEFAULT_PART,
) -> ReplayResult:
    """Read ``event_date=*/part-000.parquet`` (by_date hive) and replay.

    Optional ``coins`` filters after read. Concatenates date partitions;
    ``replay_frame`` sorts by ``(ts_s, coin)``.
    """
    dates = None if event_date is None else [event_date]
    return replay_hive(
        hive_root,
        coins=coins,
        dates=dates,
        params=params,
        slot_mode=slot_mode,
        part_name=part_name,
    )


def _normalize_coins(coins: CoinFilter) -> Optional[list[str]]:
    """``None`` → no filter. A ``str`` is one coin (not iterated as characters)."""
    if coins is None:
        return None
    if isinstance(coins, str):
        return [coins]
    return [str(c) for c in coins]


def _replay_rows(
    rows: Iterable[FeatureSnapshot],
    params: DummyParams,
    slot_mode: SlotMode,
) -> ReplayResult:
    slots: dict[str, _Slot] = {}
    trades: list[ClosedTrade] = []
    for group in _iter_timestamp_groups(rows):
        if slot_mode == "global":
            _replay_global_timestamp(group, slots, trades, params)
        else:
            for row in group:
                _replay_one_row(row, slots, trades, params, slot_mode)
    return ReplayResult(
        closed=trades,
        open_positions=_collect_open_positions(slots),
    )


def _iter_timestamp_groups(
    rows: Iterable[FeatureSnapshot],
) -> Iterable[list[FeatureSnapshot]]:
    group: list[FeatureSnapshot] = []
    current_ts: Optional[int] = None
    for row in rows:
        if current_ts is None:
            current_ts = row.ts_s
            group = [row]
            continue
        if row.ts_s != current_ts:
            yield group
            current_ts = row.ts_s
            group = [row]
        else:
            group.append(row)
    if group:
        yield group


def _replay_global_timestamp(
    group: list[FeatureSnapshot],
    slots: dict[str, _Slot],
    trades: list[ClosedTrade],
    params: DummyParams,
) -> None:
    slot = _slot_for(slots, group[0].coin, "global")
    by_coin = {row.coin: row for row in group}
    just_closed: Optional[str] = None
    if slot.state.position_side is not None:
        held = slot.state.held_coin
        held_row = by_coin.get(held) if held is not None else None
        if held_row is None:
            # Missing held coin this second: hold. Do not close on another
            # coin's spreads and do not open a replacement.
            return
        closed = _apply_close_if_any(held_row, slot, params)
        if closed is not None:
            trades.append(closed)
            just_closed = closed.coin
        if slot.state.position_side is not None:
            return
    for row in group:
        if just_closed is not None and row.coin == just_closed:
            continue
        if _apply_open_if_any(row, slot, params):
            break


def _replay_one_row(
    row: FeatureSnapshot,
    slots: dict[str, _Slot],
    trades: list[ClosedTrade],
    params: DummyParams,
    slot_mode: SlotMode,
) -> None:
    slot = _slot_for(slots, row.coin, slot_mode)
    if slot.state.position_side is not None:
        closed = _apply_close_if_any(row, slot, params)
        if closed is not None:
            trades.append(closed)
        return
    _apply_open_if_any(row, slot, params)


def _apply_open_if_any(row: FeatureSnapshot, slot: _Slot, params: DummyParams) -> bool:
    decision = decide(row, slot.state, params)
    if decision.action not in ("open_long", "open_short"):
        return False
    opened = _try_open(row, decision.action)
    if opened is None:
        return False
    slot.state = opened
    slot.reason_open = decision.reason
    slot.last_ts_s = int(row.ts_s)
    slot.last_potential_pp = None
    slot.last_exit_spread_pp = None
    return True


def _apply_close_if_any(
    row: FeatureSnapshot,
    slot: _Slot,
    params: DummyParams,
) -> Optional[ClosedTrade]:
    _touch_held_mark(row, slot, params)
    decision = decide(row, slot.state, params)
    if decision.action != "close":
        return None
    trade = _try_close(row, slot, params, decision.reason)
    if trade is None:
        return None
    slot.state = PolicyState()
    slot.reason_open = None
    slot.last_ts_s = int(row.ts_s)
    slot.last_potential_pp = float(trade.potential_pp)
    slot.last_exit_spread_pp = float(trade.exit_spread_pp)
    return trade


def _touch_held_mark(row: FeatureSnapshot, slot: _Slot, params: DummyParams) -> None:
    slot.last_ts_s = int(row.ts_s)
    slot.last_potential_pp = potential_profit_pp(
        row, slot.state, params.fee_round_trip_pp
    )
    slot.last_exit_spread_pp = _last_exit_spread_pp(row, slot.state.position_side)


def _collect_open_positions(slots: dict[str, _Slot]) -> list[OpenPosition]:
    out: list[OpenPosition] = []
    for slot in slots.values():
        state = slot.state
        if (
            state.position_side is None
            or state.held_coin is None
            or state.opened_ts_s is None
            or state.fill_spread_pp is None
            or slot.reason_open is None
        ):
            continue
        out.append(
            OpenPosition(
                coin=state.held_coin,
                side=state.position_side,
                ts_open=int(state.opened_ts_s),
                fill_spread_pp=float(state.fill_spread_pp),
                reason_open=slot.reason_open,
                last_ts_s=slot.last_ts_s,
                last_potential_pp=slot.last_potential_pp,
                last_exit_spread_pp=slot.last_exit_spread_pp,
            )
        )
    return out


def _slot_for(slots: dict[str, _Slot], coin: str, slot_mode: SlotMode) -> _Slot:
    key = _GLOBAL_KEY if slot_mode == "global" else coin
    slot = slots.get(key)
    if slot is None:
        slot = _Slot()
        slots[key] = slot
    return slot


def _last_exit_spread_pp(row: FeatureSnapshot, side: Optional[Side]) -> Optional[float]:
    if side == "long":
        value = row.spread_last_short
    elif side == "short":
        value = row.spread_last_long
    else:
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _try_open(row: FeatureSnapshot, action: str) -> Optional[PolicyState]:
    """Apply open only when that side is usable and fill is finite.

    Policy already holds on unusable / NaN fill; this is a fail-closed gate.
    """
    if action == "open_long":
        if not row.usable_long or not math.isfinite(row.spread_last_long):
            return None
        side: Side = "long"
        fill = float(row.spread_last_long)
    elif action == "open_short":
        if not row.usable_short or not math.isfinite(row.spread_last_short):
            return None
        side = "short"
        fill = float(row.spread_last_short)
    else:
        return None
    return PolicyState(
        position_side=side,
        held_coin=row.coin,
        opened_ts_s=int(row.ts_s),
        fill_spread_pp=fill,
    )


def _try_close(
    row: FeatureSnapshot,
    slot: _Slot,
    params: DummyParams,
    reason_close: str,
) -> Optional[ClosedTrade]:
    state = slot.state
    if (
        state.position_side is None
        or state.held_coin is None
        or state.opened_ts_s is None
        or state.fill_spread_pp is None
        or slot.reason_open is None
    ):
        return None
    if state.position_side == "long":
        exit_spread = row.spread_last_short
    else:
        exit_spread = row.spread_last_long
    if not math.isfinite(exit_spread):
        return None
    potential = potential_profit_pp(row, state, params.fee_round_trip_pp)
    if potential is None:
        return None
    return ClosedTrade(
        coin=state.held_coin,
        side=state.position_side,
        ts_open=int(state.opened_ts_s),
        ts_close=int(row.ts_s),
        fill_spread_pp=float(state.fill_spread_pp),
        exit_spread_pp=float(exit_spread),
        potential_pp=float(potential),
        reason_open=slot.reason_open,
        reason_close=reason_close,
    )


def _clock_order(df: pd.DataFrame) -> pd.DataFrame:
    """Time-major scan: ``ts_s`` then lexicographic ``coin`` (stable mergesort)."""
    work = df.loc[:, list(SNAPSHOT_COLUMNS)].copy()
    work["coin"] = work["coin"].astype(str)
    return work.sort_values(["ts_s", "coin"], kind="mergesort", ignore_index=True)


def _iter_snapshots(df: pd.DataFrame) -> Iterable[FeatureSnapshot]:
    for rec in df.itertuples(index=False):
        yield FeatureSnapshot(
            ts_s=int(rec.ts_s),
            coin=str(rec.coin),
            p50_1m_long=_as_float(rec.p50_1m_long),
            p50_1m_short=_as_float(rec.p50_1m_short),
            floor_long=_as_float(rec.floor_long),
            floor_short=_as_float(rec.floor_short),
            theta_1m_long=_as_float(rec.theta_1m_long),
            theta_1m_short=_as_float(rec.theta_1m_short),
            spread_last_long=_as_float(rec.spread_last_long),
            spread_last_short=_as_float(rec.spread_last_short),
            usable_long=_as_bool(rec.usable_long),
            usable_short=_as_bool(rec.usable_short),
        )


def _as_float(v: object) -> float:
    if v is None:
        return float("nan")
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def _as_bool(v: object) -> bool:
    if v is None:
        return False
    if isinstance(v, float) and not math.isfinite(v):
        return False
    try:
        if pd.isna(v):
            return False
    except (TypeError, ValueError):
        pass
    return bool(v)


def _part_paths(hive_root: Path, event_date: str | None, part_name: str) -> list[Path]:
    if event_date is not None:
        path = hive_root / f"event_date={event_date}" / part_name
        return [path] if path.is_file() else []
    return sorted(p for p in hive_root.glob(f"event_date=*/{part_name}") if p.is_file())


def _check_slot_mode(slot_mode: str) -> None:
    if slot_mode not in ("per_coin", "global"):
        raise ValueError(
            f"slot_mode must be 'per_coin' or 'global', got {slot_mode!r}"
        )


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gear-2.2 feature replay (trade count only).")
    parser.add_argument("--hive", type=Path, required=True, help="by_date hive root")
    parser.add_argument("--event-date", default=None, help="UTC date YYYY-MM-DD")
    parser.add_argument(
        "--coin",
        action="append",
        dest="coins",
        default=None,
        help="repeatable coin filter",
    )
    parser.add_argument(
        "--slot-mode",
        choices=("per_coin", "global"),
        default=DEFAULT_SLOT_MODE,
    )
    args = parser.parse_args(argv)
    result = replay_path(
        args.hive,
        event_date=args.event_date,
        coins=args.coins,
        slot_mode=args.slot_mode,
    )
    print(
        f"n_trades={len(result.closed)} "
        f"n_open_positions={len(result.open_positions)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
