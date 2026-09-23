"""EV2-12A2: pure fill evidence before K=1 lifecycle publication."""

from __future__ import annotations

import unittest

from app.bot.execution.contracts import (
    ExecutionEventType,
    SpreadState,
    SpreadStatus,
    Venue,
)
from app.bot.execution.manager_projection import (
    ManagerProjectionError,
    project_manager_exposure,
)
from app.bot.execution.state_machine import apply_events, initial_spread_state
from tests.test_execution_state_machine import (
    BYBIT_LEG,
    CLOSE_INTENT_ID,
    INTENT_ID,
    OKX_LEG,
    RUN_ID,
    Clock,
    _ack,
    _close_arm,
    _dispatch_open,
    _event,
    _fill,
    _happy_open_events,
    _orders,
    _pos,
    _sent,
)


class ManagerProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.initial = initial_spread_state(run_id=RUN_ID)

    def _opened(self) -> SpreadState:
        return apply_events(self.initial, _happy_open_events(self.clock))

    def _close_dispatch(self, opened: SpreadState) -> SpreadState:
        self.clock.seq = 0
        return apply_events(
            opened,
            [
                _close_arm(self.clock),
                _sent(
                    self.clock, Venue.OKX, OKX_LEG,
                    intent_id=CLOSE_INTENT_ID, reduce_only=True,
                ),
                _sent(
                    self.clock, Venue.BYBIT, BYBIT_LEG,
                    intent_id=CLOSE_INTENT_ID, reduce_only=True,
                ),
                _ack(self.clock, Venue.OKX, OKX_LEG, intent_id=CLOSE_INTENT_ID),
                _ack(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=CLOSE_INTENT_ID),
            ],
        )

    def test_initial_and_dual_ack_never_publish_open(self) -> None:
        initial = project_manager_exposure(self.initial, committed_trade_id=None)
        self.assertEqual(initial.publication, "none")
        self.assertTrue(initial.hold_slot)  # initial IDLE is not REST-proven flat
        with self.assertRaisesRegex(ManagerProjectionError, "no_lifecycle"):
            initial.journal_evidence()

        ack_only = apply_events(
            self.initial,
            [
                *_dispatch_open(self.clock),
                _ack(self.clock, Venue.OKX, OKX_LEG),
                _ack(self.clock, Venue.BYBIT, BYBIT_LEG),
            ],
        )
        self.assertNotEqual(ack_only.status, SpreadStatus.OPEN)
        projection = project_manager_exposure(ack_only, committed_trade_id=None)
        self.assertEqual(projection.publication, "none")
        self.assertTrue(projection.hold_slot)

    def test_partial_cancel_and_restart_keep_slot_held(self) -> None:
        partial = apply_events(
            self.initial,
            [
                *_dispatch_open(self.clock),
                _fill(self.clock, Venue.OKX, OKX_LEG, quantity="0.4", partial=True),
                _event(
                    self.clock, ExecutionEventType.CANCEL_REQUESTED,
                    venue=Venue.OKX, leg_id=OKX_LEG,
                ),
                _event(
                    self.clock, ExecutionEventType.CANCEL_ACK,
                    venue=Venue.OKX, leg_id=OKX_LEG,
                ),
            ],
        )
        self.assertNotEqual(partial.status, SpreadStatus.FLAT)
        restored = SpreadState.from_public_dict(partial.to_public_dict())
        projection = project_manager_exposure(restored, committed_trade_id=None)
        self.assertEqual(projection.publication, "none")
        self.assertTrue(projection.hold_slot)

    def test_proven_two_leg_open_has_stable_ids_and_quantities(self) -> None:
        opened = self._opened()
        proposal = project_manager_exposure(opened, committed_trade_id=None)
        self.assertEqual(proposal.publication, "open")
        self.assertTrue(proposal.hold_slot)
        self.assertEqual(proposal.trade_id, INTENT_ID)
        evidence = proposal.journal_evidence()
        self.assertEqual(evidence["ev2_trade_id"], INTENT_ID)
        self.assertEqual(
            {leg["venue"] for leg in evidence["ev2_legs"]},
            {"okx", "bybit"},
        )
        self.assertEqual(
            {leg["effective_open_quantity"] for leg in evidence["ev2_legs"]},
            {"1"},
        )
        self.assertTrue(
            all(leg["position_quantity"] is None for leg in evidence["ev2_legs"])
        )  # fill proof is not a fabricated REST position observation
        self.assertTrue(all(leg["client_id"] for leg in evidence["ev2_legs"]))
        already = project_manager_exposure(opened, committed_trade_id=INTENT_ID)
        self.assertEqual(already.publication, "none")
        with self.assertRaisesRegex(ManagerProjectionError, "open_trade_id_mismatch"):
            project_manager_exposure(opened, committed_trade_id=CLOSE_INTENT_ID)

    def test_close_ack_is_not_flat_and_full_flat_proof_proposes_close(self) -> None:
        closing = self._close_dispatch(self._opened())
        ack_only = project_manager_exposure(closing, committed_trade_id=INTENT_ID)
        self.assertEqual(ack_only.publication, "none")
        self.assertTrue(ack_only.hold_slot)

        closed = apply_events(
            closing,
            [
                _fill(self.clock, Venue.OKX, OKX_LEG, intent_id=CLOSE_INTENT_ID),
                _fill(self.clock, Venue.BYBIT, BYBIT_LEG, intent_id=CLOSE_INTENT_ID),
                _pos(self.clock, Venue.OKX, OKX_LEG, "0", intent_id=CLOSE_INTENT_ID),
                _pos(self.clock, Venue.BYBIT, BYBIT_LEG, "0", intent_id=CLOSE_INTENT_ID),
                _orders(self.clock, Venue.OKX, OKX_LEG, 0, intent_id=CLOSE_INTENT_ID),
                _orders(self.clock, Venue.BYBIT, BYBIT_LEG, 0, intent_id=CLOSE_INTENT_ID),
                _event(
                    self.clock, ExecutionEventType.FLATNESS_PROVEN,
                    intent_id=CLOSE_INTENT_ID,
                    payload={"positions_flat": True, "open_orders_flat": True},
                ),
            ],
        )
        proposal = project_manager_exposure(closed, committed_trade_id=INTENT_ID)
        self.assertEqual(proposal.publication, "close")
        self.assertTrue(proposal.hold_slot)  # fsync must precede slot release
        self.assertEqual(proposal.journal_evidence()["ev2_close_intent_id"], CLOSE_INTENT_ID)
        self.assertEqual(
            {leg["position_quantity"] for leg in proposal.journal_evidence()["ev2_legs"]},
            {"0"},
        )
        with self.assertRaisesRegex(ManagerProjectionError, "close_trade_id_mismatch"):
            project_manager_exposure(closed, committed_trade_id="different-trade")
        restored = SpreadState.from_public_dict(closed.to_public_dict())
        self.assertEqual(
            project_manager_exposure(
                restored, committed_trade_id=INTENT_ID
            ).journal_evidence(),
            proposal.journal_evidence(),
        )


if __name__ == "__main__":
    unittest.main()
