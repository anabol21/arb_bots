"""EV2-04 private event adapter tests. No live I/O or credentials."""

from __future__ import annotations

import json
import os
import unittest
from decimal import Decimal
from typing import Any, Optional

from app.bot.execution.adapters import (
    SCHEMA_VERSION as ADAPTER_SCHEMA_VERSION,
    AdapterBatch,
    AdapterError,
    AdapterIssue,
    AdapterSource,
    PrivateEventAdapter,
)
from app.bot.execution.contracts import (
    SCHEMA_VERSION,
    ExecutionEventType,
    IntentAction,
    LegPlan,
    SpreadDirection,
    SpreadStatus,
    TradeIntent,
    Venue,
    derive_client_id,
)
from app.bot.execution.state_machine import (
    apply_events,
    initial_spread_state,
    opens_allowed,
)

INTENT_ID = "95790b7c-8b21-4e82-bae2-1832ddc5bee1"
RUN_ID = "run_aa11bb22cc33dd44ee55ff6677889900"
OKX_LEG = "leg_okx"
BYBIT_LEG = "leg_bybit"
OKX_INST = "BTC-USDT-SWAP"
BYBIT_INST = "BTCUSDT"
QTY = "1"
FORBIDDEN_MARKERS = (
    "api_key",
    "api_secret",
    "passphrase",
    "RAW-ORDER-ID-99",
    "RAW-EXEC-ID-77",
    "acct-secret-1",
    "orderLinkId",
    "clOrdId",
    "execId",
    "cumExecQty",
    "accFillSz",
    "reqId",
)


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
        signal_mono_ns=500,
        signal_wall_ns=1_750_000_000_000_000_000,
        expiry_mono_ns=2_000_000,
        signal_snapshot_ref="snap_redacted_001",
        canary_stage="shadow",
        risk_policy_revision="risk.v1",
    )
    payload.update(overrides)
    return TradeIntent(**payload)  # type: ignore[arg-type]


def _plans(intent_id: str = INTENT_ID) -> tuple[LegPlan, LegPlan]:
    return (
        LegPlan.build(
            intent_id=intent_id,
            leg_id=BYBIT_LEG,
            venue=Venue.BYBIT,
            instrument=BYBIT_INST,
            side="sell",
            quantity=Decimal(QTY),
        ),
        LegPlan.build(
            intent_id=intent_id,
            leg_id=OKX_LEG,
            venue=Venue.OKX,
            instrument=OKX_INST,
            side="buy",
            quantity=Decimal(QTY),
        ),
    )


def _adapter(
    *,
    last_sequence: int = 0,
    last_monotonic_ns: int = 0,
    generations: Optional[dict[Venue, int]] = None,
) -> PrivateEventAdapter:
    adapter = PrivateEventAdapter(
        expected_generations=generations or {Venue.BYBIT: 1, Venue.OKX: 1},
        last_sequences={INTENT_ID: last_sequence},
        last_monotonic_ns=last_monotonic_ns,
    )
    adapter.register(_intent(), _plans())
    return adapter


def _okx_cid() -> str:
    return derive_client_id(INTENT_ID, Venue.OKX)


def _bybit_cid() -> str:
    return derive_client_id(INTENT_ID, Venue.BYBIT)


def _public_blob(obj: object) -> str:
    if hasattr(obj, "to_public_dict"):
        raw = json.dumps(obj.to_public_dict(), sort_keys=True)  # type: ignore[no-any-unimported]
    else:
        raw = repr(obj)
    return raw + " " + repr(obj)


def _assert_redacted(test: unittest.TestCase, obj: object) -> None:
    blob = _public_blob(obj).lower()
    for marker in FORBIDDEN_MARKERS:
        test.assertNotIn(marker.lower(), blob)


def _arm_and_send(state: Any, seq_start: int = 1, mono_start: int = 1000) -> Any:
    events = []
    seq = seq_start
    mono = mono_start
    events.append(
        _sm_event(
            ExecutionEventType.INTENT_ACCEPTED,
            seq,
            mono,
            payload={
                "action": "open",
                "coin": "BTC",
                "spread_direction": "long",
                "lot_tolerance": "0",
            },
        )
    )
    seq += 1
    mono += 1
    events.append(
        _sm_event(
            ExecutionEventType.REQUEST_SENT,
            seq,
            mono,
            venue=Venue.OKX,
            leg_id=OKX_LEG,
            payload={
                "quantity": QTY,
                "reduce_only": False,
                "instrument": OKX_INST,
                "side": "buy",
                "client_id": _okx_cid(),
            },
        )
    )
    seq += 1
    mono += 1
    events.append(
        _sm_event(
            ExecutionEventType.REQUEST_SENT,
            seq,
            mono,
            venue=Venue.BYBIT,
            leg_id=BYBIT_LEG,
            payload={
                "quantity": QTY,
                "reduce_only": False,
                "instrument": BYBIT_INST,
                "side": "sell",
                "client_id": _bybit_cid(),
            },
        )
    )
    return apply_events(state, events)


def _sm_event(
    event_type: ExecutionEventType,
    sequence: int,
    monotonic_ns: int,
    *,
    venue: Optional[Venue] = None,
    leg_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    event_id: Optional[str] = None,
) -> Any:
    from app.bot.execution.contracts import ExecutionEvent

    return ExecutionEvent(
        schema_version=SCHEMA_VERSION,
        event_id=event_id or f"evt_{monotonic_ns:032d}",
        event_type=event_type,
        intent_id=INTENT_ID,
        run_id=RUN_ID,
        sequence=sequence,
        monotonic_ns=monotonic_ns,
        venue=venue,
        leg_id=leg_id,
        payload=payload or {},
    )


