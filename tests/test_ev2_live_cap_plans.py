"""Exact CAP open/close plans from market constraints and confirmed EV2 fills."""

from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal

from app.bot.execution.contracts import (
    ContractValidationError, ExecutionEventType, IntentAction, LegPlan,
    SpreadDirection, SpreadState, SpreadStatus, Venue,
)
from app.bot.execution.live_cap_plans import LiveCapPlanBook, LiveCapPlanError
from app.bot.execution.state_machine import apply_events, initial_spread_state
from tests.test_ev2_live_cap_sizing import _current_shape
from tests.test_execution_engine import INTENT_A, INTENT_B, RUN_ID, _event, _intent


def _cap_open_state(*, bybit_fill: str = "100", okx_multiplier: str = "100"):
    events = [
        _event(
            ExecutionEventType.INTENT_ACCEPTED, intent_id=INTENT_A,
            sequence=1, monotonic_ns=1001,
            payload={"action": "open", "coin": "CAP", "spread_direction": "long", "lot_tolerance": "0"},
        ),
        _event(
            ExecutionEventType.REQUEST_SENT, intent_id=INTENT_A,
            sequence=2, monotonic_ns=1002, venue=Venue.BYBIT,
            leg_id="leg_bybit",
            payload={"quantity": "100", "reduce_only": False, "instrument": "CAPUSDT", "side": "sell"},
        ),
        _event(
            ExecutionEventType.REQUEST_SENT, intent_id=INTENT_A,
            sequence=3, monotonic_ns=1003, venue=Venue.OKX,
            leg_id="leg_okx",
            payload={"quantity": "1", "base_multiplier": okx_multiplier, "reduce_only": False, "instrument": "CAP-USDT-SWAP", "side": "buy"},
        ),
        _event(
            ExecutionEventType.FILL if bybit_fill == "100" else ExecutionEventType.PARTIAL_FILL,
            intent_id=INTENT_A,
            sequence=4, monotonic_ns=1004, venue=Venue.BYBIT,
            leg_id="leg_bybit", payload={"quantity": bybit_fill},
        ),
        _event(
            ExecutionEventType.FILL, intent_id=INTENT_A,
            sequence=5, monotonic_ns=1005, venue=Venue.OKX,
            leg_id="leg_okx", payload={"quantity": "1"},
        ),
    ]
    return apply_events(initial_spread_state(run_id=RUN_ID), events)


