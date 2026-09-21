"""EV2-03 same-loop dual-venue transport kernel tests. No live I/O."""

from __future__ import annotations

import asyncio
import inspect
import json
import unittest
from decimal import Decimal
from typing import Any, Optional

from app.bot.execution.contracts import (
    SCHEMA_VERSION,
    IntentAction,
    LegPlan,
    SpreadDirection,
    TradeIntent,
    Venue,
    derive_client_id,
)
from app.bot.execution.recovery import VenueActionKind
from app.bot.execution.transport import (
    CachedInstrument,
    DispatchStatus,
    ExecutionTransport,
    InstrumentCache,
    TransportError,
    WriteOutcome,
    declared_owner_loop,
    prepare_dual_leg,
    prepare_venue_action,
    unsigned_frame_finalizer,
)

INTENT_ID = "95790b7c-8b21-4e82-bae2-1832ddc5bee1"
RUN_ID = "run_aa11bb22cc33dd44ee55ff6677889900"
SIGNAL_MONO_NS = 500
OKX_INST_ID_CODE = 193761


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
        signal_mono_ns=SIGNAL_MONO_NS,
        signal_wall_ns=1_750_000_000_000_000_000,
        expiry_mono_ns=2_000_000,
        signal_snapshot_ref="snap_redacted_001",
        canary_stage="shadow",
        risk_policy_revision="risk.v1",
    )
    payload.update(overrides)
    return TradeIntent(**payload)  # type: ignore[arg-type]


def _plans(intent_id: str = INTENT_ID) -> tuple[LegPlan, LegPlan]:
    bybit = LegPlan.build(
        intent_id=intent_id,
        leg_id="leg_bybit",
        venue=Venue.BYBIT,
        instrument="BTCUSDT",
        side="sell",
        quantity=Decimal("1"),
    )
    okx = LegPlan.build(
        intent_id=intent_id,
        leg_id="leg_okx",
        venue=Venue.OKX,
        instrument="BTC-USDT-SWAP",
        side="buy",
        quantity=Decimal("1"),
    )
    return bybit, okx


def _cache(
    *,
    captured: int = 100,
    fresh_until: int = 10_000,
    okx_code: object = OKX_INST_ID_CODE,
    include_bybit: bool = True,
    include_okx: bool = True,
) -> InstrumentCache:
    items = []
    if include_bybit:
        items.append(
            CachedInstrument(
                venue=Venue.BYBIT,
                instrument="BTCUSDT",
                captured_mono_ns=captured,
                fresh_until_mono_ns=fresh_until,
            )
        )
    if include_okx:
        items.append(
            CachedInstrument(
                venue=Venue.OKX,
                instrument="BTC-USDT-SWAP",
                captured_mono_ns=captured,
                fresh_until_mono_ns=fresh_until,
                inst_id_code=okx_code,  # type: ignore[arg-type]
            )
        )
    return InstrumentCache.from_snapshots(items)


def _prepare(*, now: int = 1_000, cache: Optional[InstrumentCache] = None) -> Any:
    intent = _intent()
    return prepare_dual_leg(intent, _plans(), cache or _cache(), now_mono_ns=now)


def _prepare_action(*, now: int = 1_000) -> Any:
    plan = LegPlan.build(
        intent_id=INTENT_ID,
        leg_id="leg_okx",
        venue=Venue.OKX,
        instrument="BTC-USDT-SWAP",
        side="sell",
        quantity=Decimal("1"),
        reduce_only=True,
    )
    return prepare_venue_action(
        plan,
        _cache(),
        kind=VenueActionKind.PLACE,
        now_mono_ns=now,
        run_id=RUN_ID,
    )


class ScriptedClock:
    def __init__(self, values: list[int]) -> None:
        self._values = list(values)
        self.calls = 0

    def __call__(self) -> int:
        if self.calls >= len(self._values):
            raise AssertionError("scripted monotonic clock exhausted")
        value = self._values[self.calls]
        self.calls += 1
        return value