class BybitAckTests(unittest.TestCase):
    def test_bybit_ack_accept_and_reject(self) -> None:
        adapter = _adapter()
        accepted = adapter.adapt(
            {"reqId": _bybit_cid(), "retCode": 0, "op": "order.create", "orderId": "RAW-ORDER-ID-99"},
            venue=Venue.BYBIT,
            source=AdapterSource.TRADE_ACK,
            generation=1,
            receive_mono_ns=2000,
        )
        self.assertEqual(len(accepted.events), 1)
        self.assertEqual(accepted.events[0].event_type, ExecutionEventType.ACK_ACCEPTED)
        self.assertEqual(accepted.events[0].leg_id, BYBIT_LEG)
        self.assertFalse(accepted.issues)
        _assert_redacted(self, accepted)

        rejected = adapter.adapt(
            {"reqId": _bybit_cid(), "retCode": 10001, "retMsg": "qty invalid"},
            venue=Venue.BYBIT,
            source="trade_ack",
            generation=1,
            receive_mono_ns=2001,
        )
        self.assertEqual(len(rejected.events), 1)
        self.assertEqual(rejected.events[0].event_type, ExecutionEventType.ACK_REJECTED)
        self.assertEqual(rejected.events[0].payload.get("reason_code"), "venue_rejected")

    def test_ack_only_replay_never_opens(self) -> None:
        adapter = _adapter(last_sequence=3, last_monotonic_ns=1002)
        frame = {"reqId": _bybit_cid(), "retCode": 0, "success": True}
        first = adapter.adapt(
            frame, venue=Venue.BYBIT, source="trade_ack", generation=1, receive_mono_ns=2000
        )
        replay = adapter.adapt(
            frame, venue=Venue.BYBIT, source="trade_ack", generation=1, receive_mono_ns=2001
        )
        self.assertEqual(len(first.events), 1)
        self.assertEqual(len(replay.events), 0)
        self.assertEqual(adapter.last_sequence(INTENT_ID), 4)
        state = _arm_and_send(initial_spread_state(run_id=RUN_ID))
        after = apply_events(state, first.events)
        self.assertNotEqual(after.status, SpreadStatus.OPEN)
        self.assertFalse(any(leg.filled_quantity > 0 for leg in after.legs))


class OkxAckTests(unittest.TestCase):
    def test_okx_top_level_and_scode(self) -> None:
        adapter = _adapter()
        ok = adapter.adapt(
            {"id": _okx_cid(), "op": "order", "code": "0", "data": [{"sCode": "0", "ordId": "RAW-ORDER-ID-99"}]},
            venue=Venue.OKX,
            source="trade_ack",
            generation=1,
            receive_mono_ns=2000,
        )
        self.assertEqual(ok.events[0].event_type, ExecutionEventType.ACK_ACCEPTED)

        override = adapter.adapt(
            {"id": _okx_cid(), "op": "order", "code": "0", "data": [{"sCode": "51000", "sMsg": "nope"}]},
            venue=Venue.OKX,
            source="trade_ack",
            generation=1,
            receive_mono_ns=2001,
        )
        self.assertEqual(override.events[0].event_type, ExecutionEventType.ACK_REJECTED)
        self.assertEqual(override.events[0].payload.get("reason_code"), "venue_rejected")
        _assert_redacted(self, override)

    def test_okx_id_less_error_requires_exact_expected_id(self) -> None:
        adapter = _adapter()
        unbound = adapter.adapt(
            {"event": "error", "code": "60033", "msg": "Parameter id error"},
            venue=Venue.OKX,
            source="trade_ack",
            generation=1,
            receive_mono_ns=2000,
        )
        self.assertEqual(unbound.events, ())
        self.assertEqual(unbound.issues[0].reason_code, "id_less_ack_unbound")
        _assert_redacted(self, unbound)

        bound = adapter.adapt(
            {"event": "error", "code": "60033", "msg": "Parameter id error"},
            venue=Venue.OKX,
            source="trade_ack",
            generation=1,
            receive_mono_ns=2001,
            expected_client_id=_okx_cid(),
        )
        self.assertEqual(len(bound.events), 1)
        self.assertEqual(bound.events[0].event_type, ExecutionEventType.ACK_REJECTED)
        self.assertEqual(bound.events[0].leg_id, OKX_LEG)


class BybitFillTests(unittest.TestCase):
    def test_partial_to_full_and_stale_lower_qty(self) -> None:
        adapter = _adapter()
        cid = _bybit_cid()
        partial = adapter.adapt(
            {
                "topic": "order",
                "data": [
                    {
                        "orderLinkId": cid,
                        "symbol": BYBIT_INST,
                        "orderStatus": "PartiallyFilled",
                        "cumExecQty": "0.4",
                        "orderId": "RAW-ORDER-ID-99",
                    }
                ],
            },
            venue=Venue.BYBIT,
            source="order",
            generation=1,
            receive_mono_ns=3000,
        )
        self.assertEqual(partial.events[0].event_type, ExecutionEventType.PARTIAL_FILL)
        self.assertEqual(partial.events[0].payload["quantity"], "0.4")

        full = adapter.adapt(
            {
                "topic": "order",
                "data": [
                    {
                        "orderLinkId": cid,
                        "symbol": BYBIT_INST,
                        "orderStatus": "Filled",
                        "cumExecQty": "1",
                    }
                ],
            },
            venue=Venue.BYBIT,
            source="order",
            generation=1,
            receive_mono_ns=3001,
        )
        self.assertEqual(full.events[0].event_type, ExecutionEventType.FILL)
        self.assertEqual(full.events[0].payload["quantity"], "1")

        stale = adapter.adapt(
            {
                "topic": "order",
                "data": [
                    {
                        "orderLinkId": cid,
                        "symbol": BYBIT_INST,
                        "orderStatus": "PartiallyFilled",
                        "cumExecQty": "0.2",
                    }
                ],
            },
            venue=Venue.BYBIT,
            source="order",
            generation=1,
            receive_mono_ns=3002,
        )
        self.assertEqual(stale.events, ())
        self.assertEqual(stale.issues[0].reason_code, "stale_quantity")
        self.assertEqual(adapter.last_sequence(INTENT_ID), 2)


class OkxFillTests(unittest.TestCase):
    def test_partial_to_full_and_overfill_preserved(self) -> None:
        adapter = _adapter()
        cid = _okx_cid()
        partial = adapter.adapt(
            {
                "arg": {"channel": "orders", "instId": OKX_INST},
                "data": [
                    {
                        "clOrdId": cid,
                        "instId": OKX_INST,
                        "state": "partially_filled",
                        "accFillSz": "0.5",
                        "ordId": "RAW-ORDER-ID-99",
                    }
                ],
            },
            venue=Venue.OKX,
            source="orders",
            generation=1,
            receive_mono_ns=3000,
        )
        self.assertEqual(partial.events[0].event_type, ExecutionEventType.PARTIAL_FILL)
        self.assertEqual(partial.events[0].payload["quantity"], "0.5")

        over = adapter.adapt(
            {
                "arg": {"channel": "orders"},
                "data": [
                    {
                        "clOrdId": cid,
                        "instId": OKX_INST,
                        "state": "filled",
                        "accFillSz": "1.25",
                    }
                ],
            },
            venue=Venue.OKX,
            source="orders",
            generation=1,
            receive_mono_ns=3001,
        )
        self.assertEqual(over.events[0].event_type, ExecutionEventType.FILL)
        self.assertEqual(over.events[0].payload["quantity"], "1.25")


