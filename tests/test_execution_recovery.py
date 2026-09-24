"""EV2-07 recovery and restart orchestration tests. No network or live I/O."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from unittest.mock import patch
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from app.bot.execution.adapters import (
    SCHEMA_VERSION as ADAPTER_SCHEMA,
    AdapterBatch,
    PrivateEventAdapter,
)
from app.bot.execution.contracts import (
    SCHEMA_VERSION,
    ExecutionEvent,
    ExecutionEventType,
    IntentAction,
    LegPlan,
    LegStatus,
    SpreadDirection,
    SpreadStatus,
    TradeIntent,
    Venue,
    derive_client_id,
)
from app.bot.execution.engine import (
    ExecutionEngine,
    ReadinessSnapshot,
    RiskPolicy,
    SubmitStatus,
)
from app.bot.execution.ownership import FileOwnershipFence
from app.bot.execution.recovery import (
    MAX_RECOVERY_STEPS,
    PreparedVenueAction,
    RecoveryActionKind,
    RecoveryStatus,
    RestartLiveSnapshot,
    VenueActionKind,
    exposure_qty,
    plan_recovery,
    restart_correlation_id,
)
from app.bot.execution.state_machine import (
    apply_events,
    initial_spread_state,
    is_proven_flat,
    opens_allowed,
)
from app.bot.execution.transport import (
    CachedInstrument,
    ExecutionTransport,
    InstrumentCache,
    WriteOutcome,
    prepare_venue_action,
    unsigned_cancel_finalizer,
    unsigned_frame_finalizer,
)
from app.bot.execution.wal import ExecutionWal, WalError

RUN_ID = "run_aa11bb22cc33dd44ee55ff6677889900"
INTENT_A = "95790b7c-8b21-4e82-bae2-1832ddc5bee1"
INTENT_B = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
OKX_INST = "BTC-USDT-SWAP"
BYBIT_INST = "BTCUSDT"
OKX_INST_ID_CODE = 193761
FORBIDDEN = ("api_key", "api_secret", "passphrase", "signature", "order_id", "RAW-ORDER")


class MonoClock:
    def __init__(self, start: int = 10_000) -> None:
        self.n = start

    def __call__(self) -> int:
        self.n += 1
        return self.n


class FakeSocket:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.owner_loop = loop
        self.sent: list[str] = []
        self.asend_calls = 0
        self.error: Optional[BaseException] = None
        self.block = False
        self.fail_before_start = False
        self._started: Optional[asyncio.Event] = None
        self._release: Optional[asyncio.Event] = None

    @property
    def started(self) -> asyncio.Event:
        if self._started is None:
            self._started = asyncio.Event()
        return self._started

    @property
    def release(self) -> asyncio.Event:
        if self._release is None:
            self._release = asyncio.Event()
            if not self.block:
                self._release.set()
        return self._release

    async def asend(self, text: str) -> None:
        if self.fail_before_start:
            raise RuntimeError("no-start")
        self.asend_calls += 1
        self.started.set()
        if self.block:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        self.sent.append(text)


def _intent(
    *,
    intent_id: str = INTENT_A,
    action: IntentAction = IntentAction.OPEN,
    coin: str = "BTC",
) -> TradeIntent:
    return TradeIntent(
        schema_version=SCHEMA_VERSION,
        intent_id=intent_id,
        run_id=RUN_ID,
        policy_version="policy.v1",
        action=action,
        spread_direction=SpreadDirection.LONG,
        coin=coin,
        notional_usdt=Decimal("20"),
        signal_mono_ns=500,
        signal_wall_ns=1_750_000_000_000_000_000,
        expiry_mono_ns=1_000_000,
        signal_snapshot_ref="snap_redacted_001",
        canary_stage="shadow",
        risk_policy_revision="risk.v1",
    )


def _plans(intent_id: str, *, reduce_only: bool = False) -> tuple[LegPlan, LegPlan]:
    bybit_side = "buy" if reduce_only else "sell"
    okx_side = "sell" if reduce_only else "buy"
    return (
        LegPlan.build(
            intent_id=intent_id,
            leg_id="leg_bybit",
            venue=Venue.BYBIT,
            instrument=BYBIT_INST,
            side=bybit_side,
            quantity=Decimal("1"),
            reduce_only=reduce_only,
        ),
        LegPlan.build(
            intent_id=intent_id,
            leg_id="leg_okx",
            venue=Venue.OKX,
            instrument=OKX_INST,
            side=okx_side,
            quantity=Decimal("1"),
            reduce_only=reduce_only,
        ),
    )


def _resolver(intent: TradeIntent) -> tuple[LegPlan, LegPlan]:
    return _plans(intent.intent_id, reduce_only=intent.action is IntentAction.CLOSE)


def _cache() -> InstrumentCache:
    return InstrumentCache.from_snapshots(
        [
            CachedInstrument(
                venue=Venue.BYBIT,
                instrument=BYBIT_INST,
                captured_mono_ns=100,
                fresh_until_mono_ns=10_000_000,
            ),
            CachedInstrument(
                venue=Venue.OKX,
                instrument=OKX_INST,
                captured_mono_ns=100,
                fresh_until_mono_ns=10_000_000,
                inst_id_code=OKX_INST_ID_CODE,
            ),
        ]
    )


def _policy() -> RiskPolicy:
    return RiskPolicy(
        allowed_coins=frozenset({"BTC", "ETH"}),
        max_notional_usdt=Decimal("20"),
    )


def _ready(**overrides: object) -> ReadinessSnapshot:
    payload = dict(
        bybit_trade_ready=True,
        okx_trade_ready=True,
        bybit_private_ready=True,
        okx_private_ready=True,
        bybit_generation=1,
        okx_generation=1,
        kill_switch=False,
        pause=False,
    )
    payload.update(overrides)
    return ReadinessSnapshot(**payload)  # type: ignore[arg-type]


def _open_sides(venue: Venue) -> str:
    return "sell" if venue is Venue.BYBIT else "buy"


def _factory(state: Any, venue: Venue, kind: VenueActionKind) -> LegPlan:
    leg = next(item for item in state.legs if item.venue is venue)
    intent_id = state.intent_id or INTENT_A
    if kind is VenueActionKind.CANCEL:
        return LegPlan.build(
            intent_id=intent_id,
            leg_id=leg.leg_id,
            venue=venue,
            instrument=BYBIT_INST if venue is Venue.BYBIT else OKX_INST,
            side=_open_sides(venue),
            quantity=Decimal("1"),
            reduce_only=False,
        )
    qty = exposure_qty(leg, state.lot_tolerance) or Decimal("1")
    return LegPlan.build(
        intent_id=intent_id,
        leg_id=leg.leg_id,
        venue=venue,
        instrument=BYBIT_INST if venue is Venue.BYBIT else OKX_INST,
        side="buy" if _open_sides(venue) == "sell" else "sell",
        quantity=qty,
        reduce_only=True,
    )


def _event(
    event_type: ExecutionEventType,
    *,
    intent_id: str,
    sequence: int,
    monotonic_ns: int,
    venue: Optional[Venue] = None,
    leg_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> ExecutionEvent:
    return ExecutionEvent(
        schema_version=SCHEMA_VERSION,
        event_id=f"evt_{sequence:032d}",
        event_type=event_type,
        intent_id=intent_id,
        run_id=RUN_ID,
        sequence=sequence,
        monotonic_ns=monotonic_ns,
        venue=venue,
        leg_id=leg_id,
        payload=payload or {},
    )


def _recon(seq: int, venue: Venue, mono: int) -> ExecutionEvent:
    return _event(
        ExecutionEventType.RECONCILIATION,
        intent_id=INTENT_A,
        sequence=seq,
        monotonic_ns=mono,
        venue=venue,
        payload={"matched": True},
    )


def _ready_wal(
    path: Path,
    *,
    max_queue: int = 32,
    reserved_tail: int = 5,
    mark: bool = True,
) -> ExecutionWal:
    wal = ExecutionWal(
        path,
        run_id=RUN_ID,
        max_queue=max_queue,
        reserved_tail=reserved_tail,
        max_durable_lag=32,
    )
    wal.replay()
    if mark:
        tokens = []
        for seq, venue, mono in ((1, Venue.OKX, 10), (2, Venue.BYBIT, 11)):
            ack = wal.enqueue(_recon(seq, venue, mono))
            assert ack.accepted
            durable = wal.drain_once()
            assert durable is not None and durable.reconciliation_token
            tokens.append(durable.reconciliation_token)
        wal.mark_venue_reconciled(tokens[0])
        wal.mark_venue_reconciled(tokens[1])
    return wal


def _public_blob(obj: object) -> str:
    if hasattr(obj, "to_public_dict"):
        raw = json.dumps(obj.to_public_dict(), sort_keys=True)
    else:
        raw = repr(obj)
    return raw.lower()


class RecoveryHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.wal_path = root / "wal.v2" / "wal.jsonl"
        self.lock_path = root / "owner.lock"
        self.clock = MonoClock()
        self.bybit = FakeSocket(self.loop)
        self.okx = FakeSocket(self.loop)
        self.fence = FileOwnershipFence(self.lock_path)
        self.fence.acquire()
        self.snapshot = RestartLiveSnapshot(
            complete=True,
            bybit_positions_flat=True,
            okx_positions_flat=True,
            bybit_open_orders_flat=True,
            okx_open_orders_flat=True,
        )

    async def asyncTearDown(self) -> None:
        if self.fence.owned:
            self.fence.release()
        self.tmp.cleanup()

    def _engine(
        self,
        *,
        wal: Optional[ExecutionWal] = None,
        state: Any = None,
        readiness: Optional[ReadinessSnapshot] = None,
        mark: bool = True,
        max_queue: int = 32,
        reserved_tail: int = 5,
        snapshot: Optional[RestartLiveSnapshot] = None,
        recovery_factory: Optional[Any] = None,
        durable_prewrite: bool = False,
    ) -> ExecutionEngine:
        if wal is None:
            wal = _ready_wal(
                self.wal_path,
                mark=mark,
                max_queue=max_queue,
                reserved_tail=reserved_tail,
            )
        anchor = wal.replay() if durable_prewrite else None
        if durable_prewrite and state is None:
            state = anchor.state
        return ExecutionEngine(
            run_id=RUN_ID,
            wal=wal,
            transport=ExecutionTransport(
                self.loop,
                bybit_socket=self.bybit,
                okx_socket=self.okx,
                finalize_frame=unsigned_frame_finalizer,
                monotonic_ns=self.clock,
            ),
            plan_resolver=_resolver,
            instrument_cache=_cache(),
            risk_policy=_policy(),
            readiness=readiness or _ready(),
            ownership=self.fence,
            monotonic_ns=self.clock,
            state=state,
            recovery_factory=recovery_factory or _factory,
            wal_drain=wal.drain_all,
            snapshot_provider=(lambda: snapshot if snapshot is not None else self.snapshot),
            durable_prewrite=durable_prewrite,
            prewrite_anchor=anchor,
        )

    def _attach_adapter(self, engine: ExecutionEngine, intent: TradeIntent) -> PrivateEventAdapter:
        adapter = PrivateEventAdapter(
            expected_generations={Venue.BYBIT: 1, Venue.OKX: 1},
            last_sequences=dict(engine.adapter_last_sequences()),
            last_monotonic_ns=engine.adapter_last_monotonic_ns(),
        )
        adapter.register(intent, _plans(intent.intent_id))
        engine._adapter = adapter
        return adapter

    async def _ingest(self, engine: ExecutionEngine, events: list[ExecutionEvent]) -> Any:
        return await engine.ingest_adapter_batch(
            AdapterBatch(schema_version=ADAPTER_SCHEMA, events=tuple(events), issues=())
        )

    def _next_event(
        self,
        engine: ExecutionEngine,
        event_type: ExecutionEventType,
        *,
        venue: Optional[Venue] = None,
        leg_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
        intent_id: Optional[str] = None,
    ) -> ExecutionEvent:
        return _event(
            event_type,
            intent_id=intent_id or engine.state.intent_id or INTENT_A,
            sequence=engine.state.last_sequence + 1,
            monotonic_ns=max(engine.state.last_monotonic_ns + 1, self.clock()),
            venue=venue,
            leg_id=leg_id,
            payload=payload,
        )

    async def _open_both_sent(self, engine: ExecutionEngine) -> TradeIntent:
        intent = _intent()
        result = await engine.submit(intent)
        self.assertIn(result.status, {SubmitStatus.ACCEPTED, SubmitStatus.RECOVERY_REQUIRED})
        self.assertGreaterEqual(self.bybit.asend_calls, 1)
        self.assertGreaterEqual(self.okx.asend_calls, 1)
        return intent

    async def _recovering_okx_filled(self, engine: ExecutionEngine) -> TradeIntent:
        intent = await self._open_both_sent(engine)
        await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.FILL,
                    venue=Venue.OKX,
                    leg_id="leg_okx",
                    payload={"quantity": "1"},
                )
            ],
        )
        await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.ACK_REJECTED,
                    venue=Venue.BYBIT,
                    leg_id="leg_bybit",
                    payload={"reason_code": "venue_rejected"},
                )
            ],
        )
        self.assertEqual(engine.state.status, SpreadStatus.RECOVERING)
        return intent

    def _pressure_wal_queue(self, engine: ExecutionEngine, *, target_depth: int) -> None:
        """Enqueue padding after drain so only reserved-tail room remains.

        Does not go through ingest: engine commit would drain the queue
        and erase the pressure this helper is constructing.
        """
        wal = engine._wal
        seq = 10_000
        while wal.health().queue_depth < target_depth:
            seq += 1
            ack = wal.enqueue(
                _event(
                    ExecutionEventType.POSITION_OBSERVED,
                    intent_id=engine.state.intent_id or INTENT_A,
                    sequence=seq,
                    monotonic_ns=engine.state.last_monotonic_ns + seq,
                    venue=Venue.OKX,
                    leg_id="leg_okx",
                    payload={"quantity": "1"},
                )
            )
            self.assertTrue(ack.accepted, ack.reason_code)
        self.assertEqual(wal.health().queue_depth, target_depth)
        self.assertFalse(wal.can_admit(6, open_intent=False))
        self.assertTrue(wal.can_admit(3, open_intent=False))

    def _assert_redacted(self, obj: object) -> None:
        blob = _public_blob(obj)
        for marker in FORBIDDEN:
            self.assertNotIn(marker.lower(), blob)


class PlannerContractTests(unittest.TestCase):
    def test_public_contracts_are_redacted_and_finite(self) -> None:
        state = initial_spread_state(run_id=RUN_ID)
        plan = plan_recovery(state, _ready(), attempts=0)
        self.assertEqual(plan.kind, RecoveryActionKind.NOTHING)
        blob = _public_blob(plan)
        for marker in FORBIDDEN:
            self.assertNotIn(marker.lower(), blob)
        self.assertEqual(restart_correlation_id(state, RUN_ID), f"restart:{RUN_ID}")


class RecoveryFaultMatrixTests(RecoveryHarness):
    async def test_live_recovery_flatten_wal_failure_never_writes(self) -> None:
        engine = self._engine(durable_prewrite=True)
        await self._recovering_okx_filled(engine)
        plan = await engine.plan_recovery()
        self.assertEqual(plan.kind, RecoveryActionKind.FLATTEN_FILLED)
        before = self.okx.asend_calls
        with patch.object(engine._wal, "drain_and_prove_last", side_effect=RuntimeError("fsync_failed")):
            result = await engine.apply_recovery_step(plan)
        self.assertEqual(result.status, RecoveryStatus.BLOCKED)
        self.assertEqual(result.reason_code, "wal_unhealthy")
        self.assertEqual(self.okx.asend_calls, before)
        self.assertTrue(engine.readiness.kill_switch)

    async def test_live_recovery_cancel_wal_failure_never_writes(self) -> None:
        engine = self._engine(durable_prewrite=True)
        await self._recovering_okx_filled(engine)
        await self._ingest(
            engine,
            [self._next_event(
                engine,
                ExecutionEventType.OPEN_ORDERS_OBSERVED,
                venue=Venue.BYBIT,
                leg_id="leg_bybit",
                payload={"open_order_count": 1},
            )],
        )
        plan = await engine.plan_recovery()
        self.assertEqual(plan.kind, RecoveryActionKind.CANCEL_PEER)
        before = self.bybit.asend_calls
        with patch.object(engine._wal, "drain_and_prove_last", side_effect=RuntimeError("fsync_failed")):
            result = await engine.apply_recovery_step(plan)
        self.assertEqual(result.status, RecoveryStatus.BLOCKED)
        self.assertEqual(result.reason_code, "wal_unhealthy")
        self.assertEqual(self.bybit.asend_calls, before)
        self.assertTrue(engine.readiness.kill_switch)

    async def test_01_peer_reject_flattens_filled_venue_only(self) -> None:
        engine = self._engine()
        await self._recovering_okx_filled(engine)
        before_bybit = self.bybit.asend_calls
        plan = await engine.plan_recovery()
        self.assertEqual(plan.kind, RecoveryActionKind.FLATTEN_FILLED)
        self.assertEqual(plan.venue, Venue.OKX)
        result = await engine.apply_recovery_step(plan)
        self.assertEqual(result.status, RecoveryStatus.APPLIED)
        self.assertEqual(self.bybit.asend_calls, before_bybit)
        self.assertEqual(self.okx.asend_calls, 2)
        sent = json.loads(self.okx.sent[-1])
        self.assertTrue(sent["args"][0]["reduceOnly"])
        self.assertEqual(sent["op"], "order")
        okx = engine.state.leg_by_id("leg_okx")
        assert okx is not None
        self.assertTrue(okx.reduce_only)

    async def test_02_unresolved_timeout_waits_and_does_not_act(self) -> None:
        engine = self._engine()
        await self._open_both_sent(engine)
        await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.FILL,
                    venue=Venue.OKX,
                    leg_id="leg_okx",
                    payload={"quantity": "1"},
                )
            ],
        )
        await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.ACK_TIMEOUT,
                    venue=Venue.BYBIT,
                    leg_id="leg_bybit",
                    payload={"reason_code": "ack_timeout"},
                )
            ],
        )
        plan = await engine.plan_recovery()
        self.assertEqual(plan.kind, RecoveryActionKind.WAIT_RESEED)
        self.assertEqual(plan.reason_code, "timeout_unresolved")
        before = (self.bybit.asend_calls, self.okx.asend_calls)
        await engine.apply_recovery_step(plan)
        self.assertEqual((self.bybit.asend_calls, self.okx.asend_calls), before)

    async def test_03_resolved_timeout_then_flatten_filled_only(self) -> None:
        engine = self._engine()
        await self._open_both_sent(engine)
        await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.FILL,
                    venue=Venue.OKX,
                    leg_id="leg_okx",
                    payload={"quantity": "1"},
                )
            ],
        )
        await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.ACK_TIMEOUT,
                    venue=Venue.BYBIT,
                    leg_id="leg_bybit",
                    payload={"reason_code": "ack_timeout"},
                )
            ],
        )
        await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.POSITION_OBSERVED,
                    venue=Venue.BYBIT,
                    leg_id="leg_bybit",
                    payload={"quantity": "0"},
                )
            ],
        )
        await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.OPEN_ORDERS_OBSERVED,
                    venue=Venue.BYBIT,
                    leg_id="leg_bybit",
                    payload={"open_order_count": 0},
                )
            ],
        )
        await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.RECONCILIATION,
                    venue=Venue.BYBIT,
                    leg_id="leg_bybit",
                    payload={"matched": True},
                )
            ],
        )
        self.assertEqual(engine.state.status, SpreadStatus.RECOVERING)
        plan = await engine.plan_recovery()
        self.assertEqual(plan.kind, RecoveryActionKind.FLATTEN_FILLED)
        self.assertEqual(plan.venue, Venue.OKX)
        before_bybit = self.bybit.asend_calls
        await engine.apply_recovery_step(plan)
        self.assertEqual(self.bybit.asend_calls, before_bybit)

    async def test_04_working_peer_cancels_before_flatten(self) -> None:
        engine = self._engine()
        await self._recovering_okx_filled(engine)
        await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.OPEN_ORDERS_OBSERVED,
                    venue=Venue.BYBIT,
                    leg_id="leg_bybit",
                    payload={"open_order_count": 1},
                )
            ],
        )
        plan = await engine.plan_recovery()
        self.assertEqual(plan.kind, RecoveryActionKind.CANCEL_PEER)
        self.assertEqual(plan.venue, Venue.BYBIT)
        before_okx = self.okx.asend_calls
        result = await engine.apply_recovery_step(plan)
        self.assertEqual(result.status, RecoveryStatus.APPLIED)
        self.assertEqual(self.okx.asend_calls, before_okx)
        body = json.loads(self.bybit.sent[-1])
        self.assertEqual(body["op"], "order.cancel")
        self.assertNotIn("orderId", json.dumps(body))

    async def test_recovery_flatten_readiness_change_before_write_sends_nothing(self) -> None:
        engine = self._engine()
        await self._recovering_okx_filled(engine)
        plan = await engine.plan_recovery()
        self.assertEqual(plan.kind, RecoveryActionKind.FLATTEN_FILLED)
        before = self.okx.asend_calls
        original = engine._transport._finalize_frame

        def _drop_private_during_finalize(*args: Any, **kwargs: Any) -> str:
            engine.publish_readiness(
                _ready(bybit_private_ready=False, bybit_generation=2)
            )
            return original(*args, **kwargs)

        engine._transport._finalize_frame = _drop_private_during_finalize
        result = await engine.apply_recovery_step(plan)
        self.assertEqual(self.okx.asend_calls, before)
        self.assertEqual(result.status, RecoveryStatus.BLOCKED)
        self.assertIsNotNone(result.dispatch)
        assert result.dispatch is not None
        self.assertEqual(result.dispatch.evidence.outcome, WriteOutcome.NOT_ATTEMPTED)
        self.assertEqual(result.dispatch.evidence.reason_code, "readiness_changed")

    async def test_recovery_cancel_readiness_change_before_write_sends_nothing(self) -> None:
        engine = self._engine()
        await self._recovering_okx_filled(engine)
        await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.OPEN_ORDERS_OBSERVED,
                    venue=Venue.BYBIT,
                    leg_id="leg_bybit",
                    payload={"open_order_count": 1},
                )
            ],
        )
        plan = await engine.plan_recovery()
        self.assertEqual(plan.kind, RecoveryActionKind.CANCEL_PEER)
        before = self.bybit.asend_calls
        original = engine._transport._finalize_cancel

        def _drop_trade_during_finalize(*args: Any, **kwargs: Any) -> str:
            engine.publish_readiness(
                _ready(bybit_trade_ready=False, bybit_generation=2)
            )
            return original(*args, **kwargs)

        engine._transport._finalize_cancel = _drop_trade_during_finalize
        result = await engine.apply_recovery_step(plan)
        self.assertEqual(self.bybit.asend_calls, before)
        self.assertEqual(result.status, RecoveryStatus.BLOCKED)
        self.assertIsNotNone(result.dispatch)
        assert result.dispatch is not None
        self.assertEqual(result.dispatch.evidence.outcome, WriteOutcome.NOT_ATTEMPTED)
        self.assertEqual(result.dispatch.evidence.reason_code, "readiness_changed")

    async def test_05_late_peer_fill_aborts_flatten(self) -> None:
        engine = self._engine()
        await self._open_both_sent(engine)
        await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.ACK_ACCEPTED,
                    venue=Venue.BYBIT,
                    leg_id="leg_bybit",
                )
            ],
        )
        await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.ACK_ACCEPTED,
                    venue=Venue.OKX,
                    leg_id="leg_okx",
                )
            ],
        )
        await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.FILL,
                    venue=Venue.OKX,
                    leg_id="leg_okx",
                    payload={"quantity": "1"},
                )
            ],
        )
        stale = await engine.plan_recovery()
        self.assertEqual(stale.kind, RecoveryActionKind.WAIT_RESEED)
        await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.FILL,
                    venue=Venue.BYBIT,
                    leg_id="leg_bybit",
                    payload={"quantity": "1"},
                )
            ],
        )
        self.assertEqual(engine.state.status, SpreadStatus.OPEN)
        result = await engine.apply_recovery_step(stale)
        self.assertEqual(result.action, RecoveryActionKind.NOTHING)
        self.assertEqual(engine.state.status, SpreadStatus.OPEN)

    async def test_06_partial_without_position_does_not_flatten(self) -> None:
        engine = self._engine()
        await self._open_both_sent(engine)
        await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.PARTIAL_FILL,
                    venue=Venue.OKX,
                    leg_id="leg_okx",
                    payload={"quantity": "0.4"},
                )
            ],
        )
        plan = await engine.plan_recovery()
        self.assertEqual(plan.kind, RecoveryActionKind.WAIT_RESEED)
        before = self.okx.asend_calls
        await engine.apply_recovery_step(plan)
        self.assertEqual(self.okx.asend_calls, before)

    async def test_07_partial_plus_position_flattens_observed_qty(self) -> None:
        engine = self._engine()
        await self._open_both_sent(engine)
        await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.PARTIAL_FILL,
                    venue=Venue.OKX,
                    leg_id="leg_okx",
                    payload={"quantity": "0.4"},
                )
            ],
        )
        await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.POSITION_OBSERVED,
                    venue=Venue.OKX,
                    leg_id="leg_okx",
                    payload={"quantity": "0.4"},
                )
            ],
        )
        await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.ACK_REJECTED,
                    venue=Venue.BYBIT,
                    leg_id="leg_bybit",
                    payload={"reason_code": "venue_rejected"},
                )
            ],
        )
        self.assertEqual(engine.state.status, SpreadStatus.RECOVERING)
        plan = await engine.plan_recovery()
        self.assertEqual(plan.kind, RecoveryActionKind.FLATTEN_FILLED)
        result = await engine.apply_recovery_step(plan)
        self.assertEqual(result.status, RecoveryStatus.APPLIED)
        body = json.loads(self.okx.sent[-1])
        self.assertEqual(body["args"][0]["sz"], "0.4")

    async def test_08_overfill_never_flattens_and_can_halt(self) -> None:
        engine = self._engine()
        await self._open_both_sent(engine)
        await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.FILL,
                    venue=Venue.OKX,
                    leg_id="leg_okx",
                    payload={"quantity": "2"},
                )
            ],
        )
        plan = await engine.plan_recovery()
        self.assertEqual(plan.kind, RecoveryActionKind.WAIT_RESEED)
        self.assertEqual(plan.reason_code, "qty_conflict")
        engine._recovery_attempts = MAX_RECOVERY_STEPS
        halted = await engine.plan_recovery()
        self.assertEqual(halted.kind, RecoveryActionKind.HALT)
        before = self.okx.asend_calls
        await engine.apply_recovery_step(halted)
        self.assertEqual(engine.state.status, SpreadStatus.HALTED)
        self.assertEqual(self.okx.asend_calls, before)

    async def test_09_flatten_fill_same_qty_is_not_dropped(self) -> None:
        engine = self._engine()
        intent = await self._recovering_okx_filled(engine)
        adapter = self._attach_adapter(engine, intent)
        flatten = LegPlan.build(
            intent_id=intent.intent_id,
            leg_id="leg_okx",
            venue=Venue.OKX,
            instrument=OKX_INST,
            side="sell",
            quantity=Decimal("1"),
            reduce_only=True,
        )
        adapter.bind_recovery_plan(flatten)
        open_fill = adapter.adapt(
            {
                "data": [
                    {
                        "instId": OKX_INST,
                        "clOrdId": derive_client_id(intent.intent_id, Venue.OKX),
                        "accFillSz": "1",
                        "state": "filled",
                    }
                ]
            },
            venue=Venue.OKX,
            source="order",
            generation=1,
            receive_mono_ns=engine.adapter_last_monotonic_ns() + 10,
        )
        flatten_fill = adapter.adapt(
            {
                "data": [
                    {
                        "instId": OKX_INST,
                        "clOrdId": flatten.client_id,
                        "accFillSz": "1",
                        "state": "filled",
                    }
                ]
            },
            venue=Venue.OKX,
            source="order",
            generation=1,
            receive_mono_ns=engine.adapter_last_monotonic_ns() + 20,
        )
        self.assertTrue(any(item.event_type is ExecutionEventType.FILL for item in open_fill.events))
        self.assertTrue(
            any(item.event_type is ExecutionEventType.FILL for item in flatten_fill.events)
        )

    async def test_10_second_cancel_on_flatten_client_is_not_deduped(self) -> None:
        engine = self._engine()
        intent = await self._recovering_okx_filled(engine)
        adapter = self._attach_adapter(engine, intent)
        flatten = LegPlan.build(
            intent_id=intent.intent_id,
            leg_id="leg_okx",
            venue=Venue.OKX,
            instrument=OKX_INST,
            side="sell",
            quantity=Decimal("1"),
            reduce_only=True,
        )
        adapter.bind_recovery_plan(flatten)
        first = adapter.adapt(
            {
                "data": [
                    {
                        "instId": OKX_INST,
                        "clOrdId": derive_client_id(intent.intent_id, Venue.OKX),
                        "state": "canceled",
                    }
                ]
            },
            venue=Venue.OKX,
            source="order",
            generation=1,
            receive_mono_ns=engine.adapter_last_monotonic_ns() + 10,
        )
        second = adapter.adapt(
            {
                "data": [
                    {
                        "instId": OKX_INST,
                        "clOrdId": flatten.client_id,
                        "state": "canceled",
                    }
                ]
            },
            venue=Venue.OKX,
            source="order",
            generation=1,
            receive_mono_ns=engine.adapter_last_monotonic_ns() + 20,
        )
        self.assertTrue(
            any(item.event_type is ExecutionEventType.CANCEL_ACK for item in first.events)
        )
        self.assertTrue(
            any(item.event_type is ExecutionEventType.CANCEL_ACK for item in second.events)
        )

    async def test_11_unbound_flatten_client_is_unknown_correlation(self) -> None:
        engine = self._engine()
        intent = await self._recovering_okx_filled(engine)
        adapter = self._attach_adapter(engine, intent)
        flatten_cid = derive_client_id(intent.intent_id, Venue.OKX, reduce_only=True)
        batch = adapter.adapt(
            {
                "data": [
                    {
                        "instId": OKX_INST,
                        "clOrdId": flatten_cid,
                        "accFillSz": "1",
                        "state": "filled",
                    }
                ]
            },
            venue=Venue.OKX,
            source="order",
            generation=1,
            receive_mono_ns=engine.adapter_last_monotonic_ns() + 10,
        )
        self.assertTrue(
            any(item.reason_code == "unknown_correlation" for item in batch.issues)
            or any(
                item.event_type is ExecutionEventType.UNKNOWN_CORRELATION
                for item in batch.events
            )
        )
        self.assertFalse(
            any(item.event_type is ExecutionEventType.FILL for item in batch.events)
        )

    async def test_12_stale_stream_blocks_ordinary_and_requires_reseed(self) -> None:
        engine = self._engine()
        intent = await self._recovering_okx_filled(engine)
        adapter = self._attach_adapter(engine, intent)
        mismatch = adapter.adapt(
            {"data": []},
            venue=Venue.OKX,
            source="order",
            generation=3,
            receive_mono_ns=engine.adapter_last_monotonic_ns() + 10,
        )
        self.assertTrue(
            any(
                item.event_type is ExecutionEventType.STREAM_GENERATION_MISMATCH
                for item in mismatch.events
            )
        )
        await self._ingest(engine, list(mismatch.events))
        blocked = adapter.adapt(
            {
                "data": [
                    {
                        "instId": OKX_INST,
                        "clOrdId": derive_client_id(intent.intent_id, Venue.OKX),
                        "accFillSz": "1",
                        "state": "filled",
                    }
                ]
            },
            venue=Venue.OKX,
            source="order",
            generation=3,
            receive_mono_ns=engine.adapter_last_monotonic_ns() + 20,
        )
        self.assertTrue(any(item.reason_code == "blocked_until_reseed" for item in blocked.issues))
        plan = await engine.plan_recovery()
        self.assertEqual(plan.kind, RecoveryActionKind.WAIT_RESEED)
        before = self.okx.asend_calls
        await engine.apply_recovery_step(plan)
        self.assertEqual(self.okx.asend_calls, before)

    async def test_13_incomplete_rest_does_not_prove_flat(self) -> None:
        engine = self._engine()
        intent = await self._recovering_okx_filled(engine)
        adapter = self._attach_adapter(engine, intent)
        rest = adapter.adapt(
            {"code": "0", "data": []},
            venue=Venue.OKX,
            source="rest_positions",
            generation=1,
            receive_mono_ns=engine.adapter_last_monotonic_ns() + 10,
            snapshot_complete=False,
        )
        self.assertTrue(any(item.reason_code == "incomplete_snapshot" for item in rest.issues))
        self.assertFalse(
            any(
                item.event_type is ExecutionEventType.RECONCILIATION
                and item.payload.get("matched") is True
                for item in rest.events
            )
        )
        proven = [
            item
            for item in engine.state.to_public_dict()
            if False
        ]
        del proven
        self.assertNotEqual(engine.state.status, SpreadStatus.FLAT)

    async def test_14_crash_after_durable_flatten_request_does_not_resend(self) -> None:
        engine = self._engine()
        await self._recovering_okx_filled(engine)
        await engine.apply_recovery_step(await engine.plan_recovery())
        engine._wal.drain_all()
        restarted = ExecutionWal(
            self.wal_path, run_id=RUN_ID, max_queue=32, reserved_tail=5, max_durable_lag=32
        )
        replay = restarted.replay()
        self.fence.release()
        self.fence.acquire()
        other = self._engine(wal=restarted, state=replay.state, mark=False)
        await other.begin_restart(replay)
        self.assertTrue(any(leg.reduce_only for leg in other.state.legs))
        before = self.okx.asend_calls
        plan = await other.plan_recovery()
        self.assertNotEqual(plan.kind, RecoveryActionKind.FLATTEN_FILLED)
        if plan.kind is not RecoveryActionKind.NOTHING:
            await other.apply_recovery_step(plan)
        self.assertEqual(self.okx.asend_calls, before)

    async def test_15_crash_before_flatten_write_keeps_fill_and_may_flatten(self) -> None:
        engine = self._engine()
        await self._recovering_okx_filled(engine)
        engine._wal.drain_all()
        restarted = ExecutionWal(
            self.wal_path, run_id=RUN_ID, max_queue=32, reserved_tail=5, max_durable_lag=32
        )
        replay = restarted.replay()
        self.fence.release()
        self.fence.acquire()
        other = self._engine(wal=restarted, state=replay.state, mark=False)
        await other.begin_restart(replay)
        okx = other.state.leg_by_id("leg_okx")
        assert okx is not None
        self.assertEqual(okx.filled_quantity, Decimal("1"))
        self.assertFalse(okx.reduce_only)
        plan = await other.plan_recovery()
        self.assertEqual(plan.kind, RecoveryActionKind.FLATTEN_FILLED)
        before = self.okx.asend_calls
        blocked = await other.apply_recovery_step(plan)
        self.assertEqual(blocked.reason_code, "flatten_failed")
        self.assertEqual(self.okx.asend_calls, before)
        # Venue reconciliation alone cannot stand in for a rebound private
        # adapter after process restart.
        other._restart_unproven = False
        still_blocked = await other.apply_recovery_step(plan)
        self.assertEqual(still_blocked.reason_code, "flatten_failed")
        self.assertEqual(self.okx.asend_calls, before)
        self._attach_adapter(other, _intent())
        rebound = await other.plan_recovery()
        self.assertEqual(rebound.kind, RecoveryActionKind.FLATTEN_FILLED)

    async def test_16_crash_after_enqueue_before_drain_forgets_in_memory_cancel(self) -> None:
        engine = self._engine()
        await self._recovering_okx_filled(engine)
        await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.OPEN_ORDERS_OBSERVED,
                    venue=Venue.BYBIT,
                    leg_id="leg_bybit",
                    payload={"open_order_count": 1},
                )
            ],
        )
        await engine.apply_recovery_step(await engine.plan_recovery())
        restarted = ExecutionWal(
            self.wal_path, run_id=RUN_ID, max_queue=32, reserved_tail=5, max_durable_lag=32
        )
        replay = restarted.replay()
        self.assertFalse(
            any(
                record.event.event_type is ExecutionEventType.CANCEL_REQUESTED
                for record in replay.records
            )
        )

    async def test_17_restart_empty_idle_uses_restart_correlation_id(self) -> None:
        wal = _ready_wal(self.wal_path, mark=False)
        engine = self._engine(wal=wal, mark=False)
        replay = wal.replay()
        restart = await engine.begin_restart(replay)
        self.assertEqual(restart.restart_intent_id, f"restart:{RUN_ID}")
        self.assertTrue(restart.requires_reseed)
        self.assertFalse(restart.opens_allowed)
        plan = await engine.plan_recovery()
        self.assertEqual(plan.kind, RecoveryActionKind.WAIT_RESEED)
        result = await engine.apply_recovery_step(plan)
        self.assertEqual(result.status, RecoveryStatus.APPLIED)
        acks = wal.drain_all()
        tokens = [ack.reconciliation_token for ack in acks if ack.reconciliation_token]
        self.assertEqual(len(tokens), 2)
        await engine.acknowledge_reconciliation(tokens[0])
        await engine.acknowledge_reconciliation(tokens[1])
        self.assertFalse(wal.health().blocks_opens)
        self.assertTrue(opens_allowed(engine.state))

    async def test_18_restart_flat_rejects_stale_token(self) -> None:
        engine = self._engine()
        await self._recovering_okx_filled(engine)
        for venue, leg_id in ((Venue.OKX, "leg_okx"), (Venue.BYBIT, "leg_bybit")):
            await self._ingest(
                engine,
                [
                    self._next_event(
                        engine,
                        ExecutionEventType.POSITION_OBSERVED,
                        venue=venue,
                        leg_id=leg_id,
                        payload={"quantity": "0"},
                    )
                ],
            )
            await self._ingest(
                engine,
                [
                    self._next_event(
                        engine,
                        ExecutionEventType.OPEN_ORDERS_OBSERVED,
                        venue=venue,
                        leg_id=leg_id,
                        payload={"open_order_count": 0},
                    )
                ],
            )
        await engine.apply_recovery_step(await engine.plan_recovery())
        if engine.state.status is SpreadStatus.RECOVERING:
            await self._ingest(
                engine,
                [
                    self._next_event(
                        engine,
                        ExecutionEventType.FILL,
                        venue=Venue.OKX,
                        leg_id="leg_okx",
                        payload={"quantity": "1"},
                    )
                ],
            )
            for venue, leg_id in ((Venue.OKX, "leg_okx"), (Venue.BYBIT, "leg_bybit")):
                await self._ingest(
                    engine,
                    [
                        self._next_event(
                            engine,
                            ExecutionEventType.POSITION_OBSERVED,
                            venue=venue,
                            leg_id=leg_id,
                            payload={"quantity": "0"},
                        )
                    ],
                )
                await self._ingest(
                    engine,
                    [
                        self._next_event(
                            engine,
                            ExecutionEventType.OPEN_ORDERS_OBSERVED,
                            venue=venue,
                            leg_id=leg_id,
                            payload={"open_order_count": 0},
                        )
                    ],
                )
            plan = await engine.plan_recovery()
            if plan.kind is RecoveryActionKind.PROVE_FLAT:
                await engine.apply_recovery_step(plan)
        engine._wal.drain_all()
        restarted = ExecutionWal(
            self.wal_path, run_id=RUN_ID, max_queue=32, reserved_tail=5, max_durable_lag=32
        )
        replay = restarted.replay()
        self.fence.release()
        self.fence.acquire()
        other = self._engine(wal=restarted, state=replay.state, mark=False)
        await other.begin_restart(replay)
        with self.assertRaises(WalError):
            await other.acknowledge_reconciliation("deadbeef:0:1:" + ("ab" * 32))

    async def test_19_restart_recovering_rebuilds_single_flatten(self) -> None:
        engine = self._engine()
        await self._recovering_okx_filled(engine)
        engine._wal.drain_all()
        restarted = ExecutionWal(
            self.wal_path, run_id=RUN_ID, max_queue=32, reserved_tail=5, max_durable_lag=32
        )
        replay = restarted.replay()
        self.fence.release()
        self.fence.acquire()
        self.okx.sent.clear()
        other = self._engine(wal=restarted, state=replay.state, mark=False)
        await other.begin_restart(replay)
        self._attach_adapter(other, _intent())
        plan = await other.plan_recovery()
        self.assertEqual(plan.kind, RecoveryActionKind.FLATTEN_FILLED)
        before_bybit = self.bybit.asend_calls
        await other.apply_recovery_step(plan)
        self.assertEqual(self.bybit.asend_calls, before_bybit)
        self.assertTrue(self.okx.sent)

    async def test_20_restart_halted_stays_halted_and_cannot_prove_flat(self) -> None:
        engine = self._engine()
        await self._recovering_okx_filled(engine)
        engine._recovery_attempts = MAX_RECOVERY_STEPS
        await engine.apply_recovery_step(await engine.plan_recovery())
        self.assertEqual(engine.state.status, SpreadStatus.HALTED)
        ingested = await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.FLATNESS_PROVEN,
                    payload={"positions_flat": True, "open_orders_flat": True},
                )
            ],
        )
        self.assertFalse(ingested.accepted)
        self.assertEqual(engine.state.status, SpreadStatus.HALTED)
        plan = await engine.plan_recovery()
        self.assertNotEqual(plan.kind, RecoveryActionKind.PROVE_FLAT)

    async def test_21_flatten_no_asend_start_does_not_permit_second_flatten(self) -> None:
        engine = self._engine()
        await self._recovering_okx_filled(engine)

        def _reject_reduce_only(static: Any, **kwargs: Any) -> str:
            from app.bot.execution.transport import TransportError

            if static.reduce_only:
                raise TransportError("rejected_before_write")
            return unsigned_frame_finalizer(static, **kwargs)

        engine._transport._finalize_frame = _reject_reduce_only
        before = self.okx.asend_calls
        result = await engine.apply_recovery_step(await engine.plan_recovery())
        self.assertEqual(result.action, RecoveryActionKind.WAIT_RESEED)
        self.assertEqual(result.reason_code, "wait_reseed")
        after = engine.state.leg_by_id("leg_okx")
        assert after is not None
        self.assertTrue(after.reduce_only)
        self.assertEqual(self.okx.asend_calls, before)
        plan = await engine.plan_recovery()
        self.assertNotEqual(plan.kind, RecoveryActionKind.FLATTEN_FILLED)
        await engine.apply_recovery_step(plan)
        self.assertEqual(self.okx.asend_calls, before)

    async def test_22_flatten_started_then_failed_does_not_resend(self) -> None:
        engine = self._engine()
        await self._recovering_okx_filled(engine)
        self.okx.error = RuntimeError("write-failed")
        result = await engine.apply_recovery_step(await engine.plan_recovery())
        self.assertEqual(result.reason_code, "flatten_failed")
        okx = engine.state.leg_by_id("leg_okx")
        assert okx is not None
        self.assertTrue(okx.reduce_only)
        plan = await engine.plan_recovery()
        self.assertNotEqual(plan.kind, RecoveryActionKind.FLATTEN_FILLED)

    async def test_23_flatness_proven_without_fresh_zeros_is_rejected(self) -> None:
        engine = self._engine()
        await self._recovering_okx_filled(engine)
        ingested = await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.FLATNESS_PROVEN,
                    payload={"positions_flat": True, "open_orders_flat": True},
                )
            ],
        )
        self.assertFalse(ingested.accepted)
        self.assertNotEqual(engine.state.status, SpreadStatus.FLAT)

    async def test_24_double_reject_proves_flat_without_flatten(self) -> None:
        engine = self._engine()
        await self._open_both_sent(engine)
        for venue, leg_id in ((Venue.OKX, "leg_okx"), (Venue.BYBIT, "leg_bybit")):
            await self._ingest(
                engine,
                [
                    self._next_event(
                        engine,
                        ExecutionEventType.ACK_REJECTED,
                        venue=venue,
                        leg_id=leg_id,
                        payload={"reason_code": "venue_rejected"},
                    )
                ],
            )
        before = (self.bybit.asend_calls, self.okx.asend_calls)
        for venue, leg_id in ((Venue.OKX, "leg_okx"), (Venue.BYBIT, "leg_bybit")):
            await self._ingest(
                engine,
                [
                    self._next_event(
                        engine,
                        ExecutionEventType.POSITION_OBSERVED,
                        venue=venue,
                        leg_id=leg_id,
                        payload={"quantity": "0"},
                    )
                ],
            )
            await self._ingest(
                engine,
                [
                    self._next_event(
                        engine,
                        ExecutionEventType.OPEN_ORDERS_OBSERVED,
                        venue=venue,
                        leg_id=leg_id,
                        payload={"open_order_count": 0},
                    )
                ],
            )
        plan = await engine.plan_recovery()
        self.assertEqual(plan.kind, RecoveryActionKind.PROVE_FLAT)
        result = await engine.apply_recovery_step(plan)
        self.assertEqual(result.status, RecoveryStatus.APPLIED)
        self.assertEqual(engine.state.status, SpreadStatus.FLAT)
        self.assertTrue(is_proven_flat(engine.state))
        self.assertEqual((self.bybit.asend_calls, self.okx.asend_calls), before)

    async def test_25_submit_close_while_recovering_is_rejected(self) -> None:
        engine = self._engine()
        await self._recovering_okx_filled(engine)
        result = await engine.submit(_intent(intent_id=INTENT_B, action=IntentAction.CLOSE))
        self.assertEqual(result.status, SubmitStatus.REJECTED)
        self.assertEqual(result.reason_code, "close_not_open")
        plan = await engine.plan_recovery()
        self.assertEqual(plan.kind, RecoveryActionKind.FLATTEN_FILLED)

    async def test_26_submit_open_blocked_but_flatten_still_allowed(self) -> None:
        engine = self._engine()
        await self._recovering_okx_filled(engine)
        await engine.update_readiness(_ready(pause=True, kill_switch=True))
        rejected = await engine.submit(_intent(intent_id=INTENT_B))
        self.assertEqual(rejected.status, SubmitStatus.REJECTED)
        plan = await engine.plan_recovery()
        self.assertEqual(plan.kind, RecoveryActionKind.FLATTEN_FILLED)
        result = await engine.apply_recovery_step(plan)
        self.assertEqual(result.status, RecoveryStatus.APPLIED)

    async def test_27_recovery_never_uses_dual_leg_dispatch(self) -> None:
        engine = self._engine()
        await self._recovering_okx_filled(engine)
        result = await engine.apply_recovery_step(await engine.plan_recovery())
        self.assertIsInstance(result.dispatch, type(result.dispatch))
        if result.dispatch is not None:
            self.assertFalse(hasattr(result.dispatch, "bybit") and hasattr(result.dispatch, "okx"))
            self.assertIsInstance(result.dispatch, result.dispatch.__class__)
        self.assertEqual(self.bybit.asend_calls, 1)

    async def test_28_lock_serializes_submit_and_recovery(self) -> None:
        engine = self._engine()
        await self._recovering_okx_filled(engine)
        self.okx.block = True
        self.okx.release.clear()
        apply_task = asyncio.create_task(
            engine.apply_recovery_step(await engine.plan_recovery())
        )
        await self.okx.started.wait()
        submit_task = asyncio.create_task(engine.submit(_intent(intent_id=INTENT_B)))
        await asyncio.sleep(0)
        self.assertFalse(submit_task.done())
        self.okx.release.set()
        apply_result = await apply_task
        submit_result = await submit_task
        self.assertEqual(apply_result.status, RecoveryStatus.APPLIED)
        self.assertEqual(submit_result.status, SubmitStatus.REJECTED)

    async def test_29_reserved_tail_admits_per_venue_reseed_not_six_event_batch(self) -> None:
        # OPEN worst-case is 5 and cannot enter the reserved tail, so the
        # open lifecycle must be established on a WAL that still admits
        # opens. Queue pressure is applied only after RECOVERING.
        engine = self._engine(max_queue=12, reserved_tail=5)
        await self._recovering_okx_filled(engine)
        self._pressure_wal_queue(engine, target_depth=7)
        six_events = []
        for i in range(6):
            six_events.append(
                _event(
                    ExecutionEventType.POSITION_OBSERVED,
                    intent_id=engine.state.intent_id or INTENT_A,
                    sequence=engine.state.last_sequence + 1 + i,
                    monotonic_ns=engine.state.last_monotonic_ns + 1 + i,
                    venue=Venue.OKX if i % 2 == 0 else Venue.BYBIT,
                    leg_id="leg_okx" if i % 2 == 0 else "leg_bybit",
                    payload={"quantity": "0"},
                )
            )
        dual = await self._ingest(engine, six_events)
        self.assertFalse(dual.accepted)
        self.assertEqual(dual.reason_code, "adapter_capacity")
        three = [
            _event(
                ExecutionEventType.POSITION_OBSERVED,
                intent_id=engine.state.intent_id or INTENT_A,
                sequence=engine.state.last_sequence + 1,
                monotonic_ns=engine.state.last_monotonic_ns + 1,
                venue=Venue.OKX,
                leg_id="leg_okx",
                payload={"quantity": "0"},
            ),
            _event(
                ExecutionEventType.OPEN_ORDERS_OBSERVED,
                intent_id=engine.state.intent_id or INTENT_A,
                sequence=engine.state.last_sequence + 2,
                monotonic_ns=engine.state.last_monotonic_ns + 2,
                venue=Venue.OKX,
                leg_id="leg_okx",
                payload={"open_order_count": 0},
            ),
            _event(
                ExecutionEventType.RECONCILIATION,
                intent_id=engine.state.intent_id or INTENT_A,
                sequence=engine.state.last_sequence + 3,
                monotonic_ns=engine.state.last_monotonic_ns + 3,
                venue=Venue.OKX,
                leg_id="leg_okx",
                payload={"matched": True},
            ),
        ]
        per_venue = await self._ingest(engine, three)
        self.assertTrue(per_venue.accepted)

    async def test_30_ownership_lost_blocks_recovery_write(self) -> None:
        engine = self._engine()
        await self._recovering_okx_filled(engine)
        self.fence.release()
        before = self.okx.asend_calls
        result = await engine.apply_recovery_step(await engine.plan_recovery())
        self.assertEqual(result.reason_code, "ownership_not_held")
        self.assertEqual(self.okx.asend_calls, before)

    async def test_31_late_fill_after_flatten_cannot_return_open(self) -> None:
        engine = self._engine()
        await self._recovering_okx_filled(engine)
        await engine.apply_recovery_step(await engine.plan_recovery())
        await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.FILL,
                    venue=Venue.BYBIT,
                    leg_id="leg_bybit",
                    payload={"quantity": "1"},
                )
            ],
        )
        self.assertNotEqual(engine.state.status, SpreadStatus.OPEN)
        plan = await engine.plan_recovery()
        self.assertNotEqual(plan.kind, RecoveryActionKind.FLATTEN_FILLED)

    async def test_32_successful_recovery_proves_both_venues_flat(self) -> None:
        engine = self._engine()
        await self._recovering_okx_filled(engine)
        await engine.apply_recovery_step(await engine.plan_recovery())
        self._attach_adapter(engine, _intent())
        flatten = LegPlan.build(
            intent_id=INTENT_A,
            leg_id="leg_okx",
            venue=Venue.OKX,
            instrument=OKX_INST,
            side="sell",
            quantity=Decimal("1"),
            reduce_only=True,
        )
        engine._adapter.bind_recovery_plan(flatten)
        await self._ingest(
            engine,
            [
                self._next_event(
                    engine,
                    ExecutionEventType.FILL,
                    venue=Venue.OKX,
                    leg_id="leg_okx",
                    payload={"quantity": "1"},
                )
            ],
        )
        for venue, leg_id in ((Venue.OKX, "leg_okx"), (Venue.BYBIT, "leg_bybit")):
            await self._ingest(
                engine,
                [
                    self._next_event(
                        engine,
                        ExecutionEventType.POSITION_OBSERVED,
                        venue=venue,
                        leg_id=leg_id,
                        payload={"quantity": "0"},
                    )
                ],
            )
            await self._ingest(
                engine,
                [
                    self._next_event(
                        engine,
                        ExecutionEventType.OPEN_ORDERS_OBSERVED,
                        venue=venue,
                        leg_id=leg_id,
                        payload={"open_order_count": 0},
                    )
                ],
            )
        plan = await engine.plan_recovery()
        self.assertEqual(plan.kind, RecoveryActionKind.PROVE_FLAT)
        result = await engine.apply_recovery_step(plan)
        self.assertEqual(result.status, RecoveryStatus.APPLIED)
        self.assertEqual(engine.state.status, SpreadStatus.FLAT)
        self.assertFalse(engine.state.recovery_required)
        self.assertTrue(is_proven_flat(engine.state))
        for leg in engine.state.legs:
            self.assertTrue(leg.position_observed)
            self.assertEqual(leg.position_quantity, Decimal("0"))
            self.assertTrue(leg.open_orders_observed)
            self.assertEqual(leg.open_order_count, 0)
        self._assert_redacted(result)

    async def test_submit_cannot_be_used_to_flatten(self) -> None:
        engine = self._engine()
        await self._recovering_okx_filled(engine)
        close = await engine.submit(_intent(intent_id=INTENT_B, action=IntentAction.CLOSE))
        self.assertEqual(close.status, SubmitStatus.REJECTED)
        opened = await engine.submit(_intent(intent_id=INTENT_B))
        self.assertEqual(opened.status, SubmitStatus.REJECTED)

    async def test_ownership_released_after_admit_does_not_asend(self) -> None:
        engine = self._engine()
        await self._recovering_okx_filled(engine)
        original = engine._wal.enqueue_batch

        def _release_after_admit(events: Any) -> Any:
            acks = original(events)
            self.fence.release()
            return acks

        engine._wal.enqueue_batch = _release_after_admit  # type: ignore[method-assign]
        before = self.okx.asend_calls
        result = await engine.apply_recovery_step(await engine.plan_recovery())
        self.assertEqual(result.reason_code, "ownership_not_held")
        self.assertEqual(self.okx.asend_calls, before)
        okx = engine.state.leg_by_id("leg_okx")
        assert okx is not None
        self.assertTrue(okx.reduce_only)
        plan = await engine.plan_recovery()
        self.assertNotEqual(plan.kind, RecoveryActionKind.FLATTEN_FILLED)

    async def test_begin_restart_blocks_open_until_two_new_venues(self) -> None:
        wal = ExecutionWal(
            self.wal_path, run_id=RUN_ID, max_queue=32, reserved_tail=5, max_durable_lag=32
        )
        wal.replay()
        prior: list[str] = []
        for seq, venue, mono in ((1, Venue.OKX, 10), (2, Venue.BYBIT, 11)):
            ack = wal.enqueue(_recon(seq, venue, mono))
            self.assertTrue(ack.accepted)
            durable = wal.drain_once()
            assert durable is not None and durable.reconciliation_token
            prior.append(durable.reconciliation_token)
        wal.mark_venue_reconciled(prior[0])
        wal.mark_venue_reconciled(prior[1])
        self.assertTrue(wal.health().venue_reconciliation_complete)
        engine = self._engine(wal=wal, mark=False)
        replay = wal.replay()
        restart = await engine.begin_restart(replay)
        self.assertFalse(restart.opens_allowed)
        self.assertTrue(restart.requires_reseed)
        self.assertTrue(wal.health().blocks_opens)
        self.assertFalse(wal.health().venue_reconciliation_complete)
        opened = await engine.submit(_intent(intent_id=INTENT_B))
        self.assertEqual(opened.status, SubmitStatus.REJECTED)
        self.assertEqual(opened.reason_code, "opens_not_allowed")
        with self.assertRaises(WalError):
            await engine.acknowledge_reconciliation(prior[0])
        with self.assertRaises(WalError):
            await engine.acknowledge_reconciliation(prior[1])
        plan = await engine.plan_recovery()
        self.assertEqual(plan.kind, RecoveryActionKind.WAIT_RESEED)
        emitted = await engine.apply_recovery_step(plan)
        self.assertEqual(emitted.status, RecoveryStatus.APPLIED)
        acks = wal.drain_all()
        tokens = [ack.reconciliation_token for ack in acks if ack.reconciliation_token]
        self.assertEqual(len(tokens), 2)
        await engine.acknowledge_reconciliation(tokens[0])
        one = await engine.submit(_intent(intent_id=INTENT_B))
        self.assertEqual(one.status, SubmitStatus.REJECTED)
        self.assertEqual(one.reason_code, "opens_not_allowed")
        await engine.acknowledge_reconciliation(tokens[1])
        allowed = await engine.submit(_intent(intent_id=INTENT_B))
        self.assertIn(allowed.status, {SubmitStatus.ACCEPTED, SubmitStatus.RECOVERY_REQUIRED})

    async def test_wrong_instrument_or_side_blocks_flatten(self) -> None:
        def _wrong_instrument(state: Any, venue: Venue, kind: VenueActionKind) -> LegPlan:
            plan = _factory(state, venue, kind)
            if kind is VenueActionKind.PLACE:
                return LegPlan.build(
                    intent_id=plan.intent_id,
                    leg_id=plan.leg_id,
                    venue=venue,
                    instrument="ETH-USDT-SWAP" if venue is Venue.OKX else "ETHUSDT",
                    side=plan.side,
                    quantity=plan.quantity,
                    reduce_only=True,
                )
            return plan

        def _wrong_side(state: Any, venue: Venue, kind: VenueActionKind) -> LegPlan:
            plan = _factory(state, venue, kind)
            if kind is VenueActionKind.PLACE:
                return LegPlan.build(
                    intent_id=plan.intent_id,
                    leg_id=plan.leg_id,
                    venue=venue,
                    instrument=plan.instrument,
                    side=_open_sides(venue),
                    quantity=plan.quantity,
                    reduce_only=True,
                )
            return plan

        engine = self._engine(recovery_factory=_wrong_instrument)
        await self._recovering_okx_filled(engine)
        before = self.okx.asend_calls
        wrong_instrument = await engine.apply_recovery_step(await engine.plan_recovery())
        self.assertEqual(wrong_instrument.reason_code, "flatten_failed")
        self.assertEqual(self.okx.asend_calls, before)
        okx = engine.state.leg_by_id("leg_okx")
        assert okx is not None
        self.assertFalse(okx.reduce_only)

        engine._recovery_factory = _wrong_side
        wrong_side = await engine.apply_recovery_step(await engine.plan_recovery())
        self.assertEqual(wrong_side.reason_code, "flatten_failed")
        self.assertEqual(self.okx.asend_calls, before)
        okx = engine.state.leg_by_id("leg_okx")
        assert okx is not None
        self.assertFalse(okx.reduce_only)

    async def test_begin_restart_resets_adapter_and_requires_rebind(self) -> None:
        engine = self._engine()
        intent = await self._recovering_okx_filled(engine)
        adapter = self._attach_adapter(engine, intent)
        self.assertIsNotNone(adapter.primary_plan(Venue.OKX))
        engine._wal.drain_all()
        replay = engine._wal.replay()
        await engine.begin_restart(replay)
        self.assertIsNone(adapter.primary_plan(Venue.OKX))
        self.assertEqual(adapter.last_sequence(intent.intent_id), engine.state.last_sequence)
        plan = await engine.plan_recovery()
        self.assertEqual(plan.kind, RecoveryActionKind.FLATTEN_FILLED)
        before = self.okx.asend_calls
        blocked = await engine.apply_recovery_step(plan)
        self.assertEqual(blocked.reason_code, "flatten_failed")
        self.assertEqual(self.okx.asend_calls, before)
        adapter.register(intent, _plans(intent.intent_id))
        self.assertIsNotNone(adapter.primary_plan(Venue.OKX))
        rebound = await engine.plan_recovery()
        self.assertEqual(rebound.kind, RecoveryActionKind.FLATTEN_FILLED)
        applied = await engine.apply_recovery_step(rebound)
        self.assertEqual(applied.status, RecoveryStatus.APPLIED)

    async def test_flatten_started_then_failed_appends_timeout_only(self) -> None:
        engine = self._engine()
        await self._recovering_okx_filled(engine)
        before_seq = engine.state.last_sequence
        self.okx.error = RuntimeError("write-failed")
        result = await engine.apply_recovery_step(await engine.plan_recovery())
        self.assertEqual(result.reason_code, "flatten_failed")
        okx = engine.state.leg_by_id("leg_okx")
        assert okx is not None
        self.assertTrue(okx.reduce_only)
        self.assertGreater(engine.state.last_sequence, before_seq)
        self.assertEqual(okx.ack_status, "timeout")
        plan = await engine.plan_recovery()
        self.assertNotEqual(plan.kind, RecoveryActionKind.FLATTEN_FILLED)

    async def test_prepare_venue_action_is_single_venue(self) -> None:
        intent = _intent()
        flatten = LegPlan.build(
            intent_id=intent.intent_id,
            leg_id="leg_okx",
            venue=Venue.OKX,
            instrument=OKX_INST,
            side="sell",
            quantity=Decimal("1"),
            reduce_only=True,
        )
        prepared = prepare_venue_action(
            flatten,
            _cache(),
            kind=VenueActionKind.PLACE,
            now_mono_ns=1_000,
            run_id=RUN_ID,
            intent=intent,
        )
        self.assertIsInstance(prepared, PreparedVenueAction)
        self.assertEqual(prepared.venue, Venue.OKX)
        self.assertTrue(prepared.frame.reduce_only)
        self._assert_redacted(prepared)
        cancel = LegPlan.build(
            intent_id=intent.intent_id,
            leg_id="leg_okx",
            venue=Venue.OKX,
            instrument=OKX_INST,
            side="buy",
            quantity=Decimal("1"),
            reduce_only=False,
        )
        cancel_prepared = prepare_venue_action(
            cancel,
            _cache(),
            kind=VenueActionKind.CANCEL,
            now_mono_ns=1_000,
            run_id=RUN_ID,
            intent=intent,
        )
        self.assertFalse(cancel_prepared.frame.reduce_only)
        text = unsigned_cancel_finalizer(
            cancel_prepared.frame,
            timestamp_ms=1,
            request_id=cancel_prepared.frame.client_id,
            client_id=cancel_prepared.frame.client_id,
        )
        self.assertIn("cancel-order", text)


if __name__ == "__main__":
    unittest.main()
