"""Pure fill-authoritative execution v2 state machine. No I/O."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import MappingProxyType
from typing import Iterable, Optional

from app.bot.execution.contracts import (
    SCHEMA_VERSION,
    ContractValidationError,
    ExecutionEvent,
    ExecutionEventType,
    IntentAction,
    LegState,
    LegStatus,
    SpreadDirection,
    SpreadState,
    SpreadStatus,
    Venue,
    ack_timeout_is_unresolved,
    canonical_decimal,
    derive_client_id,
    flat_evidence_is_complete,
    leg_open_qty,
    leg_quantity_exceeds_plan,
    observed_position_contradicts_fill,
    observed_working_open_orders,
    open_evidence_is_complete,
    terminal_fill_is_incomplete,
    timeout_blocks_flat_proof,
    validate_client_id,
)


class InvalidTransition(ValueError):
    """Illegal transition. Message carries only redacted ids and statuses."""

    def __init__(
        self,
        *,
        intent_id: Optional[str],
        event_id: Optional[str],
        from_status: Optional[str],
        code: str,
    ) -> None:
        self.intent_id = intent_id
        self.event_id = event_id
        self.from_status = from_status
        self.code = code
        super().__init__(
            "invalid transition"
            f" intent={intent_id or '-'}"
            f" event={event_id or '-'}"
            f" from={from_status or '-'}"
            f" code={code}"
        )


def initial_spread_state(
    *,
    run_id: str,
    lot_tolerance: Decimal = Decimal("0"),
) -> SpreadState:
    state = SpreadState(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        intent_id=None,
        open_intent_id=None,
        status=SpreadStatus.IDLE,
        direction=None,
        coin=None,
        lot_tolerance=lot_tolerance,
        legs=(),
        last_sequence=0,
        last_monotonic_ns=0,
        applied_event_ids=frozenset(),
        accepted_intent_ids=frozenset(),
        sequence_hashes=MappingProxyType({}),
        event_hashes=MappingProxyType({}),
        recovery_required=False,
        pause_latched=False,
        stream_generation_ok=True,
        positions_flat=True,
        open_orders_flat=True,
        halt_reason=None,
    )
    assert_invariants(state)
    return state


def apply_events(state: SpreadState, events: Iterable[ExecutionEvent]) -> SpreadState:
    out = state
    for event in events:
        out = apply_event(out, event)
    return out


def apply_event(state: SpreadState, event: ExecutionEvent) -> SpreadState:
    if not isinstance(event, ExecutionEvent):
        raise InvalidTransition(
            intent_id=state.intent_id,
            event_id=None,
            from_status=state.status.value,
            code="not_an_event",
        )
    stored_hash = state.event_hashes.get(event.event_id)
    if stored_hash is not None:
        if stored_hash != event.content_hash():
            raise InvalidTransition(
                intent_id=event.intent_id,
                event_id=event.event_id,
                from_status=state.status.value,
                code="corrupt_event",
            )
        return state
    if event.event_id in state.applied_event_ids:
        raise InvalidTransition(
            intent_id=event.intent_id,
            event_id=event.event_id,
            from_status=state.status.value,
            code="corrupt_event",
        )

    if event.run_id != state.run_id:
        raise InvalidTransition(
            intent_id=event.intent_id,
            event_id=event.event_id,
            from_status=state.status.value,
            code="run_mismatch",
        )

    if event.monotonic_ns < state.last_monotonic_ns:
        raise InvalidTransition(
            intent_id=event.intent_id,
            event_id=event.event_id,
            from_status=state.status.value,
            code="decreasing_monotonic",
        )

    switching = _starts_new_intent(state, event)
    intent_ok = state.intent_id is None or event.intent_id == state.intent_id
    if not intent_ok and not switching:
        raise InvalidTransition(
            intent_id=event.intent_id,
            event_id=event.event_id,
            from_status=state.status.value,
            code="intent_mismatch",
        )

    seq_key = str(event.sequence)
    if not switching and seq_key in state.sequence_hashes:
        if state.sequence_hashes[seq_key] != event.sequence_hash():
            raise InvalidTransition(
                intent_id=event.intent_id,
                event_id=event.event_id,
                from_status=state.status.value,
                code="corrupt_sequence",
            )
        hashes = dict(state.event_hashes)
        hashes[event.event_id] = event.content_hash()
        recorded = replace(
            state,
            applied_event_ids=state.applied_event_ids | {event.event_id},
            event_hashes=MappingProxyType(hashes),
        )
        assert_invariants(recorded)
        return recorded

    expected_seq = 1 if switching else state.last_sequence + 1
    if event.sequence != expected_seq:
        raise InvalidTransition(
            intent_id=event.intent_id,
            event_id=event.event_id,
            from_status=state.status.value,
            code="sequence_gap",
        )

    prior_ids = state.applied_event_ids
    try:
        next_state = _dispatch(state, event)
        hashes = {} if switching else dict(state.sequence_hashes)
        hashes[seq_key] = event.sequence_hash()
        event_hashes = dict(state.event_hashes)
        event_hashes[event.event_id] = event.content_hash()
        next_state = replace(
            next_state,
            last_sequence=event.sequence,
            last_monotonic_ns=event.monotonic_ns,
            applied_event_ids=prior_ids | {event.event_id},
            sequence_hashes=MappingProxyType(hashes),
            event_hashes=MappingProxyType(event_hashes),
        )
        assert_invariants(next_state)
        return next_state
    except ContractValidationError:
        raise InvalidTransition(
            intent_id=event.intent_id,
            event_id=event.event_id,
            from_status=state.status.value,
            code="internal_contract",
        ) from None


def _starts_new_intent(state: SpreadState, event: ExecutionEvent) -> bool:
    if event.event_type is not ExecutionEventType.INTENT_ACCEPTED:
        return False
    action = event.payload.get("action")
    if action == IntentAction.OPEN.value:
        return state.status in {SpreadStatus.IDLE, SpreadStatus.FLAT}
    if action == IntentAction.CLOSE.value:
        return state.status is SpreadStatus.OPEN
    return False


def opens_allowed(state: SpreadState) -> bool:
    if state.pause_latched:
        return False
    if not state.stream_generation_ok:
        return False
    if state.recovery_required:
        return False
    if state.status not in {SpreadStatus.IDLE, SpreadStatus.FLAT}:
        return False
    if state.status is SpreadStatus.FLAT and not is_proven_flat(state):
        return False
    return True


def needs_reconciliation(state: SpreadState) -> bool:
    if state.status in {SpreadStatus.EXPOSURE_UNKNOWN, SpreadStatus.RECOVERING}:
        return True
    if not state.stream_generation_ok:
        return True
    for leg in state.legs:
        if leg.status in {LegStatus.UNKNOWN, LegStatus.RECONCILING}:
            return True
        if ack_timeout_is_unresolved(leg, state.lot_tolerance):
            return True
    return bool(state.recovery_required)


def is_proven_flat(state: SpreadState) -> bool:
    if state.status is not SpreadStatus.FLAT:
        return False
    return flat_evidence_is_complete(
        legs=state.legs,
        positions_flat=state.positions_flat,
        open_orders_flat=state.open_orders_flat,
    )


def assert_invariants(state: SpreadState) -> None:
    if state.status is SpreadStatus.OPEN:
        if not _open_evidence_valid(state):
            raise InvalidTransition(
                intent_id=state.intent_id,
                event_id=None,
                from_status=state.status.value,
                code="open_requires_fills",
            )
        if state.intent_id != state.open_intent_id:
            raise InvalidTransition(
                intent_id=state.intent_id,
                event_id=None,
                from_status=state.status.value,
                code="open_intent_mismatch",
            )
    if state.status is SpreadStatus.FLAT:
        if not is_proven_flat(state):
            raise InvalidTransition(
                intent_id=state.intent_id,
                event_id=None,
                from_status=state.status.value,
                code="flat_requires_recon",
            )
    public = state.to_public_dict()
    if "peer_open" in public or "peer_open_instruction" in public:
        raise InvalidTransition(
            intent_id=state.intent_id,
            event_id=None,
            from_status=state.status.value,
            code="peer_open_emitted",
        )
    for leg in state.legs:
        validate_client_id(leg.client_id, leg.venue)


def _reject(state: SpreadState, event: ExecutionEvent, code: str) -> None:
    raise InvalidTransition(
        intent_id=event.intent_id,
        event_id=event.event_id,
        from_status=state.status.value,
        code=code,
    )


def _replace_leg(state: SpreadState, new_leg: LegState) -> tuple[LegState, ...]:
    out: list[LegState] = []
    found = False
    for leg in state.legs:
        if leg.leg_id == new_leg.leg_id:
            out.append(new_leg)
            found = True
        else:
            out.append(leg)
    if not found:
        if any(leg.venue is new_leg.venue for leg in out):
            raise InvalidTransition(
                intent_id=state.intent_id,
                event_id=None,
                from_status=state.status.value,
                code="duplicate_venue",
            )
        if len(out) >= 2:
            raise InvalidTransition(
                intent_id=state.intent_id,
                event_id=None,
                from_status=state.status.value,
                code="too_many_legs",
            )
        out.append(new_leg)
    return tuple(out)


def _qty(payload: object, key: str) -> Decimal:
    return canonical_decimal(payload, field=key)


def _open_evidence_valid(state: SpreadState) -> bool:
    return open_evidence_is_complete(
        intent_id=state.intent_id,
        open_intent_id=state.open_intent_id,
        stream_generation_ok=state.stream_generation_ok,
        lot_tolerance=state.lot_tolerance,
        legs=state.legs,
    )


def _positive_leg_proof(leg: LegState, tolerance: Decimal) -> bool:
    qty = leg_open_qty(leg, tolerance)
    return qty is not None and qty > 0


def _leg_observed_nonzero_position(leg: LegState) -> bool:
    return leg.position_observed and leg.position_quantity != 0


def _fresh_zero_rest_snapshot(leg: LegState) -> bool:
    return (
        leg.position_observed
        and leg.position_quantity == 0
        and leg.open_orders_observed
        and leg.open_order_count == 0
    )


def _matching_positive_position_proof(leg: LegState, tolerance: Decimal) -> bool:
    return (
        not leg.reduce_only
        and leg.position_observed
        and leg.planned_quantity > 0
        and leg.position_quantity > 0
        and abs(leg.position_quantity - leg.planned_quantity) <= tolerance
        and not observed_position_contradicts_fill(leg, tolerance)
        and not leg_quantity_exceeds_plan(leg, tolerance)
    )


def _confirmed_unfilled(leg: LegState) -> bool:
    return (
        leg.filled_quantity == 0
        and not _leg_observed_nonzero_position(leg)
        and (
            leg.confirmed_unfilled
            or leg.status in {LegStatus.ACK_REJECTED, LegStatus.CANCELLED}
        )
    )


def _two_leg_quantity_mismatch(state: SpreadState) -> bool:
    if len(state.legs) != 2:
        return False
    if any(leg.reduce_only for leg in state.legs):
        return False
    qtys: list[Decimal] = []
    for leg in state.legs:
        if leg.filled_quantity > 0:
            qtys.append(leg.filled_quantity * leg.base_multiplier)
        elif leg.position_observed and leg.position_quantity > 0:
            qtys.append(leg.position_quantity * leg.base_multiplier)
        else:
            return False
    return abs(qtys[0] - qtys[1]) > state.lot_tolerance


def _has_quantity_conflict(state: SpreadState) -> bool:
    if _two_leg_quantity_mismatch(state):
        return True
    for leg in state.legs:
        if observed_position_contradicts_fill(leg, state.lot_tolerance):
            return True
        if leg_quantity_exceeds_plan(leg, state.lot_tolerance):
            return True
    return False


def _legs_with_quantity_conflicts_unknown(state: SpreadState) -> tuple[LegState, ...]:
    if not _has_quantity_conflict(state):
        return state.legs
    pair_mismatch = _two_leg_quantity_mismatch(state)
    out: list[LegState] = []
    for leg in state.legs:
        conflict = pair_mismatch or observed_position_contradicts_fill(
            leg, state.lot_tolerance
        ) or leg_quantity_exceeds_plan(leg, state.lot_tolerance)
        if conflict and leg.status not in {LegStatus.UNKNOWN, LegStatus.RECONCILING}:
            out.append(replace(leg, status=LegStatus.UNKNOWN))
        else:
            out.append(leg)
    return tuple(out)


def _one_positive_one_unfilled(state: SpreadState) -> bool:
    """One fill or position proof plus a confirmed-unfilled peer is unhedged."""
    if len(state.legs) != 2:
        return False
    if any(leg.reduce_only for leg in state.legs):
        return False
    proven: list[LegState] = []
    unfilled: list[LegState] = []
    for leg in state.legs:
        if _positive_leg_proof(leg, state.lot_tolerance):
            proven.append(leg)
        elif (
            _confirmed_unfilled(leg)
            and not _positive_leg_proof(leg, state.lot_tolerance)
        ):
            unfilled.append(leg)
    return len(proven) == 1 and len(unfilled) == 1


def _both_confirmed_unfilled_zero(state: SpreadState) -> bool:
    if len(state.legs) != 2:
        return False
    if any(leg.reduce_only for leg in state.legs):
        return False
    return all(
        _confirmed_unfilled(leg)
        and leg.status in {LegStatus.ACK_REJECTED, LegStatus.CANCELLED}
        for leg in state.legs
    )


def _has_unknown_exposure(state: SpreadState) -> bool:
    if not state.stream_generation_ok:
        return True
    if _has_quantity_conflict(state):
        return True
    for leg in state.legs:
        if leg.status in {LegStatus.PARTIAL, LegStatus.UNKNOWN, LegStatus.RECONCILING}:
            return True
        if ack_timeout_is_unresolved(leg, state.lot_tolerance):
            return True
        if observed_position_contradicts_fill(leg, state.lot_tolerance):
            return True
        if terminal_fill_is_incomplete(leg, state.lot_tolerance):
            return True
        if _positive_leg_proof(leg, state.lot_tolerance) and observed_working_open_orders(leg):
            return True
    return False


def _is_closing(state: SpreadState) -> bool:
    if state.status is SpreadStatus.CLOSING:
        return True
    return any(leg.reduce_only for leg in state.legs)


def _opening_unproven_position(state: SpreadState) -> bool:
    """Nonzero observed position during opening without complete two-leg OPEN proof.

    CLOSING already carries the pre-existing hedge; that position is expected
    and must not by itself create a new fault.
    """
    if _is_closing(state):
        return False
    if _open_evidence_valid(state):
        return False
    return any(_leg_observed_nonzero_position(leg) for leg in state.legs)


def _idle_after_failed_open(state: SpreadState) -> SpreadState:
    return replace(
        state,
        intent_id=None,
        open_intent_id=None,
        status=SpreadStatus.IDLE,
        direction=None,
        coin=None,
        legs=(),
        recovery_required=False,
        positions_flat=True,
        open_orders_flat=True,
        halt_reason=None,
    )


def _legs_still_prove_flat(legs: tuple[LegState, ...]) -> bool:
    if not legs:
        return False
    positions_flat = all(
        leg.position_observed and leg.position_quantity == 0 for leg in legs
    )
    open_orders_flat = all(
        leg.open_orders_observed and leg.open_order_count == 0 for leg in legs
    )
    return flat_evidence_is_complete(
        legs=legs,
        positions_flat=positions_flat,
        open_orders_flat=open_orders_flat,
    )


def _invalidate_observation_freshness(leg: LegState) -> LegState:
    """Keep last-known quantities as non-authoritative history.

    Freshness is an event boundary, not a wall-clock TTL. Matched
    reconciliation must not itself create fresh snapshots.
    """
    return replace(leg, position_observed=False, open_orders_observed=False)


def _invalidate_all_observation_freshness(
    legs: tuple[LegState, ...],
) -> tuple[LegState, ...]:
    return tuple(_invalidate_observation_freshness(leg) for leg in legs)


def _sync_flat_flags(state: SpreadState) -> SpreadState:
    if not state.legs:
        return replace(state, positions_flat=True, open_orders_flat=True)
    positions_flat = all(
        leg.position_observed and leg.position_quantity == 0 for leg in state.legs
    )
    open_orders_flat = all(
        leg.open_orders_observed and leg.open_order_count == 0 for leg in state.legs
    )
    status = state.status
    recovery_required = state.recovery_required
    if status is SpreadStatus.FLAT and not flat_evidence_is_complete(
        legs=state.legs,
        positions_flat=positions_flat,
        open_orders_flat=open_orders_flat,
    ):
        status = SpreadStatus.EXPOSURE_UNKNOWN
        recovery_required = True
    return replace(
        state,
        positions_flat=positions_flat,
        open_orders_flat=open_orders_flat,
        status=status,
        recovery_required=recovery_required,
    )


def _recompute(state: SpreadState) -> SpreadState:
    state = _sync_flat_flags(state)
    if _has_quantity_conflict(state):
        state = replace(state, legs=_legs_with_quantity_conflicts_unknown(state))
    unproven_position = _opening_unproven_position(state)
    if state.status is SpreadStatus.HALTED:
        return replace(state, recovery_required=True)
    if _one_positive_one_unfilled(state):
        return replace(
            state,
            status=SpreadStatus.RECOVERING,
            recovery_required=True,
        )
    if _both_confirmed_unfilled_zero(state) and state.status in {
        SpreadStatus.ARMED,
        SpreadStatus.DISPATCHING,
        SpreadStatus.EXPOSURE_UNKNOWN,
        SpreadStatus.RECOVERING,
    }:
        return replace(
            state,
            status=SpreadStatus.RECOVERING,
            recovery_required=True,
        )
    if state.status is SpreadStatus.CLOSING:
        if _has_unknown_exposure(state):
            return replace(
                state,
                status=SpreadStatus.EXPOSURE_UNKNOWN,
                recovery_required=True,
            )
        return state
    blocked = _has_unknown_exposure(state) or unproven_position
    if _open_evidence_valid(state) and not blocked and state.status not in {
        SpreadStatus.CLOSING,
        SpreadStatus.FLAT,
        SpreadStatus.IDLE,
        SpreadStatus.HALTED,
    }:
        return replace(
            state,
            status=SpreadStatus.OPEN,
            recovery_required=False,
        )
    if blocked and state.status not in {
        SpreadStatus.IDLE,
        SpreadStatus.FLAT,
        SpreadStatus.HALTED,
    }:
        return replace(
            state,
            status=SpreadStatus.EXPOSURE_UNKNOWN,
            recovery_required=True,
        )
    if unproven_position:
        return replace(state, recovery_required=True)
    return state


def _after_evidence(state: SpreadState, **changes: object) -> SpreadState:
    """Apply evidence without constructing an illegal OPEN/FLAT intermediate."""
    changes = dict(changes)
    if "status" not in changes:
        if state.status is SpreadStatus.OPEN:
            changes["status"] = SpreadStatus.EXPOSURE_UNKNOWN
        elif state.status is SpreadStatus.FLAT:
            legs = changes.get("legs", state.legs)
            if not isinstance(legs, tuple) or not _legs_still_prove_flat(legs):
                changes["status"] = SpreadStatus.EXPOSURE_UNKNOWN
                changes["recovery_required"] = True
    return _recompute(replace(state, **changes))


def _require_leg(state: SpreadState, event: ExecutionEvent) -> LegState:
    if event.leg_id is None:
        _reject(state, event, "missing_leg")
    leg = state.leg_by_id(event.leg_id or "")
    if leg is None:
        _reject(state, event, "unknown_leg")
        raise AssertionError("unreachable")
    if event.venue is not None and leg.venue is not event.venue:
        _reject(state, event, "venue_mismatch")
    return leg


def _dispatch(state: SpreadState, event: ExecutionEvent) -> SpreadState:
    handlers = {
        ExecutionEventType.INTENT_ACCEPTED: _on_intent_accepted,
        ExecutionEventType.INTENT_REJECTED: _on_intent_rejected,
        ExecutionEventType.REQUEST_SENT: _on_request_sent,
        ExecutionEventType.ACK_ACCEPTED: _on_ack_accepted,
        ExecutionEventType.ACK_REJECTED: _on_ack_rejected,
        ExecutionEventType.ACK_TIMEOUT: _on_ack_timeout,
        ExecutionEventType.PARTIAL_FILL: _on_partial_fill,
        ExecutionEventType.FILL: _on_fill,
        ExecutionEventType.CANCEL_REQUESTED: _on_cancel_requested,
        ExecutionEventType.CANCEL_ACK: _on_cancel_ack,
        ExecutionEventType.POSITION_OBSERVED: _on_position_observed,
        ExecutionEventType.OPEN_ORDERS_OBSERVED: _on_open_orders_observed,
        ExecutionEventType.RECONCILIATION: _on_reconciliation,
        ExecutionEventType.FLATNESS_PROVEN: _on_flatness_proven,
        ExecutionEventType.PAUSE: _on_pause,
        ExecutionEventType.FAULT: _on_fault,
        ExecutionEventType.STREAM_GENERATION_MISMATCH: _on_stream_mismatch,
        ExecutionEventType.UNKNOWN_CORRELATION: _on_unknown_correlation,
    }
    handler = handlers.get(event.event_type)
    if handler is None:
        _reject(state, event, "unknown_event_type")
        raise AssertionError("unreachable")
    return handler(state, event)


def _on_intent_accepted(state: SpreadState, event: ExecutionEvent) -> SpreadState:
    action = IntentAction(str(event.payload["action"]))
    if action is IntentAction.OPEN:
        if not opens_allowed(state):
            _reject(state, event, "open_not_allowed")
        if event.intent_id in state.accepted_intent_ids:
            _reject(state, event, "duplicate_intent")
        return replace(
            state,
            intent_id=event.intent_id,
            open_intent_id=event.intent_id,
            status=SpreadStatus.ARMED,
            direction=SpreadDirection(str(event.payload["spread_direction"])),
            coin=str(event.payload["coin"]),
            lot_tolerance=_qty(event.payload["lot_tolerance"], "lot_tolerance"),
            legs=(),
            positions_flat=True,
            open_orders_flat=True,
            accepted_intent_ids=state.accepted_intent_ids | {event.intent_id},
        )
    if state.status is not SpreadStatus.OPEN:
        _reject(state, event, "close_requires_open")
    coin = str(event.payload["coin"])
    direction = SpreadDirection(str(event.payload["spread_direction"]))
    if coin != state.coin or direction is not state.direction:
        _reject(state, event, "close_mismatch")
    if event.intent_id in state.accepted_intent_ids:
        _reject(state, event, "duplicate_intent")
    return replace(
        state,
        intent_id=event.intent_id,
        status=SpreadStatus.CLOSING,
        legs=tuple(
            _invalidate_observation_freshness(
                replace(
                    leg,
                    status=LegStatus.NEW,
                    filled_quantity=Decimal("0"),
                    ack_status="none",
                    reduce_only=True,
                    client_id=derive_client_id(event.intent_id, leg.venue, reduce_only=True),
                    confirmed_unfilled=False,
                )
            )
            for leg in state.legs
        ),
        recovery_required=False,
        positions_flat=False,
        open_orders_flat=False,
        accepted_intent_ids=state.accepted_intent_ids | {event.intent_id},
    )


def _on_intent_rejected(state: SpreadState, event: ExecutionEvent) -> SpreadState:
    if state.status not in {SpreadStatus.IDLE, SpreadStatus.ARMED}:
        _reject(state, event, "reject_not_applicable")
    cleaned = _idle_after_failed_open(state)
    return replace(cleaned, recovery_required=state.recovery_required)


def _on_request_sent(state: SpreadState, event: ExecutionEvent) -> SpreadState:
    reduce_only = bool(event.payload["reduce_only"])
    recovery_send = state.recovery_required or state.status in {
        SpreadStatus.EXPOSURE_UNKNOWN,
        SpreadStatus.RECOVERING,
        SpreadStatus.HALTED,
    }
    if state.status is SpreadStatus.HALTED and not reduce_only:
        _reject(state, event, "halted")
    if reduce_only:
        if state.status not in {
            SpreadStatus.OPEN,
            SpreadStatus.CLOSING,
            SpreadStatus.RECOVERING,
            SpreadStatus.HALTED,
            SpreadStatus.EXPOSURE_UNKNOWN,
        } and not state.recovery_required:
            _reject(state, event, "reduce_only_not_applicable")
    elif state.status not in {SpreadStatus.ARMED, SpreadStatus.DISPATCHING}:
        _reject(state, event, "send_not_armed")
    venue = event.venue
    if venue is None or event.leg_id is None:
        _reject(state, event, "missing_leg")
        raise AssertionError("unreachable")
    quantity = _qty(event.payload["quantity"], "quantity")
    base_multiplier = _qty(event.payload.get("base_multiplier", "1"), "base_multiplier")
    if quantity <= 0:
        _reject(state, event, "quantity_not_positive")
    if base_multiplier <= 0:
        _reject(state, event, "base_multiplier_not_positive")
    expected = derive_client_id(event.intent_id, venue, reduce_only=reduce_only)
    client_id = str(event.payload.get("client_id") or expected)
    if client_id != expected:
        _reject(state, event, "client_id_mismatch")
    existing = state.leg_by_id(event.leg_id)
    if existing is None:
        if any(leg.venue is venue for leg in state.legs):
            _reject(state, event, "duplicate_venue")
    else:
        if existing.base_multiplier != base_multiplier:
            _reject(state, event, "base_multiplier_changed")
        first_close_send = reduce_only and existing.status is LegStatus.NEW
        recovery_flatten = (
            reduce_only
            and not existing.reduce_only
            and (recovery_send or state.status in {SpreadStatus.OPEN, SpreadStatus.CLOSING})
        )
        if not first_close_send and not recovery_flatten:
            _reject(state, event, "duplicate_send")
    stream_generation = int(event.payload.get("stream_generation", 0))
    if existing is None:
        new_leg = LegState(
            schema_version=SCHEMA_VERSION,
            leg_id=event.leg_id,
            venue=venue,
            status=LegStatus.SENT,
            client_id=expected,
            planned_quantity=quantity,
            filled_quantity=Decimal("0"),
            ack_status="none",
            reduce_only=reduce_only,
            position_quantity=Decimal("0"),
            open_order_count=0,
            position_observed=False,
            open_orders_observed=False,
            stream_generation=stream_generation,
            confirmed_unfilled=False,
            base_multiplier=base_multiplier,
        )
    else:
        new_leg = replace(
            existing,
            status=LegStatus.SENT,
            client_id=expected,
            planned_quantity=quantity,
            filled_quantity=Decimal("0"),
            ack_status="none",
            reduce_only=reduce_only,
            stream_generation=stream_generation,
            confirmed_unfilled=False,
            base_multiplier=base_multiplier,
        )
        if reduce_only:
            new_leg = _invalidate_observation_freshness(new_leg)
    if reduce_only and state.status is SpreadStatus.OPEN:
        status = SpreadStatus.CLOSING
    elif reduce_only and state.status is SpreadStatus.FLAT:
        status = SpreadStatus.RECOVERING
    elif reduce_only:
        status = state.status
    else:
        status = SpreadStatus.DISPATCHING
    updated = replace(state, legs=_replace_leg(state, new_leg), status=status)
    if reduce_only:
        updated = replace(updated, positions_flat=False, open_orders_flat=False)
    return _recompute(updated)


def _on_ack_accepted(state: SpreadState, event: ExecutionEvent) -> SpreadState:
    leg = _require_leg(state, event)
    if leg.status not in {
        LegStatus.SENT,
        LegStatus.PARTIAL,
        LegStatus.FILLED,
        LegStatus.ACK_ACCEPTED,
    }:
        _reject(state, event, "ack_not_applicable")
    new_status = leg.status
    if new_status is LegStatus.SENT:
        new_status = LegStatus.ACK_ACCEPTED
    updated = replace(
        state,
        legs=_replace_leg(state, replace(leg, status=new_status, ack_status="accepted")),
    )
    return _recompute(updated)


def _on_ack_rejected(state: SpreadState, event: ExecutionEvent) -> SpreadState:
    leg = _require_leg(state, event)
    if leg.status not in {
        LegStatus.SENT,
        LegStatus.ACK_ACCEPTED,
        LegStatus.PARTIAL,
        LegStatus.FILLED,
        LegStatus.UNKNOWN,
        LegStatus.RECONCILING,
        LegStatus.ACK_REJECTED,
    }:
        _reject(state, event, "ack_not_applicable")
    contradiction = leg.filled_quantity > 0 or _leg_observed_nonzero_position(leg)
    if contradiction:
        new_status = (
            LegStatus.UNKNOWN
            if leg.status not in {LegStatus.UNKNOWN, LegStatus.RECONCILING}
            else leg.status
        )
        confirmed = False
    else:
        new_status = LegStatus.ACK_REJECTED
        confirmed = True
    return _after_evidence(
        state,
        legs=_replace_leg(
            state,
            _invalidate_observation_freshness(
                replace(
                    leg,
                    status=new_status,
                    ack_status="rejected",
                    confirmed_unfilled=confirmed,
                )
            ),
        ),
        recovery_required=True if contradiction else state.recovery_required,
    )


def _on_ack_timeout(state: SpreadState, event: ExecutionEvent) -> SpreadState:
    leg = _require_leg(state, event)
    return _after_evidence(
        state,
        legs=_replace_leg(
            state,
            _invalidate_observation_freshness(
                replace(leg, status=LegStatus.UNKNOWN, ack_status="timeout")
            ),
        ),
        recovery_required=True,
    )


def _contradictory_flat_fill(state: SpreadState, leg: LegState) -> SpreadState:
    """Late fill after FLAT is recovery evidence; never regress stored qty."""
    return _after_evidence(
        state,
        legs=_replace_leg(
            state,
            replace(leg, status=LegStatus.UNKNOWN, confirmed_unfilled=False),
        ),
        recovery_required=True,
    )


def _on_partial_fill(state: SpreadState, event: ExecutionEvent) -> SpreadState:
    leg = _require_leg(state, event)
    qty = _qty(event.payload["quantity"], "quantity")
    if qty <= 0:
        _reject(state, event, "quantity_not_positive")
    if qty < leg.filled_quantity:
        if state.status is SpreadStatus.FLAT:
            return _contradictory_flat_fill(state, leg)
        _reject(state, event, "fill_regression")
    if qty >= leg.planned_quantity and leg.planned_quantity > 0:
        _reject(state, event, "partial_not_partial")
    new_status = LegStatus.PARTIAL
    if state.status is SpreadStatus.FLAT:
        new_status = LegStatus.UNKNOWN
    return _after_evidence(
        state,
        legs=_replace_leg(
            state,
            replace(
                leg,
                status=new_status,
                filled_quantity=qty,
                confirmed_unfilled=False,
            ),
        ),
        recovery_required=True,
    )


def _on_fill(state: SpreadState, event: ExecutionEvent) -> SpreadState:
    leg = _require_leg(state, event)
    qty = _qty(event.payload["quantity"], "quantity")
    if qty <= 0:
        _reject(state, event, "quantity_not_positive")
    if qty < leg.filled_quantity:
        if state.status is SpreadStatus.FLAT:
            return _contradictory_flat_fill(state, leg)
        _reject(state, event, "fill_regression")
    overfill = (
        leg.planned_quantity > 0
        and qty > leg.planned_quantity + state.lot_tolerance
    )
    underfill = (
        leg.planned_quantity > 0
        and qty < leg.planned_quantity - state.lot_tolerance
    )
    ack_conflict = leg.ack_status == "rejected" or leg.status in {
        LegStatus.ACK_REJECTED,
        LegStatus.CANCELLED,
    }
    if overfill or ack_conflict or underfill:
        new_status = LegStatus.UNKNOWN if (overfill or ack_conflict) else LegStatus.PARTIAL
    elif state.status is SpreadStatus.FLAT:
        new_status = LegStatus.UNKNOWN
    elif leg.status in {LegStatus.UNKNOWN, LegStatus.RECONCILING}:
        new_status = leg.status
    else:
        new_status = LegStatus.FILLED
    return _after_evidence(
        state,
        legs=_replace_leg(
            state,
            replace(
                leg,
                status=new_status,
                filled_quantity=qty,
                confirmed_unfilled=False,
            ),
        ),
        recovery_required=True
        if overfill or ack_conflict or underfill
        else state.recovery_required,
    )


def _on_cancel_requested(state: SpreadState, event: ExecutionEvent) -> SpreadState:
    leg = _require_leg(state, event)
    if state.status in {SpreadStatus.IDLE, SpreadStatus.FLAT}:
        _reject(state, event, "cancel_not_applicable")
    # Cancel-vs-fill race: pre-cancel snapshots cannot prove flat in-flight.
    # Quantities stay as history; CANCEL_ACK invalidates freshness again.
    return _sync_flat_flags(
        replace(
            state,
            legs=_replace_leg(state, _invalidate_observation_freshness(leg)),
        )
    )


def _on_cancel_ack(state: SpreadState, event: ExecutionEvent) -> SpreadState:
    leg = _require_leg(state, event)
    contradiction = (
        leg.filled_quantity > 0
        or _leg_observed_nonzero_position(leg)
        or leg.status in {LegStatus.FILLED, LegStatus.PARTIAL}
    )
    if not contradiction:
        new_status = LegStatus.CANCELLED
        confirmed = True
    else:
        new_status = (
            LegStatus.UNKNOWN
            if leg.status not in {LegStatus.UNKNOWN, LegStatus.RECONCILING}
            else leg.status
        )
        confirmed = False
    return _after_evidence(
        state,
        legs=_replace_leg(
            state,
            _invalidate_observation_freshness(
                replace(
                    leg,
                    status=new_status,
                    confirmed_unfilled=confirmed,
                )
            ),
        ),
        recovery_required=True if contradiction else state.recovery_required,
    )


def _on_position_observed(state: SpreadState, event: ExecutionEvent) -> SpreadState:
    leg = _require_leg(state, event)
    qty = _qty(event.payload["quantity"], "quantity")
    return _after_evidence(
        state,
        legs=_replace_leg(
            state, replace(leg, position_quantity=qty, position_observed=True)
        ),
    )


def _on_open_orders_observed(state: SpreadState, event: ExecutionEvent) -> SpreadState:
    leg = _require_leg(state, event)
    count = int(event.payload["open_order_count"])
    return _after_evidence(
        state,
        legs=_replace_leg(
            state, replace(leg, open_order_count=count, open_orders_observed=True)
        ),
    )


def _on_reconciliation(state: SpreadState, event: ExecutionEvent) -> SpreadState:
    matched = bool(event.payload.get("matched", False))
    if event.leg_id is None:
        if not matched:
            # Empty IDLE stays latch-only. Any live legs lose observation
            # freshness and become RECONCILING so fill-proven OPEN cannot
            # re-promote from stored fills, and last-known working-order
            # counts stay history only. STREAM_GENERATION_MISMATCH on
            # proven FLAT is a separate locked path.
            if state.status is SpreadStatus.IDLE and not state.legs:
                return replace(state, recovery_required=True)
            return _after_evidence(
                state,
                legs=tuple(
                    _invalidate_observation_freshness(
                        replace(leg, status=LegStatus.RECONCILING)
                    )
                    for leg in state.legs
                ),
                recovery_required=True,
            )
        if state.status is SpreadStatus.IDLE and not state.legs:
            return replace(
                state,
                recovery_required=False,
                stream_generation_ok=True,
            )
        if state.status is SpreadStatus.FLAT and is_proven_flat(state):
            return replace(
                state,
                recovery_required=False,
                stream_generation_ok=True,
            )
        if state.status is SpreadStatus.EXPOSURE_UNKNOWN and not state.legs:
            aborted = _idle_after_failed_open(state)
            return replace(
                aborted,
                recovery_required=False,
                stream_generation_ok=True,
            )
        return replace(state, stream_generation_ok=True)
    leg = _require_leg(state, event)
    if not matched:
        return _after_evidence(
            state,
            legs=_replace_leg(
                state,
                _invalidate_observation_freshness(
                    replace(leg, status=LegStatus.RECONCILING)
                ),
            ),
            recovery_required=True,
        )
    new_status = leg.status
    confirmed = leg.confirmed_unfilled
    if leg.status in {LegStatus.UNKNOWN, LegStatus.RECONCILING}:
        zero_rest = _fresh_zero_rest_snapshot(leg)
        if leg.reduce_only and zero_rest:
            if leg.filled_quantity > 0:
                new_status = LegStatus.FILLED
                confirmed = False
            else:
                new_status = LegStatus.CANCELLED
                confirmed = True
        elif (
            not leg.reduce_only
            and zero_rest
            and leg.filled_quantity == 0
            and not _leg_observed_nonzero_position(leg)
        ):
            new_status = LegStatus.CANCELLED
            confirmed = True
        elif _matching_positive_position_proof(leg, state.lot_tolerance):
            new_status = LegStatus.FILLED
            confirmed = False
        elif _confirmed_unfilled(leg) and not _leg_observed_nonzero_position(leg):
            new_status = LegStatus.CANCELLED
            confirmed = True
        else:
            new_status = LegStatus.RECONCILING
    legs = _replace_leg(
        state,
        replace(
            leg,
            status=new_status,
            ack_status=leg.ack_status,
            confirmed_unfilled=confirmed,
        ),
    )
    return _after_evidence(
        state,
        legs=legs,
        recovery_required=state.recovery_required,
    )


def _on_flatness_proven(state: SpreadState, event: ExecutionEvent) -> SpreadState:
    if state.status not in {
        SpreadStatus.CLOSING,
        SpreadStatus.RECOVERING,
        SpreadStatus.EXPOSURE_UNKNOWN,
    }:
        _reject(state, event, "flat_not_applicable")
    if not state.stream_generation_ok:
        _reject(state, event, "flat_requires_recon")
    if not bool(event.payload["positions_flat"]) or not bool(
        event.payload["open_orders_flat"]
    ):
        _reject(state, event, "flat_requires_recon")
    synced = _sync_flat_flags(state)
    if any(
        leg.status in {LegStatus.PARTIAL, LegStatus.UNKNOWN, LegStatus.RECONCILING}
        or timeout_blocks_flat_proof(leg)
        for leg in synced.legs
    ):
        _reject(state, event, "flat_requires_recon")
    if not flat_evidence_is_complete(
        legs=synced.legs,
        positions_flat=synced.positions_flat,
        open_orders_flat=synced.open_orders_flat,
    ):
        _reject(state, event, "flat_requires_recon")
    return replace(
        synced,
        status=SpreadStatus.FLAT,
        recovery_required=False,
        halt_reason=None,
    )


def _on_pause(state: SpreadState, event: ExecutionEvent) -> SpreadState:
    pause = bool(event.payload.get("pause", True))
    if not pause:
        return state
    return replace(state, pause_latched=True)


def _on_fault(state: SpreadState, event: ExecutionEvent) -> SpreadState:
    halt = bool(event.payload.get("halt", False))
    reason = str(event.payload.get("reason_code") or "fault")
    live_exposure = state.status not in {SpreadStatus.FLAT, SpreadStatus.IDLE}
    if halt:
        legs = (
            _invalidate_all_observation_freshness(state.legs)
            if live_exposure
            else state.legs
        )
        return replace(
            state,
            status=SpreadStatus.HALTED,
            legs=legs,
            recovery_required=True,
            halt_reason=reason,
        )
    if state.status is SpreadStatus.HALTED:
        return _sync_flat_flags(
            replace(
                state,
                legs=_invalidate_all_observation_freshness(state.legs),
                recovery_required=True,
            )
        )
    if state.status in {SpreadStatus.FLAT, SpreadStatus.IDLE}:
        return replace(state, recovery_required=True)
    return _after_evidence(
        state,
        status=SpreadStatus.EXPOSURE_UNKNOWN,
        legs=tuple(
            _invalidate_observation_freshness(replace(leg, status=LegStatus.RECONCILING))
            for leg in state.legs
        ),
        recovery_required=True,
    )


def _on_stream_mismatch(state: SpreadState, event: ExecutionEvent) -> SpreadState:
    del event
    if state.status is SpreadStatus.FLAT:
        return replace(
            state,
            stream_generation_ok=False,
            recovery_required=True,
        )
    legs = _invalidate_all_observation_freshness(state.legs)
    if state.status is SpreadStatus.HALTED:
        return _sync_flat_flags(
            replace(
                state,
                legs=legs,
                stream_generation_ok=False,
                recovery_required=True,
            )
        )
    if state.status is SpreadStatus.IDLE:
        return replace(
            state,
            stream_generation_ok=False,
            recovery_required=True,
        )
    return _after_evidence(
        state,
        status=SpreadStatus.EXPOSURE_UNKNOWN,
        legs=legs,
        stream_generation_ok=False,
        recovery_required=True,
    )


def _on_unknown_correlation(state: SpreadState, event: ExecutionEvent) -> SpreadState:
    existing = state.leg_by_id(event.leg_id) if event.leg_id is not None else None
    if existing is not None:
        legs = _replace_leg(
            state,
            _invalidate_observation_freshness(
                replace(existing, status=LegStatus.UNKNOWN)
            ),
        )
    else:
        legs = tuple(
            _invalidate_observation_freshness(replace(leg, status=LegStatus.UNKNOWN))
            for leg in state.legs
        )
    if state.status is SpreadStatus.HALTED:
        return _sync_flat_flags(replace(state, legs=legs, recovery_required=True))
    return _after_evidence(
        state,
        status=SpreadStatus.EXPOSURE_UNKNOWN,
        legs=legs,
        recovery_required=True,
    )
