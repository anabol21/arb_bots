"""Fill-authoritative spread state machine tests. No sockets or secrets."""

from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Any, Optional

from app.bot.execution.contracts import (
    SCHEMA_VERSION,
    ContractValidationError,
    ExecutionEvent,
    ExecutionEventType,
    LegPlan,
    LegStatus,
    SpreadState,
    SpreadStatus,
    Venue,
    derive_client_id,
)
from app.bot.execution.state_machine import (
    InvalidTransition,
    apply_event,
    apply_events,
    initial_spread_state,
    is_proven_flat,
    needs_reconciliation,
    opens_allowed,
)

INTENT_ID = "95790b7c-8b21-4e82-bae2-1832ddc5bee1"
CLOSE_INTENT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
NEXT_INTENT_ID = "c1c1c1c1-d2d2-4e3e-8f4f-a5a5a5a5a5a5"
RUN_ID = "run_aa11bb22cc33dd44ee55ff6677889900"
OKX_LEG = "leg_okx"
BYBIT_LEG = "leg_bybit"
QTY = "1"


class Clock:
    def __init__(self) -> None:
        self.seq = 0
        self.mono = 1000

    def next(self) -> tuple[int, int]:
        self.seq += 1
        self.mono += 1
        return self.seq, self.mono


def _event(
    clock: Clock,
    event_type: ExecutionEventType,
    *,
    intent_id: str = INTENT_ID,
    venue: Optional[Venue] = None,
    leg_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    event_id: Optional[str] = None,
    sequence: Optional[int] = None,
    monotonic_ns: Optional[int] = None,
) -> ExecutionEvent:
    seq, mono = clock.next()
    if sequence is not None:
        seq = sequence
        clock.seq = sequence
    if monotonic_ns is not None:
        mono = monotonic_ns
        clock.mono = monotonic_ns
    eid = event_id or f"evt_{mono:032d}"
    return ExecutionEvent(
        schema_version=SCHEMA_VERSION,
        event_id=eid,
        event_type=event_type,
        intent_id=intent_id,
        run_id=RUN_ID,
        sequence=seq,
        monotonic_ns=mono,
        venue=venue,
        leg_id=leg_id,
        payload=payload or {},
    )


def _arm(clock: Clock, *, intent_id: str = INTENT_ID) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.INTENT_ACCEPTED,
        intent_id=intent_id,
        payload={
            "action": "open",
            "coin": "BTC",
            "spread_direction": "long",
            "lot_tolerance": "0",
        },
    )


def _sent(
    clock: Clock,
    venue: Venue,
    leg_id: str,
    *,
    intent_id: str = INTENT_ID,
    reduce_only: bool = False,
    quantity: str = QTY,
) -> ExecutionEvent:
    side = "buy" if venue is Venue.OKX else "sell"
    if reduce_only:
        side = "sell" if venue is Venue.OKX else "buy"
    instrument = "BTC-USDT-SWAP" if venue is Venue.OKX else "BTCUSDT"
    return _event(
        clock,
        ExecutionEventType.REQUEST_SENT,
        intent_id=intent_id,
        venue=venue,
        leg_id=leg_id,
        payload={
            "quantity": quantity,
            "reduce_only": reduce_only,
            "instrument": instrument,
            "side": side,
            "client_id": derive_client_id(intent_id, venue, reduce_only=reduce_only),
        },
    )


def _ack(
    clock: Clock,
    venue: Venue,
    leg_id: str,
    *,
    ok: bool = True,
    intent_id: str = INTENT_ID,
) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.ACK_ACCEPTED if ok else ExecutionEventType.ACK_REJECTED,
        intent_id=intent_id,
        venue=venue,
        leg_id=leg_id,
        payload={} if ok else {"reason_code": "venue_rejected"},
    )


def _fill(
    clock: Clock,
    venue: Venue,
    leg_id: str,
    *,
    quantity: str = QTY,
    partial: bool = False,
    intent_id: str = INTENT_ID,
) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.PARTIAL_FILL if partial else ExecutionEventType.FILL,
        intent_id=intent_id,
        venue=venue,
        leg_id=leg_id,
        payload={"quantity": quantity},
    )


def _pos(
    clock: Clock,
    venue: Venue,
    leg_id: str,
    quantity: str,
    *,
    intent_id: str = INTENT_ID,
) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.POSITION_OBSERVED,
        intent_id=intent_id,
        venue=venue,
        leg_id=leg_id,
        payload={"quantity": quantity},
    )


def _orders(
    clock: Clock,
    venue: Venue,
    leg_id: str,
    count: int,
    *,
    intent_id: str = INTENT_ID,
) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.OPEN_ORDERS_OBSERVED,
        intent_id=intent_id,
        venue=venue,
        leg_id=leg_id,
        payload={"open_order_count": count},
    )


def _close_arm(
    clock: Clock,
    *,
    intent_id: str = CLOSE_INTENT_ID,
    coin: str = "BTC",
    spread_direction: str = "long",
) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.INTENT_ACCEPTED,
        intent_id=intent_id,
        payload={
            "action": "close",
            "coin": coin,
            "spread_direction": spread_direction,
            "lot_tolerance": "0",
        },
    )


def _recon(
    clock: Clock,
    venue: Venue,
    leg_id: str,
    *,
    matched: bool = True,
    intent_id: str = INTENT_ID,
) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.RECONCILIATION,
        intent_id=intent_id,
        venue=venue,
        leg_id=leg_id,
        payload={"matched": matched},
    )


def _global_recon(
    clock: Clock,
    *,
    matched: bool = True,
    intent_id: str = INTENT_ID,
) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.RECONCILIATION,
        intent_id=intent_id,
        payload={"matched": matched},
    )


def _flatness(
    clock: Clock,
    *,
    intent_id: str = INTENT_ID,
) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.FLATNESS_PROVEN,
        intent_id=intent_id,
        payload={"positions_flat": True, "open_orders_flat": True},
    )


def _observe_both_flat(
    clock: Clock,
    *,
    intent_id: str = INTENT_ID,
) -> list[ExecutionEvent]:
    return [
        _pos(clock, Venue.OKX, OKX_LEG, "0", intent_id=intent_id),
        _pos(clock, Venue.BYBIT, BYBIT_LEG, "0", intent_id=intent_id),
        _orders(clock, Venue.OKX, OKX_LEG, 0, intent_id=intent_id),
        _orders(clock, Venue.BYBIT, BYBIT_LEG, 0, intent_id=intent_id),
    ]


def _dispatch_open(clock: Clock) -> list[ExecutionEvent]:
    return [
        _arm(clock),
        _sent(clock, Venue.OKX, OKX_LEG),
        _sent(clock, Venue.BYBIT, BYBIT_LEG),
    ]


def _happy_open_events(clock: Clock) -> list[ExecutionEvent]:
    return [
        *_dispatch_open(clock),
        _ack(clock, Venue.OKX, OKX_LEG),
        _ack(clock, Venue.BYBIT, BYBIT_LEG),
        _fill(clock, Venue.OKX, OKX_LEG),
        _fill(clock, Venue.BYBIT, BYBIT_LEG),
    ]


class StateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.state = initial_spread_state(run_id=RUN_ID)

    def test_happy_open_and_close_acks_before_fills(self) -> None:
        events = _happy_open_events(self.clock)
        opened = apply_events(self.state, events)
        self.assertEqual(opened.status, SpreadStatus.OPEN)
        self.assertFalse(opens_allowed(opened))
        self.assertEqual(opened.legs[0].ack_status, "accepted")
        self.clock.seq = 0
        cid = CLOSE_INTENT_ID
        close = [
            _close_arm(self.clock, intent_id=cid),
            _sent(self.clock, Venue.OKX, OKX_LEG, intent_id=cid, reduce_only=True),
            _sent(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid, reduce_only=True),
            _ack(self.clock, Venue.OKX, OKX_LEG, intent_id=cid),
            _ack(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
            _fill(self.clock, Venue.OKX, OKX_LEG, intent_id=cid),
            _fill(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
            _pos(self.clock, Venue.OKX, OKX_LEG, "0", intent_id=cid),
            _pos(self.clock, Venue.BYBIT, BYBIT_LEG, "0", intent_id=cid),
            _orders(self.clock, Venue.OKX, OKX_LEG, 0, intent_id=cid),
            _orders(self.clock, Venue.BYBIT, BYBIT_LEG, 0, intent_id=cid),
            _event(
                self.clock,
                ExecutionEventType.FLATNESS_PROVEN,
                intent_id=cid,
                payload={"positions_flat": True, "open_orders_flat": True},
            ),
        ]
        closed = apply_events(opened, close)
        self.assertEqual(closed.status, SpreadStatus.FLAT)
        self.assertTrue(is_proven_flat(closed))
        self.assertTrue(opens_allowed(closed))

    def test_fills_before_acks(self) -> None:
        events = [
            *_dispatch_open(self.clock),
            _fill(self.clock, Venue.OKX, OKX_LEG),
            _fill(self.clock, Venue.BYBIT, BYBIT_LEG),
        ]
        opened = apply_events(self.state, events)
        self.assertEqual(opened.status, SpreadStatus.OPEN)
        acked = apply_events(
            opened,
            [
                _ack(self.clock, Venue.OKX, OKX_LEG),
                _ack(self.clock, Venue.BYBIT, BYBIT_LEG),
            ],
        )
        self.assertEqual(acked.status, SpreadStatus.OPEN)
        self.assertEqual({leg.ack_status for leg in acked.legs}, {"accepted"})
        self.assertEqual({leg.filled_quantity for leg in acked.legs}, {Decimal("1")})

    def test_ack_only_never_opens(self) -> None:
        events = [
            *_dispatch_open(self.clock),
            _ack(self.clock, Venue.OKX, OKX_LEG),
            _ack(self.clock, Venue.BYBIT, BYBIT_LEG),
        ]
        state = apply_events(self.state, events)
        self.assertEqual(state.status, SpreadStatus.DISPATCHING)
        self.assertNotEqual(state.status, SpreadStatus.OPEN)
        self.assertFalse(opens_allowed(state))
        self.assertTrue(all(leg.filled_quantity == 0 for leg in state.legs))
        self.assertTrue(all(leg.position_quantity == 0 for leg in state.legs))
        self.assertFalse(state.recovery_required)

    def test_one_reject_plus_one_fill_enters_recovery(self) -> None:
        events = [
            *_dispatch_open(self.clock),
            _fill(self.clock, Venue.OKX, OKX_LEG),
            _ack(self.clock, Venue.BYBIT, BYBIT_LEG, ok=False),
        ]
        state = apply_events(self.state, events)
        self.assertEqual(state.status, SpreadStatus.RECOVERING)
        self.assertTrue(state.recovery_required)
        self.assertTrue(needs_reconciliation(state))
        self.assertFalse(opens_allowed(state))
        public = state.to_public_dict()
        self.assertNotIn("peer_open", public)
        self.assertNotIn("peer_open_instruction", public)
        bybit = state.leg_by_id(BYBIT_LEG)
        assert bybit is not None
        self.assertTrue(bybit.confirmed_unfilled)
        self.assertEqual(bybit.filled_quantity, Decimal("0"))

    def test_partial_fill_then_full_fill(self) -> None:
        events = [
            *_dispatch_open(self.clock),
            _fill(self.clock, Venue.OKX, OKX_LEG, quantity="0.4", partial=True),
        ]
        partial = apply_events(self.state, events)
        self.assertEqual(partial.status, SpreadStatus.EXPOSURE_UNKNOWN)
        self.assertTrue(needs_reconciliation(partial))
        self.assertFalse(opens_allowed(partial))
        opened = apply_events(
            partial,
            [
                _fill(self.clock, Venue.OKX, OKX_LEG, quantity="1"),
                _fill(self.clock, Venue.BYBIT, BYBIT_LEG, quantity="1"),
            ],
        )
        self.assertEqual(opened.status, SpreadStatus.OPEN)
        self.assertFalse(opened.recovery_required)

    def test_partial_fill_then_cancel(self) -> None:
        events = [
            *_dispatch_open(self.clock),
            _fill(self.clock, Venue.OKX, OKX_LEG, quantity="0.4", partial=True),
            _event(
                self.clock,
                ExecutionEventType.CANCEL_REQUESTED,
                venue=Venue.OKX,
                leg_id=OKX_LEG,
                payload={},
            ),
            _event(
                self.clock,
                ExecutionEventType.CANCEL_ACK,
                venue=Venue.OKX,
                leg_id=OKX_LEG,
                payload={},
            ),
        ]
        state = apply_events(self.state, events)
        self.assertNotEqual(state.status, SpreadStatus.FLAT)
        self.assertFalse(is_proven_flat(state))
        self.assertTrue(state.recovery_required)
        self.assertEqual(state.status, SpreadStatus.EXPOSURE_UNKNOWN)
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(
                state,
                _event(
                    self.clock,
                    ExecutionEventType.FLATNESS_PROVEN,
                    payload={"positions_flat": True, "open_orders_flat": True},
                ),
            )
        self.assertIn("flat_requires_recon", str(ctx.exception))
        self.assertIn(INTENT_ID, str(ctx.exception))
        self.assertNotIn("api_key", str(ctx.exception))

    def test_ack_timeout_and_unknown_correlation(self) -> None:
        timeout = apply_events(
            self.state,
            [
                *_dispatch_open(self.clock),
                _event(
                    self.clock,
                    ExecutionEventType.ACK_TIMEOUT,
                    venue=Venue.OKX,
                    leg_id=OKX_LEG,
                    payload={"reason_code": "ack_timeout"},
                ),
            ],
        )
        self.assertEqual(timeout.status, SpreadStatus.EXPOSURE_UNKNOWN)
        self.assertTrue(needs_reconciliation(timeout))
        self.assertFalse(opens_allowed(timeout))
        unknown = apply_event(
            timeout,
            _event(
                self.clock,
                ExecutionEventType.UNKNOWN_CORRELATION,
                venue=Venue.BYBIT,
                leg_id=BYBIT_LEG,
                payload={"reason_code": "unknown_correlation"},
            ),
        )
        self.assertEqual(unknown.status, SpreadStatus.EXPOSURE_UNKNOWN)
        self.assertTrue(needs_reconciliation(unknown))

    def test_duplicate_replay_and_conflicting_duplicate(self) -> None:
        events = _happy_open_events(self.clock)
        opened = apply_events(self.state, events)
        replayed = apply_event(opened, events[-1])
        self.assertEqual(replayed.to_public_dict(), opened.to_public_dict())
        conflict = ExecutionEvent.from_public_dict(events[-1].to_public_dict())
        raw = conflict.to_public_dict()
        raw["event_id"] = "evt_conflict_0000000000000000000001"
        raw["payload"] = {"quantity": "2"}
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(opened, ExecutionEvent.from_public_dict(raw))
        self.assertIn("corrupt_sequence", str(ctx.exception))

    def test_process_restart_reconstruction(self) -> None:
        events = _happy_open_events(self.clock)
        live = apply_events(self.state, events)
        restarted = apply_events(initial_spread_state(run_id=RUN_ID), events)
        self.assertEqual(live.to_public_dict(), restarted.to_public_dict())
        twice = apply_events(restarted, events)
        self.assertEqual(twice.to_public_dict(), live.to_public_dict())

    def test_positions_flat_but_open_order_remains(self) -> None:
        opened = apply_events(self.state, _happy_open_events(self.clock))
        self.clock.seq = 0
        cid = CLOSE_INTENT_ID
        closing = apply_events(
            opened,
            [
                _close_arm(self.clock, intent_id=cid),
                _sent(self.clock, Venue.OKX, OKX_LEG, intent_id=cid, reduce_only=True),
                _sent(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid, reduce_only=True),
                _ack(self.clock, Venue.OKX, OKX_LEG, intent_id=cid),
                _ack(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
                _event(
                    self.clock,
                    ExecutionEventType.CANCEL_ACK,
                    intent_id=cid,
                    venue=Venue.OKX,
                    leg_id=OKX_LEG,
                    payload={},
                ),
                _pos(self.clock, Venue.OKX, OKX_LEG, "0", intent_id=cid),
                _pos(self.clock, Venue.BYBIT, BYBIT_LEG, "0", intent_id=cid),
                _orders(self.clock, Venue.OKX, OKX_LEG, 1, intent_id=cid),
                _orders(self.clock, Venue.BYBIT, BYBIT_LEG, 0, intent_id=cid),
            ],
        )
        self.assertTrue(closing.positions_flat)
        self.assertFalse(closing.open_orders_flat)
        self.assertNotEqual(closing.status, SpreadStatus.FLAT)
        self.assertFalse(is_proven_flat(closing))
        self.assertFalse(opens_allowed(closing))
        with self.assertRaises(InvalidTransition):
            apply_event(
                closing,
                _event(
                    self.clock,
                    ExecutionEventType.FLATNESS_PROVEN,
                    intent_id=CLOSE_INTENT_ID,
                    payload={"positions_flat": True, "open_orders_flat": True},
                ),
            )

    def test_stale_stream_generation_mismatch(self) -> None:
        dispatched = apply_events(self.state, _dispatch_open(self.clock))
        mismatched = apply_event(
            dispatched,
            _event(
                self.clock,
                ExecutionEventType.STREAM_GENERATION_MISMATCH,
                payload={"stream_generation": 2, "reason_code": "stream_generation_mismatch"},
            ),
        )
        self.assertEqual(mismatched.status, SpreadStatus.EXPOSURE_UNKNOWN)
        self.assertFalse(mismatched.stream_generation_ok)
        self.assertTrue(needs_reconciliation(mismatched))
        self.assertFalse(opens_allowed(mismatched))
        filled = apply_events(
            mismatched,
            [
                _fill(self.clock, Venue.OKX, OKX_LEG),
                _fill(self.clock, Venue.BYBIT, BYBIT_LEG),
            ],
        )
        self.assertEqual(filled.status, SpreadStatus.EXPOSURE_UNKNOWN)
        self.assertFalse(opens_allowed(filled))

    def test_spread_state_round_trip_and_foreign_intent_rejected(self) -> None:
        opened = apply_events(self.state, _happy_open_events(self.clock))
        restored = type(opened).from_public_dict(opened.to_public_dict())
        self.assertEqual(restored.to_public_dict(), opened.to_public_dict())
        self.assertEqual(opened.accepted_intent_ids, frozenset({INTENT_ID}))
        self.assertEqual(restored.accepted_intent_ids, frozenset({INTENT_ID}))
        self.assertEqual(restored.intent_id, restored.open_intent_id)
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(
                opened,
                _event(
                    self.clock,
                    ExecutionEventType.PAUSE,
                    intent_id="bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
                    payload={"pause": True, "reason_code": "pause"},
                ),
            )
        self.assertIn("intent_mismatch", str(ctx.exception))
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(
                opened,
                _event(
                    self.clock,
                    ExecutionEventType.PAUSE,
                    payload={"pause": True, "reason_code": "pause"},
                    monotonic_ns=1,
                ),
            )
        self.assertIn("decreasing_monotonic", str(ctx.exception))

    def test_leg_plan_matches_state_client_ids(self) -> None:
        dispatched = apply_events(self.state, _dispatch_open(self.clock))
        okx = LegPlan.build(
            intent_id=INTENT_ID,
            leg_id=OKX_LEG,
            venue=Venue.OKX,
            instrument="BTC-USDT-SWAP",
            side="buy",
            quantity=Decimal("1"),
        )
        bybit = LegPlan.build(
            intent_id=INTENT_ID,
            leg_id=BYBIT_LEG,
            venue=Venue.BYBIT,
            instrument="BTCUSDT",
            side="sell",
            quantity=Decimal("1"),
        )
        self.assertEqual(dispatched.leg_by_id(OKX_LEG).client_id, okx.client_id)
        self.assertEqual(dispatched.leg_by_id(BYBIT_LEG).client_id, bybit.client_id)


class CriticRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.state = initial_spread_state(run_id=RUN_ID)

    def test_halted_is_sticky_against_fill_unknown_and_open(self) -> None:
        dispatched = apply_events(self.state, _dispatch_open(self.clock))
        halted = apply_event(
            dispatched,
            _event(
                self.clock,
                ExecutionEventType.FAULT,
                payload={"halt": True, "reason_code": "halt"},
            ),
        )
        self.assertEqual(halted.status, SpreadStatus.HALTED)
        filled = apply_events(
            halted,
            [
                _fill(self.clock, Venue.OKX, OKX_LEG),
                _fill(self.clock, Venue.BYBIT, BYBIT_LEG),
            ],
        )
        self.assertEqual(filled.status, SpreadStatus.HALTED)
        self.assertTrue(filled.recovery_required)
        self.assertFalse(opens_allowed(filled))
        unknown = apply_event(
            filled,
            _event(
                self.clock,
                ExecutionEventType.UNKNOWN_CORRELATION,
                venue=Venue.OKX,
                leg_id="leg_third",
                payload={"reason_code": "unknown_correlation"},
            ),
        )
        self.assertEqual(unknown.status, SpreadStatus.HALTED)
        self.assertEqual(len(unknown.legs), 2)
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(unknown, _arm(self.clock))
        self.assertIn("open_not_allowed", str(ctx.exception))
        self.assertEqual(unknown.status, SpreadStatus.HALTED)

    def test_intent_accepted_open_cannot_clear_recovery_latch(self) -> None:
        faulted = apply_event(
            self.state,
            _event(
                self.clock,
                ExecutionEventType.FAULT,
                payload={"halt": False, "reason_code": "fault"},
            ),
        )
        self.assertTrue(faulted.recovery_required)
        self.assertFalse(opens_allowed(faulted))
        self.clock.seq = 0
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(faulted, _arm(self.clock, intent_id=NEXT_INTENT_ID))
        self.assertIn("open_not_allowed", str(ctx.exception))
        self.assertTrue(faulted.recovery_required)

    def test_same_event_id_conflicting_content_is_corrupt(self) -> None:
        events = _happy_open_events(self.clock)
        opened = apply_events(self.state, events)
        raw = events[-1].to_public_dict()
        raw["payload"] = {"quantity": "2"}
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(opened, ExecutionEvent.from_public_dict(raw))
        self.assertIn("corrupt_event", str(ctx.exception))

    def test_same_sequence_same_content_records_alternate_event_id(self) -> None:
        events = _happy_open_events(self.clock)
        opened = apply_events(self.state, events)
        raw = events[-1].to_public_dict()
        raw["event_id"] = "evt_alternate_00000000000000000001"
        alternate = ExecutionEvent.from_public_dict(raw)
        recorded = apply_event(opened, alternate)
        self.assertEqual(recorded.status, opened.status)
        self.assertIn(events[-1].event_id, recorded.applied_event_ids)
        self.assertIn(alternate.event_id, recorded.applied_event_ids)
        self.assertEqual(recorded.event_hashes[alternate.event_id], alternate.content_hash())
        self.assertEqual(recorded.sequence_hashes, opened.sequence_hashes)

    def test_reject_then_new_intent_seq_one_and_restart_fold(self) -> None:
        first = [
            _arm(self.clock),
            _event(
                self.clock,
                ExecutionEventType.INTENT_REJECTED,
                payload={"reason_code": "intent_rejected"},
            ),
        ]
        rejected = apply_events(self.state, first)
        self.assertEqual(rejected.status, SpreadStatus.IDLE)
        self.assertIsNone(rejected.intent_id)
        self.assertIsNone(rejected.open_intent_id)
        self.assertEqual(rejected.legs, ())
        self.clock.seq = 0
        armed = apply_event(rejected, _arm(self.clock, intent_id=NEXT_INTENT_ID))
        self.assertEqual(armed.status, SpreadStatus.ARMED)
        self.assertEqual(armed.intent_id, NEXT_INTENT_ID)
        self.assertEqual(armed.last_sequence, 1)

        replay_clock = Clock()
        fold = [
            _arm(replay_clock),
            _event(
                replay_clock,
                ExecutionEventType.INTENT_REJECTED,
                payload={"reason_code": "intent_rejected"},
            ),
        ]
        replay_clock.seq = 0
        fold.append(_arm(replay_clock, intent_id=NEXT_INTENT_ID))
        live = apply_events(self.state, fold)
        restarted = apply_events(initial_spread_state(run_id=RUN_ID), fold)
        self.assertEqual(live.to_public_dict(), restarted.to_public_dict())
        self.assertEqual(live.last_sequence, 1)
        self.assertEqual(live.intent_id, NEXT_INTENT_ID)

    def test_unexpected_third_leg_does_not_insert_or_open(self) -> None:
        dispatched = apply_events(self.state, _dispatch_open(self.clock))
        unknown = apply_event(
            dispatched,
            _event(
                self.clock,
                ExecutionEventType.UNKNOWN_CORRELATION,
                venue=Venue.OKX,
                leg_id="leg_third",
                payload={"reason_code": "unknown_correlation"},
            ),
        )
        self.assertEqual(unknown.status, SpreadStatus.EXPOSURE_UNKNOWN)
        self.assertTrue(unknown.recovery_required)
        self.assertEqual(len(unknown.legs), 2)
        self.assertIsNone(unknown.leg_by_id("leg_third"))
        self.assertTrue(all(leg.status is LegStatus.UNKNOWN for leg in unknown.legs))
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(
                unknown,
                _fill(self.clock, Venue.OKX, "leg_third"),
            )
        self.assertIn("unknown_leg", str(ctx.exception))
        self.clock.seq = unknown.last_sequence
        later = apply_events(
            unknown,
            [
                _fill(self.clock, Venue.OKX, OKX_LEG),
                _fill(self.clock, Venue.BYBIT, BYBIT_LEG),
            ],
        )
        self.assertEqual(later.status, SpreadStatus.EXPOSURE_UNKNOWN)
        self.assertNotEqual(later.status, SpreadStatus.OPEN)
        self.assertTrue(all(leg.status is LegStatus.UNKNOWN for leg in later.legs))
        self.assertEqual(later.leg_by_id(OKX_LEG).filled_quantity, Decimal("1"))

    def test_fill_on_unknown_leg_updates_evidence_not_status(self) -> None:
        timeout = apply_events(
            self.state,
            [
                *_dispatch_open(self.clock),
                _event(
                    self.clock,
                    ExecutionEventType.ACK_TIMEOUT,
                    venue=Venue.OKX,
                    leg_id=OKX_LEG,
                    payload={"reason_code": "ack_timeout"},
                ),
            ],
        )
        filled = apply_event(timeout, _fill(self.clock, Venue.OKX, OKX_LEG))
        okx = filled.leg_by_id(OKX_LEG)
        assert okx is not None
        self.assertEqual(okx.status, LegStatus.UNKNOWN)
        self.assertEqual(okx.filled_quantity, Decimal("1"))
        self.assertEqual(filled.status, SpreadStatus.EXPOSURE_UNKNOWN)
        self.assertTrue(needs_reconciliation(filled))

    def test_fault_then_fills_do_not_open(self) -> None:
        dispatched = apply_events(self.state, _dispatch_open(self.clock))
        faulted = apply_event(
            dispatched,
            _event(
                self.clock,
                ExecutionEventType.FAULT,
                payload={"halt": False, "reason_code": "fault"},
            ),
        )
        self.assertEqual(faulted.status, SpreadStatus.EXPOSURE_UNKNOWN)
        self.assertTrue(faulted.recovery_required)
        self.assertTrue(all(leg.status is LegStatus.RECONCILING for leg in faulted.legs))
        filled = apply_events(
            faulted,
            [
                _fill(self.clock, Venue.OKX, OKX_LEG),
                _fill(self.clock, Venue.BYBIT, BYBIT_LEG),
            ],
        )
        self.assertEqual(filled.status, SpreadStatus.EXPOSURE_UNKNOWN)
        self.assertNotEqual(filled.status, SpreadStatus.OPEN)
        self.assertTrue(filled.recovery_required)
        self.assertTrue(all(leg.status is LegStatus.RECONCILING for leg in filled.legs))
        self.assertTrue(needs_reconciliation(filled))
        self.assertFalse(opens_allowed(filled))

    def test_nonzero_position_without_matching_proof_blocks_open(self) -> None:
        dispatched = apply_events(self.state, _dispatch_open(self.clock))
        observed = apply_events(
            dispatched,
            [
                _pos(self.clock, Venue.OKX, OKX_LEG, "2"),
                _pos(self.clock, Venue.BYBIT, BYBIT_LEG, "2"),
            ],
        )
        self.assertNotEqual(observed.status, SpreadStatus.OPEN)
        self.assertTrue(observed.recovery_required)
        self.assertTrue(needs_reconciliation(observed))
        self.assertFalse(opens_allowed(observed))

    def test_position_evidence_can_prove_open(self) -> None:
        dispatched = apply_events(self.state, _dispatch_open(self.clock))
        opened = apply_events(
            dispatched,
            [
                _pos(self.clock, Venue.OKX, OKX_LEG, "1"),
                _pos(self.clock, Venue.BYBIT, BYBIT_LEG, "1"),
            ],
        )
        self.assertEqual(opened.status, SpreadStatus.OPEN)
        self.assertFalse(opened.recovery_required)
        self.assertTrue(all(leg.status is not LegStatus.FILLED for leg in opened.legs))
        self.assertTrue(all(leg.position_quantity == Decimal("1") for leg in opened.legs))

    def test_double_reject_requires_flat_proof_then_seq_one(self) -> None:
        rejected = apply_events(
            self.state,
            [
                *_dispatch_open(self.clock),
                _ack(self.clock, Venue.OKX, OKX_LEG, ok=False),
                _ack(self.clock, Venue.BYBIT, BYBIT_LEG, ok=False),
            ],
        )
        self.assertIn(
            rejected.status,
            {SpreadStatus.RECOVERING, SpreadStatus.EXPOSURE_UNKNOWN},
        )
        self.assertNotEqual(rejected.status, SpreadStatus.IDLE)
        self.assertEqual(len(rejected.legs), 2)
        self.assertTrue(rejected.recovery_required)
        self.assertFalse(opens_allowed(rejected))
        self.assertEqual(rejected.intent_id, INTENT_ID)
        self.clock.seq = rejected.last_sequence
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(rejected, _arm(self.clock, intent_id=NEXT_INTENT_ID))
        self.assertIn(ctx.exception.code, {"open_not_allowed", "intent_mismatch"})
        self.clock.seq = rejected.last_sequence
        proven = apply_events(
            rejected,
            [*_observe_both_flat(self.clock), _flatness(self.clock)],
        )
        self.assertEqual(proven.status, SpreadStatus.FLAT)
        self.assertTrue(is_proven_flat(proven))
        self.assertTrue(opens_allowed(proven))
        self.assertFalse(proven.recovery_required)
        self.clock.seq = 0
        armed = apply_event(proven, _arm(self.clock, intent_id=NEXT_INTENT_ID))
        self.assertEqual(armed.last_sequence, 1)
        self.assertEqual(armed.intent_id, NEXT_INTENT_ID)
        self.assertEqual(armed.status, SpreadStatus.ARMED)

    def test_forged_flat_restore_cannot_lie(self) -> None:
        raw = self.state.to_public_dict()
        raw["status"] = "FLAT"
        with self.assertRaises(ContractValidationError):
            SpreadState.from_public_dict(raw)


class SafetyReview2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.state = initial_spread_state(run_id=RUN_ID)

    def test_one_exact_position_only_requires_recovery(self) -> None:
        state = apply_events(
            self.state,
            [
                *_dispatch_open(self.clock),
                _pos(self.clock, Venue.OKX, OKX_LEG, QTY),
            ],
        )
        self.assertIn(
            state.status,
            {SpreadStatus.EXPOSURE_UNKNOWN, SpreadStatus.RECOVERING},
        )
        self.assertNotEqual(state.status, SpreadStatus.DISPATCHING)
        self.assertNotEqual(state.status, SpreadStatus.OPEN)
        self.assertTrue(state.recovery_required)
        self.assertTrue(needs_reconciliation(state))
        self.assertFalse(opens_allowed(state))

    def test_one_position_plus_rejected_peer_enters_recovering(self) -> None:
        state = apply_events(
            self.state,
            [
                *_dispatch_open(self.clock),
                _pos(self.clock, Venue.OKX, OKX_LEG, QTY),
                _ack(self.clock, Venue.BYBIT, BYBIT_LEG, ok=False),
            ],
        )
        self.assertEqual(state.status, SpreadStatus.RECOVERING)
        self.assertTrue(state.recovery_required)
        self.assertTrue(needs_reconciliation(state))
        self.assertFalse(opens_allowed(state))
        bybit = state.leg_by_id(BYBIT_LEG)
        assert bybit is not None
        self.assertTrue(bybit.confirmed_unfilled)

    def test_rejected_leg_plus_both_positions_never_open(self) -> None:
        state = apply_events(
            self.state,
            [
                *_dispatch_open(self.clock),
                _ack(self.clock, Venue.OKX, OKX_LEG, ok=False),
                _pos(self.clock, Venue.OKX, OKX_LEG, QTY),
                _pos(self.clock, Venue.BYBIT, BYBIT_LEG, QTY),
            ],
        )
        self.assertNotEqual(state.status, SpreadStatus.OPEN)
        self.assertTrue(state.recovery_required)
        self.assertTrue(needs_reconciliation(state))
        okx = state.leg_by_id(OKX_LEG)
        assert okx is not None
        self.assertEqual(okx.status, LegStatus.ACK_REJECTED)
        self.assertEqual(okx.position_quantity, Decimal("1"))

    def test_double_reject_with_nonzero_position_never_idle(self) -> None:
        state = apply_events(
            self.state,
            [
                *_dispatch_open(self.clock),
                _ack(self.clock, Venue.OKX, OKX_LEG, ok=False),
                _pos(self.clock, Venue.OKX, OKX_LEG, QTY),
                _pos(self.clock, Venue.BYBIT, BYBIT_LEG, QTY),
                _ack(self.clock, Venue.BYBIT, BYBIT_LEG, ok=False),
            ],
        )
        self.assertNotEqual(state.status, SpreadStatus.IDLE)
        self.assertNotEqual(state.status, SpreadStatus.OPEN)
        self.assertTrue(state.recovery_required)
        self.assertTrue(needs_reconciliation(state))
        self.assertEqual(len(state.legs), 2)
        self.assertTrue(
            any(leg.position_observed and leg.position_quantity > 0 for leg in state.legs)
        )

    def test_forged_open_restore_cannot_lie(self) -> None:
        dispatched = apply_events(self.state, _dispatch_open(self.clock))
        raw = dispatched.to_public_dict()
        raw["status"] = "OPEN"
        with self.assertRaises(ContractValidationError):
            SpreadState.from_public_dict(raw)

    def test_same_intent_reuse_after_reject_is_duplicate(self) -> None:
        first = [
            _arm(self.clock),
            _event(
                self.clock,
                ExecutionEventType.INTENT_REJECTED,
                payload={"reason_code": "intent_rejected"},
            ),
        ]
        rejected = apply_events(self.state, first)
        self.assertEqual(rejected.status, SpreadStatus.IDLE)
        self.assertEqual(rejected.accepted_intent_ids, frozenset({INTENT_ID}))
        restored = SpreadState.from_public_dict(rejected.to_public_dict())
        self.assertEqual(restored.accepted_intent_ids, frozenset({INTENT_ID}))
        replayed = apply_event(rejected, first[0])
        self.assertEqual(replayed.to_public_dict(), rejected.to_public_dict())
        self.clock.seq = 0
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(rejected, _arm(self.clock, intent_id=INTENT_ID))
        self.assertIn("duplicate_intent", str(ctx.exception))
        self.clock.seq = 0
        armed = apply_event(rejected, _arm(self.clock, intent_id=NEXT_INTENT_ID))
        self.assertEqual(armed.status, SpreadStatus.ARMED)
        self.assertEqual(
            armed.accepted_intent_ids,
            frozenset({INTENT_ID, NEXT_INTENT_ID}),
        )

    def test_same_intent_reuse_after_flat_round_trip_is_duplicate(self) -> None:
        opened = apply_events(self.state, _happy_open_events(self.clock))
        self.clock.seq = 0
        cid = CLOSE_INTENT_ID
        closed = apply_events(
            opened,
            [
                _close_arm(self.clock, intent_id=cid),
                _sent(self.clock, Venue.OKX, OKX_LEG, intent_id=cid, reduce_only=True),
                _sent(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid, reduce_only=True),
                _ack(self.clock, Venue.OKX, OKX_LEG, intent_id=cid),
                _ack(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
                _fill(self.clock, Venue.OKX, OKX_LEG, intent_id=cid),
                _fill(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
                _pos(self.clock, Venue.OKX, OKX_LEG, "0", intent_id=cid),
                _pos(self.clock, Venue.BYBIT, BYBIT_LEG, "0", intent_id=cid),
                _orders(self.clock, Venue.OKX, OKX_LEG, 0, intent_id=cid),
                _orders(self.clock, Venue.BYBIT, BYBIT_LEG, 0, intent_id=cid),
                _event(
                    self.clock,
                    ExecutionEventType.FLATNESS_PROVEN,
                    intent_id=cid,
                    payload={"positions_flat": True, "open_orders_flat": True},
                ),
            ],
        )
        self.assertEqual(closed.status, SpreadStatus.FLAT)
        self.assertTrue(opens_allowed(closed))
        self.assertEqual(
            closed.accepted_intent_ids,
            frozenset({INTENT_ID, CLOSE_INTENT_ID}),
        )
        restored = SpreadState.from_public_dict(closed.to_public_dict())
        self.assertEqual(restored.to_public_dict(), closed.to_public_dict())
        self.clock.seq = 0
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(closed, _arm(self.clock, intent_id=INTENT_ID))
        self.assertIn("duplicate_intent", str(ctx.exception))
        self.clock.seq = 0
        armed = apply_event(closed, _arm(self.clock, intent_id=NEXT_INTENT_ID))
        self.assertEqual(armed.status, SpreadStatus.ARMED)
        self.assertEqual(armed.last_sequence, 1)

    def test_position_only_unknown_matched_reconciliation_opens(self) -> None:
        timed_out = apply_events(
            self.state,
            [
                *_dispatch_open(self.clock),
                _event(
                    self.clock,
                    ExecutionEventType.ACK_TIMEOUT,
                    venue=Venue.OKX,
                    leg_id=OKX_LEG,
                    payload={"reason_code": "ack_timeout"},
                ),
                _event(
                    self.clock,
                    ExecutionEventType.ACK_TIMEOUT,
                    venue=Venue.BYBIT,
                    leg_id=BYBIT_LEG,
                    payload={"reason_code": "ack_timeout"},
                ),
                _pos(self.clock, Venue.OKX, OKX_LEG, QTY),
                _pos(self.clock, Venue.BYBIT, BYBIT_LEG, QTY),
            ],
        )
        self.assertEqual(timed_out.status, SpreadStatus.EXPOSURE_UNKNOWN)
        self.assertNotEqual(timed_out.status, SpreadStatus.OPEN)
        self.assertTrue(all(leg.status is LegStatus.UNKNOWN for leg in timed_out.legs))
        opened = apply_events(
            timed_out,
            [
                _recon(self.clock, Venue.OKX, OKX_LEG),
                _recon(self.clock, Venue.BYBIT, BYBIT_LEG),
            ],
        )
        self.assertEqual(opened.status, SpreadStatus.OPEN)
        self.assertFalse(opened.recovery_required)
        self.assertFalse(needs_reconciliation(opened))
        self.assertTrue(all(leg.status is LegStatus.FILLED for leg in opened.legs))
        self.assertTrue(all(leg.ack_status == "timeout" for leg in opened.legs))
        restored = SpreadState.from_public_dict(opened.to_public_dict())
        self.assertEqual(restored.status, SpreadStatus.OPEN)
        self.assertEqual(restored.intent_id, restored.open_intent_id)

    def test_close_coin_and_direction_mismatch_fail_closed(self) -> None:
        opened = apply_events(self.state, _happy_open_events(self.clock))
        self.clock.seq = 0
        with self.assertRaises(InvalidTransition) as coin_ctx:
            apply_event(
                opened,
                _close_arm(self.clock, coin="ETH"),
            )
        self.assertIn("close_mismatch", str(coin_ctx.exception))
        self.clock.seq = 0
        with self.assertRaises(InvalidTransition) as dir_ctx:
            apply_event(
                opened,
                _close_arm(self.clock, spread_direction="short"),
            )
        self.assertIn("close_mismatch", str(dir_ctx.exception))
        self.assertEqual(opened.status, SpreadStatus.OPEN)


class SafetyReview3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.state = initial_spread_state(run_id=RUN_ID)

    def test_flat_fault_then_matched_global_recon_allows_opens(self) -> None:
        opened = apply_events(self.state, _happy_open_events(self.clock))
        self.clock.seq = 0
        cid = CLOSE_INTENT_ID
        closed = apply_events(
            opened,
            [
                _close_arm(self.clock, intent_id=cid),
                _sent(self.clock, Venue.OKX, OKX_LEG, intent_id=cid, reduce_only=True),
                _sent(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid, reduce_only=True),
                _ack(self.clock, Venue.OKX, OKX_LEG, intent_id=cid),
                _ack(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
                _fill(self.clock, Venue.OKX, OKX_LEG, intent_id=cid),
                _fill(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
                *_observe_both_flat(self.clock, intent_id=cid),
                _flatness(self.clock, intent_id=cid),
            ],
        )
        self.assertEqual(closed.status, SpreadStatus.FLAT)
        self.assertTrue(opens_allowed(closed))
        faulted = apply_event(
            closed,
            _event(
                self.clock,
                ExecutionEventType.FAULT,
                intent_id=cid,
                payload={"halt": False, "reason_code": "fault"},
            ),
        )
        self.assertEqual(faulted.status, SpreadStatus.FLAT)
        self.assertTrue(faulted.recovery_required)
        self.assertFalse(opens_allowed(faulted))
        unmatched = apply_event(
            faulted,
            _global_recon(self.clock, matched=False, intent_id=cid),
        )
        self.assertTrue(unmatched.recovery_required)
        self.assertFalse(opens_allowed(unmatched))
        self.assertIn(
            unmatched.status,
            {SpreadStatus.EXPOSURE_UNKNOWN, SpreadStatus.RECOVERING},
        )
        self.assertTrue(all(not leg.position_observed for leg in unmatched.legs))
        self.assertTrue(all(not leg.open_orders_observed for leg in unmatched.legs))
        recovered = apply_event(
            unmatched,
            _global_recon(self.clock, matched=True, intent_id=cid),
        )
        self.assertIn(
            recovered.status,
            {SpreadStatus.EXPOSURE_UNKNOWN, SpreadStatus.RECOVERING},
        )
        self.assertNotEqual(recovered.status, SpreadStatus.OPEN)
        self.assertNotEqual(recovered.status, SpreadStatus.FLAT)
        self.assertTrue(recovered.recovery_required)
        self.assertTrue(recovered.stream_generation_ok)
        self.assertFalse(opens_allowed(recovered))
        self.assertFalse(is_proven_flat(recovered))
        proven = apply_events(
            recovered,
            [
                *_observe_both_flat(self.clock, intent_id=cid),
                _recon(self.clock, Venue.OKX, OKX_LEG, intent_id=cid),
                _recon(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
                _flatness(self.clock, intent_id=cid),
            ],
        )
        self.assertEqual(proven.status, SpreadStatus.FLAT)
        self.assertTrue(is_proven_flat(proven))
        self.assertFalse(proven.recovery_required)
        self.assertTrue(opens_allowed(proven))
        self.clock.seq = 0
        armed = apply_event(proven, _arm(self.clock, intent_id=NEXT_INTENT_ID))
        self.assertEqual(armed.status, SpreadStatus.ARMED)
        self.assertEqual(armed.last_sequence, 1)

    def test_presend_fault_zero_legs_recon_returns_idle(self) -> None:
        armed = apply_event(self.state, _arm(self.clock))
        self.assertEqual(armed.status, SpreadStatus.ARMED)
        self.assertEqual(armed.legs, ())
        faulted = apply_event(
            armed,
            _event(
                self.clock,
                ExecutionEventType.FAULT,
                payload={"halt": False, "reason_code": "fault"},
            ),
        )
        self.assertEqual(faulted.status, SpreadStatus.EXPOSURE_UNKNOWN)
        self.assertEqual(faulted.legs, ())
        self.assertTrue(faulted.recovery_required)
        self.assertFalse(opens_allowed(faulted))
        recovered = apply_event(faulted, _global_recon(self.clock, matched=True))
        self.assertEqual(recovered.status, SpreadStatus.IDLE)
        self.assertEqual(recovered.legs, ())
        self.assertIsNone(recovered.intent_id)
        self.assertIsNone(recovered.open_intent_id)
        self.assertEqual(recovered.accepted_intent_ids, frozenset({INTENT_ID}))
        self.assertFalse(recovered.recovery_required)
        self.assertTrue(recovered.stream_generation_ok)
        self.assertTrue(opens_allowed(recovered))
        self.clock.seq = 0
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(recovered, _arm(self.clock, intent_id=INTENT_ID))
        self.assertIn("duplicate_intent", str(ctx.exception))
        self.clock.seq = 0
        next_armed = apply_event(recovered, _arm(self.clock, intent_id=NEXT_INTENT_ID))
        self.assertEqual(next_armed.status, SpreadStatus.ARMED)
        self.assertEqual(next_armed.last_sequence, 1)

    def test_double_reject_late_fill_correlates_and_blocks_open(self) -> None:
        rejected = apply_events(
            self.state,
            [
                *_dispatch_open(self.clock),
                _ack(self.clock, Venue.OKX, OKX_LEG, ok=False),
                _ack(self.clock, Venue.BYBIT, BYBIT_LEG, ok=False),
            ],
        )
        self.assertEqual(len(rejected.legs), 2)
        late = apply_event(rejected, _fill(self.clock, Venue.OKX, OKX_LEG))
        self.assertEqual(len(late.legs), 2)
        okx = late.leg_by_id(OKX_LEG)
        assert okx is not None
        self.assertEqual(okx.filled_quantity, Decimal("1"))
        self.assertNotEqual(late.status, SpreadStatus.IDLE)
        self.assertNotEqual(late.status, SpreadStatus.OPEN)
        self.assertTrue(late.recovery_required)
        self.assertFalse(opens_allowed(late))
        self.clock.seq = late.last_sequence
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(late, _arm(self.clock, intent_id=NEXT_INTENT_ID))
        self.assertIn(ctx.exception.code, {"open_not_allowed", "intent_mismatch"})

    def test_fill_then_reject_is_contradiction(self) -> None:
        filled = apply_events(
            self.state,
            [
                *_dispatch_open(self.clock),
                _fill(self.clock, Venue.OKX, OKX_LEG),
            ],
        )
        rejected = apply_event(filled, _ack(self.clock, Venue.OKX, OKX_LEG, ok=False))
        okx = rejected.leg_by_id(OKX_LEG)
        assert okx is not None
        self.assertEqual(okx.filled_quantity, Decimal("1"))
        self.assertIn(okx.status, {LegStatus.UNKNOWN, LegStatus.RECONCILING})
        self.assertEqual(okx.ack_status, "rejected")
        self.assertFalse(okx.confirmed_unfilled)
        self.assertTrue(rejected.recovery_required)
        self.assertNotEqual(rejected.status, SpreadStatus.OPEN)
        self.assertFalse(opens_allowed(rejected))

    def test_reject_then_fill_converges_with_fill_then_reject(self) -> None:
        rejected = apply_events(
            self.state,
            [
                *_dispatch_open(self.clock),
                _ack(self.clock, Venue.OKX, OKX_LEG, ok=False),
            ],
        )
        filled = apply_event(rejected, _fill(self.clock, Venue.OKX, OKX_LEG))
        okx = filled.leg_by_id(OKX_LEG)
        assert okx is not None
        self.assertEqual(okx.filled_quantity, Decimal("1"))
        self.assertIn(okx.status, {LegStatus.UNKNOWN, LegStatus.RECONCILING})
        self.assertEqual(okx.ack_status, "rejected")
        self.assertFalse(okx.confirmed_unfilled)
        self.assertTrue(filled.recovery_required)
        self.assertNotEqual(filled.status, SpreadStatus.OPEN)

    def test_fill_plus_zero_position_is_contradiction(self) -> None:
        opened = apply_events(self.state, _happy_open_events(self.clock))
        self.assertEqual(opened.status, SpreadStatus.OPEN)
        contradicted = apply_event(
            opened,
            _pos(self.clock, Venue.OKX, OKX_LEG, "0"),
        )
        self.assertNotEqual(contradicted.status, SpreadStatus.OPEN)
        self.assertIn(
            contradicted.status,
            {SpreadStatus.EXPOSURE_UNKNOWN, SpreadStatus.RECOVERING},
        )
        self.assertTrue(contradicted.recovery_required)
        self.assertFalse(opens_allowed(contradicted))
        okx = contradicted.leg_by_id(OKX_LEG)
        assert okx is not None
        self.assertEqual(okx.filled_quantity, Decimal("1"))
        self.assertEqual(okx.position_quantity, Decimal("0"))
        self.assertTrue(okx.position_observed)
        raw = contradicted.to_public_dict()
        raw["status"] = "OPEN"
        with self.assertRaises(ContractValidationError):
            SpreadState.from_public_dict(raw)

    def test_duplicate_send_after_fill_rejected(self) -> None:
        filled = apply_events(
            self.state,
            [
                *_dispatch_open(self.clock),
                _fill(self.clock, Venue.OKX, OKX_LEG),
            ],
        )
        okx_before = filled.leg_by_id(OKX_LEG)
        assert okx_before is not None
        self.assertEqual(okx_before.filled_quantity, Decimal("1"))
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(filled, _sent(self.clock, Venue.OKX, OKX_LEG))
        self.assertIn("duplicate_send", str(ctx.exception))
        self.assertEqual(okx_before.filled_quantity, Decimal("1"))
        self.assertEqual(filled.leg_by_id(OKX_LEG).filled_quantity, Decimal("1"))

    def test_duplicate_venue_different_leg_is_invalid_transition(self) -> None:
        dispatched_one = apply_events(
            self.state,
            [
                _arm(self.clock),
                _sent(self.clock, Venue.OKX, OKX_LEG),
            ],
        )
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(dispatched_one, _sent(self.clock, Venue.OKX, "leg_okx_other"))
        self.assertIn("duplicate_venue", str(ctx.exception))
        self.assertNotIn("duplicate venue leg", str(ctx.exception))

    def test_overfill_persists_qty_and_requires_recovery(self) -> None:
        dispatched = apply_events(self.state, _dispatch_open(self.clock))
        overfilled = apply_event(
            dispatched,
            _fill(self.clock, Venue.OKX, OKX_LEG, quantity="2"),
        )
        okx = overfilled.leg_by_id(OKX_LEG)
        assert okx is not None
        self.assertEqual(okx.filled_quantity, Decimal("2"))
        self.assertIn(okx.status, {LegStatus.UNKNOWN, LegStatus.RECONCILING})
        self.assertNotEqual(overfilled.status, SpreadStatus.DISPATCHING)
        self.assertNotEqual(overfilled.status, SpreadStatus.OPEN)
        self.assertIn(
            overfilled.status,
            {SpreadStatus.EXPOSURE_UNKNOWN, SpreadStatus.RECOVERING},
        )
        self.assertTrue(overfilled.recovery_required)
        self.assertFalse(opens_allowed(overfilled))

    def test_two_leg_quantity_mismatch_requires_recovery(self) -> None:
        mismatched = apply_events(
            self.state,
            [
                *_dispatch_open(self.clock),
                _fill(self.clock, Venue.OKX, OKX_LEG, quantity="1"),
                _fill(self.clock, Venue.BYBIT, BYBIT_LEG, quantity="2"),
            ],
        )
        self.assertEqual(mismatched.leg_by_id(OKX_LEG).filled_quantity, Decimal("1"))
        self.assertEqual(mismatched.leg_by_id(BYBIT_LEG).filled_quantity, Decimal("2"))
        self.assertNotEqual(mismatched.status, SpreadStatus.DISPATCHING)
        self.assertNotEqual(mismatched.status, SpreadStatus.OPEN)
        self.assertTrue(mismatched.recovery_required)
        self.assertIn(
            mismatched.status,
            {SpreadStatus.EXPOSURE_UNKNOWN, SpreadStatus.RECOVERING},
        )

    def test_partial_quantity_regression_is_rejected(self) -> None:
        partial = apply_events(
            self.state,
            [
                *_dispatch_open(self.clock),
                _fill(self.clock, Venue.OKX, OKX_LEG, quantity="0.4", partial=True),
            ],
        )
        self.assertEqual(partial.leg_by_id(OKX_LEG).filled_quantity, Decimal("0.4"))
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(
                partial,
                _fill(
                    self.clock,
                    Venue.OKX,
                    OKX_LEG,
                    quantity="0.2",
                    partial=True,
                ),
            )
        self.assertIn("fill_regression", str(ctx.exception))
        self.assertEqual(partial.leg_by_id(OKX_LEG).filled_quantity, Decimal("0.4"))

    def test_partial_then_smaller_final_fill_is_rejected(self) -> None:
        partial = apply_events(
            self.state,
            [
                *_dispatch_open(self.clock),
                _fill(self.clock, Venue.OKX, OKX_LEG, quantity="0.4", partial=True),
            ],
        )
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(
                partial,
                _fill(self.clock, Venue.OKX, OKX_LEG, quantity="0.3"),
            )
        self.assertIn("fill_regression", str(ctx.exception))
        self.assertEqual(partial.leg_by_id(OKX_LEG).filled_quantity, Decimal("0.4"))

    def test_pause_false_cannot_unlatch(self) -> None:
        dispatched = apply_events(self.state, _dispatch_open(self.clock))
        paused = apply_event(
            dispatched,
            _event(
                self.clock,
                ExecutionEventType.PAUSE,
                payload={"pause": True, "reason_code": "pause"},
            ),
        )
        self.assertTrue(paused.pause_latched)
        self.assertFalse(opens_allowed(paused))
        unchanged = apply_event(
            paused,
            _event(
                self.clock,
                ExecutionEventType.PAUSE,
                payload={"pause": False, "reason_code": "pause"},
            ),
        )
        self.assertTrue(unchanged.pause_latched)
        self.assertFalse(opens_allowed(unchanged))
        self.assertEqual(unchanged.status, paused.status)

    def test_halted_reduce_only_send_remains_halted(self) -> None:
        opened = apply_events(self.state, _happy_open_events(self.clock))
        halted = apply_event(
            opened,
            _event(
                self.clock,
                ExecutionEventType.FAULT,
                payload={"halt": True, "reason_code": "halt"},
            ),
        )
        self.assertEqual(halted.status, SpreadStatus.HALTED)
        flattened = apply_event(
            halted,
            _sent(self.clock, Venue.OKX, OKX_LEG, reduce_only=True),
        )
        self.assertEqual(flattened.status, SpreadStatus.HALTED)
        okx = flattened.leg_by_id(OKX_LEG)
        assert okx is not None
        self.assertTrue(okx.reduce_only)
        self.assertEqual(okx.status, LegStatus.SENT)
        with self.assertRaises(InvalidTransition) as send_ctx:
            apply_event(flattened, _sent(self.clock, Venue.BYBIT, BYBIT_LEG))
        self.assertIn("halted", str(send_ctx.exception))
        self.clock.seq = flattened.last_sequence
        with self.assertRaises(InvalidTransition) as open_ctx:
            apply_event(flattened, _arm(self.clock, intent_id=NEXT_INTENT_ID))
        self.assertIn(open_ctx.exception.code, {"open_not_allowed", "intent_mismatch"})
        self.assertEqual(flattened.status, SpreadStatus.HALTED)

    def test_idle_fault_matched_global_recon_clears_latch(self) -> None:
        faulted = apply_event(
            self.state,
            _event(
                self.clock,
                ExecutionEventType.FAULT,
                payload={"halt": False, "reason_code": "fault"},
            ),
        )
        self.assertEqual(faulted.status, SpreadStatus.IDLE)
        self.assertEqual(faulted.legs, ())
        self.assertTrue(faulted.recovery_required)
        self.assertFalse(opens_allowed(faulted))
        recovered = apply_event(faulted, _global_recon(self.clock, matched=True))
        self.assertEqual(recovered.status, SpreadStatus.IDLE)
        self.assertFalse(recovered.recovery_required)
        self.assertTrue(recovered.stream_generation_ok)
        self.assertTrue(opens_allowed(recovered))

    def test_flat_stream_mismatch_then_matched_global_recon(self) -> None:
        opened = apply_events(self.state, _happy_open_events(self.clock))
        self.clock.seq = 0
        cid = CLOSE_INTENT_ID
        closed = apply_events(
            opened,
            [
                _close_arm(self.clock, intent_id=cid),
                _sent(self.clock, Venue.OKX, OKX_LEG, intent_id=cid, reduce_only=True),
                _sent(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid, reduce_only=True),
                _ack(self.clock, Venue.OKX, OKX_LEG, intent_id=cid),
                _ack(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
                _fill(self.clock, Venue.OKX, OKX_LEG, intent_id=cid),
                _fill(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
                *_observe_both_flat(self.clock, intent_id=cid),
                _flatness(self.clock, intent_id=cid),
            ],
        )
        mismatched = apply_event(
            closed,
            _event(
                self.clock,
                ExecutionEventType.STREAM_GENERATION_MISMATCH,
                intent_id=cid,
                payload={
                    "stream_generation": 2,
                    "reason_code": "stream_generation_mismatch",
                },
            ),
        )
        self.assertEqual(mismatched.status, SpreadStatus.FLAT)
        self.assertFalse(mismatched.stream_generation_ok)
        self.assertTrue(mismatched.recovery_required)
        self.assertFalse(opens_allowed(mismatched))
        recovered = apply_event(
            mismatched,
            _global_recon(self.clock, matched=True, intent_id=cid),
        )
        self.assertEqual(recovered.status, SpreadStatus.FLAT)
        self.assertTrue(recovered.stream_generation_ok)
        self.assertFalse(recovered.recovery_required)
        self.assertTrue(opens_allowed(recovered))

    def test_foreign_client_id_restore_rejected(self) -> None:
        opened = apply_events(self.state, _happy_open_events(self.clock))
        raw = opened.to_public_dict()
        raw["legs"][0]["client_id"] = derive_client_id(NEXT_INTENT_ID, Venue.OKX)
        with self.assertRaises(ContractValidationError):
            SpreadState.from_public_dict(raw)
        self.clock.seq = 0
        cid = CLOSE_INTENT_ID
        closing = apply_events(
            opened,
            [
                _close_arm(self.clock, intent_id=cid),
                _sent(self.clock, Venue.OKX, OKX_LEG, intent_id=cid, reduce_only=True),
            ],
        )
        close_raw = closing.to_public_dict()
        close_raw["legs"][0]["client_id"] = derive_client_id(
            INTENT_ID, Venue.OKX, reduce_only=False
        )
        with self.assertRaises(ContractValidationError):
            SpreadState.from_public_dict(close_raw)


def _cancel_req(
    clock: Clock,
    venue: Venue,
    leg_id: str,
    *,
    intent_id: str = INTENT_ID,
) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.CANCEL_REQUESTED,
        intent_id=intent_id,
        venue=venue,
        leg_id=leg_id,
        payload={},
    )


def _stream_mismatch(clock: Clock, *, intent_id: str = INTENT_ID) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.STREAM_GENERATION_MISMATCH,
        intent_id=intent_id,
        payload={
            "stream_generation": 2,
            "reason_code": "stream_generation_mismatch",
        },
    )


class SafetyReview4Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.state = initial_spread_state(run_id=RUN_ID)

    def _proven_flat(self) -> tuple[SpreadState, str]:
        self.clock = Clock()
        self.state = initial_spread_state(run_id=RUN_ID)
        opened = apply_events(self.state, _happy_open_events(self.clock))
        self.clock.seq = 0
        cid = CLOSE_INTENT_ID
        closed = apply_events(
            opened,
            [
                _close_arm(self.clock, intent_id=cid),
                _sent(self.clock, Venue.OKX, OKX_LEG, intent_id=cid, reduce_only=True),
                _sent(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid, reduce_only=True),
                _ack(self.clock, Venue.OKX, OKX_LEG, intent_id=cid),
                _ack(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
                _fill(self.clock, Venue.OKX, OKX_LEG, intent_id=cid),
                _fill(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
                *_observe_both_flat(self.clock, intent_id=cid),
                _flatness(self.clock, intent_id=cid),
            ],
        )
        self.assertEqual(closed.status, SpreadStatus.FLAT)
        return closed, cid

    def _assert_flat_invalidated(self, state: SpreadState) -> None:
        self.assertNotEqual(state.status, SpreadStatus.OPEN)
        self.assertFalse(opens_allowed(state))
        self.assertTrue(state.recovery_required)
        self.assertIn(
            state.status,
            {SpreadStatus.EXPOSURE_UNKNOWN, SpreadStatus.RECOVERING, SpreadStatus.FLAT},
        )
        if state.status is SpreadStatus.FLAT:
            self.assertFalse(is_proven_flat(state) and opens_allowed(state))

    def test_flat_unmatched_per_leg_recon_demotes_to_recovery(self) -> None:
        closed, cid = self._proven_flat()
        try:
            invalidated = apply_event(
                closed,
                _recon(self.clock, Venue.OKX, OKX_LEG, matched=False, intent_id=cid),
            )
        except ContractValidationError:
            self.fail("FLAT unmatched recon leaked ContractValidationError")
        self.assertIsInstance(invalidated, SpreadState)
        okx = invalidated.leg_by_id(OKX_LEG)
        assert okx is not None
        self.assertEqual(okx.status, LegStatus.RECONCILING)
        self.assertIn(
            invalidated.status,
            {SpreadStatus.EXPOSURE_UNKNOWN, SpreadStatus.RECOVERING},
        )
        self.assertTrue(invalidated.recovery_required)
        self.assertFalse(opens_allowed(invalidated))
        self.assertEqual(okx.filled_quantity, Decimal("1"))
        self.assertEqual(len(invalidated.legs), 2)

    def test_flat_contradictory_evidence_fail_closed_without_contract_leak(self) -> None:
        cases = [
            ("position", lambda clock, cid: _pos(clock, Venue.OKX, OKX_LEG, "1", intent_id=cid)),
            ("orders", lambda clock, cid: _orders(clock, Venue.BYBIT, BYBIT_LEG, 1, intent_id=cid)),
            ("fill", lambda clock, cid: _fill(clock, Venue.OKX, OKX_LEG, intent_id=cid)),
            ("partial", lambda clock, cid: _fill(clock, Venue.OKX, OKX_LEG, quantity="0.4", partial=True, intent_id=cid)),
            (
                "timeout",
                lambda clock, cid: _event(
                    clock,
                    ExecutionEventType.ACK_TIMEOUT,
                    intent_id=cid,
                    venue=Venue.OKX,
                    leg_id=OKX_LEG,
                    payload={"reason_code": "ack_timeout"},
                ),
            ),
            (
                "unknown_correlation",
                lambda clock, cid: _event(
                    clock,
                    ExecutionEventType.UNKNOWN_CORRELATION,
                    intent_id=cid,
                    venue=Venue.BYBIT,
                    leg_id=BYBIT_LEG,
                    payload={"reason_code": "unknown_correlation"},
                ),
            ),
            ("stream", lambda clock, cid: _stream_mismatch(clock, intent_id=cid)),
        ]
        for name, factory in cases:
            with self.subTest(name=name):
                closed, cid = self._proven_flat()
                try:
                    state = apply_event(closed, factory(self.clock, cid))
                except ContractValidationError:
                    self.fail(f"{name} leaked ContractValidationError")
                except InvalidTransition as exc:
                    self.assertIsInstance(exc, InvalidTransition)
                    self.assertNotIsInstance(exc, ContractValidationError)
                    self.fail(f"{name} should fail closed, not reject: {exc.code}")
                self.assertFalse(opens_allowed(state), msg=name)
                self.assertTrue(state.recovery_required, msg=name)
                self.assertNotEqual(state.status, SpreadStatus.OPEN, msg=name)
                self.assertEqual(len(state.legs), 2, msg=name)

    def test_flat_benign_zero_snapshot_does_not_deadlock(self) -> None:
        closed, cid = self._proven_flat()
        still_flat = apply_events(
            closed,
            [
                _pos(self.clock, Venue.OKX, OKX_LEG, "0", intent_id=cid),
                _orders(self.clock, Venue.OKX, OKX_LEG, 0, intent_id=cid),
            ],
        )
        self.assertEqual(still_flat.status, SpreadStatus.FLAT)
        self.assertTrue(is_proven_flat(still_flat))
        self.assertTrue(opens_allowed(still_flat))

    def test_apply_event_exposes_invalid_transition_not_contract_error(self) -> None:
        dispatched = apply_events(self.state, _dispatch_open(self.clock))
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(dispatched, _sent(self.clock, Venue.OKX, "leg_okx_other"))
        self.assertIsInstance(ctx.exception, InvalidTransition)
        self.assertNotIsInstance(ctx.exception, ContractValidationError)
        self.assertEqual(ctx.exception.code, "duplicate_venue")
        self.assertIn("duplicate_venue", str(ctx.exception))
        self.assertNotIn("duplicate venue leg", str(ctx.exception))

    def test_recovery_reduce_only_both_legs_and_cancel_from_recovery(self) -> None:
        recovered = apply_events(
            self.state,
            [
                *_dispatch_open(self.clock),
                _fill(self.clock, Venue.OKX, OKX_LEG),
                _ack(self.clock, Venue.BYBIT, BYBIT_LEG, ok=False),
            ],
        )
        self.assertEqual(recovered.status, SpreadStatus.RECOVERING)
        okx_sent = apply_event(
            recovered,
            _sent(self.clock, Venue.OKX, OKX_LEG, reduce_only=True),
        )
        okx = okx_sent.leg_by_id(OKX_LEG)
        assert okx is not None
        self.assertTrue(okx.reduce_only)
        self.assertEqual(okx.status, LegStatus.SENT)
        self.assertEqual(
            okx.client_id,
            derive_client_id(INTENT_ID, Venue.OKX, reduce_only=True),
        )
        both = apply_event(
            okx_sent,
            _sent(self.clock, Venue.BYBIT, BYBIT_LEG, reduce_only=True),
        )
        bybit = both.leg_by_id(BYBIT_LEG)
        assert bybit is not None
        self.assertTrue(bybit.reduce_only)
        self.assertEqual(
            bybit.client_id,
            derive_client_id(INTENT_ID, Venue.BYBIT, reduce_only=True),
        )
        with self.assertRaises(InvalidTransition) as dup_ctx:
            apply_event(both, _sent(self.clock, Venue.OKX, OKX_LEG, reduce_only=True))
        self.assertEqual(dup_ctx.exception.code, "duplicate_send")
        self.clock.seq = both.last_sequence
        with self.assertRaises(InvalidTransition) as open_send_ctx:
            apply_event(both, _sent(self.clock, Venue.OKX, OKX_LEG))
        self.assertIn(open_send_ctx.exception.code, {"duplicate_send", "send_not_armed"})
        self.clock.seq = both.last_sequence
        cancelled = apply_event(both, _cancel_req(self.clock, Venue.OKX, OKX_LEG))
        self.assertTrue(cancelled.recovery_required)
        self.assertFalse(opens_allowed(cancelled))
        self.clock.seq = cancelled.last_sequence
        with self.assertRaises(InvalidTransition) as open_ctx:
            apply_event(cancelled, _arm(self.clock, intent_id=NEXT_INTENT_ID))
        self.assertIn(open_ctx.exception.code, {"open_not_allowed", "intent_mismatch"})

    def test_reduce_only_allowed_in_exposure_unknown_both_venues(self) -> None:
        unknown = apply_events(
            self.state,
            [
                *_dispatch_open(self.clock),
                _fill(self.clock, Venue.OKX, OKX_LEG, quantity="0.4", partial=True),
            ],
        )
        self.assertEqual(unknown.status, SpreadStatus.EXPOSURE_UNKNOWN)
        okx_sent = apply_event(
            unknown,
            _sent(self.clock, Venue.OKX, OKX_LEG, reduce_only=True),
        )
        bybit_sent = apply_event(
            okx_sent,
            _sent(self.clock, Venue.BYBIT, BYBIT_LEG, reduce_only=True),
        )
        self.assertTrue(bybit_sent.leg_by_id(OKX_LEG).reduce_only)
        self.assertTrue(bybit_sent.leg_by_id(BYBIT_LEG).reduce_only)
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(bybit_sent, _sent(self.clock, Venue.OKX, OKX_LEG))
        self.assertIn(ctx.exception.code, {"duplicate_send", "send_not_armed", "halted"})

    def test_cancel_requested_available_in_halted(self) -> None:
        opened = apply_events(self.state, _happy_open_events(self.clock))
        halted = apply_event(
            opened,
            _event(
                self.clock,
                ExecutionEventType.FAULT,
                payload={"halt": True, "reason_code": "halt"},
            ),
        )
        cancelled = apply_event(halted, _cancel_req(self.clock, Venue.OKX, OKX_LEG))
        self.assertEqual(cancelled.status, SpreadStatus.HALTED)
        flattened = apply_event(
            cancelled,
            _sent(self.clock, Venue.OKX, OKX_LEG, reduce_only=True),
        )
        self.assertEqual(flattened.status, SpreadStatus.HALTED)
        self.assertTrue(flattened.leg_by_id(OKX_LEG).reduce_only)

    def test_global_matched_recon_with_legs_restores_stream_not_recovery(self) -> None:
        opened = apply_events(self.state, _happy_open_events(self.clock))
        mismatched = apply_event(opened, _stream_mismatch(self.clock))
        self.assertEqual(mismatched.status, SpreadStatus.EXPOSURE_UNKNOWN)
        self.assertFalse(mismatched.stream_generation_ok)
        self.assertTrue(mismatched.recovery_required)
        restored = apply_event(mismatched, _global_recon(self.clock, matched=True))
        self.assertTrue(restored.stream_generation_ok)
        self.assertTrue(restored.recovery_required)
        self.assertNotEqual(restored.status, SpreadStatus.OPEN)
        self.assertNotEqual(restored.status, SpreadStatus.FLAT)
        self.assertFalse(opens_allowed(restored))
        positioned = apply_events(
            restored,
            [
                _pos(self.clock, Venue.OKX, OKX_LEG, QTY),
                _pos(self.clock, Venue.BYBIT, BYBIT_LEG, QTY),
                _recon(self.clock, Venue.OKX, OKX_LEG),
                _recon(self.clock, Venue.BYBIT, BYBIT_LEG),
            ],
        )
        self.assertEqual(positioned.status, SpreadStatus.OPEN)
        self.assertFalse(positioned.recovery_required)

    def test_terminal_underfill_is_ambiguous_not_dispatching_filled(self) -> None:
        dispatched = apply_events(self.state, _dispatch_open(self.clock))
        underfilled = apply_events(
            dispatched,
            [
                _fill(self.clock, Venue.OKX, OKX_LEG, quantity="0.5"),
                _fill(self.clock, Venue.BYBIT, BYBIT_LEG, quantity="0.5"),
            ],
        )
        self.assertNotEqual(underfilled.status, SpreadStatus.DISPATCHING)
        self.assertNotEqual(underfilled.status, SpreadStatus.OPEN)
        self.assertEqual(underfilled.status, SpreadStatus.EXPOSURE_UNKNOWN)
        self.assertTrue(underfilled.recovery_required)
        for leg in underfilled.legs:
            self.assertIn(leg.status, {LegStatus.PARTIAL, LegStatus.UNKNOWN, LegStatus.RECONCILING})
            self.assertEqual(leg.filled_quantity, Decimal("0.5"))
        exact_clock = Clock()
        exact = apply_events(
            initial_spread_state(run_id=RUN_ID),
            [
                *_dispatch_open(exact_clock),
                _fill(exact_clock, Venue.OKX, OKX_LEG, quantity="1"),
                _fill(exact_clock, Venue.BYBIT, BYBIT_LEG, quantity="1"),
            ],
        )
        self.assertEqual(exact.status, SpreadStatus.OPEN)

    def test_terminal_underfill_exact_and_regression_still_hold(self) -> None:
        dispatched = apply_events(self.state, _dispatch_open(self.clock))
        opened = apply_events(
            dispatched,
            [
                _fill(self.clock, Venue.OKX, OKX_LEG, quantity="1"),
                _fill(self.clock, Venue.BYBIT, BYBIT_LEG, quantity="1"),
            ],
        )
        self.assertEqual(opened.status, SpreadStatus.OPEN)
        clock = Clock()
        started = apply_events(
            initial_spread_state(run_id=RUN_ID),
            _dispatch_open(clock),
        )
        first = apply_event(
            started,
            _fill(clock, Venue.OKX, OKX_LEG, quantity="0.4", partial=True),
        )
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(first, _fill(clock, Venue.OKX, OKX_LEG, quantity="0.2"))
        self.assertEqual(ctx.exception.code, "fill_regression")

    def test_working_open_order_on_open_demotes_missing_snapshot_does_not(self) -> None:
        opened = apply_events(self.state, _happy_open_events(self.clock))
        self.assertEqual(opened.status, SpreadStatus.OPEN)
        self.assertTrue(all(not leg.open_orders_observed for leg in opened.legs))
        demoted = apply_event(opened, _orders(self.clock, Venue.OKX, OKX_LEG, 1))
        self.assertIn(
            demoted.status,
            {SpreadStatus.EXPOSURE_UNKNOWN, SpreadStatus.RECOVERING},
        )
        self.assertTrue(demoted.recovery_required)
        self.assertFalse(opens_allowed(demoted))
        zero_clock = Clock()
        zero_opened = apply_events(
            initial_spread_state(run_id=RUN_ID),
            _happy_open_events(zero_clock),
        )
        zero = apply_event(zero_opened, _orders(zero_clock, Venue.OKX, OKX_LEG, 0))
        self.assertEqual(zero.status, SpreadStatus.OPEN)

    def test_closing_working_order_expected_flat_needs_fresh_zero(self) -> None:
        opened = apply_events(self.state, _happy_open_events(self.clock))
        self.clock.seq = 0
        cid = CLOSE_INTENT_ID
        closing = apply_events(
            opened,
            [
                _close_arm(self.clock, intent_id=cid),
                _sent(self.clock, Venue.OKX, OKX_LEG, intent_id=cid, reduce_only=True),
                _sent(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid, reduce_only=True),
                _orders(self.clock, Venue.OKX, OKX_LEG, 1, intent_id=cid),
            ],
        )
        self.assertEqual(closing.status, SpreadStatus.CLOSING)
        self.assertFalse(closing.recovery_required)
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(closing, _flatness(self.clock, intent_id=cid))
        self.assertEqual(ctx.exception.code, "flat_requires_recon")

    def test_reject_fill_matched_recon_needs_position_proof(self) -> None:
        rejected = apply_events(
            self.state,
            [
                *_dispatch_open(self.clock),
                _ack(self.clock, Venue.OKX, OKX_LEG, ok=False),
                _fill(self.clock, Venue.OKX, OKX_LEG),
            ],
        )
        okx = rejected.leg_by_id(OKX_LEG)
        assert okx is not None
        self.assertEqual(okx.ack_status, "rejected")
        matched = apply_event(rejected, _recon(self.clock, Venue.OKX, OKX_LEG, matched=True))
        still = matched.leg_by_id(OKX_LEG)
        assert still is not None
        self.assertIn(still.status, {LegStatus.UNKNOWN, LegStatus.RECONCILING})
        self.assertEqual(still.ack_status, "rejected")
        self.assertEqual(still.filled_quantity, Decimal("1"))
        self.assertNotEqual(matched.status, SpreadStatus.OPEN)
        proved = apply_events(
            matched,
            [
                _fill(self.clock, Venue.BYBIT, BYBIT_LEG),
                _pos(self.clock, Venue.OKX, OKX_LEG, QTY),
                _pos(self.clock, Venue.BYBIT, BYBIT_LEG, QTY),
                _recon(self.clock, Venue.OKX, OKX_LEG),
                _recon(self.clock, Venue.BYBIT, BYBIT_LEG),
            ],
        )
        proved_okx = proved.leg_by_id(OKX_LEG)
        assert proved_okx is not None
        self.assertEqual(proved_okx.ack_status, "rejected")
        self.assertEqual(proved.status, SpreadStatus.OPEN)

    def test_fills_before_ack_still_open_without_accepted_ack(self) -> None:
        opened = apply_events(
            self.state,
            [
                *_dispatch_open(self.clock),
                _fill(self.clock, Venue.OKX, OKX_LEG),
                _fill(self.clock, Venue.BYBIT, BYBIT_LEG),
            ],
        )
        self.assertEqual(opened.status, SpreadStatus.OPEN)
        self.assertEqual({leg.ack_status for leg in opened.legs}, {"none"})

    def test_flatness_proven_requires_stream_fresh_zeros_and_clean_legs(self) -> None:
        opened = apply_events(self.state, _happy_open_events(self.clock))
        self.clock.seq = 0
        cid = CLOSE_INTENT_ID
        closing = apply_events(
            opened,
            [
                _close_arm(self.clock, intent_id=cid),
                _sent(self.clock, Venue.OKX, OKX_LEG, intent_id=cid, reduce_only=True),
                _sent(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid, reduce_only=True),
            ],
        )
        partial = apply_event(
            closing,
            _fill(self.clock, Venue.OKX, OKX_LEG, quantity="0.4", partial=True, intent_id=cid),
        )
        self.assertEqual(partial.status, SpreadStatus.EXPOSURE_UNKNOWN)
        self.assertTrue(partial.recovery_required)
        self.assertNotEqual(partial.status, SpreadStatus.CLOSING)
        with self.assertRaises(InvalidTransition) as partial_ctx:
            apply_event(partial, _flatness(self.clock, intent_id=cid))
        self.assertEqual(partial_ctx.exception.code, "flat_requires_recon")
        self.assertIsInstance(partial_ctx.exception, InvalidTransition)

        timeout_clock = Clock()
        timeout_opened = apply_events(
            initial_spread_state(run_id=RUN_ID),
            _happy_open_events(timeout_clock),
        )
        timeout_clock.seq = 0
        timeout_close = apply_events(
            timeout_opened,
            [
                _close_arm(timeout_clock, intent_id=cid),
                _sent(timeout_clock, Venue.OKX, OKX_LEG, intent_id=cid, reduce_only=True),
                _sent(timeout_clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid, reduce_only=True),
                _event(
                    timeout_clock,
                    ExecutionEventType.ACK_TIMEOUT,
                    intent_id=cid,
                    venue=Venue.OKX,
                    leg_id=OKX_LEG,
                    payload={"reason_code": "ack_timeout"},
                ),
            ],
        )
        self.assertEqual(timeout_close.status, SpreadStatus.EXPOSURE_UNKNOWN)
        mismatch_clock = Clock()
        mismatch_opened = apply_events(
            initial_spread_state(run_id=RUN_ID),
            _happy_open_events(mismatch_clock),
        )
        mismatch_clock.seq = 0
        mismatched = apply_events(
            mismatch_opened,
            [
                _close_arm(mismatch_clock, intent_id=cid),
                _sent(mismatch_clock, Venue.OKX, OKX_LEG, intent_id=cid, reduce_only=True),
                _sent(mismatch_clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid, reduce_only=True),
                _ack(mismatch_clock, Venue.OKX, OKX_LEG, intent_id=cid),
                _ack(mismatch_clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
                _fill(mismatch_clock, Venue.OKX, OKX_LEG, intent_id=cid),
                _fill(mismatch_clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
                *_observe_both_flat(mismatch_clock, intent_id=cid),
                _stream_mismatch(mismatch_clock, intent_id=cid),
            ],
        )
        self.assertFalse(mismatched.stream_generation_ok)
        with self.assertRaises(InvalidTransition) as stream_ctx:
            apply_event(mismatched, _flatness(mismatch_clock, intent_id=cid))
        self.assertEqual(stream_ctx.exception.code, "flat_requires_recon")

    def test_stale_close_snapshots_cannot_prove_flat(self) -> None:
        dispatched = apply_events(self.state, _dispatch_open(self.clock))
        opened = apply_events(
            dispatched,
            [
                _pos(self.clock, Venue.OKX, OKX_LEG, QTY),
                _pos(self.clock, Venue.BYBIT, BYBIT_LEG, QTY),
                _orders(self.clock, Venue.OKX, OKX_LEG, 0),
                _orders(self.clock, Venue.BYBIT, BYBIT_LEG, 0),
            ],
        )
        self.assertEqual(opened.status, SpreadStatus.OPEN)
        self.clock.seq = 0
        cid = CLOSE_INTENT_ID
        armed_close = apply_event(opened, _close_arm(self.clock, intent_id=cid))
        stale_before_send = apply_events(
            armed_close,
            [
                _pos(self.clock, Venue.OKX, OKX_LEG, "0", intent_id=cid),
                _pos(self.clock, Venue.BYBIT, BYBIT_LEG, "0", intent_id=cid),
                _orders(self.clock, Venue.OKX, OKX_LEG, 0, intent_id=cid),
                _orders(self.clock, Venue.BYBIT, BYBIT_LEG, 0, intent_id=cid),
            ],
        )
        sent = apply_events(
            stale_before_send,
            [
                _sent(self.clock, Venue.OKX, OKX_LEG, intent_id=cid, reduce_only=True),
                _sent(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid, reduce_only=True),
                _ack(self.clock, Venue.OKX, OKX_LEG, intent_id=cid),
                _ack(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
                _fill(self.clock, Venue.OKX, OKX_LEG, intent_id=cid),
                _fill(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
            ],
        )
        self.assertTrue(all(not leg.position_observed for leg in sent.legs))
        self.assertTrue(all(not leg.open_orders_observed for leg in sent.legs))
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(sent, _flatness(self.clock, intent_id=cid))
        self.assertEqual(ctx.exception.code, "flat_requires_recon")
        self.clock.seq = sent.last_sequence
        proven = apply_events(
            sent,
            [*_observe_both_flat(self.clock, intent_id=cid), _flatness(self.clock, intent_id=cid)],
        )
        self.assertEqual(proven.status, SpreadStatus.FLAT)

    def test_halted_reduce_only_and_pause_remain_sticky(self) -> None:
        opened = apply_events(self.state, _happy_open_events(self.clock))
        halted = apply_event(
            opened,
            _event(
                self.clock,
                ExecutionEventType.FAULT,
                payload={"halt": True, "reason_code": "halt"},
            ),
        )
        both = apply_events(
            halted,
            [
                _sent(self.clock, Venue.OKX, OKX_LEG, reduce_only=True),
                _sent(self.clock, Venue.BYBIT, BYBIT_LEG, reduce_only=True),
            ],
        )
        self.assertEqual(both.status, SpreadStatus.HALTED)
        paused = apply_event(
            both,
            _event(
                self.clock,
                ExecutionEventType.PAUSE,
                payload={"pause": True, "reason_code": "pause"},
            ),
        )
        unpause = apply_event(
            paused,
            _event(
                self.clock,
                ExecutionEventType.PAUSE,
                payload={"pause": False, "reason_code": "pause"},
            ),
        )
        self.assertTrue(unpause.pause_latched)
        self.assertEqual(unpause.status, SpreadStatus.HALTED)
        self.assertFalse(opens_allowed(unpause))


class SafetyReview5Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.state = initial_spread_state(run_id=RUN_ID)

    def test_one_leg_fill_peer_timeout_resolved_zero_enters_recovering(self) -> None:
        unknown = apply_events(
            self.state,
            [
                *_dispatch_open(self.clock),
                _fill(self.clock, Venue.OKX, OKX_LEG),
                _event(
                    self.clock,
                    ExecutionEventType.ACK_TIMEOUT,
                    venue=Venue.BYBIT,
                    leg_id=BYBIT_LEG,
                    payload={"reason_code": "ack_timeout"},
                ),
            ],
        )
        self.assertEqual(unknown.status, SpreadStatus.EXPOSURE_UNKNOWN)
        self.assertNotEqual(unknown.status, SpreadStatus.RECOVERING)
        zeros = apply_events(
            unknown,
            [
                _pos(self.clock, Venue.BYBIT, BYBIT_LEG, "0"),
                _orders(self.clock, Venue.BYBIT, BYBIT_LEG, 0),
            ],
        )
        self.assertNotEqual(zeros.status, SpreadStatus.RECOVERING)
        peer = zeros.leg_by_id(BYBIT_LEG)
        assert peer is not None
        self.assertEqual(peer.status, LegStatus.UNKNOWN)
        self.assertEqual(peer.ack_status, "timeout")
        recovered = apply_event(zeros, _recon(self.clock, Venue.BYBIT, BYBIT_LEG))
        try:
            self.assertEqual(recovered.status, SpreadStatus.RECOVERING)
        except ContractValidationError:
            self.fail("timeout zero recon leaked ContractValidationError")
        bybit = recovered.leg_by_id(BYBIT_LEG)
        assert bybit is not None
        self.assertEqual(bybit.status, LegStatus.CANCELLED)
        self.assertTrue(bybit.confirmed_unfilled)
        self.assertEqual(bybit.ack_status, "timeout")
        self.assertTrue(recovered.recovery_required)
        self.assertFalse(opens_allowed(recovered))
        flatten = apply_event(
            recovered,
            _sent(self.clock, Venue.OKX, OKX_LEG, reduce_only=True),
        )
        okx = flatten.leg_by_id(OKX_LEG)
        assert okx is not None
        self.assertTrue(okx.reduce_only)
        self.assertEqual(okx.status, LegStatus.SENT)
        self.assertEqual(flatten.status, SpreadStatus.RECOVERING)

    def test_close_timeout_resolved_by_fresh_zero_matched_recon_proves_flat(self) -> None:
        opened = apply_events(self.state, _happy_open_events(self.clock))
        self.clock.seq = 0
        cid = CLOSE_INTENT_ID
        timed_out = apply_events(
            opened,
            [
                _close_arm(self.clock, intent_id=cid),
                _sent(self.clock, Venue.OKX, OKX_LEG, intent_id=cid, reduce_only=True),
                _sent(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid, reduce_only=True),
                _event(
                    self.clock,
                    ExecutionEventType.ACK_TIMEOUT,
                    intent_id=cid,
                    venue=Venue.OKX,
                    leg_id=OKX_LEG,
                    payload={"reason_code": "ack_timeout"},
                ),
                _event(
                    self.clock,
                    ExecutionEventType.ACK_TIMEOUT,
                    intent_id=cid,
                    venue=Venue.BYBIT,
                    leg_id=BYBIT_LEG,
                    payload={"reason_code": "ack_timeout"},
                ),
            ],
        )
        self.assertEqual(timed_out.status, SpreadStatus.EXPOSURE_UNKNOWN)
        observed = apply_events(
            timed_out,
            _observe_both_flat(self.clock, intent_id=cid),
        )
        with self.assertRaises(InvalidTransition) as before_recon:
            apply_event(observed, _flatness(self.clock, intent_id=cid))
        self.assertEqual(before_recon.exception.code, "flat_requires_recon")
        self.clock.seq = observed.last_sequence
        resolved = apply_events(
            observed,
            [
                _recon(self.clock, Venue.OKX, OKX_LEG, intent_id=cid),
                _recon(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
            ],
        )
        self.assertTrue(
            all(leg.status is LegStatus.CANCELLED for leg in resolved.legs)
        )
        self.assertTrue(all(leg.ack_status == "timeout" for leg in resolved.legs))
        self.assertTrue(all(leg.confirmed_unfilled for leg in resolved.legs))
        try:
            proven = apply_event(resolved, _flatness(self.clock, intent_id=cid))
        except ContractValidationError:
            self.fail("resolved close timeout leaked ContractValidationError")
        self.assertEqual(proven.status, SpreadStatus.FLAT)
        self.assertTrue(is_proven_flat(proven))
        self.assertTrue(opens_allowed(proven))
        self.assertFalse(needs_reconciliation(proven))
        self.assertTrue(all(leg.ack_status == "timeout" for leg in proven.legs))
        restored = SpreadState.from_public_dict(proven.to_public_dict())
        self.assertEqual(restored.status, SpreadStatus.FLAT)
        self.assertTrue(is_proven_flat(restored))

    def test_close_timeout_with_fill_resolves_filled_and_proves_flat(self) -> None:
        opened = apply_events(self.state, _happy_open_events(self.clock))
        self.clock.seq = 0
        cid = CLOSE_INTENT_ID
        mixed = apply_events(
            opened,
            [
                _close_arm(self.clock, intent_id=cid),
                _sent(self.clock, Venue.OKX, OKX_LEG, intent_id=cid, reduce_only=True),
                _sent(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid, reduce_only=True),
                _fill(self.clock, Venue.OKX, OKX_LEG, intent_id=cid),
                _event(
                    self.clock,
                    ExecutionEventType.ACK_TIMEOUT,
                    intent_id=cid,
                    venue=Venue.OKX,
                    leg_id=OKX_LEG,
                    payload={"reason_code": "ack_timeout"},
                ),
                _event(
                    self.clock,
                    ExecutionEventType.ACK_TIMEOUT,
                    intent_id=cid,
                    venue=Venue.BYBIT,
                    leg_id=BYBIT_LEG,
                    payload={"reason_code": "ack_timeout"},
                ),
                *_observe_both_flat(self.clock, intent_id=cid),
                _recon(self.clock, Venue.OKX, OKX_LEG, intent_id=cid),
                _recon(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
            ],
        )
        okx = mixed.leg_by_id(OKX_LEG)
        bybit = mixed.leg_by_id(BYBIT_LEG)
        assert okx is not None and bybit is not None
        self.assertEqual(okx.status, LegStatus.FILLED)
        self.assertGreater(okx.filled_quantity, Decimal("0"))
        self.assertFalse(okx.confirmed_unfilled)
        self.assertEqual(bybit.status, LegStatus.CANCELLED)
        self.assertTrue(bybit.confirmed_unfilled)
        self.assertEqual({okx.ack_status, bybit.ack_status}, {"timeout"})
        proven = apply_event(mixed, _flatness(self.clock, intent_id=cid))
        self.assertEqual(proven.status, SpreadStatus.FLAT)
        self.assertTrue(is_proven_flat(proven))
        self.assertTrue(opens_allowed(proven))

    def test_unresolved_timeout_still_blocks_open_and_flat(self) -> None:
        timed_out = apply_events(
            self.state,
            [
                *_dispatch_open(self.clock),
                _fill(self.clock, Venue.OKX, OKX_LEG),
                _fill(self.clock, Venue.BYBIT, BYBIT_LEG),
                _event(
                    self.clock,
                    ExecutionEventType.ACK_TIMEOUT,
                    venue=Venue.OKX,
                    leg_id=OKX_LEG,
                    payload={"reason_code": "ack_timeout"},
                ),
            ],
        )
        self.assertEqual(timed_out.status, SpreadStatus.EXPOSURE_UNKNOWN)
        self.assertNotEqual(timed_out.status, SpreadStatus.OPEN)
        matched_fill_only = apply_event(
            timed_out,
            _recon(self.clock, Venue.OKX, OKX_LEG, matched=True),
        )
        still = matched_fill_only.leg_by_id(OKX_LEG)
        assert still is not None
        self.assertIn(still.status, {LegStatus.UNKNOWN, LegStatus.RECONCILING})
        self.assertEqual(still.ack_status, "timeout")
        self.assertEqual(still.filled_quantity, Decimal("1"))
        self.assertNotEqual(matched_fill_only.status, SpreadStatus.OPEN)

        close_clock = Clock()
        opened = apply_events(
            initial_spread_state(run_id=RUN_ID),
            _happy_open_events(close_clock),
        )
        close_clock.seq = 0
        cid = CLOSE_INTENT_ID
        close_timeout = apply_events(
            opened,
            [
                _close_arm(close_clock, intent_id=cid),
                _sent(close_clock, Venue.OKX, OKX_LEG, intent_id=cid, reduce_only=True),
                _sent(close_clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid, reduce_only=True),
                _event(
                    close_clock,
                    ExecutionEventType.ACK_TIMEOUT,
                    intent_id=cid,
                    venue=Venue.OKX,
                    leg_id=OKX_LEG,
                    payload={"reason_code": "ack_timeout"},
                ),
                *_observe_both_flat(close_clock, intent_id=cid),
            ],
        )
        okx = close_timeout.leg_by_id(OKX_LEG)
        assert okx is not None
        self.assertEqual(okx.status, LegStatus.UNKNOWN)
        self.assertEqual(okx.ack_status, "timeout")
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(close_timeout, _flatness(close_clock, intent_id=cid))
        self.assertEqual(ctx.exception.code, "flat_requires_recon")
        self.assertIsInstance(ctx.exception, InvalidTransition)
        self.assertNotIsInstance(ctx.exception, ContractValidationError)

    def test_timeout_matching_position_opens_without_changing_ack_status(self) -> None:
        timed_out = apply_events(
            self.state,
            [
                *_dispatch_open(self.clock),
                _ack(self.clock, Venue.OKX, OKX_LEG),
                _fill(self.clock, Venue.OKX, OKX_LEG),
                _event(
                    self.clock,
                    ExecutionEventType.ACK_TIMEOUT,
                    venue=Venue.BYBIT,
                    leg_id=BYBIT_LEG,
                    payload={"reason_code": "ack_timeout"},
                ),
                _pos(self.clock, Venue.OKX, OKX_LEG, QTY),
                _pos(self.clock, Venue.BYBIT, BYBIT_LEG, QTY),
            ],
        )
        self.assertEqual(timed_out.status, SpreadStatus.EXPOSURE_UNKNOWN)
        bybit = timed_out.leg_by_id(BYBIT_LEG)
        assert bybit is not None
        self.assertEqual(bybit.ack_status, "timeout")
        opened = apply_event(timed_out, _recon(self.clock, Venue.BYBIT, BYBIT_LEG))
        self.assertEqual(opened.status, SpreadStatus.OPEN)
        proved = opened.leg_by_id(BYBIT_LEG)
        assert proved is not None
        self.assertEqual(proved.status, LegStatus.FILLED)
        self.assertEqual(proved.ack_status, "timeout")
        self.assertNotEqual(proved.ack_status, "accepted")
        okx = opened.leg_by_id(OKX_LEG)
        assert okx is not None
        self.assertEqual(okx.ack_status, "accepted")
        self.assertFalse(needs_reconciliation(opened))
        restored = SpreadState.from_public_dict(opened.to_public_dict())
        self.assertEqual(restored.status, SpreadStatus.OPEN)
        restored_bybit = restored.leg_by_id(BYBIT_LEG)
        assert restored_bybit is not None
        self.assertEqual(restored_bybit.ack_status, "timeout")

    def test_restore_cannot_forge_unresolved_timeout_open_or_flat(self) -> None:
        dispatched = apply_events(self.state, _dispatch_open(self.clock))
        timed_out = apply_event(
            dispatched,
            _event(
                self.clock,
                ExecutionEventType.ACK_TIMEOUT,
                venue=Venue.OKX,
                leg_id=OKX_LEG,
                payload={"reason_code": "ack_timeout"},
            ),
        )
        open_raw = timed_out.to_public_dict()
        open_raw["status"] = "OPEN"
        with self.assertRaises(ContractValidationError):
            SpreadState.from_public_dict(open_raw)

        opened = apply_events(
            initial_spread_state(run_id=RUN_ID),
            _happy_open_events(Clock()),
        )
        forged_open = opened.to_public_dict()
        forged_open["legs"][0]["ack_status"] = "timeout"
        with self.assertRaises(ContractValidationError):
            SpreadState.from_public_dict(forged_open)

        close_clock = Clock()
        live = apply_events(
            initial_spread_state(run_id=RUN_ID),
            _happy_open_events(close_clock),
        )
        close_clock.seq = 0
        cid = CLOSE_INTENT_ID
        close_timeout = apply_events(
            live,
            [
                _close_arm(close_clock, intent_id=cid),
                _sent(close_clock, Venue.OKX, OKX_LEG, intent_id=cid, reduce_only=True),
                _sent(close_clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid, reduce_only=True),
                _event(
                    close_clock,
                    ExecutionEventType.ACK_TIMEOUT,
                    intent_id=cid,
                    venue=Venue.OKX,
                    leg_id=OKX_LEG,
                    payload={"reason_code": "ack_timeout"},
                ),
                *_observe_both_flat(close_clock, intent_id=cid),
            ],
        )
        flat_raw = close_timeout.to_public_dict()
        flat_raw["status"] = "FLAT"
        flat_raw["positions_flat"] = True
        flat_raw["open_orders_flat"] = True
        with self.assertRaises(ContractValidationError):
            SpreadState.from_public_dict(flat_raw)


def _ack_timeout(
    clock: Clock,
    venue: Venue,
    leg_id: str,
    *,
    intent_id: str = INTENT_ID,
) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.ACK_TIMEOUT,
        intent_id=intent_id,
        venue=venue,
        leg_id=leg_id,
        payload={"reason_code": "ack_timeout"},
    )


def _cancel_ack(
    clock: Clock,
    venue: Venue,
    leg_id: str,
    *,
    intent_id: str = INTENT_ID,
) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.CANCEL_ACK,
        intent_id=intent_id,
        venue=venue,
        leg_id=leg_id,
        payload={},
    )


def _fault(clock: Clock, *, halt: bool = False, intent_id: str = INTENT_ID) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.FAULT,
        intent_id=intent_id,
        payload={"halt": halt, "reason_code": "halt" if halt else "fault"},
    )


def _assert_stale_history(test: unittest.TestCase, state: SpreadState) -> None:
    test.assertTrue(all(not leg.position_observed for leg in state.legs))
    test.assertTrue(all(not leg.open_orders_observed for leg in state.legs))
    test.assertTrue(all(leg.position_quantity == Decimal("0") for leg in state.legs))
    test.assertTrue(all(leg.open_order_count == 0 for leg in state.legs))


class SafetyReview6Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.state = initial_spread_state(run_id=RUN_ID)

    def test_a_pre_timeout_zeros_cannot_resolve_until_post_timeout_snapshots(self) -> None:
        dispatched = apply_events(self.state, _dispatch_open(self.clock))
        stale = apply_events(dispatched, _observe_both_flat(self.clock))
        self.assertTrue(all(leg.position_observed for leg in stale.legs))
        timed_out = apply_events(
            stale,
            [
                _ack_timeout(self.clock, Venue.OKX, OKX_LEG),
                _ack_timeout(self.clock, Venue.BYBIT, BYBIT_LEG),
            ],
        )
        self.assertEqual(timed_out.status, SpreadStatus.EXPOSURE_UNKNOWN)
        _assert_stale_history(self, timed_out)
        self.assertTrue(all(leg.ack_status == "timeout" for leg in timed_out.legs))
        reused = apply_events(
            timed_out,
            [
                _recon(self.clock, Venue.OKX, OKX_LEG),
                _recon(self.clock, Venue.BYBIT, BYBIT_LEG),
            ],
        )
        self.assertTrue(
            all(
                leg.status in {LegStatus.UNKNOWN, LegStatus.RECONCILING}
                for leg in reused.legs
            )
        )
        self.assertNotEqual(reused.status, SpreadStatus.FLAT)
        self.assertFalse(opens_allowed(reused))
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(reused, _flatness(self.clock))
        self.assertEqual(ctx.exception.code, "flat_requires_recon")
        self.clock.seq = reused.last_sequence
        resolved = apply_events(
            reused,
            [
                *_observe_both_flat(self.clock),
                _recon(self.clock, Venue.OKX, OKX_LEG),
                _recon(self.clock, Venue.BYBIT, BYBIT_LEG),
            ],
        )
        self.assertTrue(all(leg.status is LegStatus.CANCELLED for leg in resolved.legs))
        self.assertTrue(all(leg.ack_status == "timeout" for leg in resolved.legs))
        proven = apply_event(resolved, _flatness(self.clock))
        self.assertEqual(proven.status, SpreadStatus.FLAT)
        self.assertTrue(is_proven_flat(proven))
        self.assertTrue(opens_allowed(proven))

    def test_b_pre_mismatch_zeros_global_matched_cannot_prove_flat_or_open(self) -> None:
        dispatched = apply_events(self.state, _dispatch_open(self.clock))
        stale = apply_events(dispatched, _observe_both_flat(self.clock))
        mismatched = apply_event(stale, _stream_mismatch(self.clock))
        self.assertEqual(mismatched.status, SpreadStatus.EXPOSURE_UNKNOWN)
        self.assertFalse(mismatched.stream_generation_ok)
        _assert_stale_history(self, mismatched)
        restored = apply_event(mismatched, _global_recon(self.clock, matched=True))
        self.assertTrue(restored.stream_generation_ok)
        _assert_stale_history(self, restored)
        self.assertTrue(restored.recovery_required)
        self.assertFalse(opens_allowed(restored))
        self.assertNotEqual(restored.status, SpreadStatus.FLAT)
        self.assertNotEqual(restored.status, SpreadStatus.OPEN)
        with self.assertRaises(InvalidTransition) as flat_ctx:
            apply_event(restored, _flatness(self.clock))
        self.assertEqual(flat_ctx.exception.code, "flat_requires_recon")
        self.clock.seq = restored.last_sequence
        with self.assertRaises(InvalidTransition) as open_ctx:
            apply_event(restored, _arm(self.clock, intent_id=NEXT_INTENT_ID))
        self.assertIn(open_ctx.exception.code, {"open_not_allowed", "intent_mismatch"})
        self.clock.seq = restored.last_sequence
        terminal = apply_events(
            restored,
            [
                *_observe_both_flat(self.clock),
                _cancel_ack(self.clock, Venue.OKX, OKX_LEG),
                _cancel_ack(self.clock, Venue.BYBIT, BYBIT_LEG),
            ],
        )
        _assert_stale_history(self, terminal)
        with self.assertRaises(InvalidTransition) as after_cancel:
            apply_event(terminal, _flatness(self.clock))
        self.assertEqual(after_cancel.exception.code, "flat_requires_recon")
        self.clock.seq = terminal.last_sequence
        proven = apply_events(
            terminal,
            [*_observe_both_flat(self.clock), _flatness(self.clock)],
        )
        self.assertEqual(proven.status, SpreadStatus.FLAT)
        self.assertTrue(is_proven_flat(proven))
        self.assertTrue(opens_allowed(proven))

    def test_c_close_pre_timeout_zeros_cannot_prove_flat(self) -> None:
        opened = apply_events(self.state, _happy_open_events(self.clock))
        self.clock.seq = 0
        cid = CLOSE_INTENT_ID
        sent = apply_events(
            opened,
            [
                _close_arm(self.clock, intent_id=cid),
                _sent(self.clock, Venue.OKX, OKX_LEG, intent_id=cid, reduce_only=True),
                _sent(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid, reduce_only=True),
            ],
        )
        stale = apply_events(sent, _observe_both_flat(self.clock, intent_id=cid))
        self.assertTrue(all(leg.position_observed for leg in stale.legs))
        timed_out = apply_events(
            stale,
            [
                _ack_timeout(self.clock, Venue.OKX, OKX_LEG, intent_id=cid),
                _ack_timeout(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
            ],
        )
        self.assertEqual(timed_out.status, SpreadStatus.EXPOSURE_UNKNOWN)
        _assert_stale_history(self, timed_out)
        reused = apply_events(
            timed_out,
            [
                _recon(self.clock, Venue.OKX, OKX_LEG, intent_id=cid),
                _recon(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
            ],
        )
        self.assertTrue(
            all(
                leg.status in {LegStatus.UNKNOWN, LegStatus.RECONCILING}
                for leg in reused.legs
            )
        )
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(reused, _flatness(self.clock, intent_id=cid))
        self.assertEqual(ctx.exception.code, "flat_requires_recon")
        self.clock.seq = reused.last_sequence
        resolved = apply_events(
            reused,
            [
                *_observe_both_flat(self.clock, intent_id=cid),
                _recon(self.clock, Venue.OKX, OKX_LEG, intent_id=cid),
                _recon(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
            ],
        )
        self.assertTrue(all(leg.status is LegStatus.CANCELLED for leg in resolved.legs))
        proven = apply_event(resolved, _flatness(self.clock, intent_id=cid))
        self.assertEqual(proven.status, SpreadStatus.FLAT)
        self.assertTrue(is_proven_flat(proven))
        self.assertTrue(opens_allowed(proven))

    def test_d_pre_reject_and_pre_cancel_zeros_cannot_prove_flat(self) -> None:
        dispatched = apply_events(self.state, _dispatch_open(self.clock))
        stale = apply_events(dispatched, _observe_both_flat(self.clock))
        rejected = apply_events(
            stale,
            [
                _ack(self.clock, Venue.OKX, OKX_LEG, ok=False),
                _ack(self.clock, Venue.BYBIT, BYBIT_LEG, ok=False),
            ],
        )
        _assert_stale_history(self, rejected)
        self.assertTrue(all(leg.status is LegStatus.ACK_REJECTED for leg in rejected.legs))
        self.assertTrue(all(leg.filled_quantity == Decimal("0") for leg in rejected.legs))
        with self.assertRaises(InvalidTransition) as reject_ctx:
            apply_event(rejected, _flatness(self.clock))
        self.assertEqual(reject_ctx.exception.code, "flat_requires_recon")
        self.clock.seq = rejected.last_sequence
        reject_proven = apply_events(
            rejected,
            [*_observe_both_flat(self.clock), _flatness(self.clock)],
        )
        self.assertEqual(reject_proven.status, SpreadStatus.FLAT)

        cancel_clock = Clock()
        cancel_stale = apply_events(
            initial_spread_state(run_id=RUN_ID),
            [*_dispatch_open(cancel_clock), *_observe_both_flat(cancel_clock)],
        )
        cancelled = apply_events(
            cancel_stale,
            [
                _cancel_ack(cancel_clock, Venue.OKX, OKX_LEG),
                _cancel_ack(cancel_clock, Venue.BYBIT, BYBIT_LEG),
            ],
        )
        _assert_stale_history(self, cancelled)
        self.assertTrue(all(leg.status is LegStatus.CANCELLED for leg in cancelled.legs))
        with self.assertRaises(InvalidTransition) as cancel_ctx:
            apply_event(cancelled, _flatness(cancel_clock))
        self.assertEqual(cancel_ctx.exception.code, "flat_requires_recon")
        cancel_clock.seq = cancelled.last_sequence
        cancel_proven = apply_events(
            cancelled,
            [*_observe_both_flat(cancel_clock), _flatness(cancel_clock)],
        )
        self.assertEqual(cancel_proven.status, SpreadStatus.FLAT)
        self.assertTrue(is_proven_flat(cancel_proven))

    def test_e_unmatched_recon_and_fault_clear_freshness(self) -> None:
        dispatched = apply_events(self.state, _dispatch_open(self.clock))
        stale = apply_events(dispatched, _observe_both_flat(self.clock))
        per_leg = apply_event(stale, _recon(self.clock, Venue.OKX, OKX_LEG, matched=False))
        okx = per_leg.leg_by_id(OKX_LEG)
        bybit = per_leg.leg_by_id(BYBIT_LEG)
        assert okx is not None and bybit is not None
        self.assertEqual(okx.status, LegStatus.RECONCILING)
        self.assertFalse(okx.position_observed)
        self.assertFalse(okx.open_orders_observed)
        self.assertEqual(okx.position_quantity, Decimal("0"))
        self.assertTrue(bybit.position_observed)
        reused_okx = apply_event(per_leg, _recon(self.clock, Venue.OKX, OKX_LEG, matched=True))
        still_okx = reused_okx.leg_by_id(OKX_LEG)
        assert still_okx is not None
        self.assertIn(still_okx.status, {LegStatus.UNKNOWN, LegStatus.RECONCILING})
        self.assertFalse(still_okx.position_observed)
        self.assertNotEqual(reused_okx.status, SpreadStatus.FLAT)
        self.assertFalse(opens_allowed(reused_okx))

        global_clock = Clock()
        global_stale = apply_events(
            initial_spread_state(run_id=RUN_ID),
            [*_dispatch_open(global_clock), *_observe_both_flat(global_clock)],
        )
        unmatched = apply_event(global_stale, _global_recon(global_clock, matched=False))
        _assert_stale_history(self, unmatched)
        self.assertTrue(unmatched.recovery_required)
        matched_global = apply_event(unmatched, _global_recon(global_clock, matched=True))
        _assert_stale_history(self, matched_global)
        self.assertFalse(opens_allowed(matched_global))
        with self.assertRaises(InvalidTransition) as global_ctx:
            apply_event(matched_global, _flatness(global_clock))
        self.assertIn(
            global_ctx.exception.code,
            {"flat_requires_recon", "flat_not_applicable"},
        )

        fault_clock = Clock()
        fault_stale = apply_events(
            initial_spread_state(run_id=RUN_ID),
            [*_dispatch_open(fault_clock), *_observe_both_flat(fault_clock)],
        )
        faulted = apply_event(fault_stale, _fault(fault_clock))
        self.assertEqual(faulted.status, SpreadStatus.EXPOSURE_UNKNOWN)
        self.assertTrue(all(leg.status is LegStatus.RECONCILING for leg in faulted.legs))
        _assert_stale_history(self, faulted)
        reused_fault = apply_events(
            faulted,
            [
                _recon(fault_clock, Venue.OKX, OKX_LEG, matched=True),
                _recon(fault_clock, Venue.BYBIT, BYBIT_LEG, matched=True),
            ],
        )
        self.assertTrue(
            all(
                leg.status in {LegStatus.UNKNOWN, LegStatus.RECONCILING}
                for leg in reused_fault.legs
            )
        )
        self.assertFalse(opens_allowed(reused_fault))
        with self.assertRaises(InvalidTransition) as fault_ctx:
            apply_event(reused_fault, _flatness(fault_clock))
        self.assertEqual(fault_ctx.exception.code, "flat_requires_recon")
        fault_clock.seq = reused_fault.last_sequence
        resolved = apply_events(
            reused_fault,
            [
                *_observe_both_flat(fault_clock),
                _recon(fault_clock, Venue.OKX, OKX_LEG, matched=True),
                _recon(fault_clock, Venue.BYBIT, BYBIT_LEG, matched=True),
            ],
        )
        self.assertTrue(all(leg.status is LegStatus.CANCELLED for leg in resolved.legs))


class SafetyReview7Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.state = initial_spread_state(run_id=RUN_ID)

    def _proven_flat(self) -> tuple[SpreadState, str]:
        opened = apply_events(self.state, _happy_open_events(self.clock))
        self.clock.seq = 0
        cid = CLOSE_INTENT_ID
        closed = apply_events(
            opened,
            [
                _close_arm(self.clock, intent_id=cid),
                _sent(self.clock, Venue.OKX, OKX_LEG, intent_id=cid, reduce_only=True),
                _sent(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid, reduce_only=True),
                _ack(self.clock, Venue.OKX, OKX_LEG, intent_id=cid),
                _ack(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
                _fill(self.clock, Venue.OKX, OKX_LEG, intent_id=cid),
                _fill(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
                *_observe_both_flat(self.clock, intent_id=cid),
                _flatness(self.clock, intent_id=cid),
            ],
        )
        self.assertEqual(closed.status, SpreadStatus.FLAT)
        self.assertTrue(is_proven_flat(closed))
        self.assertTrue(opens_allowed(closed))
        return closed, cid

    def test_flat_global_unmatched_recon_requires_fresh_post_mismatch_proof(self) -> None:
        closed, cid = self._proven_flat()
        unmatched = apply_event(
            closed,
            _global_recon(self.clock, matched=False, intent_id=cid),
        )
        self.assertIn(
            unmatched.status,
            {SpreadStatus.EXPOSURE_UNKNOWN, SpreadStatus.RECOVERING},
        )
        self.assertTrue(unmatched.recovery_required)
        self.assertFalse(opens_allowed(unmatched))
        self.assertFalse(is_proven_flat(unmatched))
        _assert_stale_history(self, unmatched)
        reused = apply_event(
            unmatched,
            _global_recon(self.clock, matched=True, intent_id=cid),
        )
        self.assertIn(
            reused.status,
            {SpreadStatus.EXPOSURE_UNKNOWN, SpreadStatus.RECOVERING},
        )
        self.assertTrue(reused.recovery_required)
        self.assertFalse(opens_allowed(reused))
        self.assertFalse(is_proven_flat(reused))
        self.assertNotEqual(reused.status, SpreadStatus.FLAT)
        self.assertNotEqual(reused.status, SpreadStatus.OPEN)
        _assert_stale_history(self, reused)
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(reused, _flatness(self.clock, intent_id=cid))
        self.assertEqual(ctx.exception.code, "flat_requires_recon")
        self.clock.seq = reused.last_sequence
        with self.assertRaises(InvalidTransition) as open_ctx:
            apply_event(reused, _arm(self.clock, intent_id=NEXT_INTENT_ID))
        self.assertIn(open_ctx.exception.code, {"open_not_allowed", "intent_mismatch"})
        self.clock.seq = reused.last_sequence
        proven = apply_events(
            reused,
            [
                *_observe_both_flat(self.clock, intent_id=cid),
                _recon(self.clock, Venue.OKX, OKX_LEG, intent_id=cid),
                _recon(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
                _flatness(self.clock, intent_id=cid),
            ],
        )
        self.assertEqual(proven.status, SpreadStatus.FLAT)
        self.assertTrue(is_proven_flat(proven))
        self.assertFalse(proven.recovery_required)
        self.assertTrue(opens_allowed(proven))
        self.clock.seq = 0
        armed = apply_event(proven, _arm(self.clock, intent_id=NEXT_INTENT_ID))
        self.assertEqual(armed.status, SpreadStatus.ARMED)
        self.assertEqual(armed.last_sequence, 1)

    def test_cancel_requested_invalidates_pre_cancel_zeros(self) -> None:
        opened = apply_events(self.state, _happy_open_events(self.clock))
        self.clock.seq = 0
        cid = CLOSE_INTENT_ID
        sent = apply_events(
            opened,
            [
                _close_arm(self.clock, intent_id=cid),
                _sent(self.clock, Venue.OKX, OKX_LEG, intent_id=cid, reduce_only=True),
                _sent(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid, reduce_only=True),
            ],
        )
        stale = apply_events(sent, _observe_both_flat(self.clock, intent_id=cid))
        self.assertTrue(all(leg.position_observed for leg in stale.legs))
        self.assertTrue(all(leg.open_orders_observed for leg in stale.legs))
        self.assertEqual(stale.status, SpreadStatus.CLOSING)
        requested = apply_event(stale, _cancel_req(self.clock, Venue.OKX, OKX_LEG, intent_id=cid))
        okx = requested.leg_by_id(OKX_LEG)
        bybit = requested.leg_by_id(BYBIT_LEG)
        assert okx is not None and bybit is not None
        self.assertFalse(okx.position_observed)
        self.assertFalse(okx.open_orders_observed)
        self.assertEqual(okx.position_quantity, Decimal("0"))
        self.assertEqual(okx.open_order_count, 0)
        self.assertEqual(okx.filled_quantity, Decimal("0"))
        self.assertEqual(okx.status, LegStatus.SENT)
        self.assertTrue(bybit.position_observed)
        self.assertEqual(requested.status, SpreadStatus.CLOSING)
        self.assertFalse(requested.recovery_required)
        with self.assertRaises(InvalidTransition) as ctx:
            apply_event(requested, _flatness(self.clock, intent_id=cid))
        self.assertEqual(ctx.exception.code, "flat_requires_recon")
        self.clock.seq = requested.last_sequence
        terminal = apply_event(
            requested,
            _cancel_ack(self.clock, Venue.OKX, OKX_LEG, intent_id=cid),
        )
        terminal_okx = terminal.leg_by_id(OKX_LEG)
        assert terminal_okx is not None
        self.assertEqual(terminal_okx.status, LegStatus.CANCELLED)
        self.assertFalse(terminal_okx.position_observed)
        self.assertFalse(terminal_okx.open_orders_observed)
        with self.assertRaises(InvalidTransition) as after_ack:
            apply_event(terminal, _flatness(self.clock, intent_id=cid))
        self.assertEqual(after_ack.exception.code, "flat_requires_recon")
        self.clock.seq = terminal.last_sequence
        proven = apply_events(
            terminal,
            [*_observe_both_flat(self.clock, intent_id=cid), _flatness(self.clock, intent_id=cid)],
        )
        self.assertEqual(proven.status, SpreadStatus.FLAT)
        self.assertTrue(is_proven_flat(proven))
        self.assertTrue(opens_allowed(proven))


class SafetyReview8Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.state = initial_spread_state(run_id=RUN_ID)

    def test_open_global_unmatched_stays_reconciling_until_fresh_leg_proof(self) -> None:
        opened = apply_events(self.state, _happy_open_events(self.clock))
        self.assertEqual(opened.status, SpreadStatus.OPEN)
        unmatched = apply_event(opened, _global_recon(self.clock, matched=False))
        self.assertIn(
            unmatched.status,
            {SpreadStatus.EXPOSURE_UNKNOWN, SpreadStatus.RECOVERING},
        )
        self.assertTrue(all(leg.status is LegStatus.RECONCILING for leg in unmatched.legs))
        self.assertTrue(unmatched.recovery_required)
        self.assertFalse(opens_allowed(unmatched))
        self.assertTrue(needs_reconciliation(unmatched))
        self.assertNotEqual(unmatched.status, SpreadStatus.OPEN)
        self.assertTrue(all(not leg.position_observed for leg in unmatched.legs))
        self.assertTrue(all(not leg.open_orders_observed for leg in unmatched.legs))
        refilled = apply_events(
            unmatched,
            [
                _fill(self.clock, Venue.OKX, OKX_LEG),
                _fill(self.clock, Venue.BYBIT, BYBIT_LEG),
            ],
        )
        self.assertTrue(all(leg.status is LegStatus.RECONCILING for leg in refilled.legs))
        self.assertNotEqual(refilled.status, SpreadStatus.OPEN)
        self.assertTrue(refilled.recovery_required)
        self.assertTrue(needs_reconciliation(refilled))
        reused = apply_event(refilled, _global_recon(self.clock, matched=True))
        self.assertIn(
            reused.status,
            {SpreadStatus.EXPOSURE_UNKNOWN, SpreadStatus.RECOVERING},
        )
        self.assertNotEqual(reused.status, SpreadStatus.OPEN)
        self.assertTrue(all(leg.status is LegStatus.RECONCILING for leg in reused.legs))
        self.assertTrue(reused.stream_generation_ok)
        self.assertTrue(reused.recovery_required)
        self.assertFalse(opens_allowed(reused))
        self.assertTrue(needs_reconciliation(reused))
        restored = apply_events(
            reused,
            [
                _pos(self.clock, Venue.OKX, OKX_LEG, QTY),
                _pos(self.clock, Venue.BYBIT, BYBIT_LEG, QTY),
                _recon(self.clock, Venue.OKX, OKX_LEG),
                _recon(self.clock, Venue.BYBIT, BYBIT_LEG),
            ],
        )
        self.assertEqual(restored.status, SpreadStatus.OPEN)
        self.assertTrue(all(leg.status is LegStatus.FILLED for leg in restored.legs))
        self.assertFalse(restored.recovery_required)
        self.assertFalse(opens_allowed(restored))
        self.assertFalse(needs_reconciliation(restored))

    def test_open_working_order_unmatched_retains_count_and_needs_fresh_proof(self) -> None:
        opened = apply_events(self.state, _happy_open_events(self.clock))
        demoted = apply_event(opened, _orders(self.clock, Venue.OKX, OKX_LEG, 1))
        self.assertIn(
            demoted.status,
            {SpreadStatus.EXPOSURE_UNKNOWN, SpreadStatus.RECOVERING},
        )
        self.assertNotEqual(demoted.status, SpreadStatus.OPEN)
        unmatched = apply_event(demoted, _global_recon(self.clock, matched=False))
        self.assertIn(
            unmatched.status,
            {SpreadStatus.EXPOSURE_UNKNOWN, SpreadStatus.RECOVERING},
        )
        self.assertNotEqual(unmatched.status, SpreadStatus.OPEN)
        self.assertTrue(all(leg.status is LegStatus.RECONCILING for leg in unmatched.legs))
        okx = unmatched.leg_by_id(OKX_LEG)
        bybit = unmatched.leg_by_id(BYBIT_LEG)
        assert okx is not None and bybit is not None
        self.assertEqual(okx.open_order_count, 1)
        self.assertFalse(okx.open_orders_observed)
        self.assertFalse(okx.position_observed)
        self.assertEqual(bybit.open_order_count, 0)
        self.assertFalse(bybit.open_orders_observed)
        self.assertTrue(unmatched.recovery_required)
        self.assertFalse(opens_allowed(unmatched))
        self.assertTrue(needs_reconciliation(unmatched))
        reused = apply_event(unmatched, _global_recon(self.clock, matched=True))
        self.assertNotEqual(reused.status, SpreadStatus.OPEN)
        self.assertTrue(all(leg.status is LegStatus.RECONCILING for leg in reused.legs))
        reused_okx = reused.leg_by_id(OKX_LEG)
        assert reused_okx is not None
        self.assertEqual(reused_okx.open_order_count, 1)
        self.assertFalse(reused_okx.open_orders_observed)
        self.assertTrue(reused.recovery_required)
        self.assertFalse(opens_allowed(reused))
        self.assertTrue(needs_reconciliation(reused))
        restored = apply_events(
            reused,
            [
                _pos(self.clock, Venue.OKX, OKX_LEG, QTY),
                _pos(self.clock, Venue.BYBIT, BYBIT_LEG, QTY),
                _orders(self.clock, Venue.OKX, OKX_LEG, 0),
                _orders(self.clock, Venue.BYBIT, BYBIT_LEG, 0),
                _recon(self.clock, Venue.OKX, OKX_LEG),
                _recon(self.clock, Venue.BYBIT, BYBIT_LEG),
            ],
        )
        self.assertEqual(restored.status, SpreadStatus.OPEN)
        self.assertTrue(all(leg.status is LegStatus.FILLED for leg in restored.legs))
        restored_okx = restored.leg_by_id(OKX_LEG)
        assert restored_okx is not None
        self.assertEqual(restored_okx.open_order_count, 0)
        self.assertTrue(restored_okx.open_orders_observed)
        self.assertFalse(restored.recovery_required)
        self.assertFalse(needs_reconciliation(restored))


if __name__ == "__main__":
    unittest.main()