class MultiRowAndDedupeTests(unittest.TestCase):
    def test_every_row_in_multi_row_frame_is_processed(self) -> None:
        adapter = _adapter()
        batch = adapter.adapt(
            {
                "topic": "order",
                "data": [
                    {
                        "orderLinkId": _bybit_cid(),
                        "symbol": BYBIT_INST,
                        "orderStatus": "PartiallyFilled",
                        "cumExecQty": "0.3",
                    },
                    {
                        "orderLinkId": "unknown-client-zz",
                        "symbol": BYBIT_INST,
                        "orderStatus": "New",
                        "cumExecQty": "0",
                    },
                ],
            },
            venue=Venue.BYBIT,
            source="order",
            generation=1,
            receive_mono_ns=4000,
        )
        types = [event.event_type for event in batch.events]
        self.assertIn(ExecutionEventType.PARTIAL_FILL, types)
        self.assertIn(ExecutionEventType.UNKNOWN_CORRELATION, types)
        self.assertTrue(any(issue.reason_code == "unknown_correlation" for issue in batch.issues))

    def test_cross_channel_duplicate_fill_emitted_once(self) -> None:
        adapter = _adapter()
        cid = _bybit_cid()
        order = adapter.adapt(
            {
                "topic": "order",
                "data": [
                    {
                        "orderLinkId": cid,
                        "symbol": BYBIT_INST,
                        "orderStatus": "Filled",
                        "cumExecQty": "1",
                    }
                ],
            },
            venue=Venue.BYBIT,
            source="order",
            generation=1,
            receive_mono_ns=4000,
        )
        execution = adapter.adapt(
            {
                "topic": "execution",
                "data": [
                    {
                        "orderLinkId": cid,
                        "symbol": BYBIT_INST,
                        "execType": "Trade",
                        "cumExecQty": "1",
                        "execId": "RAW-EXEC-ID-77",
                    }
                ],
            },
            venue=Venue.BYBIT,
            source="execution",
            generation=1,
            receive_mono_ns=4001,
        )
        self.assertEqual(len(order.events), 1)
        self.assertEqual(len(execution.events), 0)
        self.assertEqual(adapter.last_sequence(INTENT_ID), 1)


class PositionTests(unittest.TestCase):
    def test_buy_sell_net_and_explicit_zero(self) -> None:
        adapter = _adapter()
        sell = adapter.adapt(
            {
                "topic": "position",
                "data": [
                    {"symbol": BYBIT_INST, "size": "1", "side": "Sell", "positionIdx": 0}
                ],
            },
            venue=Venue.BYBIT,
            source="position",
            generation=1,
            receive_mono_ns=5000,
        )
        self.assertEqual(sell.events[0].event_type, ExecutionEventType.POSITION_OBSERVED)
        self.assertEqual(sell.events[0].payload["quantity"], "1")

        zero = adapter.adapt(
            {
                "topic": "position",
                "data": [
                    {"symbol": BYBIT_INST, "size": "0", "side": "None", "positionIdx": 0}
                ],
            },
            venue=Venue.BYBIT,
            source="position",
            generation=1,
            receive_mono_ns=5001,
        )
        self.assertEqual(zero.events[0].payload["quantity"], "0")

        net = adapter.adapt(
            {
                "arg": {"channel": "positions"},
                "data": [{"instId": OKX_INST, "pos": "1", "posSide": "net"}],
            },
            venue=Venue.OKX,
            source="positions",
            generation=1,
            receive_mono_ns=5002,
        )
        self.assertEqual(net.events[0].payload["quantity"], "1")

        short_net = PrivateEventAdapter(
            expected_generations={Venue.BYBIT: 1, Venue.OKX: 1}
        )
        short_net.register(_intent(), _plans())
        negative = short_net.adapt(
            {
                "arg": {"channel": "positions"},
                "data": [{"instId": OKX_INST, "pos": "-1", "posSide": "net"}],
            },
            venue=Venue.OKX,
            source="position",
            generation=1,
            receive_mono_ns=5003,
        )
        self.assertEqual(negative.issues[0].reason_code, "conflicting_position")
        self.assertEqual(negative.events, ())

        hedge = adapter.adapt(
            {
                "topic": "position",
                "data": [
                    {"symbol": BYBIT_INST, "size": "1", "side": "Buy", "positionIdx": 1},
                    {"symbol": BYBIT_INST, "size": "1", "side": "Sell", "positionIdx": 2},
                ],
            },
            venue=Venue.BYBIT,
            source="position",
            generation=1,
            receive_mono_ns=5004,
        )
        self.assertEqual(hedge.events, ())
        self.assertEqual(hedge.issues[0].reason_code, "conflicting_position")


class RestSnapshotTests(unittest.TestCase):
    def test_complete_rest_absence_to_zero_and_matched_order(self) -> None:
        adapter = _adapter()
        positions = adapter.adapt(
            {"retCode": 0, "result": {"list": []}},
            venue=Venue.BYBIT,
            source="rest_positions",
            generation=1,
            receive_mono_ns=6000,
            snapshot_complete=True,
        )
        self.assertEqual(
            [event.event_type for event in positions.events],
            [ExecutionEventType.POSITION_OBSERVED],
        )
        self.assertEqual(positions.events[0].payload["quantity"], "0")
        self.assertEqual(positions.events[0].leg_id, BYBIT_LEG)

        orders = adapter.adapt(
            {"retCode": 0, "result": {"list": []}},
            venue=Venue.BYBIT,
            source="rest_open_orders",
            generation=1,
            receive_mono_ns=6001,
            snapshot_complete=True,
        )
        types = [event.event_type for event in orders.events]
        self.assertEqual(types[0], ExecutionEventType.OPEN_ORDERS_OBSERVED)
        self.assertEqual(orders.events[0].payload["open_order_count"], 0)
        self.assertEqual(types[1], ExecutionEventType.RECONCILIATION)
        self.assertTrue(orders.events[1].payload["matched"])
        self.assertEqual(orders.events[1].venue, Venue.BYBIT)
        self.assertEqual(orders.events[1].leg_id, BYBIT_LEG)

    def test_incomplete_paginated_failed_rest_no_false_zero(self) -> None:
        adapter = _adapter()
        incomplete = adapter.adapt(
            {"retCode": 0, "result": {"list": []}},
            venue=Venue.BYBIT,
            source="rest_positions",
            generation=1,
            receive_mono_ns=6000,
            snapshot_complete=False,
        )
        self.assertEqual(incomplete.events, ())
        self.assertEqual(incomplete.issues[0].reason_code, "incomplete_snapshot")

        paged = adapter.adapt(
            {"retCode": 0, "result": {"list": [], "nextPageCursor": "abc"}},
            venue=Venue.BYBIT,
            source="rest_positions",
            generation=1,
            receive_mono_ns=6001,
            snapshot_complete=True,
        )
        self.assertEqual(paged.events, ())
        self.assertEqual(paged.issues[0].reason_code, "paginated_snapshot")

        failed = adapter.adapt(
            {"retCode": 10001, "retMsg": "boom", "result": {"list": []}},
            venue=Venue.BYBIT,
            source="rest_positions",
            generation=1,
            receive_mono_ns=6002,
            snapshot_complete=True,
        )
        self.assertEqual(failed.events, ())
        self.assertEqual(failed.issues[0].reason_code, "snapshot_failed")
        _assert_redacted(self, failed)

        okx_fail = adapter.adapt(
            {"code": "50001", "msg": "nope", "data": []},
            venue=Venue.OKX,
            source="rest_open_orders",
            generation=1,
            receive_mono_ns=6003,
            snapshot_complete=True,
        )
        self.assertEqual(okx_fail.events, ())
        self.assertEqual(okx_fail.issues[0].reason_code, "snapshot_failed")


