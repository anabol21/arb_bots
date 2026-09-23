"""Public contract round-trip, client-id, and forbidden-field tests."""

from __future__ import annotations

import hashlib
import unittest
from decimal import Decimal

from app.bot.execution.contracts import (
    SCHEMA_VERSION,
    ContractValidationError,
    ExecutionEvent,
    ExecutionEventType,
    IntentAction,
    LegPlan,
    SpreadDirection,
    SpreadState,
    TradeIntent,
    Venue,
    derive_client_id,
)
from app.bot.execution.state_machine import initial_spread_state

INTENT_ID = "95790b7c-8b21-4e82-bae2-1832ddc5bee1"
NEAR_NIBBLE_ID = "95790b7c-8b21-4e82-bae2-1832ddc5bee0"
RUN_ID = "run_aa11bb22cc33dd44ee55ff6677889900"
INTENT_DIGEST = hashlib.sha256(INTENT_ID.encode("utf-8")).hexdigest()


def _intent(**overrides: object) -> TradeIntent:
    payload = dict(
        schema_version=SCHEMA_VERSION,
        intent_id=INTENT_ID,
        run_id=RUN_ID,
        policy_version="policy.v1",
        action=IntentAction.OPEN,
        spread_direction=SpreadDirection.LONG,
        coin="BTC",
        notional_usdt=Decimal("20"),
        signal_mono_ns=1_000,
        signal_wall_ns=1_750_000_000_000_000_000,
        expiry_mono_ns=2_000,
        signal_snapshot_ref="snap_redacted_001",
        canary_stage="shadow",
        risk_policy_revision="risk.v1",
    )
    payload.update(overrides)
    return TradeIntent(**payload)  # type: ignore[arg-type]


class ClientIdDerivationTests(unittest.TestCase):
    def test_okx_shape_is_o_plus_31_digest_hex(self) -> None:
        client_id = derive_client_id(INTENT_ID, Venue.OKX)
        self.assertEqual(client_id, "o" + INTENT_DIGEST[:31])
        self.assertEqual(len(client_id), 32)
        self.assertTrue(client_id.isalnum())
        self.assertNotIn("_", client_id)
        self.assertTrue(client_id.startswith("o"))

    def test_bybit_stays_within_36_and_shares_digest_tail(self) -> None:
        client_id = derive_client_id(INTENT_ID, Venue.BYBIT)
        self.assertEqual(client_id, "b" + INTENT_DIGEST[:35])
        self.assertEqual(len(client_id), 36)
        self.assertTrue(client_id.startswith("b"))
        self.assertTrue(INTENT_DIGEST[:12] in client_id)

    def test_okx_truncation_does_not_collide_on_last_uuid_nibble(self) -> None:
        a = derive_client_id(INTENT_ID, Venue.OKX)
        b = derive_client_id(NEAR_NIBBLE_ID, Venue.OKX)
        self.assertNotEqual(a, b)
        self.assertEqual(len(a), 32)
        self.assertEqual(len(b), 32)
        compact_a = INTENT_ID.replace("-", "")
        compact_b = NEAR_NIBBLE_ID.replace("-", "")
        self.assertEqual(compact_a[:-1], compact_b[:-1])

    def test_reduce_only_uses_flatten_prefix(self) -> None:
        okx = derive_client_id(INTENT_ID, Venue.OKX, reduce_only=True)
        bybit = derive_client_id(INTENT_ID, Venue.BYBIT, reduce_only=True)
        self.assertTrue(okx.startswith("fo"))
        self.assertTrue(bybit.startswith("fb"))
        self.assertEqual(okx, "fo" + INTENT_DIGEST[:30])
        self.assertEqual(bybit, "fb" + INTENT_DIGEST[:34])
        self.assertLessEqual(len(okx), 32)
        self.assertLessEqual(len(bybit), 36)
        self.assertTrue(okx.isalnum())

    def test_leg_plan_client_id_is_derived(self) -> None:
        plan = LegPlan.build(
            intent_id=INTENT_ID,
            leg_id="leg_okx",
            venue=Venue.OKX,
            instrument="BTC-USDT-SWAP",
            side="buy",
            quantity=Decimal("1"),
        )
        self.assertEqual(plan.client_id, derive_client_id(INTENT_ID, Venue.OKX))
        with self.assertRaises(ContractValidationError):
            LegPlan(
                schema_version=SCHEMA_VERSION,
                intent_id=INTENT_ID,
                leg_id="leg_okx",
                venue=Venue.OKX,
                instrument="BTC-USDT-SWAP",
                side="buy",
                quantity=Decimal("1"),
                reduce_only=False,
                client_id="not-derived-id-value-00000001",
                lot_tolerance=Decimal("0"),
            )