class FakeSocket:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.owner_loop = loop
        self.sent: list[str] = []
        self.asend_calls = 0
        self.recv_calls = 0
        self.send_text_calls = 0
        self.connect_calls = 0
        self.close_calls = 0
        self._started: Optional[asyncio.Event] = None
        self._release: Optional[asyncio.Event] = None
        self._release_set = True
        self.error: Optional[BaseException] = None
        self.block = False

    @property
    def started(self) -> asyncio.Event:
        if self._started is None:
            self._started = asyncio.Event()
        return self._started

    @property
    def release(self) -> asyncio.Event:
        if self._release is None:
            self._release = asyncio.Event()
            if self._release_set:
                self._release.set()
        return self._release

    def connect(self) -> None:
        self.connect_calls += 1
        raise AssertionError("connect must not run in EV2-03")

    def close(self) -> None:
        self.close_calls += 1
        raise AssertionError("close must not run in EV2-03")

    def send_text(self, text: str) -> None:
        self.send_text_calls += 1
        raise AssertionError("send_text thread hop is forbidden")

    def recv_text(self, *, timeout_sec: Optional[float] = None) -> str:
        self.recv_calls += 1
        raise AssertionError("recv/ACK wait is forbidden")

    async def asend(self, text: str) -> None:
        self.asend_calls += 1
        self.started.set()
        if self.block:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        self.sent.append(text)


class FirstCancelIgnoredSocket(FakeSocket):
    """Stay blocked after the first asend CancelledError so drain can be interrupted."""

    async def asend(self, text: str) -> None:
        self.asend_calls += 1
        self.started.set()
        swallow_first = True
        while True:
            try:
                if self.block:
                    await self.release.wait()
                break
            except asyncio.CancelledError:
                if swallow_first:
                    swallow_first = False
                    continue
                raise
        if self.error is not None:
            raise self.error
        self.sent.append(text)


class OwnerDuck:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop


class DuckLoopOwnedSocket:
    """Mimics LoopOwnedSocket ownership without constructing a warm loop."""

    loop_owned = True

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._owner = OwnerDuck(loop)
        self.sent: list[str] = []
        self.asend_calls = 0
        self.recv_calls = 0

    async def asend(self, text: str) -> None:
        self.asend_calls += 1
        self.sent.append(text)

    def recv_text(self) -> str:
        self.recv_calls += 1
        raise AssertionError("recv forbidden")


def _transport(
    loop: asyncio.AbstractEventLoop,
    bybit: Any,
    okx: Any,
    *,
    monotonic_ns: Any = None,
    wall_ms: Any = None,
    finalize_frame: Any = unsigned_frame_finalizer,
) -> ExecutionTransport:
    return ExecutionTransport(
        loop,
        bybit_socket=bybit,
        okx_socket=okx,
        finalize_frame=finalize_frame,
        monotonic_ns=monotonic_ns or (lambda: 1_500),
        wall_ms=wall_ms or (lambda: 1_700_000_000_000),
    )