class GenerationFenceTests(unittest.TestCase):
    def test_stale_mismatch_block_and_reseed(self) -> None:
        adapter = _adapter()
        stale = adapter.adapt(
            {"reqId": _bybit_cid(), "retCode": 0},
            venue=Venue.BYBIT,
            source="trade_ack",
            generation=0,
            receive_mono_ns=7000,
        )
        self.assertEqual(stale.events, ())
        self.assertEqual(stale.issues[0].reason_code, "stale_generation")

        mismatch = adapter.adapt(
            {
                "topic": "order",
                "data": [
                    {
                        "orderLinkId": _bybit_cid(),
                        "symbol": BYBIT_INST,
                        "orderStatus": "Filled",
                        "cumExecQty": "1",
                    }
                ],
            },
            venue=Venue.BYBIT,
            source="order",
            generation=2,
            receive_mono_ns=7001,
        )
        self.assertEqual(len(mismatch.events), 1)
        self.assertEqual(
            mismatch.events[0].event_type, ExecutionEventType.STREAM_GENERATION_MISMATCH
        )
        self.assertEqual(mismatch.events[0].payload["stream_generation"], 2)
        self.assertTrue(adapter.is_blocked(Venue.BYBIT))
        self.assertEqual(adapter.expected_generation(Venue.BYBIT), 2)
        self.assertTrue(any(issue.reason_code == "blocked_until_reseed" for issue in mismatch.issues))

        repeat = adapter.adapt(
            {
                "topic": "order",
                "data": [
                    {
                        "orderLinkId": _bybit_cid(),
                        "symbol": BYBIT_INST,
                        "orderStatus": "Filled",
                        "cumExecQty": "1",
                    }
                ],
            },
            venue=Venue.BYBIT,
            source="order",
            generation=2,
            receive_mono_ns=7002,
        )
        self.assertEqual(repeat.events, ())
        self.assertEqual(repeat.issues[0].reason_code, "blocked_until_reseed")
        self.assertEqual(adapter.last_sequence(INTENT_ID), 1)

        adapter.adapt(
            {"retCode": 0, "result": {"list": []}},
            venue=Venue.BYBIT,
            source="rest_positions",
            generation=2,
            receive_mono_ns=7003,
            snapshot_complete=True,
        )
        reseed = adapter.adapt(
            {"retCode": 0, "result": {"list": []}},
            venue=Venue.BYBIT,
            source="rest_open_orders",
            generation=2,
            receive_mono_ns=7004,
            snapshot_complete=True,
        )
        types = [event.event_type for event in reseed.events]
        self.assertIn(ExecutionEventType.OPEN_ORDERS_OBSERVED, types)
        self.assertIn(ExecutionEventType.RECONCILIATION, types)
        self.assertFalse(adapter.is_blocked(Venue.BYBIT))

        after = adapter.adapt(
            {"reqId": _bybit_cid(), "retCode": 0},
            venue=Venue.BYBIT,
            source="trade_ack",
            generation=2,
            receive_mono_ns=7005,
        )
        self.assertEqual(after.events[0].event_type, ExecutionEventType.ACK_ACCEPTED)


class ReplayAndRestartTests(unittest.TestCase):
    def test_duplicate_frame_preserves_sequence_and_ids(self) -> None:
        adapter = _adapter()
        frame = {
            "id": _okx_cid(),
            "code": "0",
            "data": [{"sCode": "0"}],
        }
        first = adapter.adapt(
            frame, venue=Venue.OKX, source="trade_ack", generation=1, receive_mono_ns=8000
        )
        second = adapter.adapt(
            frame, venue=Venue.OKX, source="trade_ack", generation=1, receive_mono_ns=8001
        )
        self.assertEqual(len(first.events), 1)
        self.assertEqual(len(second.events), 0)
        self.assertEqual(adapter.last_sequence(INTENT_ID), 1)
        again = _adapter()
        twin = again.adapt(
            frame, venue=Venue.OKX, source="trade_ack", generation=1, receive_mono_ns=8000
        )
        self.assertEqual(twin.events[0].event_id, first.events[0].event_id)
        self.assertEqual(twin.events[0].sequence, first.events[0].sequence)

    def test_restart_continues_sequence_and_monotonic(self) -> None:
        restarted = _adapter(last_sequence=9, last_monotonic_ns=9000)
        batch = restarted.adapt(
            {"reqId": _bybit_cid(), "retCode": 0},
            venue=Venue.BYBIT,
            source="trade_ack",
            generation=1,
            receive_mono_ns=9001,
        )
        self.assertEqual(batch.events[0].sequence, 10)
        self.assertEqual(batch.events[0].monotonic_ns, 9001)
        earlier = restarted.adapt(
            {
                "id": _okx_cid(),
                "code": "0",
                "data": [{"sCode": "0"}],
            },
            venue=Venue.OKX,
            source="trade_ack",
            generation=1,
            receive_mono_ns=100,
        )
        self.assertEqual(earlier.events[0].sequence, 11)
        self.assertGreaterEqual(earlier.events[0].monotonic_ns, 9001)

    def test_primary_plan_is_non_reduce_and_reset_drops_bindings(self) -> None:
        adapter = _adapter(last_sequence=4, last_monotonic_ns=4000)
        primary = adapter.primary_plan(Venue.OKX)
        assert primary is not None
        self.assertFalse(primary.reduce_only)
        self.assertEqual(primary.instrument, OKX_INST)
        self.assertEqual(primary.side, "buy")
        flatten = LegPlan.build(
            intent_id=INTENT_ID,
            leg_id=OKX_LEG,
            venue=Venue.OKX,
            instrument=OKX_INST,
            side="sell",
            quantity=Decimal(QTY),
            reduce_only=True,
        )
        adapter.bind_recovery_plan(flatten)
        self.assertEqual(adapter.primary_plan(Venue.OKX), primary)
        adapter.reset_for_restart(
            last_sequences={INTENT_ID: 4}, last_monotonic_ns=4000
        )
        self.assertIsNone(adapter.primary_plan(Venue.OKX))
        self.assertEqual(adapter.last_sequence(INTENT_ID), 4)
        unknown = adapter.adapt(
            {
                "id": _okx_cid(),
                "code": "0",
                "data": [{"sCode": "0"}],
            },
            venue=Venue.OKX,
            source="trade_ack",
            generation=1,
            receive_mono_ns=4001,
        )
        self.assertTrue(
            any(item.reason_code == "unknown_correlation" for item in unknown.issues)
        )
        adapter.register(_intent(), _plans())
        self.assertIsNotNone(adapter.primary_plan(Venue.OKX))


