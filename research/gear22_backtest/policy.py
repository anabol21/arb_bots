"""Gear-2.2 backtest policy. Pure function, no I/O.

Side rule (locked): a long trade (open long **or** close short) uses ``*_long``.
A short trade (open short **or** close long) uses ``*_short``. Closing a long
is a short-side unwind; closing a short uses long fields.

Open gates (unified thresholds, per-side fields): ``usable`` + optional
``theta > theta_open`` + ``p50_1m > p50_open`` + ``spread_last >= min_spread_open``.
Does not look at profit. Prefer long if both qualify. Fill snapshot is
``spread_last`` of the opened side (pp); NaN fill → do not open.

Close (live potential profit, dual-leg unwind; same units as the feature table):

    potential_pp(t) = fill_spread_pp + spread_last_opposite(t) - fee_round_trip_pp

This is gear-2 ``_closed_trade`` PnL at qty=100:
``(open_price + close_price) * (qty/100) - fees`` with 4×0.00075×100 = 0.30 pp.
Replay v0 uses 1 Hz ``spread_last``, not Trade_Lat 100ms fills.

``spread_last_opposite`` is the **close-side** spread (short when flat-long).
Optional ``min_theta_close`` uses ``theta_1m_*`` on that same close side.

Close when enabled close gates pass. Opposite NaN / not usable → hold
(missing ≠ close). Same-type overlap: if already in S and ``qualify_open(S)``,
do **not** close (no close-then-reopen on the same tick). Flip is not this
rule. K=1: close does not open the other side in the same call.

``None`` on an optional gate disables that gate. NaN on an **enabled** gate
is fail-closed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Optional

Side = Literal["long", "short"]
Action = Literal["hold", "open_long", "open_short", "close"]

_NAN_REASON = "hold_nan"
_NOT_USABLE_REASON = "hold_not_usable"
_BELOW_REASON = "hold_below_threshold"
_BELOW_MIN_PROFIT_REASON = "hold_below_min_profit"
_BELOW_MIN_THETA_REASON = "hold_below_min_theta"
_OPEN_OVERLAP_REASON = "hold_open_overlap"
_CLOSE_REASON = "close_min_profit"


@dataclass(frozen=True)
class FeatureSnapshot:
    ts_s: int
    coin: str
    p50_1m_long: float
    p50_1m_short: float
    floor_long: float
    floor_short: float
    theta_1m_long: float
    theta_1m_short: float
    spread_last_long: float
    spread_last_short: float
    usable_long: bool
    usable_short: bool


@dataclass(frozen=True)
class PolicyState:
    """Caller stores fill snapshot at open; policy does not mutate state."""

    position_side: Optional[Side] = None
    held_coin: Optional[str] = None
    opened_ts_s: Optional[int] = None
    fill_spread_pp: Optional[float] = None  # spread_last of opened side at open row


@dataclass(frozen=True)
class PolicyParams:
    """Trade-manager gates. ``None`` on an optional field disables that gate.

    Existing defaults keep pass-1 tests: ``theta_open=0.0``, ``p50_open=0.30``,
    ``min_profit_pp=0.0``. New gates default ``None`` (off).
    """

    theta_open: Optional[float] = 0.0
    p50_open: Optional[float] = 0.30  # fee round-trip pp, documented occupancy
    min_profit_pp: Optional[float] = 0.0  # 0.0 = at least cover fees; None = off
    fee_round_trip_pp: float = 0.30  # 4 taker legs × 0.00075 × 100
    min_spread_open: Optional[float] = None  # unified |spread_last| threshold, both sides
    min_theta_close: Optional[float] = None  # close-side theta_1m_*; None = off


DummyParams = PolicyParams

# Observation-candidate knobs are not these defaults (pass-1 tests keep
# theta_open=0.0 / p50_open=0.30 / min_profit_pp=0.0). The frozen 2.2
# observe set is research.gear22_backtest.params_frozen.DEFAULT_OBSERVE_PARAMS.
# Ridge, not a 20-30% plateau; not a PnL claim; not live-ready.


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    theta: Optional[float] = None
    p50: Optional[float] = None


def decide(
    row: FeatureSnapshot,
    state: PolicyState,
    params: PolicyParams | None = None,
) -> Decision:
    """Dispatch: coin mismatch → hold; flat → open; in position → close."""
    if params is None:
        params = PolicyParams()

    if state.held_coin is not None and state.held_coin != row.coin:
        return Decision(action="hold", reason="hold_coin_mismatch")

    if state.position_side is None:
        return decide_open(row, params)
    return decide_close(row, state, params)


def decide_open(
    row: FeatureSnapshot,
    params: PolicyParams | None = None,
) -> Decision:
    """Open gates only. Ignores position; caller uses when flat. No profit check."""
    if params is None:
        params = PolicyParams()

    long_sig = _open_side_status(row, "long", params)
    if long_sig == "nan":
        return _hold(_NAN_REASON)
    if long_sig == "qualify":
        if not _finite(row.spread_last_long):
            return _hold(_NAN_REASON)
        return Decision(
            action="open_long",
            reason="open_long",
            theta=row.theta_1m_long,
            p50=row.p50_1m_long,
        )

    short_sig = _open_side_status(row, "short", params)
    if short_sig == "nan":
        return _hold(_NAN_REASON)
    if short_sig == "qualify":
        if not _finite(row.spread_last_short):
            return _hold(_NAN_REASON)
        return Decision(
            action="open_short",
            reason="open_short",
            theta=row.theta_1m_short,
            p50=row.p50_1m_short,
        )
    if long_sig == "not_usable" and short_sig == "not_usable":
        return _hold(_NOT_USABLE_REASON)
    return _hold(_BELOW_REASON)


def decide_close(
    row: FeatureSnapshot,
    state: PolicyState,
    params: PolicyParams | None = None,
) -> Decision:
    """Close on enabled close-side gates. Same-type open overlap → hold."""
    if params is None:
        params = PolicyParams()

    if state.position_side is None:
        return _hold(_NAN_REASON)

    close_side = _close_side(state.position_side)
    usable, close_theta, _, opposite_last = _side_fields(row, close_side)
    if not usable:
        return _hold(_NOT_USABLE_REASON)
    if not _finite(opposite_last):
        return _hold(_NAN_REASON)

    theta_fail = _enabled_gt(close_theta, params.min_theta_close)
    if theta_fail == "nan":
        return _hold(_NAN_REASON)
    if theta_fail == "below":
        return _hold(_BELOW_MIN_THETA_REASON)

    potential = potential_profit_pp(row, state, params.fee_round_trip_pp)
    if potential is None:
        return _hold(_NAN_REASON)
    if params.min_profit_pp is not None:
        if not _finite(potential) or potential < params.min_profit_pp:
            return _hold(_BELOW_MIN_PROFIT_REASON)

    if _qualify_open(row, state.position_side, params):
        return _hold(_OPEN_OVERLAP_REASON)

    return Decision(action="close", reason=_CLOSE_REASON)


def potential_profit_pp(
    row: FeatureSnapshot,
    state: PolicyState,
    fee_round_trip_pp: float,
) -> Optional[float]:
    """Live dual-leg unwind PnL in percentage points, or None if uncomputable.

    potential_pp = fill_spread_pp + spread_last_opposite - fee_round_trip_pp
    Does not gate on ``usable`` (that is decide_close).
    ``spread_last_opposite`` is the close-side spread (short when long).
    """
    if state.position_side is None or state.fill_spread_pp is None:
        return None
    if not _finite(state.fill_spread_pp) or not _finite(fee_round_trip_pp):
        return None
    opposite_last = _opposite_spread_last(row, state.position_side)
    if not _finite(opposite_last):
        return None
    return state.fill_spread_pp + opposite_last - fee_round_trip_pp


def _finite(x: float) -> bool:
    return math.isfinite(x)


def _close_side(position_side: Side) -> Side:
    """Unwind side: close long → short fields; close short → long fields."""
    if position_side == "long":
        return "short"
    return "long"


def _opposite_spread_last(row: FeatureSnapshot, position_side: Side) -> float:
    return _side_fields(row, _close_side(position_side))[3]


def _side_fields(
    row: FeatureSnapshot, side: Side
) -> tuple[bool, float, float, float]:
    """usable, theta, p50, spread_last for one side."""
    if side == "long":
        return (
            row.usable_long,
            row.theta_1m_long,
            row.p50_1m_long,
            row.spread_last_long,
        )
    return (
        row.usable_short,
        row.theta_1m_short,
        row.p50_1m_short,
        row.spread_last_short,
    )


def _qualify_open(row: FeatureSnapshot, side: Side, params: PolicyParams) -> bool:
    """Open gates only (usable, theta_open, p50_open, min_spread_open)."""
    return _open_side_status(row, side, params) == "qualify"


def _open_side_status(row: FeatureSnapshot, side: Side, params: PolicyParams) -> str:
    """Return qualify / not_usable / nan / below for one open side."""
    usable, theta, p50, spread_last = _side_fields(row, side)
    if not usable:
        return "not_usable"
    for status in (
        _enabled_gt(theta, params.theta_open),
        _enabled_gt(p50, params.p50_open),
        _enabled_ge(spread_last, params.min_spread_open),
    ):
        if status is not None:
            return status
    return "qualify"


def _enabled_gt(value: float, threshold: Optional[float]) -> Optional[str]:
    """Strict ``>``. ``None`` threshold = gate off. NaN → fail-closed."""
    if threshold is None:
        return None
    if not _finite(value):
        return "nan"
    if not (value > threshold):
        return "below"
    return None


def _enabled_ge(value: float, threshold: Optional[float]) -> Optional[str]:
    """``>=``. ``None`` threshold = gate off. NaN → fail-closed."""
    if threshold is None:
        return None
    if not _finite(value):
        return "nan"
    if not (value >= threshold):
        return "below"
    return None


def _hold(reason: str) -> Decision:
    return Decision(action="hold", reason=reason)