class CapLivePlanTests(unittest.TestCase):
    def test_matched_open_and_reduce_only_close_from_confirmed_fills(self) -> None:
        book = LiveCapPlanBook(
            okx_inst_id_code=333127, okx_base_per_contract=Decimal("100"),
        )
        opened = book.prepare_open(
            _intent(coin="CAP", notional=Decimal("10")), _current_shape(),
        )
        self.assertEqual(opened[0].quantity, Decimal("100"))
        self.assertEqual(opened[1].quantity, Decimal("1"))
        self.assertEqual(opened[1].base_multiplier, Decimal("100"))
        self.assertEqual((opened[0].side, opened[1].side), ("sell", "buy"))
        close_intent = _intent(
            intent_id=INTENT_B, coin="CAP", action=IntentAction.CLOSE,
            notional=Decimal("10"),
        )
        closed = book.prepare_close(close_intent, _cap_open_state())
        self.assertEqual((closed[0].side, closed[1].side), ("buy", "sell"))
        self.assertEqual((closed[0].quantity, closed[1].quantity), (Decimal("100"), Decimal("1")))
        self.assertTrue(all(plan.reduce_only for plan in closed))
        self.assertEqual(closed[1].base_multiplier, Decimal("100"))
        self.assertEqual(tuple(book.resolve(close_intent)), closed)

    def test_cap_open_proof_survives_state_replay(self) -> None:
        state = _cap_open_state()
        self.assertIs(state.status, SpreadStatus.OPEN)
        self.assertEqual(state.leg_by_id("leg_okx").base_multiplier, Decimal("100"))
        self.assertEqual(SpreadState.from_public_dict(state.to_public_dict()), state)

    def test_cap_open_closes_with_native_quantities_and_flat_proof(self) -> None:
        state = _cap_open_state()
        events = [
            _event(
                ExecutionEventType.INTENT_ACCEPTED, intent_id=INTENT_B,
                sequence=1, monotonic_ns=1006,
                payload={"action": "close", "coin": "CAP", "spread_direction": "long", "lot_tolerance": "0"},
            ),
            _event(
                ExecutionEventType.REQUEST_SENT, intent_id=INTENT_B,
                sequence=2, monotonic_ns=1007, venue=Venue.BYBIT,
                leg_id="leg_bybit",
                payload={"quantity": "100", "base_multiplier": "1", "reduce_only": True, "instrument": "CAPUSDT", "side": "buy"},
            ),
            _event(
                ExecutionEventType.REQUEST_SENT, intent_id=INTENT_B,
                sequence=3, monotonic_ns=1008, venue=Venue.OKX,
                leg_id="leg_okx",
                payload={"quantity": "1", "base_multiplier": "100", "reduce_only": True, "instrument": "CAP-USDT-SWAP", "side": "sell"},
            ),
            _event(
                ExecutionEventType.FILL, intent_id=INTENT_B,
                sequence=4, monotonic_ns=1009, venue=Venue.BYBIT,
                leg_id="leg_bybit", payload={"quantity": "100"},
            ),
            _event(
                ExecutionEventType.FILL, intent_id=INTENT_B,
                sequence=5, monotonic_ns=1010, venue=Venue.OKX,
                leg_id="leg_okx", payload={"quantity": "1"},
            ),
        ]
        for seq, venue, leg_id, event_type, payload in (
            (11, Venue.BYBIT, "leg_bybit", ExecutionEventType.POSITION_OBSERVED, {"quantity": "0"}),
            (12, Venue.OKX, "leg_okx", ExecutionEventType.POSITION_OBSERVED, {"quantity": "0"}),
            (13, Venue.BYBIT, "leg_bybit", ExecutionEventType.OPEN_ORDERS_OBSERVED, {"open_order_count": 0}),
            (14, Venue.OKX, "leg_okx", ExecutionEventType.OPEN_ORDERS_OBSERVED, {"open_order_count": 0}),
        ):
            events.append(_event(event_type, intent_id=INTENT_B, sequence=seq - 5,
                                 monotonic_ns=1000 + seq, venue=venue,
                                 leg_id=leg_id, payload=payload))
        events.append(_event(
            ExecutionEventType.FLATNESS_PROVEN, intent_id=INTENT_B,
            sequence=10, monotonic_ns=1015,
            payload={"positions_flat": True, "open_orders_flat": True},
        ))
        flat = apply_events(
            state,
            [replace(event, event_id=f"evt_{event.monotonic_ns:032d}") for event in events],
        )
        self.assertIs(flat.status, SpreadStatus.FLAT)
        self.assertEqual(SpreadState.from_public_dict(flat.to_public_dict()), flat)

    def test_unconverted_contracts_do_not_prove_open(self) -> None:
        state = _cap_open_state(okx_multiplier="1")
        self.assertIsNot(state.status, SpreadStatus.OPEN)
        self.assertTrue(state.recovery_required)

    def test_multiplier_must_be_positive_and_round_trips(self) -> None:
        plan = LegPlan.build(
            intent_id=INTENT_A, leg_id="leg_okx", venue=Venue.OKX,
            instrument="CAP-USDT-SWAP", side="buy", quantity=Decimal("1"),
            base_multiplier=Decimal("100"),
        )
        self.assertEqual(LegPlan.from_public_dict(plan.to_public_dict()), plan)
        with self.assertRaises(ContractValidationError):
            LegPlan.build(
                intent_id=INTENT_A, leg_id="leg_okx", venue=Venue.OKX,
                instrument="CAP-USDT-SWAP", side="buy", quantity=Decimal("1"),
                base_multiplier=Decimal("0"),
            )

    def test_close_rejects_unproven_or_mismatched_exposure(self) -> None:
        book = LiveCapPlanBook(
            okx_inst_id_code=333127, okx_base_per_contract=Decimal("100"),
        )
        close_intent = _intent(
            intent_id=INTENT_B, coin="CAP", action=IntentAction.CLOSE,
            notional=Decimal("10"),
        )
        with self.assertRaisesRegex(LiveCapPlanError, "proven_cap_open"):
            book.prepare_close(close_intent, initial_spread_state(run_id=RUN_ID))
        with self.assertRaisesRegex(LiveCapPlanError, "proven_cap_open"):
            book.prepare_close(close_intent, _cap_open_state(bybit_fill="90"))

    def test_direction_and_duplicate_intent_are_gated(self) -> None:
        book = LiveCapPlanBook(
            okx_inst_id_code=333127, okx_base_per_contract=Decimal("100"),
        )
        intent = _intent(coin="CAP", notional=Decimal("10"))
        book.prepare_open(intent, _current_shape())
        with self.assertRaisesRegex(LiveCapPlanError, "duplicate_live_intent"):
            book.prepare_open(intent, _current_shape())
        wrong = _intent(
            intent_id=INTENT_B, coin="CAP", action=IntentAction.CLOSE,
            notional=Decimal("10"),
        )
        wrong = replace(wrong, spread_direction=SpreadDirection.SHORT)
        with self.assertRaisesRegex(LiveCapPlanError, "proven_cap_open"):
            book.prepare_close(wrong, _cap_open_state())


if __name__ == "__main__":
    unittest.main()