class CorrelationTests(unittest.TestCase):
    def test_unknown_and_ambiguous_never_create_third_leg(self) -> None:
        adapter = _adapter()
        unknown = adapter.adapt(
            {
                "topic": "order",
                "data": [
                    {
                        "orderLinkId": "totally-unknown",
                        "symbol": "ETHUSDT",
                        "orderStatus": "Filled",
                        "cumExecQty": "1",
                    }
                ],
            },
            venue=Venue.BYBIT,
            source="order",
            generation=1,
            receive_mono_ns=8100,
        )
        self.assertEqual(unknown.events, ())
        self.assertEqual(unknown.issues[0].reason_code, "unknown_correlation")
        self.assertIsNone(unknown.issues[0].leg_id)

        on_known = adapter.adapt(
            {
                "arg": {"channel": "orders"},
                "data": [
                    {
                        "clOrdId": "foreignclord",
                        "instId": OKX_INST,
                        "state": "live",
                        "accFillSz": "0.1",
                    }
                ],
            },
            venue=Venue.OKX,
            source="orders",
            generation=1,
            receive_mono_ns=8101,
        )
        self.assertEqual(
            on_known.events[0].event_type, ExecutionEventType.UNKNOWN_CORRELATION
        )
        self.assertEqual(on_known.events[0].leg_id, OKX_LEG)
        self.assertNotIn("foreignclord", _public_blob(on_known))
        self.assertEqual(len(adapter._legs_by_client), 2)  # noqa: SLF001 — bounded registry

    def test_unknown_rest_orders_counted_and_not_hidden(self) -> None:
        adapter = _adapter()
        adapter.adapt(
            {"code": "0", "data": []},
            venue=Venue.OKX,
            source="rest_positions",
            generation=1,
            receive_mono_ns=8200,
            snapshot_complete=True,
        )
        orders = adapter.adapt(
            {
                "code": "0",
                "data": [
                    {"clOrdId": "stranger", "instId": OKX_INST, "state": "live"},
                    {"clOrdId": _okx_cid(), "instId": OKX_INST, "state": "live"},
                ],
            },
            venue=Venue.OKX,
            source="rest_open_orders",
            generation=1,
            receive_mono_ns=8201,
            snapshot_complete=True,
        )
        observed = [
            event
            for event in orders.events
            if event.event_type is ExecutionEventType.OPEN_ORDERS_OBSERVED
        ][0]
        self.assertEqual(observed.payload["open_order_count"], 2)
        recon = [
            event
            for event in orders.events
            if event.event_type is ExecutionEventType.RECONCILIATION
        ][0]
        self.assertFalse(recon.payload["matched"])
        self.assertTrue(any(issue.reason_code == "unknown_correlation" for issue in orders.issues))


class FailClosedTests(unittest.TestCase):
    def test_float_nan_negative_malformed_redact(self) -> None:
        adapter = _adapter()
        cid = _bybit_cid()
        flo = adapter.adapt(
            {
                "topic": "order",
                "data": [
                    {
                        "orderLinkId": cid,
                        "symbol": BYBIT_INST,
                        "orderStatus": "Filled",
                        "cumExecQty": 0.5,
                    }
                ],
            },
            venue=Venue.BYBIT,
            source="order",
            generation=1,
            receive_mono_ns=8300,
        )
        self.assertEqual(flo.events, ())
        self.assertEqual(flo.issues[0].reason_code, "float_rejected")

        nan = adapter.adapt(
            {
                "topic": "order",
                "data": [
                    {
                        "orderLinkId": cid,
                        "symbol": BYBIT_INST,
                        "orderStatus": "Filled",
                        "cumExecQty": "NaN",
                    }
                ],
            },
            venue=Venue.BYBIT,
            source="order",
            generation=1,
            receive_mono_ns=8301,
        )
        self.assertEqual(nan.events, ())
        self.assertEqual(nan.issues[0].reason_code, "malformed_quantity")

        neg = adapter.adapt(
            {
                "arg": {"channel": "orders"},
                "data": [
                    {
                        "clOrdId": _okx_cid(),
                        "instId": OKX_INST,
                        "state": "filled",
                        "accFillSz": "-1",
                    }
                ],
            },
            venue=Venue.OKX,
            source="orders",
            generation=1,
            receive_mono_ns=8302,
        )
        self.assertEqual(neg.events, ())
        self.assertEqual(neg.issues[0].reason_code, "negative_quantity")

        missing = adapter.adapt(
            {
                "topic": "execution",
                "data": [
                    {
                        "orderLinkId": cid,
                        "symbol": BYBIT_INST,
                        "execQty": "0.2",
                    }
                ],
            },
            venue=Venue.BYBIT,
            source="execution",
            generation=1,
            receive_mono_ns=8303,
        )
        self.assertEqual(missing.events, ())
        self.assertEqual(missing.issues[0].reason_code, "missing_quantity")
        _assert_redacted(self, flo)
        _assert_redacted(self, nan)
        _assert_redacted(self, neg)


