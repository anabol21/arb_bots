"""Exact CAP dual-leg OPEN and reduce-only CLOSE plans for EV2 live route.

The caller must supply fresh executable L1 and live instrument metadata to
``CapOpenSizingInput``. No pricing or account I/O occurs inside this module.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from app.bot.execution.contracts import (
    IntentAction, LegPlan, LegStatus, SpreadDirection, SpreadState, SpreadStatus,
    TradeIntent, Venue,
)
from app.bot.execution.live_cap_sizing import CapOpenSizingInput, plan_cap_matched_open


class LiveCapPlanError(ValueError):
    """A CAP submission cannot be proved exactly matched and bounded."""


class LiveCapPlanBook:
    def __init__(
        self,
        *,
        bybit_instrument: str = "CAPUSDT",
        okx_instrument: str = "CAP-USDT-SWAP",
        okx_inst_id_code: int,
        okx_base_per_contract: Decimal,
    ) -> None:
        if (
            bybit_instrument != "CAPUSDT"
            or okx_instrument != "CAP-USDT-SWAP"
            or isinstance(okx_inst_id_code, bool)
            or not isinstance(okx_inst_id_code, int)
            or okx_inst_id_code <= 0
            or not isinstance(okx_base_per_contract, Decimal)
            or not okx_base_per_contract.is_finite()
            or okx_base_per_contract <= 0
        ):
            raise LiveCapPlanError("invalid_cap_metadata")
        self.bybit_instrument = bybit_instrument
        self.okx_instrument = okx_instrument
        self.okx_inst_id_code = okx_inst_id_code
        self.okx_base_per_contract = okx_base_per_contract
        self._prepared: dict[str, tuple[LegPlan, LegPlan]] = {}

    @staticmethod
    def _open_sides(direction: SpreadDirection) -> tuple[str, str]:
        if direction is SpreadDirection.LONG:
            return "sell", "buy"  # Bybit, OKX
        if direction is SpreadDirection.SHORT:
            return "buy", "sell"
        raise LiveCapPlanError("invalid_direction")

    def prepare_open(
        self, intent: TradeIntent, sizing: CapOpenSizingInput
    ) -> tuple[LegPlan, LegPlan]:
        if (
            intent.coin != "CAP" or intent.action is not IntentAction.OPEN
            or intent.notional_usdt != Decimal("10")
            or sizing.okx_base_per_contract != self.okx_base_per_contract
        ):
            raise LiveCapPlanError("invalid_open_intent_or_metadata")
        size = plan_cap_matched_open(sizing)
        bybit_side, okx_side = self._open_sides(intent.spread_direction)
        plans = (
            LegPlan.build(
                intent_id=intent.intent_id, leg_id="leg_bybit", venue=Venue.BYBIT,
                instrument=self.bybit_instrument, side=bybit_side,
                quantity=size.bybit_base_qty, base_multiplier=Decimal("1"),
            ),
            LegPlan.build(
                intent_id=intent.intent_id, leg_id="leg_okx", venue=Venue.OKX,
                instrument=self.okx_instrument, side=okx_side,
                quantity=size.okx_contract_qty,
                base_multiplier=self.okx_base_per_contract,
            ),
        )
        self._store(intent, plans)
        return plans

    def prepare_close(
        self, intent: TradeIntent, state: SpreadState
    ) -> tuple[LegPlan, LegPlan]:
        if (
            intent.coin != "CAP" or intent.action is not IntentAction.CLOSE
            or intent.notional_usdt != Decimal("10")
            or state.status is not SpreadStatus.OPEN
            or state.coin != "CAP"
            or state.direction is not intent.spread_direction
        ):
            raise LiveCapPlanError("close_requires_proven_cap_open")
        legs = {leg.venue: leg for leg in state.legs}
        if len(state.legs) != 2 or set(legs) != {Venue.BYBIT, Venue.OKX}:
            raise LiveCapPlanError("close_leg_set_invalid")
        bybit, okx = legs[Venue.BYBIT], legs[Venue.OKX]
        if (
            bybit.status is not LegStatus.FILLED
            or okx.status is not LegStatus.FILLED
            or bybit.filled_quantity <= 0 or okx.filled_quantity <= 0
            or bybit.filled_quantity != okx.filled_quantity * self.okx_base_per_contract
            or bybit.filled_quantity != bybit.planned_quantity
            or okx.filled_quantity != okx.planned_quantity
            or bybit.base_multiplier != Decimal("1")
            or okx.base_multiplier != self.okx_base_per_contract
        ):
            raise LiveCapPlanError("close_exposure_not_exactly_proven")
        bybit_open, okx_open = self._open_sides(intent.spread_direction)
        opposite = {"buy": "sell", "sell": "buy"}
        plans = (
            LegPlan.build(
                intent_id=intent.intent_id, leg_id="leg_bybit", venue=Venue.BYBIT,
                instrument=self.bybit_instrument, side=opposite[bybit_open],
                quantity=bybit.filled_quantity, reduce_only=True,
                base_multiplier=Decimal("1"),
            ),
            LegPlan.build(
                intent_id=intent.intent_id, leg_id="leg_okx", venue=Venue.OKX,
                instrument=self.okx_instrument, side=opposite[okx_open],
                quantity=okx.filled_quantity, reduce_only=True,
                base_multiplier=self.okx_base_per_contract,
            ),
        )
        self._store(intent, plans)
        return plans

    def _store(self, intent: TradeIntent, plans: tuple[LegPlan, LegPlan]) -> None:
        if intent.intent_id in self._prepared:
            raise LiveCapPlanError("duplicate_live_intent")
        self._prepared[intent.intent_id] = plans

    def resolve(self, intent: TradeIntent) -> Sequence[LegPlan]:
        plans = self._prepared.get(intent.intent_id)
        if plans is None or any(plan.intent_id != intent.intent_id for plan in plans):
            raise LiveCapPlanError("live_plan_not_prepared")
        return plans