class PrepareValidationTests(unittest.TestCase):
    def test_duplicate_venue_fails_before_write(self) -> None:
        intent = _intent()
        bybit, _okx = _plans()
        other = LegPlan.build(
            intent_id=INTENT_ID,
            leg_id="leg_bybit_2",
            venue=Venue.BYBIT,
            instrument="ETHUSDT",
            side="buy",
            quantity=Decimal("1"),
        )
        with self.assertRaises(TransportError) as ctx:
            prepare_dual_leg(intent, (bybit, other), _cache(), now_mono_ns=1_000)
        self.assertEqual(ctx.exception.reason_code, "duplicate_venue")

    def test_missing_okx_fails_before_write(self) -> None:
        intent = _intent()
        bybit, _okx = _plans()
        with self.assertRaises(TransportError) as ctx:
            prepare_dual_leg(intent, (bybit,), _cache(), now_mono_ns=1_000)
        self.assertEqual(ctx.exception.reason_code, "invalid_leg_set")

    def test_mismatched_intent_ids_fail_before_write(self) -> None:
        intent = _intent()
        foreign = LegPlan.build(
            intent_id="aaaaaaaa-bbbb-4ccc-dddd-eeeeeeeeeeee",
            leg_id="leg_okx",
            venue=Venue.OKX,
            instrument="BTC-USDT-SWAP",
            side="buy",
            quantity=Decimal("1"),
        )
        bybit, _okx = _plans()
        with self.assertRaises(TransportError) as ctx:
            prepare_dual_leg(intent, (bybit, foreign), _cache(), now_mono_ns=1_000)
        self.assertEqual(ctx.exception.reason_code, "intent_id_mismatch")

    def test_missing_metadata_fails_before_write(self) -> None:
        with self.assertRaises(TransportError) as ctx:
            prepare_dual_leg(
                _intent(),
                _plans(),
                _cache(include_okx=False),
                now_mono_ns=1_000,
            )
        self.assertEqual(ctx.exception.reason_code, "missing_metadata")

    def test_stale_metadata_fails_before_write(self) -> None:
        with self.assertRaises(TransportError) as ctx:
            prepare_dual_leg(
                _intent(),
                _plans(),
                _cache(captured=100, fresh_until=200),
                now_mono_ns=500,
            )
        self.assertEqual(ctx.exception.reason_code, "stale_metadata")

    def test_invalid_okx_inst_id_code_fails_before_write(self) -> None:
        with self.assertRaises(TransportError) as ctx:
            _cache(okx_code=0)
        self.assertEqual(ctx.exception.reason_code, "invalid_inst_id_code")
        with self.assertRaises(TransportError) as ctx2:
            _cache(okx_code=-3)
        self.assertEqual(ctx2.exception.reason_code, "invalid_inst_id_code")
        with self.assertRaises(TransportError) as ctx3:
            CachedInstrument(
                venue=Venue.OKX,
                instrument="BTC-USDT-SWAP",
                captured_mono_ns=1,
                fresh_until_mono_ns=10,
                inst_id_code=None,
            )
        self.assertEqual(ctx3.exception.reason_code, "invalid_inst_id_code")

    def test_bool_inst_id_code_is_invalid(self) -> None:
        with self.assertRaises(TransportError) as ctx:
            _cache(okx_code=True)
        self.assertEqual(ctx.exception.reason_code, "invalid_inst_id_code")


class ImportConstructTests(unittest.TestCase):
    def test_importing_and_constructing_does_not_start_sockets(self) -> None:
        import app.bot.execution as execution_pkg
        import app.bot.execution.transport as transport_mod

        self.assertIs(execution_pkg.ExecutionTransport, transport_mod.ExecutionTransport)
        loop = asyncio.new_event_loop()
        try:
            bybit = FakeSocket(loop)
            okx = FakeSocket(loop)
            transport = ExecutionTransport(
                loop,
                bybit_socket=bybit,
                okx_socket=okx,
                finalize_frame=unsigned_frame_finalizer,
            )
            self.assertIs(transport.loop, loop)
            self.assertEqual(bybit.connect_calls, 0)
            self.assertEqual(okx.connect_calls, 0)
            self.assertEqual(bybit.asend_calls, 0)
            self.assertEqual(okx.asend_calls, 0)
        finally:
            loop.close()

    def test_unknown_ownership_fails_at_construction(self) -> None:
        loop = asyncio.new_event_loop()

        class NoOwner:
            async def asend(self, text: str) -> None:
                return None

        try:
            with self.assertRaises(TransportError) as ctx:
                ExecutionTransport(
                    loop,
                    bybit_socket=NoOwner(),  # type: ignore[arg-type]
                    okx_socket=FakeSocket(loop),
                    finalize_frame=unsigned_frame_finalizer,
                )
            self.assertEqual(ctx.exception.reason_code, "unknown_loop_ownership")
        finally:
            loop.close()

    def test_mixed_ownership_fails_at_construction(self) -> None:
        loop_a = asyncio.new_event_loop()
        loop_b = asyncio.new_event_loop()
        try:
            with self.assertRaises(TransportError) as ctx:
                ExecutionTransport(
                    loop_a,
                    bybit_socket=FakeSocket(loop_a),
                    okx_socket=FakeSocket(loop_b),
                    finalize_frame=unsigned_frame_finalizer,
                )
            self.assertEqual(ctx.exception.reason_code, "mixed_loop_ownership")
        finally:
            loop_a.close()
            loop_b.close()

    def test_loop_owned_socket_asend_boundary_exists(self) -> None:
        from app.bot.private.ws_warm_loop import LoopOwnedSocket

        params = list(inspect.signature(LoopOwnedSocket.asend).parameters)
        self.assertEqual(params, ["self", "text"])
        self.assertTrue(inspect.iscoroutinefunction(LoopOwnedSocket.asend))


class DispatchKernelTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_guard_rejects_before_either_write_is_scheduled(self) -> None:
        loop = asyncio.get_running_loop()
        bybit = FakeSocket(loop)
        okx = FakeSocket(loop)
        transport = _transport(loop, bybit, okx)
        calls = 0

        def guard() -> bool:
            nonlocal calls
            calls += 1
            return False

        result = await transport.dispatch(_prepare(), pre_send_guard=guard)
        self.assertEqual(result.status, DispatchStatus.REJECTED)
        self.assertEqual(result.reason_code, "readiness_changed")
        self.assertEqual(result.bybit.outcome, WriteOutcome.NOT_ATTEMPTED)
        self.assertEqual(result.okx.outcome, WriteOutcome.NOT_ATTEMPTED)
        self.assertEqual(calls, 1)
        self.assertEqual(bybit.asend_calls, 0)
        self.assertEqual(okx.asend_calls, 0)

    async def test_final_guard_exception_fails_closed_before_write(self) -> None:
        loop = asyncio.get_running_loop()
        bybit = FakeSocket(loop)
        okx = FakeSocket(loop)
        transport = _transport(loop, bybit, okx)

        def guard() -> bool:
            raise RuntimeError("readiness source failed")

        result = await transport.dispatch(_prepare(), pre_send_guard=guard)
        self.assertEqual(result.status, DispatchStatus.REJECTED)
        self.assertEqual(result.reason_code, "readiness_changed")
        self.assertEqual(bybit.asend_calls, 0)
        self.assertEqual(okx.asend_calls, 0)

    async def test_recovery_action_final_guard_blocks_target_write(self) -> None:
        loop = asyncio.get_running_loop()
        bybit = FakeSocket(loop)
        okx = FakeSocket(loop)
        transport = _transport(loop, bybit, okx)

        result = await transport.dispatch_action(
            _prepare_action(), pre_send_guard=lambda: False
        )
        self.assertEqual(result.evidence.outcome, WriteOutcome.NOT_ATTEMPTED)
        self.assertEqual(result.evidence.reason_code, "readiness_changed")
        self.assertEqual(bybit.asend_calls, 0)
        self.assertEqual(okx.asend_calls, 0)

    async def test_recovery_action_guard_exception_fails_closed(self) -> None:
        loop = asyncio.get_running_loop()
        bybit = FakeSocket(loop)
        okx = FakeSocket(loop)
        transport = _transport(loop, bybit, okx)

        def guard() -> bool:
            raise RuntimeError("readiness source failed")

        result = await transport.dispatch_action(
            _prepare_action(), pre_send_guard=guard
        )
        self.assertEqual(result.evidence.outcome, WriteOutcome.NOT_ATTEMPTED)
        self.assertEqual(result.evidence.reason_code, "readiness_changed")
        self.assertEqual(okx.asend_calls, 0)

    async def test_both_sockets_owned_by_active_loop(self) -> None:
        loop = asyncio.get_running_loop()
        bybit = FakeSocket(loop)
        okx = FakeSocket(loop)
        transport = _transport(loop, bybit, okx)
        prepared = _prepare()
        result = await transport.dispatch(prepared)
        self.assertEqual(result.status, DispatchStatus.BOTH_COMPLETED)
        self.assertEqual(bybit.asend_calls, 1)
        self.assertEqual(okx.asend_calls, 1)
        self.assertIs(declared_owner_loop(bybit), loop)
        self.assertIs(declared_owner_loop(okx), loop)
        self.assertIs(transport.loop, loop)

    async def test_duck_typed_warm_socket_owner_loop(self) -> None:
        loop = asyncio.get_running_loop()
        bybit = DuckLoopOwnedSocket(loop)
        okx = DuckLoopOwnedSocket(loop)
        transport = _transport(loop, bybit, okx)
        result = await transport.dispatch(_prepare())
        self.assertEqual(result.status, DispatchStatus.BOTH_COMPLETED)
        self.assertEqual(bybit.asend_calls, 1)
        self.assertEqual(okx.asend_calls, 1)
        self.assertEqual(bybit.recv_calls, 0)
        self.assertEqual(okx.recv_calls, 0)

    async def test_both_writes_start_before_either_is_released(self) -> None:
        loop = asyncio.get_running_loop()
        bybit = FakeSocket(loop)
        okx = FakeSocket(loop)
        bybit.block = True
        okx.block = True
        bybit.release.clear()
        okx.release.clear()
        transport = _transport(loop, bybit, okx)
        task = asyncio.create_task(transport.dispatch(_prepare()))
        await asyncio.wait_for(bybit.started.wait(), timeout=1)
        await asyncio.wait_for(okx.started.wait(), timeout=1)
        self.assertFalse(task.done())
        self.assertEqual(bybit.asend_calls, 1)
        self.assertEqual(okx.asend_calls, 1)
        self.assertEqual(bybit.sent, [])
        self.assertEqual(okx.sent, [])
        bybit.release.set()
        okx.release.set()
        result = await task
        self.assertEqual(result.status, DispatchStatus.BOTH_COMPLETED)
        self.assertEqual(len(bybit.sent), 1)
        self.assertEqual(len(okx.sent), 1)

    async def test_blocked_first_venue_does_not_serialize_second(self) -> None:
        loop = asyncio.get_running_loop()
        bybit = FakeSocket(loop)
        okx = FakeSocket(loop)
        bybit.block = True
        okx.block = True
        bybit.release.clear()
        okx.release.clear()
        transport = _transport(loop, bybit, okx)
        task = asyncio.create_task(transport.dispatch(_prepare()))
        await asyncio.wait_for(asyncio.gather(bybit.started.wait(), okx.started.wait()), 1)
        okx.release.set()
        for _ in range(50):
            await asyncio.sleep(0)
            if okx.sent:
                break
        self.assertEqual(okx.sent.__len__(), 1)
        self.assertEqual(bybit.sent, [])
        self.assertFalse(task.done())
        bybit.release.set()
        result = await task
        self.assertEqual(result.status, DispatchStatus.BOTH_COMPLETED)
        self.assertEqual(result.okx.outcome, WriteOutcome.WRITE_COMPLETED)
        self.assertEqual(result.bybit.outcome, WriteOutcome.WRITE_COMPLETED)

    async def test_one_write_raises_while_peer_is_attempted(self) -> None:
        loop = asyncio.get_running_loop()
        bybit = FakeSocket(loop)
        okx = FakeSocket(loop)
        bybit.error = OSError("bybit send failed")
        okx.block = True
        okx.release.clear()
        transport = _transport(loop, bybit, okx)
        task = asyncio.create_task(transport.dispatch(_prepare()))
        await asyncio.wait_for(okx.started.wait(), 1)
        self.assertEqual(bybit.asend_calls, 1)
        self.assertEqual(okx.asend_calls, 1)
        okx.release.set()
        result = await task
        self.assertEqual(result.status, DispatchStatus.PARTIAL)
        self.assertEqual(result.bybit.outcome, WriteOutcome.WRITE_FAILED)
        self.assertEqual(result.okx.outcome, WriteOutcome.WRITE_COMPLETED)
        self.assertEqual(result.bybit.reason_code, "write_failed")
        self.assertEqual(bybit.asend_calls, 1)
        self.assertEqual(okx.asend_calls, 1)

    async def test_both_writes_fail_without_retry(self) -> None:
        loop = asyncio.get_running_loop()
        bybit = FakeSocket(loop)
        okx = FakeSocket(loop)
        bybit.error = RuntimeError("bybit")
        okx.error = RuntimeError("okx")
        transport = _transport(loop, bybit, okx)
        result = await transport.dispatch(_prepare())
        self.assertEqual(result.status, DispatchStatus.BOTH_FAILED)
        self.assertEqual(result.reason_code, "write_failed")
        self.assertEqual(bybit.asend_calls, 1)
        self.assertEqual(okx.asend_calls, 1)
        self.assertEqual(result.bybit.outcome, WriteOutcome.WRITE_FAILED)
        self.assertEqual(result.okx.outcome, WriteOutcome.WRITE_FAILED)

    async def test_cancellation_after_scheduling_leaves_no_child_tasks(self) -> None:
        loop = asyncio.get_running_loop()
        bybit = FakeSocket(loop)
        okx = FakeSocket(loop)
        bybit.block = True
        okx.block = True
        bybit.release.clear()
        okx.release.clear()
        transport = _transport(loop, bybit, okx)
        task = asyncio.create_task(transport.dispatch(_prepare()))
        await asyncio.wait_for(asyncio.gather(bybit.started.wait(), okx.started.wait()), 1)
        task.cancel()
        result = await task
        self.assertEqual(result.status, DispatchStatus.CANCELLED)
        self.assertEqual(result.reason_code, "cancelled")
        self.assertEqual(result.bybit.outcome, WriteOutcome.CANCELLED)
        self.assertEqual(result.okx.outcome, WriteOutcome.CANCELLED)
        pending = [
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        self.assertEqual(pending, [])
        self.assertEqual(bybit.asend_calls, 1)
        self.assertEqual(okx.asend_calls, 1)

    async def test_double_cancel_while_both_fakes_blocked_leaves_no_pending_tasks(self) -> None:
        loop = asyncio.get_running_loop()
        bybit = FirstCancelIgnoredSocket(loop)
        okx = FirstCancelIgnoredSocket(loop)
        bybit.block = True
        okx.block = True
        bybit.release.clear()
        okx.release.clear()
        transport = _transport(loop, bybit, okx)
        task = asyncio.create_task(transport.dispatch(_prepare()))
        await asyncio.wait_for(asyncio.gather(bybit.started.wait(), okx.started.wait()), 1)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        task.cancel()
        try:
            result = await asyncio.wait_for(task, timeout=1)
        except asyncio.CancelledError:
            self.fail("CancelledError escaped dispatch after repeated cancel")
        self.assertEqual(result.status, DispatchStatus.CANCELLED)
        self.assertEqual(result.reason_code, "cancelled")
        self.assertEqual(result.bybit.outcome, WriteOutcome.CANCELLED)
        self.assertEqual(result.okx.outcome, WriteOutcome.CANCELLED)
        pending = [
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        self.assertEqual(pending, [])
        self.assertEqual(bybit.asend_calls, 1)
        self.assertEqual(okx.asend_calls, 1)

    async def test_clock_regression_after_both_asend_returns_completed_evidence(self) -> None:
        loop = asyncio.get_running_loop()
        bybit = FakeSocket(loop)
        okx = FakeSocket(loop)
        bybit.block = True
        okx.block = True
        bybit.release.clear()
        okx.release.clear()
        clock = ScriptedClock([1_000, 1_100, 1_200, 50, 60])
        transport = _transport(loop, bybit, okx, monotonic_ns=clock)
        task = asyncio.create_task(transport.dispatch(_prepare()))
        await asyncio.wait_for(asyncio.gather(bybit.started.wait(), okx.started.wait()), 1)
        bybit.release.set()
        okx.release.set()
        result = await task
        self.assertEqual(result.status, DispatchStatus.BOTH_COMPLETED)
        self.assertEqual(result.reason_code, "clock_regression")
        self.assertEqual(result.bybit.outcome, WriteOutcome.WRITE_COMPLETED)
        self.assertEqual(result.okx.outcome, WriteOutcome.WRITE_COMPLETED)
        self.assertEqual(result.bybit.reason_code, "clock_regression")
        self.assertEqual(result.okx.reason_code, "clock_regression")
        self.assertIsNone(result.signal_to_first_write_ns)
        self.assertIsNone(result.dual_leg_write_ns)
        self.assertEqual(bybit.asend_calls, 1)
        self.assertEqual(okx.asend_calls, 1)
        public = result.to_public_dict()
        self.assertEqual(public["reason_code"], "clock_regression")
        self.assertEqual(public["bybit"]["outcome"], "write_completed")
        self.assertEqual(public["okx"]["outcome"], "write_completed")

    async def test_clock_regression_versus_signal_after_writes_returns_result(self) -> None:
        loop = asyncio.get_running_loop()
        bybit = FakeSocket(loop)
        okx = FakeSocket(loop)
        prepared = prepare_dual_leg(
            _intent(signal_mono_ns=10_000_000, expiry_mono_ns=20_000_000),
            _plans(),
            _cache(captured=100, fresh_until=20_000_000),
            now_mono_ns=1_000,
        )
        clock = ScriptedClock([1_500, 1_600, 1_700, 1_800, 1_900])
        transport = _transport(loop, bybit, okx, monotonic_ns=clock)
        result = await transport.dispatch(prepared)
        self.assertEqual(result.status, DispatchStatus.BOTH_COMPLETED)
        self.assertEqual(result.reason_code, "clock_regression")
        self.assertEqual(result.bybit.outcome, WriteOutcome.WRITE_COMPLETED)
        self.assertEqual(result.okx.outcome, WriteOutcome.WRITE_COMPLETED)
        self.assertIsNone(result.signal_to_first_write_ns)
        self.assertIsNone(result.dual_leg_write_ns)
        self.assertEqual(bybit.asend_calls, 1)
        self.assertEqual(okx.asend_calls, 1)

    async def test_foreign_loop_fails_before_write(self) -> None:
        running = asyncio.get_running_loop()
        other = asyncio.new_event_loop()
        try:
            bybit = FakeSocket(other)
            okx = FakeSocket(other)
            transport = _transport(other, bybit, okx)
            result = await transport.dispatch(_prepare())
            self.assertEqual(result.status, DispatchStatus.REJECTED)
            self.assertEqual(result.reason_code, "foreign_loop")
            self.assertEqual(result.bybit.outcome, WriteOutcome.NOT_ATTEMPTED)
            self.assertEqual(result.okx.outcome, WriteOutcome.NOT_ATTEMPTED)
            self.assertEqual(bybit.asend_calls, 0)
            self.assertEqual(okx.asend_calls, 0)
            self.assertIsNot(running, other)
        finally:
            other.close()

    async def test_stale_cache_at_dispatch_fails_before_write(self) -> None:
        loop = asyncio.get_running_loop()
        bybit = FakeSocket(loop)
        okx = FakeSocket(loop)
        prepared = prepare_dual_leg(
            _intent(),
            _plans(),
            _cache(captured=100, fresh_until=200),
            now_mono_ns=150,
        )
        clock = ScriptedClock([250])
        transport = _transport(loop, bybit, okx, monotonic_ns=clock)
        result = await transport.dispatch(prepared)
        self.assertEqual(result.status, DispatchStatus.REJECTED)
        self.assertEqual(result.reason_code, "stale_metadata")
        self.assertEqual(bybit.asend_calls, 0)
        self.assertEqual(okx.asend_calls, 0)

    async def test_deterministic_client_and_request_ids(self) -> None:
        loop = asyncio.get_running_loop()
        bybit = FakeSocket(loop)
        okx = FakeSocket(loop)
        transport = _transport(loop, bybit, okx)
        result = await transport.dispatch(_prepare())
        self.assertEqual(result.status, DispatchStatus.BOTH_COMPLETED)
        bybit_id = derive_client_id(INTENT_ID, Venue.BYBIT)
        okx_id = derive_client_id(INTENT_ID, Venue.OKX)
        bybit_frame = json.loads(bybit.sent[0])
        okx_frame = json.loads(okx.sent[0])
        self.assertEqual(bybit_frame["reqId"], bybit_id)
        self.assertEqual(bybit_frame["args"][0]["orderLinkId"], bybit_id)
        self.assertLessEqual(len(bybit_frame["reqId"]), 36)
        self.assertEqual(okx_frame["id"], okx_id)
        self.assertEqual(okx_frame["args"][0]["clOrdId"], okx_id)
        self.assertTrue(okx_frame["id"].isalnum())
        self.assertLessEqual(len(okx_frame["id"]), 32)
        self.assertEqual(okx_frame["args"][0]["instIdCode"], OKX_INST_ID_CODE)
        self.assertEqual(result.bybit.client_id, bybit_id)
        self.assertEqual(result.okx.client_id, okx_id)

    async def test_injected_clock_chronometry_is_exact(self) -> None:
        loop = asyncio.get_running_loop()
        bybit = FakeSocket(loop)
        okx = FakeSocket(loop)
        bybit.block = True
        okx.block = True
        bybit.release.clear()
        okx.release.clear()
        clock = ScriptedClock([1_000, 1_100, 1_200, 1_300, 1_400])
        transport = _transport(loop, bybit, okx, monotonic_ns=clock)
        task = asyncio.create_task(transport.dispatch(_prepare()))
        await asyncio.wait_for(asyncio.gather(bybit.started.wait(), okx.started.wait()), 1)
        bybit.release.set()
        okx.release.set()
        result = await task
        self.assertEqual(result.status, DispatchStatus.BOTH_COMPLETED)
        self.assertEqual(result.dispatch_entry_mono_ns, 1_000)
        self.assertEqual(result.bybit.asend_start_mono_ns, 1_100)
        self.assertEqual(result.okx.asend_start_mono_ns, 1_200)
        self.assertEqual(result.bybit.asend_done_mono_ns, 1_300)
        self.assertEqual(result.okx.asend_done_mono_ns, 1_400)
        self.assertEqual(result.bybit.write_latency_ns, 200)
        self.assertEqual(result.okx.write_latency_ns, 200)
        self.assertEqual(result.signal_to_first_write_ns, 1_100 - SIGNAL_MONO_NS)
        self.assertEqual(result.dual_leg_write_ns, 200)
        self.assertGreaterEqual(result.signal_to_first_write_ns, 0)
        self.assertGreaterEqual(result.dual_leg_write_ns, 0)
        self.assertEqual(clock.calls, 5)

    async def test_no_receive_or_ack_method_is_invoked(self) -> None:
        loop = asyncio.get_running_loop()
        bybit = FakeSocket(loop)
        okx = FakeSocket(loop)
        transport = _transport(loop, bybit, okx)
        await transport.dispatch(_prepare())
        self.assertEqual(bybit.recv_calls, 0)
        self.assertEqual(okx.recv_calls, 0)
        self.assertEqual(bybit.send_text_calls, 0)
        self.assertEqual(okx.send_text_calls, 0)
        self.assertEqual(bybit.connect_calls, 0)
        self.assertEqual(okx.close_calls, 0)

    async def test_public_result_contains_no_raw_frame_or_secret(self) -> None:
        loop = asyncio.get_running_loop()
        bybit = FakeSocket(loop)
        okx = FakeSocket(loop)
        secret = "super-secret-api-key-value"
        signature = "deadbeefsignature"
        raw_frame = "RAW_FRAME_MUST_NOT_LEAK"

        def leaking_finalizer(static: Any, *, timestamp_ms: int, request_id: str, client_id: str) -> str:
            return json.dumps(
                {
                    "api_key": secret,
                    "signature": signature,
                    "frame": raw_frame,
                    "client_id": client_id,
                    "id": request_id,
                    "ts": timestamp_ms,
                    "venue": static.venue.value,
                },
                separators=(",", ":"),
            )

        transport = _transport(loop, bybit, okx, finalize_frame=leaking_finalizer)
        result = await transport.dispatch(_prepare())
        public = result.to_public_dict()
        dumped = json.dumps(public, sort_keys=True)
        self.assertNotIn(secret, dumped)
        self.assertNotIn(signature, dumped)
        self.assertNotIn(raw_frame, dumped)
        self.assertNotIn("api_key", dumped)
        self.assertNotIn("signature", dumped)
        self.assertNotIn('"frame"', dumped)
        self.assertNotIn(secret, repr(result))
        self.assertNotIn(raw_frame, repr(result))
        self.assertIn("intent_id", public)
        self.assertEqual(public["bybit"]["payload_bytes"], len(bybit.sent[0].encode("utf-8")))
        self.assertEqual(public["okx"]["payload_bytes"], len(okx.sent[0].encode("utf-8")))
        self.assertEqual(result.bybit.outcome.value, "write_completed")
        self.assertNotEqual(result.bybit.outcome.value, "accepted")
        self.assertNotEqual(result.bybit.outcome.value, "filled")
        self.assertNotEqual(result.bybit.outcome.value, "open")
