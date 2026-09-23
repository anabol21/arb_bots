"""Pure EV2-to-K=1 publication contract; no broker, socket, or journal I/O.

The caller must durably record ``journal_evidence`` before changing the manager
slot. This module deliberately cannot send orders or mutate K=1 state.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Optional

from app.bot.execution.contracts import (
    SpreadState,
    SpreadStatus,
    Venue,
    decimal_to_canonical,
    leg_open_qty,
)
from app.bot.execution.state_machine import assert_invariants, is_proven_flat


class ManagerProjectionError(ValueError):
    """EV2 exposure and committed manager identity disagree."""


@dataclass(frozen=True)
class LegExposureEvidence:
    venue: Venue
    leg_id: str
    client_id: str
    filled_quantity: Decimal
    position_quantity: Optional[Decimal]
    effective_open_quantity: Decimal
    position_observed: bool

    def to_journal_dict(self) -> dict[str, object]:
        return {
            "venue": self.venue.value,
            "leg_id": self.leg_id,
            "client_id": self.client_id,
            "filled_quantity": decimal_to_canonical(self.filled_quantity),
            "position_quantity": (
                decimal_to_canonical(self.position_quantity)
                if self.position_quantity is not None
                else None
            ),
            "effective_open_quantity": decimal_to_canonical(
                self.effective_open_quantity
            ),
            "position_observed": self.position_observed,
        }


@dataclass(frozen=True)
class ManagerExposureProjection:
    publication: Literal["none", "open", "close"]
    trade_id: Optional[str]
    close_intent_id: Optional[str]
    coin: Optional[str]
    side: Optional[str]
    legs: tuple[LegExposureEvidence, ...]
    hold_slot: bool
    reason: str

    def journal_evidence(self) -> dict[str, object]:
        """Stable, credential-free evidence to fsync before K=1 publication."""

        if self.publication == "none":
            raise ManagerProjectionError("no_lifecycle_to_journal")
        return {
            "ev2_trade_id": self.trade_id,
            "ev2_close_intent_id": self.close_intent_id,
            "ev2_coin": self.coin,
            "ev2_side": self.side,
            "ev2_legs": [leg.to_journal_dict() for leg in self.legs],
        }


def _leg_evidence(state: SpreadState, *, opening: bool) -> tuple[LegExposureEvidence, ...]:
    if len(state.legs) != 2 or {leg.venue for leg in state.legs} != {
        Venue.OKX,
        Venue.BYBIT,
    }:
        raise ManagerProjectionError("two_venue_evidence_required")
    out: list[LegExposureEvidence] = []
    for leg in sorted(state.legs, key=lambda item: item.venue.value):
        quantity = leg_open_qty(leg, state.lot_tolerance) if opening else Decimal("0")
        if quantity is None or (opening and quantity <= 0):
            raise ManagerProjectionError("filled_quantity_not_proven")
        out.append(
            LegExposureEvidence(
                venue=leg.venue,
                leg_id=leg.leg_id,
                client_id=leg.client_id,
                filled_quantity=leg.filled_quantity,
                position_quantity=(
                    leg.position_quantity if leg.position_observed else None
                ),
                effective_open_quantity=quantity,
                position_observed=leg.position_observed,
            )
        )
    return tuple(out)


def project_manager_exposure(
    state: SpreadState,
    *,
    committed_trade_id: Optional[str],
) -> ManagerExposureProjection:
    """Return a proposed lifecycle row, never an ACK-based slot transition.

    ``committed_trade_id`` comes from the durable K=1 journal, not a mutable
    broker cache. ``publication`` is only a proposal until the caller fsyncs
    its row; unresolved/partial/closing states always hold the K=1 slot.
    """

    if not isinstance(state, SpreadState):
        raise ManagerProjectionError("invalid_ev2_state")
    assert_invariants(state)
    committed = str(committed_trade_id or "").strip() or None
    trade_id = state.open_intent_id
    side = state.direction.value if state.direction is not None else None

    if state.status is SpreadStatus.OPEN:
        if not trade_id or not state.coin or not side:
            raise ManagerProjectionError("open_identity_incomplete")
        if committed is not None and committed != trade_id:
            raise ManagerProjectionError("open_trade_id_mismatch")
        return ManagerExposureProjection(
            publication="open" if committed is None else "none",
            trade_id=trade_id,
            close_intent_id=None,
            coin=state.coin,
            side=side,
            legs=_leg_evidence(state, opening=True),
            hold_slot=True,
            reason="proven_open" if committed is None else "already_committed_open",
        )

    if state.status is SpreadStatus.FLAT:
        if not is_proven_flat(state):
            raise ManagerProjectionError("flatness_not_proven")
        if committed is not None:
            if not trade_id or committed != trade_id:
                raise ManagerProjectionError("close_trade_id_mismatch")
            return ManagerExposureProjection(
                publication="close",
                trade_id=trade_id,
                close_intent_id=state.intent_id,
                coin=state.coin,
                side=side,
                legs=_leg_evidence(state, opening=False),
                hold_slot=True,  # release only after the close row is durable
                reason="proven_flat_pending_commit",
            )
        return ManagerExposureProjection(
            publication="none",
            trade_id=trade_id,
            close_intent_id=state.intent_id,
            coin=state.coin,
            side=side,
            legs=(),
            hold_slot=False,
            reason="proven_flat_no_pending_lifecycle",
        )

    return ManagerExposureProjection(
        publication="none",
        trade_id=trade_id,
        close_intent_id=state.intent_id if state.status is SpreadStatus.CLOSING else None,
        coin=state.coin,
        side=side,
        legs=(),
        hold_slot=True,
        reason=f"awaiting_{state.status.value.lower()}_evidence",
    )