class ApplyEventsReplayTests(unittest.TestCase):
    def test_ack_before_fill_fill_before_ack_partial_and_reconnect(self) -> None:
        state = _arm_and_send(initial_spread_state(run_id=RUN_ID))
        ack_adapter = _adapter(last_sequence=3, last_monotonic_ns=1002)
        acks = []
        acks.extend(
            ack_adapter.adapt(
                {"reqId": _bybit_cid(), "retCode": 0},
                venue=Venue.BYBIT,
                source="trade_ack",
                generation=1,
                receive_mono_ns=2000,
            ).events
        )
        acks.extend(
            ack_adapter.adapt(
                {"id": _okx_cid(), "code": "0", "data": [{"sCode": "0"}]},
                venue=Venue.OKX,
                source="trade_ack",
                generation=1,
                receive_mono_ns=2001,
            ).events
        )
        acked = apply_events(state, acks)
        self.assertNotEqual(acked.status, SpreadStatus.OPEN)
        self.assertFalse(opens_allowed(acked))

        fill_state = _arm_and_send(initial_spread_state(run_id=RUN_ID))
        fill_adapter = _adapter(last_sequence=3, last_monotonic_ns=1002)
        fills = []
        fills.extend(
            fill_adapter.adapt(
                {
                    "topic": "order",
                    "data": [
                        {
                            "orderLinkId": _bybit_cid(),
                            "symbol": BYBIT_INST,
                            "orderStatus": "PartiallyFilled",
                            "cumExecQty": "0.4",
                        }
                    ],
                },
                venue=Venue.BYBIT,
                source="order",
                generation=1,
                receive_mono_ns=3000,
            ).events
        )
        fills.extend(
            fill_adapter.adapt(
                {
                    "topic": "order",
                    "data": [
                        {
                            "orderLinkId": _bybit_cid(),
                            "symbol": BYBIT_INST,
                            "orderStatus": "Filled",
                            "cumExecQty": "1",
                        }
                    ],
                },
                venue=Venue.BYBIT,
                source="order",
                generation=1,
                receive_mono_ns=3001,
            ).events
        )
        fills.extend(
            fill_adapter.adapt(
                {
                    "arg": {"channel": "orders"},
                    "data": [
                        {
                            "clOrdId": _okx_cid(),
                            "instId": OKX_INST,
                            "state": "filled",
                            "accFillSz": "1",
                        }
                    ],
                },
                venue=Venue.OKX,
                source="orders",
                generation=1,
                receive_mono_ns=3002,
            ).events
        )
        opened = apply_events(fill_state, fills)
        self.assertEqual(opened.status, SpreadStatus.OPEN)

        reconnect_adapter = _adapter(
            last_sequence=opened.last_sequence,
            last_monotonic_ns=opened.last_monotonic_ns,
        )
        mismatch = reconnect_adapter.adapt(
            {
                "topic": "order",
                "data": [
                    {
                        "orderLinkId": _bybit_cid(),
                        "symbol": BYBIT_INST,
                        "orderStatus": "Filled",
                        "cumExecQty": "1",
                    }
                ],
            },
            venue=Venue.BYBIT,
            source="order",
            generation=4,
            receive_mono_ns=opened.last_monotonic_ns + 1,
        )
        after_mismatch = apply_events(opened, mismatch.events)
        self.assertFalse(after_mismatch.stream_generation_ok)
        self.assertTrue(after_mismatch.recovery_required)

        rest_events = []
        rest_events.extend(
            reconnect_adapter.adapt(
                {
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "symbol": BYBIT_INST,
                                "size": "1",
                                "side": "Sell",
                                "positionIdx": 0,
                            }
                        ]
                    },
                },
                venue=Venue.BYBIT,
                source="rest_positions",
                generation=4,
                receive_mono_ns=opened.last_monotonic_ns + 2,
                snapshot_complete=True,
            ).events
        )
        rest_events.extend(
            reconnect_adapter.adapt(
                {"retCode": 0, "result": {"list": []}},
                venue=Venue.BYBIT,
                source="rest_open_orders",
                generation=4,
                receive_mono_ns=opened.last_monotonic_ns + 3,
                snapshot_complete=True,
            ).events
        )
        recovered = apply_events(after_mismatch, rest_events)
        self.assertTrue(recovered.recovery_required or not recovered.stream_generation_ok)
        self.assertNotEqual(recovered.status, SpreadStatus.FLAT)


class PublicViewAndImportTests(unittest.TestCase):
    def test_public_views_omit_forbidden_fields(self) -> None:
        adapter = _adapter()
        batch = adapter.adapt(
            {
                "reqId": _bybit_cid(),
                "retCode": 0,
                "api_key": "acct-secret-1",
                "orderId": "RAW-ORDER-ID-99",
            },
            venue=Venue.BYBIT,
            source="trade_ack",
            generation=1,
            receive_mono_ns=9000,
        )
        _assert_redacted(self, batch)
        _assert_redacted(self, batch.events[0])
        public = batch.to_public_dict()
        self.assertEqual(public["schema_version"], ADAPTER_SCHEMA_VERSION)
        blob = json.dumps(public)
        self.assertNotIn("RAW-ORDER-ID-99", blob)
        self.assertNotIn("acct-secret-1", blob)
        with self.assertRaises(AdapterError):
            PrivateEventAdapter().register(_intent(), (_plans()[0],))

    def test_import_and_construct_perform_no_io(self) -> None:
        opened: list[str] = []
        real_open = os.open

        def guarded_open(path: str, flags: int, *args: object, **kwargs: object) -> int:
            opened.append(str(path))
            return real_open(path, flags, *args, **kwargs)

        import app.bot.execution.adapters as adapters_mod

        self.assertFalse(hasattr(adapters_mod, "urllib"))
        self.assertFalse(hasattr(adapters_mod, "socket"))
        self.assertNotIn("requests", dir(adapters_mod))
        original = os.open
        os.open = guarded_open  # type: ignore[assignment]
        try:
            adapter = PrivateEventAdapter(
                expected_generations={Venue.BYBIT: 1, Venue.OKX: 1}
            )
            adapter.register(_intent(), _plans())
            self.assertFalse(opened)
            self.assertFalse(adapter.is_blocked(Venue.BYBIT))
        finally:
            os.open = original
        self.assertIsInstance(AdapterIssue, type)
        self.assertIsInstance(AdapterBatch, type)


def _matched_recons(batch: AdapterBatch) -> list[Any]:
    return [
        event
        for event in batch.events
        if event.event_type is ExecutionEventType.RECONCILIATION
        and event.payload.get("matched") is True
    ]