class PublicRoundTripTests(unittest.TestCase):
    def test_frozen_single_letter_h_coin_round_trip(self) -> None:
        intent = _intent(coin="H")
        self.assertEqual(TradeIntent.from_public_dict(intent.to_public_dict()), intent)
        with self.assertRaises(ContractValidationError):
            _intent(coin="X")

    def test_trade_intent_round_trip(self) -> None:
        intent = _intent()
        restored = TradeIntent.from_public_dict(intent.to_public_dict())
        self.assertEqual(restored, intent)
        self.assertEqual(restored.notional_usdt, Decimal("20"))
        self.assertIsInstance(intent.to_public_dict()["notional_usdt"], str)

    def test_leg_plan_round_trip(self) -> None:
        plan = LegPlan.build(
            intent_id=INTENT_ID,
            leg_id="leg_bybit",
            venue=Venue.BYBIT,
            instrument="BTCUSDT",
            side="sell",
            quantity=Decimal("1"),
            lot_tolerance=Decimal("0.01"),
        )
        restored = LegPlan.from_public_dict(plan.to_public_dict())
        self.assertEqual(restored, plan)

    def test_execution_event_round_trip(self) -> None:
        event = ExecutionEvent(
            schema_version=SCHEMA_VERSION,
            event_id="evt_00000000000000000000000000000001",
            event_type=ExecutionEventType.FILL,
            intent_id=INTENT_ID,
            run_id=RUN_ID,
            sequence=4,
            monotonic_ns=4000,
            venue=Venue.OKX,
            leg_id="leg_okx",
            payload={"quantity": "1"},
        )
        restored = ExecutionEvent.from_public_dict(event.to_public_dict())
        self.assertEqual(restored.to_public_dict(), event.to_public_dict())

    def test_unknown_field_rejected(self) -> None:
        raw = _intent().to_public_dict()
        raw["extra"] = "nope"
        with self.assertRaises(ContractValidationError):
            TradeIntent.from_public_dict(raw)

    def test_missing_field_rejected(self) -> None:
        raw = _intent().to_public_dict()
        del raw["coin"]
        with self.assertRaises(ContractValidationError):
            TradeIntent.from_public_dict(raw)

    def test_forbidden_api_key_rejected(self) -> None:
        raw = _intent().to_public_dict()
        raw["api_key"] = "should-never-serialize"
        with self.assertRaises(ContractValidationError):
            TradeIntent.from_public_dict(raw)

    def test_forbidden_venue_order_id_rejected(self) -> None:
        raw = ExecutionEvent(
            schema_version=SCHEMA_VERSION,
            event_id="evt_00000000000000000000000000000002",
            event_type=ExecutionEventType.ACK_ACCEPTED,
            intent_id=INTENT_ID,
            run_id=RUN_ID,
            sequence=2,
            monotonic_ns=2000,
            venue=Venue.BYBIT,
            leg_id="leg_bybit",
            payload={},
        ).to_public_dict()
        raw["payload"] = {"order_id": "venue-ord-1"}
        with self.assertRaises(ContractValidationError):
            ExecutionEvent.from_public_dict(raw)

    def test_forbidden_balance_and_signature_rejected(self) -> None:
        raw = _intent().to_public_dict()
        raw["balance"] = "1"
        with self.assertRaises(ContractValidationError):
            TradeIntent.from_public_dict(raw)
        event_raw = {
            "schema_version": SCHEMA_VERSION,
            "event_id": "evt_00000000000000000000000000000003",
            "event_type": "fault",
            "intent_id": INTENT_ID,
            "run_id": RUN_ID,
            "sequence": 1,
            "monotonic_ns": 1,
            "venue": None,
            "leg_id": None,
            "payload": {"signature": "hex"},
        }
        with self.assertRaises(ContractValidationError):
            ExecutionEvent.from_public_dict(event_raw)

    def test_float_decimal_rejected(self) -> None:
        with self.assertRaises(ContractValidationError):
            _intent(notional_usdt=20.5)  # type: ignore[arg-type]

    def test_schema_version_mismatch_rejected(self) -> None:
        raw = _intent().to_public_dict()
        raw["schema_version"] = "bbot.private.journal.v1"
        with self.assertRaises(ContractValidationError):
            TradeIntent.from_public_dict(raw)

    def test_forged_flat_restore_rejected(self) -> None:
        raw = initial_spread_state(run_id=RUN_ID).to_public_dict()
        raw["status"] = "FLAT"
        with self.assertRaises(ContractValidationError):
            SpreadState.from_public_dict(raw)

    def test_forged_flat_two_legs_without_observations_rejected(self) -> None:
        okx_id = derive_client_id(INTENT_ID, Venue.OKX)
        bybit_id = derive_client_id(INTENT_ID, Venue.BYBIT)

        def _leg(leg_id: str, venue: str, client_id: str) -> dict[str, object]:
            return {
                "schema_version": SCHEMA_VERSION,
                "leg_id": leg_id,
                "venue": venue,
                "status": "CANCELLED",
                "client_id": client_id,
                "planned_quantity": "1",
                "filled_quantity": "0",
                "ack_status": "accepted",
                "reduce_only": True,
                "position_quantity": "0",
                "open_order_count": 0,
                "position_observed": False,
                "open_orders_observed": True,
                "stream_generation": 0,
                "confirmed_unfilled": True,
            }

        raw = initial_spread_state(run_id=RUN_ID).to_public_dict()
        raw["status"] = "FLAT"
        raw["positions_flat"] = True
        raw["open_orders_flat"] = True
        raw["legs"] = [
            _leg("leg_okx", "okx", okx_id),
            _leg("leg_bybit", "bybit", bybit_id),
        ]
        with self.assertRaises(ContractValidationError):
            SpreadState.from_public_dict(raw)

    def test_forged_open_restore_without_proof_rejected(self) -> None:
        okx_id = derive_client_id(INTENT_ID, Venue.OKX)
        bybit_id = derive_client_id(INTENT_ID, Venue.BYBIT)

        def _sent_leg(leg_id: str, venue: str, client_id: str) -> dict[str, object]:
            return {
                "schema_version": SCHEMA_VERSION,
                "leg_id": leg_id,
                "venue": venue,
                "status": "SENT",
                "client_id": client_id,
                "planned_quantity": "1",
                "filled_quantity": "0",
                "ack_status": "none",
                "reduce_only": False,
                "position_quantity": "0",
                "open_order_count": 0,
                "position_observed": False,
                "open_orders_observed": False,
                "stream_generation": 0,
                "confirmed_unfilled": False,
            }

        raw = initial_spread_state(run_id=RUN_ID).to_public_dict()
        raw["intent_id"] = INTENT_ID
        raw["open_intent_id"] = INTENT_ID
        raw["accepted_intent_ids"] = [INTENT_ID]
        raw["status"] = "OPEN"
        raw["direction"] = "long"
        raw["coin"] = "BTC"
        raw["legs"] = [
            _sent_leg("leg_okx", "okx", okx_id),
            _sent_leg("leg_bybit", "bybit", bybit_id),
        ]
        with self.assertRaises(ContractValidationError):
            SpreadState.from_public_dict(raw)

    def test_forged_open_intent_mismatch_rejected(self) -> None:
        raw = initial_spread_state(run_id=RUN_ID).to_public_dict()
        raw["status"] = "OPEN"
        raw["intent_id"] = INTENT_ID
        raw["open_intent_id"] = NEAR_NIBBLE_ID
        raw["accepted_intent_ids"] = [INTENT_ID, NEAR_NIBBLE_ID]
        with self.assertRaises(ContractValidationError):
            SpreadState.from_public_dict(raw)

    def test_accepted_intent_ids_round_trip_and_missing_rejected(self) -> None:
        raw = initial_spread_state(run_id=RUN_ID).to_public_dict()
        self.assertEqual(raw["accepted_intent_ids"], [])
        restored = SpreadState.from_public_dict(raw)
        self.assertEqual(restored.accepted_intent_ids, frozenset())
        self.assertEqual(restored.to_public_dict(), raw)
        del raw["accepted_intent_ids"]
        with self.assertRaises(ContractValidationError):
            SpreadState.from_public_dict(raw)

    def test_foreign_client_id_restore_rejected(self) -> None:
        okx_id = derive_client_id(INTENT_ID, Venue.OKX)
        bybit_id = derive_client_id(INTENT_ID, Venue.BYBIT)
        foreign_okx = derive_client_id(NEAR_NIBBLE_ID, Venue.OKX)

        def _filled_leg(leg_id: str, venue: str, client_id: str) -> dict[str, object]:
            return {
                "schema_version": SCHEMA_VERSION,
                "leg_id": leg_id,
                "venue": venue,
                "status": "FILLED",
                "client_id": client_id,
                "planned_quantity": "1",
                "filled_quantity": "1",
                "ack_status": "accepted",
                "reduce_only": False,
                "position_quantity": "1",
                "open_order_count": 0,
                "position_observed": True,
                "open_orders_observed": True,
                "stream_generation": 0,
                "confirmed_unfilled": False,
            }

        raw = initial_spread_state(run_id=RUN_ID).to_public_dict()
        raw["intent_id"] = INTENT_ID
        raw["open_intent_id"] = INTENT_ID
        raw["accepted_intent_ids"] = [INTENT_ID]
        raw["status"] = "OPEN"
        raw["direction"] = "long"
        raw["coin"] = "BTC"
        raw["legs"] = [
            _filled_leg("leg_okx", "okx", foreign_okx),
            _filled_leg("leg_bybit", "bybit", bybit_id),
        ]
        with self.assertRaises(ContractValidationError):
            SpreadState.from_public_dict(raw)
        raw["legs"] = [
            _filled_leg("leg_okx", "okx", okx_id),
            _filled_leg("leg_bybit", "bybit", bybit_id),
        ]
        restored = SpreadState.from_public_dict(raw)
        self.assertEqual(restored.status.value, "OPEN")

    def test_open_restore_fill_versus_zero_position_rejected(self) -> None:
        okx_id = derive_client_id(INTENT_ID, Venue.OKX)
        bybit_id = derive_client_id(INTENT_ID, Venue.BYBIT)

        def _filled_leg(
            leg_id: str,
            venue: str,
            client_id: str,
            *,
            position_quantity: str,
            position_observed: bool,
        ) -> dict[str, object]:
            return {
                "schema_version": SCHEMA_VERSION,
                "leg_id": leg_id,
                "venue": venue,
                "status": "FILLED",
                "client_id": client_id,
                "planned_quantity": "1",
                "filled_quantity": "1",
                "ack_status": "accepted",
                "reduce_only": False,
                "position_quantity": position_quantity,
                "open_order_count": 0,
                "position_observed": position_observed,
                "open_orders_observed": True,
                "stream_generation": 0,
                "confirmed_unfilled": False,
            }

        raw = initial_spread_state(run_id=RUN_ID).to_public_dict()
        raw["intent_id"] = INTENT_ID
        raw["open_intent_id"] = INTENT_ID
        raw["accepted_intent_ids"] = [INTENT_ID]
        raw["status"] = "OPEN"
        raw["direction"] = "long"
        raw["coin"] = "BTC"
        raw["legs"] = [
            _filled_leg(
                "leg_okx",
                "okx",
                okx_id,
                position_quantity="0",
                position_observed=True,
            ),
            _filled_leg(
                "leg_bybit",
                "bybit",
                bybit_id,
                position_quantity="1",
                position_observed=True,
            ),
        ]
        with self.assertRaises(ContractValidationError):
            SpreadState.from_public_dict(raw)

    def _filled_leg(
        self,
        leg_id: str,
        venue: str,
        client_id: str,
        *,
        open_order_count: int = 0,
        open_orders_observed: bool = True,
    ) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "leg_id": leg_id,
            "venue": venue,
            "status": "FILLED",
            "client_id": client_id,
            "planned_quantity": "1",
            "filled_quantity": "1",
            "ack_status": "accepted",
            "reduce_only": False,
            "position_quantity": "1",
            "open_order_count": open_order_count,
            "position_observed": True,
            "open_orders_observed": open_orders_observed,
            "stream_generation": 0,
            "confirmed_unfilled": False,
        }

    def test_forged_open_relabelled_idle_rejected(self) -> None:
        okx_id = derive_client_id(INTENT_ID, Venue.OKX)
        bybit_id = derive_client_id(INTENT_ID, Venue.BYBIT)
        raw = initial_spread_state(run_id=RUN_ID).to_public_dict()
        raw["intent_id"] = INTENT_ID
        raw["open_intent_id"] = INTENT_ID
        raw["accepted_intent_ids"] = [INTENT_ID]
        raw["status"] = "IDLE"
        raw["direction"] = "long"
        raw["coin"] = "BTC"
        raw["legs"] = [
            self._filled_leg("leg_okx", "okx", okx_id),
            self._filled_leg("leg_bybit", "bybit", bybit_id),
        ]
        with self.assertRaises(ContractValidationError):
            SpreadState.from_public_dict(raw)

    def test_fault_latched_empty_idle_restore_allowed(self) -> None:
        raw = initial_spread_state(run_id=RUN_ID).to_public_dict()
        raw["recovery_required"] = True
        raw["pause_latched"] = True
        restored = SpreadState.from_public_dict(raw)
        self.assertEqual(restored.status.value, "IDLE")
        self.assertEqual(restored.legs, ())
        self.assertTrue(restored.recovery_required)
        self.assertTrue(restored.pause_latched)

    def test_armed_with_filled_legs_rejected(self) -> None:
        okx_id = derive_client_id(INTENT_ID, Venue.OKX)
        bybit_id = derive_client_id(INTENT_ID, Venue.BYBIT)
        raw = initial_spread_state(run_id=RUN_ID).to_public_dict()
        raw["intent_id"] = INTENT_ID
        raw["open_intent_id"] = INTENT_ID
        raw["accepted_intent_ids"] = [INTENT_ID]
        raw["status"] = "ARMED"
        raw["direction"] = "long"
        raw["coin"] = "BTC"
        raw["legs"] = [
            self._filled_leg("leg_okx", "okx", okx_id),
            self._filled_leg("leg_bybit", "bybit", bybit_id),
        ]
        with self.assertRaises(ContractValidationError):
            SpreadState.from_public_dict(raw)

    def test_normal_armed_restore_allowed(self) -> None:
        raw = initial_spread_state(run_id=RUN_ID).to_public_dict()
        raw["intent_id"] = INTENT_ID
        raw["open_intent_id"] = INTENT_ID
        raw["accepted_intent_ids"] = [INTENT_ID]
        raw["status"] = "ARMED"
        raw["direction"] = "long"
        raw["coin"] = "BTC"
        restored = SpreadState.from_public_dict(raw)
        self.assertEqual(restored.status.value, "ARMED")
        self.assertEqual(restored.legs, ())
        self.assertEqual(restored.intent_id, INTENT_ID)

    def test_exposure_unknown_zero_legs_restore_allowed(self) -> None:
        raw = initial_spread_state(run_id=RUN_ID).to_public_dict()
        raw["intent_id"] = INTENT_ID
        raw["open_intent_id"] = INTENT_ID
        raw["accepted_intent_ids"] = [INTENT_ID]
        raw["status"] = "EXPOSURE_UNKNOWN"
        raw["direction"] = "long"
        raw["coin"] = "BTC"
        raw["recovery_required"] = True
        restored = SpreadState.from_public_dict(raw)
        self.assertEqual(restored.status.value, "EXPOSURE_UNKNOWN")
        self.assertEqual(restored.legs, ())
        self.assertTrue(restored.recovery_required)

    def test_open_restore_with_working_orders_rejected(self) -> None:
        okx_id = derive_client_id(INTENT_ID, Venue.OKX)
        bybit_id = derive_client_id(INTENT_ID, Venue.BYBIT)
        raw = initial_spread_state(run_id=RUN_ID).to_public_dict()
        raw["intent_id"] = INTENT_ID
        raw["open_intent_id"] = INTENT_ID
        raw["accepted_intent_ids"] = [INTENT_ID]
        raw["status"] = "OPEN"
        raw["direction"] = "long"
        raw["coin"] = "BTC"
        raw["legs"] = [
            self._filled_leg(
                "leg_okx",
                "okx",
                okx_id,
                open_order_count=1,
                open_orders_observed=True,
            ),
            self._filled_leg("leg_bybit", "bybit", bybit_id),
        ]
        with self.assertRaises(ContractValidationError):
            SpreadState.from_public_dict(raw)

    def test_restore_unresolved_timeout_cannot_forge_open_or_flat(self) -> None:
        okx_id = derive_client_id(INTENT_ID, Venue.OKX)
        bybit_id = derive_client_id(INTENT_ID, Venue.BYBIT)
        close_okx = derive_client_id(INTENT_ID, Venue.OKX, reduce_only=True)
        close_bybit = derive_client_id(INTENT_ID, Venue.BYBIT, reduce_only=True)

        def _open_leg(
            *,
            leg_id: str,
            venue: str,
            client_id: str,
            status: str,
            ack_status: str,
            filled_quantity: str,
            position_quantity: str,
            position_observed: bool,
        ) -> dict[str, object]:
            return {
                "schema_version": SCHEMA_VERSION,
                "leg_id": leg_id,
                "venue": venue,
                "status": status,
                "client_id": client_id,
                "planned_quantity": "1",
                "filled_quantity": filled_quantity,
                "ack_status": ack_status,
                "reduce_only": False,
                "position_quantity": position_quantity,
                "open_order_count": 0,
                "position_observed": position_observed,
                "open_orders_observed": True,
                "stream_generation": 0,
                "confirmed_unfilled": False,
            }

        def _flat_leg(
            *,
            leg_id: str,
            venue: str,
            client_id: str,
            status: str,
            ack_status: str,
        ) -> dict[str, object]:
            return {
                "schema_version": SCHEMA_VERSION,
                "leg_id": leg_id,
                "venue": venue,
                "status": status,
                "client_id": client_id,
                "planned_quantity": "1",
                "filled_quantity": "0",
                "ack_status": ack_status,
                "reduce_only": True,
                "position_quantity": "0",
                "open_order_count": 0,
                "position_observed": True,
                "open_orders_observed": True,
                "stream_generation": 0,
                "confirmed_unfilled": True,
            }

        open_raw = initial_spread_state(run_id=RUN_ID).to_public_dict()
        open_raw["intent_id"] = INTENT_ID
        open_raw["open_intent_id"] = INTENT_ID
        open_raw["accepted_intent_ids"] = [INTENT_ID]
        open_raw["status"] = "OPEN"
        open_raw["direction"] = "long"
        open_raw["coin"] = "BTC"
        open_raw["legs"] = [
            _open_leg(
                leg_id="leg_okx",
                venue="okx",
                client_id=okx_id,
                status="UNKNOWN",
                ack_status="timeout",
                filled_quantity="1",
                position_quantity="1",
                position_observed=True,
            ),
            _open_leg(
                leg_id="leg_bybit",
                venue="bybit",
                client_id=bybit_id,
                status="FILLED",
                ack_status="accepted",
                filled_quantity="1",
                position_quantity="1",
                position_observed=True,
            ),
        ]
        with self.assertRaises(ContractValidationError):
            SpreadState.from_public_dict(open_raw)

        filled_timeout_no_pos = dict(open_raw)
        filled_timeout_no_pos["legs"] = [
            _open_leg(
                leg_id="leg_okx",
                venue="okx",
                client_id=okx_id,
                status="FILLED",
                ack_status="timeout",
                filled_quantity="1",
                position_quantity="0",
                position_observed=False,
            ),
            _open_leg(
                leg_id="leg_bybit",
                venue="bybit",
                client_id=bybit_id,
                status="FILLED",
                ack_status="accepted",
                filled_quantity="1",
                position_quantity="1",
                position_observed=True,
            ),
        ]
        with self.assertRaises(ContractValidationError):
            SpreadState.from_public_dict(filled_timeout_no_pos)

        resolved_open = dict(open_raw)
        resolved_open["legs"] = [
            _open_leg(
                leg_id="leg_okx",
                venue="okx",
                client_id=okx_id,
                status="FILLED",
                ack_status="timeout",
                filled_quantity="1",
                position_quantity="1",
                position_observed=True,
            ),
            _open_leg(
                leg_id="leg_bybit",
                venue="bybit",
                client_id=bybit_id,
                status="FILLED",
                ack_status="accepted",
                filled_quantity="1",
                position_quantity="1",
                position_observed=True,
            ),
        ]
        restored_open = SpreadState.from_public_dict(resolved_open)
        self.assertEqual(restored_open.status.value, "OPEN")
        self.assertEqual(restored_open.legs[0].ack_status, "timeout")

        flat_raw = initial_spread_state(run_id=RUN_ID).to_public_dict()
        flat_raw["intent_id"] = INTENT_ID
        flat_raw["open_intent_id"] = INTENT_ID
        flat_raw["accepted_intent_ids"] = [INTENT_ID]
        flat_raw["status"] = "FLAT"
        flat_raw["direction"] = "long"
        flat_raw["coin"] = "BTC"
        flat_raw["positions_flat"] = True
        flat_raw["open_orders_flat"] = True
        flat_raw["legs"] = [
            _flat_leg(
                leg_id="leg_okx",
                venue="okx",
                client_id=close_okx,
                status="UNKNOWN",
                ack_status="timeout",
            ),
            _flat_leg(
                leg_id="leg_bybit",
                venue="bybit",
                client_id=close_bybit,
                status="CANCELLED",
                ack_status="accepted",
            ),
        ]
        with self.assertRaises(ContractValidationError):
            SpreadState.from_public_dict(flat_raw)

        resolved_flat = dict(flat_raw)
        resolved_flat["legs"] = [
            _flat_leg(
                leg_id="leg_okx",
                venue="okx",
                client_id=close_okx,
                status="CANCELLED",
                ack_status="timeout",
            ),
            _flat_leg(
                leg_id="leg_bybit",
                venue="bybit",
                client_id=close_bybit,
                status="CANCELLED",
                ack_status="accepted",
            ),
        ]
        restored_flat = SpreadState.from_public_dict(resolved_flat)
        self.assertEqual(restored_flat.status.value, "FLAT")
        self.assertEqual(restored_flat.legs[0].ack_status, "timeout")


if __name__ == "__main__":
    unittest.main()
