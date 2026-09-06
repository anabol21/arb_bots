"""Streaming Gear-2 Arm A market manager (pure policy; no I/O).

Ports the global K=1 elif chain from ``research/gear2_backtest.py`` so live
would_send can share semantics: held coin, pending_skip, slot_busy, close only
on the held coin. Arm A: ``regime_topn`` is never consulted.

This live-data profile uses GEAR2_WOULD_SEND_* (thresh 0.02, 4 coins, MA 10s).
It is not the frozen all-crypto 0.5 close stamp and is not alpha.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional

from app.policy.trade_manager import (
    GEAR2_WOULD_SEND_HYPER,
    GEAR2_WOULD_SEND_VARIATION,
    Intent,
    IntentAction,
    Side,
    TickView,
    _pass_gates,
    _resolve_spreads,
)


@dataclass
class MarketState:
    """Global K=1 slot + pending across coins."""

    position_side: Optional[Side] = None
    held_coin: Optional[str] = None
    pending_fill: bool = False
    pending_coin: Optional[str] = None
    k_live: int = 1
    n_signals_raw: int = 0
    n_filtered_slot_busy: int = 0
    n_filtered_pending_skip: int = 0
    n_filtered_by_freshness: int = 0
    n_filtered_by_latency: int = 0
    n_filtered_by_avg: int = 0
    n_filtered_by_size: int = 0
    n_filtered_by_l1_depth: int = 0
    n_filtered_not_topn: int = 0
    seq: int = 0

    def snapshot_counters(self) -> dict[str, int]:
        return {
            "n_signals_raw": self.n_signals_raw,
            "n_filtered_slot_busy": self.n_filtered_slot_busy,
            "n_filtered_pending_skip": self.n_filtered_pending_skip,
            "n_filtered_by_freshness": self.n_filtered_by_freshness,
            "n_filtered_by_latency": self.n_filtered_by_latency,
            "n_filtered_by_avg": self.n_filtered_by_avg,
            "n_filtered_by_size": self.n_filtered_by_size,
            "n_filtered_by_l1_depth": self.n_filtered_by_l1_depth,
            "n_filtered_not_topn": self.n_filtered_not_topn,
            "seq": self.seq,
        }


@dataclass
class MarketDecision:
    action: IntentAction
    reason: str
    coin: str
    ordering_key: int = 0
    counters: dict[str, int] = field(default_factory=dict)


def _count_gate_fail(state: MarketState, reason: str) -> None:
    if reason == "gate_a_freshness":
        state.n_filtered_by_freshness += 1
    elif reason == "gate_a_latency":
        state.n_filtered_by_latency += 1
    elif reason == "gate_b_ma":
        state.n_filtered_by_avg += 1
    elif reason == "gate_volume":
        state.n_filtered_by_size += 1
    elif reason == "gate_l1_depth":
        state.n_filtered_by_l1_depth += 1


def decide_market_tick(
    tick: TickView,
    coin: str,
    state: MarketState,
    variation: Mapping[str, float] | None = None,
    hyper: Mapping[str, object] | None = None,
) -> MarketDecision:
    """One serialized tick. Mutates counter fields on ``state`` only.

    Position / pending / held_coin are inputs; the broker updates them after
    fill. Arm A: Top-N is ignored even if hyper contains a map.
    """
    v = variation if variation is not None else GEAR2_WOULD_SEND_VARIATION
    h = hyper if hyper is not None else GEAR2_WOULD_SEND_HYPER
    coin_u = str(coin).strip().upper()
    state.seq += 1
    ordering_key = state.seq

    def _done(action: IntentAction, reason: str) -> MarketDecision:
        return MarketDecision(
            action=action,
            reason=reason,
            coin=coin_u,
            ordering_key=ordering_key,
            counters=state.snapshot_counters(),
        )

    if tick.suppressed or not tick.valid:
        return _done("flat", "suppressed")
    if tick.stale:
        return _done("flat", "stale")
    if state.k_live < 1:
        return _done("flat", "k_live_zero")

    spread_long, spread_short = _resolve_spreads(tick)
    if spread_long is None or spread_short is None:
        return _done("flat", "missing_spread")

    thresh_ol = float(v["thresh_open_long"])
    thresh_os = float(v["thresh_open_short"])
    thresh_cl = float(v["thresh_close_long"])
    thresh_cs = float(v["thresh_close_short"])
    open_frac = float(v["open_frac"])
    close_frac = float(v["close_frac"])
    open_hit = spread_long > thresh_ol or spread_short > thresh_os

    if state.pending_fill:
        pending_coin = (state.pending_coin or "").upper()
        if coin_u != pending_coin and open_hit:
            state.n_filtered_pending_skip += 1
            return _done("flat", "pending_skip")
        return _done("flat", "pending")

    slot_full = state.position_side is not None
    held = (state.held_coin or "").upper() or None

    if spread_long > thresh_ol and not slot_full:
        state.n_signals_raw += 1
        reject = _pass_gates(
            tick,
            side="long",
            is_open=True,
            frac=open_frac,
            thresh=thresh_ol,
            ma_val=tick.ma_long,
            variation=v,
            hyper=h,
        )
        if reject is not None:
            _count_gate_fail(state, reject.reason)
            return _done("flat", reject.reason)
        return _done("open_long", "signal")

    if spread_short > thresh_os and not slot_full:
        state.n_signals_raw += 1
        reject = _pass_gates(
            tick,
            side="short",
            is_open=True,
            frac=open_frac,
            thresh=thresh_os,
            ma_val=tick.ma_short,
            variation=v,
            hyper=h,
        )
        if reject is not None:
            _count_gate_fail(state, reject.reason)
            return _done("flat", reject.reason)
        return _done("open_short", "signal")

    if slot_full and open_hit and (held is None or coin_u != held):
        state.n_filtered_slot_busy += 1
        return _done("flat", "slot_busy")

    if (
        spread_short > thresh_cl
        and state.position_side == "long"
        and held is not None
        and coin_u == held
    ):
        state.n_signals_raw += 1
        reject = _pass_gates(
            tick,
            side="long",
            is_open=False,
            frac=close_frac,
            thresh=thresh_cl,
            ma_val=tick.ma_short,
            variation=v,
            hyper=h,
        )
        if reject is not None:
            _count_gate_fail(state, reject.reason)
            return _done("flat", reject.reason)
        return _done("close", "signal")

    if (
        spread_long > thresh_cs
        and state.position_side == "short"
        and held is not None
        and coin_u == held
    ):
        state.n_signals_raw += 1
        reject = _pass_gates(
            tick,
            side="short",
            is_open=False,
            frac=close_frac,
            thresh=thresh_cs,
            ma_val=tick.ma_long,
            variation=v,
            hyper=h,
        )
        if reject is not None:
            _count_gate_fail(state, reject.reason)
            return _done("flat", reject.reason)
        return _done("close", "signal")

    return _done("flat", "no_signal")


def intent_from_decision(decision: MarketDecision) -> Intent:
    return Intent(decision.action, decision.reason)