class CriticBlockerRegressionTests(unittest.TestCase):
    def test_rest_non_mapping_position_row_invalidates_snapshot(self) -> None:
        adapter = _adapter()
        stream = adapter.adapt(
            {
                "topic": "position",
                "data": [
                    {"symbol": BYBIT_INST, "size": "1", "side": "Sell", "positionIdx": 0},
                    "not-a-mapping",
                ],
            },
            venue=Venue.BYBIT,
            source="position",
            generation=1,
            receive_mono_ns=9100,
        )
        self.assertEqual(
            [event.event_type for event in stream.events],
            [ExecutionEventType.POSITION_OBSERVED],
        )
        self.assertEqual(stream.events[0].payload["quantity"], "1")
        self.assertTrue(any(issue.reason_code == "malformed_frame" for issue in stream.issues))

        rest = adapter.adapt(
            {"retCode": 0, "result": {"list": ["not-a-mapping"]}},
            venue=Venue.BYBIT,
            source="rest_positions",
            generation=1,
            receive_mono_ns=9101,
            snapshot_complete=True,
        )
        self.assertEqual(rest.events, ())
        self.assertTrue(any(issue.reason_code == "malformed_frame" for issue in rest.issues))

        orders = adapter.adapt(
            {"retCode": 0, "result": {"list": []}},
            venue=Venue.BYBIT,
            source="rest_open_orders",
            generation=1,
            receive_mono_ns=9102,
            snapshot_complete=True,
        )
        self.assertEqual(_matched_recons(orders), [])
        self.assertFalse(
            any(
                event.event_type is ExecutionEventType.POSITION_OBSERVED
                and event.payload.get("quantity") == "0"
                for event in rest.events + orders.events
            )
        )

    def test_rest_position_missing_instrument_no_absence_to_zero(self) -> None:
        adapter = _adapter()
        batch = adapter.adapt(
            {
                "retCode": 0,
                "result": {"list": [{"size": "0", "side": "None", "positionIdx": 0}]},
            },
            venue=Venue.BYBIT,
            source="rest_positions",
            generation=1,
            receive_mono_ns=9200,
            snapshot_complete=True,
        )
        self.assertEqual(batch.events, ())
        self.assertTrue(any(issue.reason_code == "malformed_frame" for issue in batch.issues))
        orders = adapter.adapt(
            {"retCode": 0, "result": {"list": []}},
            venue=Venue.BYBIT,
            source="rest_open_orders",
            generation=1,
            receive_mono_ns=9201,
            snapshot_complete=True,
        )
        self.assertEqual(_matched_recons(orders), [])
        self.assertFalse(
            any(
                event.event_type is ExecutionEventType.POSITION_OBSERVED
                for event in batch.events + orders.events
            )
        )

    def test_rest_open_order_missing_instrument_not_hidden_as_zero_or_matched(self) -> None:
        adapter = _adapter()
        adapter.adapt(
            {"retCode": 0, "result": {"list": []}},
            venue=Venue.BYBIT,
            source="rest_positions",
            generation=1,
            receive_mono_ns=9300,
            snapshot_complete=True,
        )
        orders = adapter.adapt(
            {
                "retCode": 0,
                "result": {"list": [{"orderLinkId": "stranger", "orderStatus": "New"}]},
            },
            venue=Venue.BYBIT,
            source="rest_open_orders",
            generation=1,
            receive_mono_ns=9301,
            snapshot_complete=True,
        )
        self.assertFalse(
            any(
                event.event_type is ExecutionEventType.OPEN_ORDERS_OBSERVED
                and event.payload.get("open_order_count") == 0
                for event in orders.events
            )
        )
        self.assertEqual(_matched_recons(orders), [])
        self.assertTrue(any(issue.reason_code == "malformed_frame" for issue in orders.issues))
        self.assertNotIn("stranger", _public_blob(orders))

    def test_resolve_bound_rejects_other_venue_client_id(self) -> None:
        adapter = _adapter()
        missing_inst = adapter.adapt(
            {
                "arg": {"channel": "orders"},
                "data": [
                    {
                        "clOrdId": _bybit_cid(),
                        "state": "filled",
                        "accFillSz": "1",
                    }
                ],
            },
            venue=Venue.OKX,
            source="orders",
            generation=1,
            receive_mono_ns=9400,
        )
        self.assertEqual(missing_inst.events, ())
        self.assertTrue(
            any(issue.reason_code == "conflicting_identity" for issue in missing_inst.issues)
        )
        self.assertFalse(any(event.leg_id == BYBIT_LEG for event in missing_inst.events))
        self.assertFalse(any(event.leg_id == OKX_LEG for event in missing_inst.events))

        foreign = adapter.adapt(
            {
                "arg": {"channel": "orders"},
                "data": [
                    {
                        "clOrdId": _bybit_cid(),
                        "instId": "ETH-USDT-SWAP",
                        "state": "filled",
                        "accFillSz": "1",
                    }
                ],
            },
            venue=Venue.OKX,
            source="orders",
            generation=1,
            receive_mono_ns=9401,
        )
        self.assertEqual(foreign.events, ())
        self.assertTrue(
            any(issue.reason_code == "conflicting_identity" for issue in foreign.issues)
        )
        self.assertFalse(any(event.venue is Venue.BYBIT for event in foreign.events))

    def test_bybit_retcode_failure_overrides_success_true(self) -> None:
        adapter = _adapter()
        batch = adapter.adapt(
            {"reqId": _bybit_cid(), "retCode": 10001, "success": True},
            venue=Venue.BYBIT,
            source="trade_ack",
            generation=1,
            receive_mono_ns=9500,
        )
        self.assertEqual(len(batch.events), 1)
        self.assertEqual(batch.events[0].event_type, ExecutionEventType.ACK_REJECTED)
        self.assertEqual(batch.events[0].payload.get("reason_code"), "venue_rejected")

    def test_dirty_mismatch_reseed_then_later_clean_full_pair(self) -> None:
        adapter = _adapter()
        mismatch = adapter.adapt(
            {
                "topic": "order",
                "data": [
                    {
                        "orderLinkId": _bybit_cid(),
                        "symbol": BYBIT_INST,
                        "orderStatus": "Filled",
                        "cumExecQty": "1",
                    }
                ],
            },
            venue=Venue.BYBIT,
            source="order",
            generation=2,
            receive_mono_ns=9600,
        )
        self.assertEqual(
            mismatch.events[0].event_type, ExecutionEventType.STREAM_GENERATION_MISMATCH
        )
        self.assertTrue(adapter.is_blocked(Venue.BYBIT))

        dirty_pos = adapter.adapt(
            {
                "retCode": 0,
                "result": {
                    "list": [
                        {"symbol": BYBIT_INST, "size": "1", "side": "Buy", "positionIdx": 1},
                        {"symbol": BYBIT_INST, "size": "1", "side": "Sell", "positionIdx": 2},
                    ]
                },
            },
            venue=Venue.BYBIT,
            source="rest_positions",
            generation=2,
            receive_mono_ns=9601,
            snapshot_complete=True,
        )
        self.assertEqual(_matched_recons(dirty_pos), [])
        self.assertTrue(
            any(issue.reason_code == "conflicting_position" for issue in dirty_pos.issues)
        )

        leftover_orders = adapter.adapt(
            {"retCode": 0, "result": {"list": []}},
            venue=Venue.BYBIT,
            source="rest_open_orders",
            generation=2,
            receive_mono_ns=9602,
            snapshot_complete=True,
        )
        self.assertEqual(_matched_recons(leftover_orders), [])
        self.assertTrue(adapter.is_blocked(Venue.BYBIT))

        failed_pos = adapter.adapt(
            {"retCode": 10001, "result": {"list": []}, "success": True},
            venue=Venue.BYBIT,
            source="rest_positions",
            generation=2,
            receive_mono_ns=9603,
            snapshot_complete=True,
        )
        self.assertEqual(failed_pos.events, ())
        self.assertEqual(failed_pos.issues[0].reason_code, "snapshot_failed")
        self.assertEqual(_matched_recons(failed_pos), [])
        self.assertTrue(adapter.is_blocked(Venue.BYBIT))

        clean_pos = adapter.adapt(
            {"retCode": 0, "result": {"list": []}},
            venue=Venue.BYBIT,
            source="rest_positions",
            generation=2,
            receive_mono_ns=9605,
            snapshot_complete=True,
        )
        self.assertEqual(
            [event.event_type for event in clean_pos.events],
            [ExecutionEventType.POSITION_OBSERVED],
        )
        self.assertEqual(_matched_recons(clean_pos), [])
        self.assertTrue(adapter.is_blocked(Venue.BYBIT))

        clean_pair = adapter.adapt(
            {"retCode": 0, "result": {"list": []}},
            venue=Venue.BYBIT,
            source="rest_open_orders",
            generation=2,
            receive_mono_ns=9606,
            snapshot_complete=True,
        )
        self.assertTrue(_matched_recons(clean_pair))
        self.assertFalse(adapter.is_blocked(Venue.BYBIT))
        after = adapter.adapt(
            {"reqId": _bybit_cid(), "retCode": 0},
            venue=Venue.BYBIT,
            source="trade_ack",
            generation=2,
            receive_mono_ns=9607,
        )
        self.assertEqual(after.events[0].event_type, ExecutionEventType.ACK_ACCEPTED)

    def test_same_generation_explicit_position_retry_after_failed_orders_unblocks(
        self,
    ) -> None:
        adapter = _adapter()
        mismatch = adapter.adapt(
            {
                "topic": "order",
                "data": [
                    {
                        "orderLinkId": _bybit_cid(),
                        "symbol": BYBIT_INST,
                        "orderStatus": "Filled",
                        "cumExecQty": "1",
                    }
                ],
            },
            venue=Venue.BYBIT,
            source="order",
            generation=2,
            receive_mono_ns=9800,
        )
        self.assertEqual(
            mismatch.events[0].event_type, ExecutionEventType.STREAM_GENERATION_MISMATCH
        )
        self.assertTrue(adapter.is_blocked(Venue.BYBIT))
        seq_after_mismatch = adapter.last_sequence(INTENT_ID)

        pos_payload = {
            "retCode": 0,
            "result": {
                "list": [
                    {"symbol": BYBIT_INST, "size": "1", "side": "Sell", "positionIdx": 0}
                ]
            },
        }
        first_pos = adapter.adapt(
            pos_payload,
            venue=Venue.BYBIT,
            source="rest_positions",
            generation=2,
            receive_mono_ns=9801,
            snapshot_complete=True,
        )
        self.assertEqual(
            [event.event_type for event in first_pos.events],
            [ExecutionEventType.POSITION_OBSERVED],
        )
        self.assertEqual(first_pos.events[0].payload["quantity"], "1")
        seq_after_first_pos = adapter.last_sequence(INTENT_ID)
        self.assertEqual(seq_after_first_pos, seq_after_mismatch + 1)

        failed_orders = adapter.adapt(
            {"retCode": 10001, "result": {"list": []}},
            venue=Venue.BYBIT,
            source="rest_open_orders",
            generation=2,
            receive_mono_ns=9802,
            snapshot_complete=True,
        )
        self.assertEqual(failed_orders.events, ())
        self.assertEqual(failed_orders.issues[0].reason_code, "snapshot_failed")
        self.assertEqual(_matched_recons(failed_orders), [])
        self.assertTrue(adapter.is_blocked(Venue.BYBIT))

        incomplete_orders = adapter.adapt(
            {"retCode": 0, "result": {"list": []}},
            venue=Venue.BYBIT,
            source="rest_open_orders",
            generation=2,
            receive_mono_ns=9803,
            snapshot_complete=False,
        )
        self.assertEqual(incomplete_orders.events, ())
        self.assertEqual(incomplete_orders.issues[0].reason_code, "incomplete_snapshot")
        self.assertEqual(_matched_recons(incomplete_orders), [])
        self.assertTrue(adapter.is_blocked(Venue.BYBIT))
        self.assertEqual(adapter.last_sequence(INTENT_ID), seq_after_first_pos)

        retry_pos = adapter.adapt(
            pos_payload,
            venue=Venue.BYBIT,
            source="rest_positions",
            generation=2,
            receive_mono_ns=9804,
            snapshot_complete=True,
        )
        self.assertFalse(
            any(
                event.event_type is ExecutionEventType.POSITION_OBSERVED
                for event in retry_pos.events
            )
        )
        self.assertEqual(adapter.last_sequence(INTENT_ID), seq_after_first_pos)
        self.assertEqual(_matched_recons(retry_pos), [])
        self.assertTrue(adapter.is_blocked(Venue.BYBIT))

        clean_orders = adapter.adapt(
            {"retCode": 0, "result": {"list": []}},
            venue=Venue.BYBIT,
            source="rest_open_orders",
            generation=2,
            receive_mono_ns=9805,
            snapshot_complete=True,
        )
        self.assertTrue(_matched_recons(clean_orders))
        self.assertFalse(adapter.is_blocked(Venue.BYBIT))
        self.assertFalse(
            any(
                event.event_type is ExecutionEventType.POSITION_OBSERVED
                for event in clean_orders.events
            )
        )
        self.assertEqual(adapter.last_sequence(INTENT_ID), seq_after_first_pos + 2)
        after = adapter.adapt(
            {"reqId": _bybit_cid(), "retCode": 0},
            venue=Venue.BYBIT,
            source="trade_ack",
            generation=2,
            receive_mono_ns=9806,
        )
        self.assertEqual(after.events[0].event_type, ExecutionEventType.ACK_ACCEPTED)

    def test_okx_execution_missing_state_does_not_invent_terminal_fill(self) -> None:
        adapter = _adapter()
        batch = adapter.adapt(
            {
                "arg": {"channel": "fills"},
                "data": [
                    {
                        "clOrdId": _okx_cid(),
                        "instId": OKX_INST,
                        "accFillSz": "0.4",
                        "tradeId": "RAW-EXEC-ID-77",
                    }
                ],
            },
            venue=Venue.OKX,
            source="execution",
            generation=1,
            receive_mono_ns=9700,
        )
        self.assertEqual(len(batch.events), 1)
        self.assertEqual(batch.events[0].event_type, ExecutionEventType.PARTIAL_FILL)
        self.assertEqual(batch.events[0].payload["quantity"], "0.4")
        _assert_redacted(self, batch)


if __name__ == "__main__":
    unittest.main()
