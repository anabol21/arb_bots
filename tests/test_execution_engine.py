"""EV2-06 execution engine and risk-gate tests. No live I/O."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

from app.bot.execution.adapters import SCHEMA_VERSION as ADAPTER_SCHEMA, AdapterBatch
from app.bot.execution.contracts import (
    SCHEMA_VERSION,
    ExecutionEvent,
    ExecutionEventType,
    IntentAction,
    LegPlan,
    SpreadDirection,
    SpreadStatus,
    TradeIntent,
    Venue,
)
from app.bot.execution.engine import (
    SCHEMA_VERSION as ENGINE_SCHEMA,
    DualReadinessFence,
    EngineError,
    ExecutionEngine,
    IngestResult,
    ReadinessSnapshot,
    RiskPolicy,
    SubmitResult,
    SubmitStatus,
)
from app.bot.execution.exporters import InMemoryExporter
from app.bot.execution.live_private_bridge import (
    LivePrivateBridgeError,
    LivePrivateEvidenceBridge,
)
from app.bot.execution.ownership import FileOwnershipFence, OwnershipError
from app.bot.execution.recovery import RecoveryActionKind, RecoveryStatus
from app.bot.execution.state_machine import apply_events, initial_spread_state
from app.bot.execution.transport import (
    SCHEMA_VERSION as TRANSPORT_SCHEMA,
    CachedInstrument,
    DispatchResult,
    DispatchStatus,
    ExecutionTransport,
    InstrumentCache,
    VenueWriteEvidence,
    WriteOutcome,
    unsigned_frame_finalizer,
)
from app.bot.execution.wal import (
    CRASH_AFTER_FLUSH_BEFORE_FSYNC,
    CRASH_AFTER_FSYNC_BEFORE_ACK,
    CRASH_BEFORE_WRITE,
    CRASH_TORN_WRITE,
    SUBMIT_WORST_CASE_EVENTS,
    ExecutionWal,
    WalError,
    admission_capacity_ok,
)

RUN_ID = "run_aa11bb22cc33dd44ee55ff6677889900"
OTHER_RUN = "run_other_mismatch_aa11bb22cc33dd44"
INTENT_A = "95790b7c-8b21-4e82-bae2-1832ddc5bee1"
INTENT_B = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
CLOSE_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
INTENT_C = "c1c1c1c1-d2d2-4e3e-8f4f-a5a5a5a5a5a5"
OKX_INST_ID_CODE = 193761
FORBIDDEN = ("api_key", "api_secret", "passphrase", "signature", "order_id", "RAW-ORDER")


class MonoClock:
    def __init__(self, start: int = 10_000) -> None:
        self.n = start
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        self.n += 1
        return self.n


class FakeSocket:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.owner_loop = loop
        self.sent: list[str] = []
        self.asend_calls = 0
        self.recv_calls = 0
        self.error: Optional[BaseException] = None
        self.block = False
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
        self.asend_calls += 1
        self.started.set()
        if self.block:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        self.sent.append(text)

    async def recv(self) -> str:
        self.recv_calls += 1
        return "{}"


def _intent(
    *,
    intent_id: str = INTENT_A,
    coin: str = "BTC",
    action: IntentAction = IntentAction.OPEN,
    notional: Decimal = Decimal("20"),
    signal_mono: int = 500,
    expiry_mono: int = 1_000_000,
) -> TradeIntent:
    return TradeIntent(
        schema_version=SCHEMA_VERSION,
        intent_id=intent_id,
        run_id=RUN_ID,
        policy_version="policy.v1",
        action=action,
        spread_direction=SpreadDirection.LONG,
        coin=coin,
        notional_usdt=notional,
        signal_mono_ns=signal_mono,
        signal_wall_ns=1_750_000_000_000_000_000,
        expiry_mono_ns=expiry_mono,
        signal_snapshot_ref="snap_redacted_001",
        canary_stage="shadow",
        risk_policy_revision="risk.v1",
    )


def _plans(
    intent_id: str,
    *,
    reduce_only: bool = False,
    coin: str = "BTC",
) -> tuple[LegPlan, LegPlan]:
    bybit_inst = f"{coin}USDT"
    okx_inst = f"{coin}-USDT-SWAP"
    bybit_side = "buy" if reduce_only else "sell"
    okx_side = "sell" if reduce_only else "buy"
    return (
        LegPlan.build(
            intent_id=intent_id,
            leg_id="leg_bybit",
            venue=Venue.BYBIT,
            instrument=bybit_inst,
            side=bybit_side,
            quantity=Decimal("1"),
            reduce_only=reduce_only,
        ),
        LegPlan.build(
            intent_id=intent_id,
            leg_id="leg_okx",
            venue=Venue.OKX,
            instrument=okx_inst,
            side=okx_side,
            quantity=Decimal("1"),
            reduce_only=reduce_only,
        ),
    )


def _resolver(intent: TradeIntent) -> tuple[LegPlan, LegPlan]:
    return _plans(intent.intent_id, reduce_only=intent.action is IntentAction.CLOSE, coin=intent.coin)


def _cache(*coins: str) -> InstrumentCache:
    if not coins:
        coins = ("BTC",)
    items = []
    for coin in coins:
        items.append(
            CachedInstrument(
                venue=Venue.BYBIT,
                instrument=f"{coin}USDT",
                captured_mono_ns=100,
                fresh_until_mono_ns=10_000_000,
            )
        )
        items.append(
            CachedInstrument(
                venue=Venue.OKX,
                instrument=f"{coin}-USDT-SWAP",
                captured_mono_ns=100,
                fresh_until_mono_ns=10_000_000,
                inst_id_code=OKX_INST_ID_CODE,
            )
        )
    return InstrumentCache.from_snapshots(items)


def _policy(*coins: str) -> RiskPolicy:
    return RiskPolicy(
        allowed_coins=frozenset(coins or ("BTC", "ETH", "KAITO")),
        max_notional_usdt=Decimal("20"),
        lot_tolerance=Decimal("0"),
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


def _dispatch_result(
    *,
    intent_id: str = INTENT_A,
    run_id: str = RUN_ID,
    status: DispatchStatus = DispatchStatus.BOTH_COMPLETED,
) -> DispatchResult:
    return DispatchResult(
        schema_version=TRANSPORT_SCHEMA,
        status=status,
        intent_id=intent_id,
        run_id=run_id,
        dispatch_entry_mono_ns=10,
        signal_mono_ns=500,
        signal_to_first_write_ns=1,
        dual_leg_write_ns=1,
        bybit=VenueWriteEvidence(
            venue=Venue.BYBIT,
            outcome=WriteOutcome.WRITE_COMPLETED,
            leg_id="leg_bybit",
            client_id="cid_bybit",
            payload_bytes=8,
            asend_start_mono_ns=10,
            asend_done_mono_ns=11,
            write_latency_ns=1,
            reason_code=None,
        ),
        okx=VenueWriteEvidence(
            venue=Venue.OKX,
            outcome=WriteOutcome.WRITE_COMPLETED,
            leg_id="leg_okx",
            client_id="cid_okx",
            payload_bytes=8,
            asend_start_mono_ns=10,
            asend_done_mono_ns=11,
            write_latency_ns=1,
            reason_code=None,
        ),
        reason_code=None,
    )


def _wal_cursor(wal: ExecutionWal) -> tuple[int, int, str, bool]:
    health = wal.health()
    return (
        health.queue_depth,
        health.next_wal_seq,
        wal._enqueue_prev_hash,
        health.hard_full,
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


def _open_state() -> Any:
    events = [
        _event(
            ExecutionEventType.INTENT_ACCEPTED,
            intent_id=INTENT_A,
            sequence=1,
            monotonic_ns=1001,
            payload={
                "action": "open",
                "coin": "BTC",
                "spread_direction": "long",
                "lot_tolerance": "0",
            },
        ),
        _event(
            ExecutionEventType.REQUEST_SENT,
            intent_id=INTENT_A,
            sequence=2,
            monotonic_ns=1002,
            venue=Venue.BYBIT,
            leg_id="leg_bybit",
            payload={
                "quantity": "1",
                "reduce_only": False,
                "instrument": "BTCUSDT",
                "side": "sell",
            },
        ),
        _event(
            ExecutionEventType.REQUEST_SENT,
            intent_id=INTENT_A,
            sequence=3,
            monotonic_ns=1003,
            venue=Venue.OKX,
            leg_id="leg_okx",
            payload={
                "quantity": "1",
                "reduce_only": False,
                "instrument": "BTC-USDT-SWAP",
                "side": "buy",
            },
        ),
        _event(
            ExecutionEventType.FILL,
            intent_id=INTENT_A,
            sequence=4,
            monotonic_ns=1004,
            venue=Venue.BYBIT,
            leg_id="leg_bybit",
            payload={"quantity": "1"},
        ),
        _event(
            ExecutionEventType.FILL,
            intent_id=INTENT_A,
            sequence=5,
            monotonic_ns=1005,
            venue=Venue.OKX,
            leg_id="leg_okx",
            payload={"quantity": "1"},
        ),
    ]
    return apply_events(initial_spread_state(run_id=RUN_ID), events)


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
    max_queue: int = 16,
    reserved_tail: int = 5,
    max_durable_lag: int = 16,
    mark: bool = True,
) -> ExecutionWal:
    wal = ExecutionWal(
        path,
        run_id=RUN_ID,
        max_queue=max_queue,
        reserved_tail=reserved_tail,
        max_durable_lag=max_durable_lag,
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


def _fill_wal(wal: ExecutionWal, count: int, *, start_seq: int = 3) -> None:
    for i in range(count):
        event = _event(
            ExecutionEventType.PAUSE,
            intent_id=INTENT_A,
            sequence=start_seq + i,
            monotonic_ns=100 + i,
            payload={"pause": True},
        )
        ack = wal.enqueue(event)
        assert ack.accepted, ack.reason_code


class EngineHarness(unittest.IsolatedAsyncioTestCase):
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

    async def asyncTearDown(self) -> None:
        if self.fence.owned:
            self.fence.release()
        self.tmp.cleanup()

    def _reclaim_fence(self) -> None:
        if self.fence.owned:
            self.fence.release()
        self.fence.acquire()

    def _transport(self, *, finalize: Any = unsigned_frame_finalizer) -> ExecutionTransport:
        return ExecutionTransport(
            self.loop,
            bybit_socket=self.bybit,
            okx_socket=self.okx,
            finalize_frame=finalize,
            monotonic_ns=self.clock,
        )

    def _engine(
        self,
        *,
        wal: Optional[ExecutionWal] = None,
        state: Any = None,
        readiness: Optional[ReadinessSnapshot] = None,
        policy: Optional[RiskPolicy] = None,
        cache: Optional[InstrumentCache] = None,
        resolver: Any = _resolver,
        mark: bool = True,
        max_queue: int = 16,
        reserved_tail: int = 5,
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
            transport=self._transport(),
            plan_resolver=resolver,
            instrument_cache=cache or _cache("BTC", "ETH", "KAITO"),
            risk_policy=policy or _policy("BTC", "ETH", "KAITO"),
            readiness=readiness or _ready(),
            ownership=self.fence,
            monotonic_ns=self.clock,
            state=state,
            durable_prewrite=durable_prewrite,
            prewrite_anchor=anchor,
        )


class AdmissionApiTests(unittest.TestCase):
    def test_dual_readiness_lease_invalidates_on_disconnect_or_generation_change(self) -> None:
        fence = DualReadinessFence(_ready())
        lease, reason = fence.acquire(require_controls=True)
        assert lease is not None
        self.assertIsNone(reason)
        self.assertTrue(fence.validate(lease))
        self.assertFalse(fence.publish(_ready()))
        self.assertTrue(fence.validate(lease))

        self.assertTrue(
            fence.publish(_ready(okx_private_ready=False, okx_generation=2))
        )
        self.assertFalse(fence.validate(lease))
        blocked, reason = fence.acquire(require_controls=True)
        self.assertIsNone(blocked)
        self.assertEqual(reason, "private_stream_not_ready")

        self.assertTrue(fence.publish(_ready(okx_generation=2)))
        replacement, reason = fence.acquire(require_controls=True)
        assert replacement is not None
        self.assertIsNone(reason)
        self.assertTrue(fence.validate(replacement))
        self.assertFalse(fence.validate(lease))

    def test_close_lease_ignores_control_change_but_open_lease_does_not(self) -> None:
        fence = DualReadinessFence(_ready())
        open_lease, _ = fence.acquire(require_controls=True)
        close_lease, _ = fence.acquire(require_controls=False)
        assert open_lease is not None and close_lease is not None

        self.assertTrue(fence.publish(_ready(pause=True, kill_switch=True)))
        self.assertFalse(fence.validate(open_lease))
        self.assertTrue(fence.validate(close_lease))

    def test_recovery_lease_requires_target_trade_and_both_private_streams(self) -> None:
        fence = DualReadinessFence(_ready(bybit_trade_ready=False))
        lease, reason = fence.acquire_recovery(Venue.OKX)
        assert lease is not None
        self.assertIsNone(reason)
        self.assertTrue(fence.validate_recovery(lease))

        self.assertTrue(
            fence.publish(
                _ready(
                    bybit_trade_ready=False,
                    bybit_private_ready=False,
                    bybit_generation=2,
                )
            )
        )
        self.assertFalse(fence.validate_recovery(lease))
        blocked, reason = fence.acquire_recovery(Venue.OKX)
        self.assertIsNone(blocked)
        self.assertEqual(reason, "stream_blocked")

        self.assertTrue(
            fence.publish(_ready(bybit_trade_ready=False, okx_trade_ready=False))
        )
        blocked, reason = fence.acquire_recovery(Venue.OKX)
        self.assertIsNone(blocked)
        self.assertEqual(reason, "trade_socket_not_ready")

    def test_admission_capacity_ok_is_pure(self) -> None:
        self.assertFalse(
            admission_capacity_ok(
                queue_depth=0,
                max_queue=8,
                reserved_tail=5,
                count=SUBMIT_WORST_CASE_EVENTS,
                open_intent=True,
                scanned=True,
                hard_full=False,
                integrity_unhealthy=False,
            )
        )
        self.assertTrue(
            admission_capacity_ok(
                queue_depth=0,
                max_queue=16,
                reserved_tail=5,
                count=SUBMIT_WORST_CASE_EVENTS,
                open_intent=True,
                scanned=True,
                hard_full=False,
                integrity_unhealthy=False,
            )
        )
        self.assertFalse(
            admission_capacity_ok(
                queue_depth=3,
                max_queue=8,
                reserved_tail=5,
                count=5,
                open_intent=True,
                scanned=True,
                hard_full=False,
                integrity_unhealthy=False,
            )
        )
        self.assertTrue(
            admission_capacity_ok(
                queue_depth=3,
                max_queue=8,
                reserved_tail=5,
                count=5,
                open_intent=False,
                scanned=True,
                hard_full=False,
                integrity_unhealthy=False,
            )
        )


class OwnershipTests(unittest.TestCase):
    def test_same_process_cannot_own_path_twice(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "owner.lock"
            first = FileOwnershipFence(path)
            second = FileOwnershipFence(path)
            first.acquire()
            first.assert_owned()
            with self.assertRaises(OwnershipError):
                second.acquire()
            first.release()
            second.acquire()
            second.assert_owned()
            second.release()

    def test_subprocess_conflict_then_release(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "owner.lock"
            parent = FileOwnershipFence(path)
            parent.acquire()
            repo = Path(__file__).resolve().parents[1]
            child = (
                "from app.bot.execution.ownership import FileOwnershipFence, OwnershipError\n"
                "import sys\n"
                f"fence = FileOwnershipFence({str(path)!r})\n"
                "try:\n"
                "    fence.acquire()\n"
                "    print('acquired')\n"
                "    fence.release()\n"
                "except OwnershipError as exc:\n"
                "    print(exc.reason_code)\n"
            )
            env = dict(os.environ)
            env["PYTHONPATH"] = str(repo)
            blocked = subprocess.run(
                [sys.executable, "-c", child],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(blocked.stdout.strip(), "lock_held")
            parent.release()
            freed = subprocess.run(
                [sys.executable, "-c", child],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(freed.stdout.strip(), "acquired")

    def test_assert_owned_does_not_touch_filesystem(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "owner.lock"
            fence = FileOwnershipFence(path)
            fence.acquire()
            with patch("builtins.open", side_effect=AssertionError("fs")):
                with patch("os.open", side_effect=AssertionError("os.open")):
                    fence.assert_owned()
            fence.release()

    def test_claim_engine_requires_acquired_fence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "owner.lock"
            fence = FileOwnershipFence(path)
            with self.assertRaises(OwnershipError):
                fence.claim_engine()
            fence.acquire()
            claim = fence.claim_engine()
            self.assertTrue(claim)
            with self.assertRaises(OwnershipError):
                fence.claim_engine()
            fence.assert_owned(claim)
            fence.release()
            with self.assertRaises(OwnershipError):
                fence.claim_engine()

    def test_fork_inherited_claim_fails_parent_still_owns(self) -> None:
        if not hasattr(os, "fork"):
            self.skipTest("os.fork is not available")
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "owner.lock"
            fence = FileOwnershipFence(path)
            fence.acquire()
            claim = fence.claim_engine()
            pid = os.fork()
            if pid == 0:
                try:
                    fence.assert_owned(claim)
                except OwnershipError:
                    os._exit(2)
                except BaseException:
                    os._exit(3)
                os._exit(0)
            _waited, status = os.waitpid(pid, 0)
            self.assertTrue(os.WIFEXITED(status))
            self.assertEqual(os.WEXITSTATUS(status), 2)
            fence.assert_owned(claim)
            self.assertTrue(fence.owned)
            fence.release()

    def test_old_claim_fails_after_reacquire_before_new_claim(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "owner.lock"
            fence = FileOwnershipFence(path)
            fence.acquire()
            old = fence.claim_engine()
            fence.assert_owned(old)
            fence.release()
            fence.acquire()
            with self.assertRaises(OwnershipError):
                fence.assert_owned(old)
            new = fence.claim_engine()
            self.assertNotEqual(old, new)
            fence.assert_owned(new)
            with self.assertRaises(OwnershipError):
                fence.assert_owned(old)
            fence.release()


class SubmitHappyPathTests(EngineHarness):
    async def test_opt_in_prewrite_is_durable_before_socket_send(self) -> None:
        engine = self._engine(durable_prewrite=True)
        original_send = self.bybit.asend

        async def assert_durable_then_send(text: str) -> None:
            replay = engine._wal.replay()
            self.assertEqual(replay.records[-1].event.event_type, ExecutionEventType.INTENT_ACCEPTED)
            self.assertEqual(replay.state, engine.state)
            self.assertEqual(engine._wal.health().queue_depth, 0)
            await original_send(text)

        self.bybit.asend = assert_durable_then_send
        result = await engine.submit(_intent())
        self.assertEqual(result.status, SubmitStatus.ACCEPTED)
        self.assertEqual(self.bybit.asend_calls, 1)
        self.assertEqual(self.okx.asend_calls, 1)

    async def test_opt_in_prewrite_failure_never_sends_and_requires_recovery(self) -> None:
        engine = self._engine(durable_prewrite=True)
        with patch.object(engine._wal, "drain_and_prove_last", side_effect=WalError("write_failed")):
            result = await engine.submit(_intent())
        self.assertEqual(result.status, SubmitStatus.RECOVERY_REQUIRED)
        self.assertEqual(result.reason_code, "wal_unhealthy")
        self.assertEqual(engine.state.status, SpreadStatus.ARMED)
        self.assertEqual(self.bybit.asend_calls, 0)
        self.assertEqual(self.okx.asend_calls, 0)

    async def test_opt_in_prewrite_does_not_replay_on_signal(self) -> None:
        engine = self._engine(durable_prewrite=True)
        with patch.object(engine._wal, "replay", side_effect=AssertionError("hot_path_replay")):
            result = await engine.submit(_intent())
        self.assertEqual(result.status, SubmitStatus.ACCEPTED)
        self.assertEqual(self.bybit.asend_calls, 1)

    async def test_opt_in_prewrite_rejects_external_wal_append(self) -> None:
        engine = self._engine(durable_prewrite=True)
        with engine._wal.path.open("ab") as handle:
            handle.write(b"external\n")
        result = await engine.submit(_intent())
        self.assertEqual(result.status, SubmitStatus.RECOVERY_REQUIRED)
        self.assertEqual(self.bybit.asend_calls, 0)
        self.assertEqual(self.okx.asend_calls, 0)

    async def _assert_prewrite_crash_fails_closed(self, stage: str) -> None:
        engine = self._engine(durable_prewrite=True)

        def crash(at: str) -> None:
            if at == stage:
                raise RuntimeError("injected_crash")

        with patch.object(engine._wal, "_maybe_crash", side_effect=crash):
            result = await engine.submit(_intent())
        self.assertEqual(result.status, SubmitStatus.RECOVERY_REQUIRED)
        self.assertEqual(result.reason_code, "wal_unhealthy")
        self.assertEqual(self.bybit.asend_calls, 0)
        self.assertEqual(self.okx.asend_calls, 0)

    async def test_prewrite_crash_before_write_fails_closed(self) -> None:
        await self._assert_prewrite_crash_fails_closed(CRASH_BEFORE_WRITE)

    async def test_prewrite_torn_write_fails_closed(self) -> None:
        await self._assert_prewrite_crash_fails_closed(CRASH_TORN_WRITE)

    async def test_prewrite_crash_before_fsync_fails_closed(self) -> None:
        await self._assert_prewrite_crash_fails_closed(CRASH_AFTER_FLUSH_BEFORE_FSYNC)

    async def test_prewrite_crash_after_fsync_before_ack_fails_closed(self) -> None:
        await self._assert_prewrite_crash_fails_closed(CRASH_AFTER_FSYNC_BEFORE_ACK)

    async def test_opt_in_prewrite_rejects_unreplayed_prior_wal_state(self) -> None:
        with self.assertRaises(EngineError):
            self._engine(
                durable_prewrite=True,
                state=initial_spread_state(run_id=RUN_ID),
            )
        self.assertEqual(self.bybit.asend_calls, 0)
        self.assertEqual(self.okx.asend_calls, 0)

    async def test_opt_in_prewrite_rechecks_ttl_after_fsync(self) -> None:
        engine = self._engine(durable_prewrite=True)
        original_drain = engine._wal.drain_and_prove_last

        def expire_after_drain(event: ExecutionEvent) -> Any:
            proof = original_drain(event)
            self.clock.n = 1_000_000
            return proof

        with patch.object(engine._wal, "drain_and_prove_last", side_effect=expire_after_drain):
            result = await engine.submit(_intent())
        self.assertEqual(result.status, SubmitStatus.REJECTED)
        self.assertEqual(result.reason_code, "ttl_expired")
        self.assertEqual(self.bybit.asend_calls, 0)
        self.assertEqual(self.okx.asend_calls, 0)

    async def test_open_both_completed_is_accepted_and_dispatches_once(self) -> None:
        engine = self._engine()
        result = await engine.submit(_intent())
        self.assertEqual(result.status, SubmitStatus.ACCEPTED)
        self.assertFalse(result.recovery_required)
        self.assertEqual(result.run_id, RUN_ID)
        self.assertIsNotNone(result.dispatch)
        self.assertEqual(result.dispatch.intent_id, INTENT_A)
        self.assertEqual(result.dispatch.run_id, RUN_ID)
        self.assertEqual(result.to_public_dict()["dispatch"]["status"], "both_completed")
        self.assertEqual(self.bybit.asend_calls, 1)
        self.assertEqual(self.okx.asend_calls, 1)
        self.assertEqual(engine.state.status, SpreadStatus.DISPATCHING)
        self.assertEqual(engine.state.last_sequence, 3)
        types = [item.event.event_type for item in engine._wal._queue]
        self.assertEqual(
            [item.value for item in types[-3:]],
            ["intent_accepted", "request_sent", "request_sent"],
        )
        self.assertEqual(engine._wal._queue[-2].event.venue, Venue.BYBIT)
        self.assertEqual(engine._wal._queue[-1].event.venue, Venue.OKX)

    async def test_simultaneous_opens_across_coins_dispatch_exactly_once(self) -> None:
        engine = self._engine()
        first = _intent(intent_id=INTENT_A, coin="BTC")
        second = _intent(intent_id=INTENT_B, coin="ETH")
        third = _intent(intent_id=INTENT_C, coin="KAITO")
        results = await asyncio.gather(engine.submit(first), engine.submit(second), engine.submit(third))
        accepted = [item for item in results if item.status is SubmitStatus.ACCEPTED]
        rejected = [item for item in results if item.status is SubmitStatus.REJECTED]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 2)
        self.assertTrue(all(item.reason_code == "opens_not_allowed" for item in rejected))
        self.assertEqual(self.bybit.asend_calls, 1)
        self.assertEqual(self.okx.asend_calls, 1)

    async def test_stress_opens_across_coins_one_dispatch(self) -> None:
        coins = ("BTC", "ETH", "KAITO")
        engine = self._engine()
        intents = [
            _intent(intent_id=f"{i:08x}-aaaa-4bbb-8ccc-ddddeeeeffff", coin=coins[i % 3])
            for i in range(12)
        ]
        results = await asyncio.gather(*[engine.submit(item) for item in intents])
        accepted = [item for item in results if item.status is SubmitStatus.ACCEPTED]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(self.bybit.asend_calls + self.okx.asend_calls, 2)
        self.assertTrue(
            all(
                item.reason_code == "opens_not_allowed"
                for item in results
                if item.status is SubmitStatus.REJECTED
            )
        )


class RiskGateTests(EngineHarness):
    async def test_ttl_expired(self) -> None:
        engine = self._engine()
        result = await engine.submit(_intent(signal_mono=0, expiry_mono=1))
        self.assertEqual(result.status, SubmitStatus.REJECTED)
        self.assertEqual(result.reason_code, "ttl_expired")
        self.assertEqual(self.bybit.asend_calls, 0)

    async def test_ttl_expired_after_resolver_advances_clock(self) -> None:
        expiry = 20_000

        def expire_during_resolve(intent: TradeIntent) -> tuple[LegPlan, LegPlan]:
            self.clock.n = expiry
            return _resolver(intent)

        engine = self._engine(resolver=expire_during_resolve)
        depth = engine._wal.health().queue_depth
        result = await engine.submit(_intent(signal_mono=500, expiry_mono=expiry))
        self.assertEqual(result.status, SubmitStatus.REJECTED)
        self.assertEqual(result.reason_code, "ttl_expired")
        self.assertEqual(self.bybit.asend_calls, 0)
        self.assertEqual(self.okx.asend_calls, 0)
        self.assertEqual(engine._wal.health().queue_depth, depth)

    async def test_coin_not_allowed(self) -> None:
        engine = self._engine()
        result = await engine.submit(_intent(coin="DOGE"))
        self.assertEqual(result.reason_code, "coin_not_allowed")
        self.assertEqual(self.bybit.asend_calls, 0)

    async def test_notional_exceeds_cap(self) -> None:
        engine = self._engine()
        result = await engine.submit(_intent(notional=Decimal("20.01")))
        self.assertEqual(result.reason_code, "notional_exceeds_cap")
        self.assertEqual(self.bybit.asend_calls, 0)

    async def test_pause_blocks_open_only(self) -> None:
        engine = self._engine(readiness=_ready(pause=True))
        opened = await engine.submit(_intent())
        self.assertEqual(opened.reason_code, "pause")
        self.assertIsNone(opened.dispatch)
        self._reclaim_fence()
        closer = self._engine(readiness=_ready(pause=True), state=_open_state(), wal=engine._wal)
        closed = await closer.submit(_intent(intent_id=CLOSE_ID, action=IntentAction.CLOSE))
        self.assertIn(closed.status, {SubmitStatus.ACCEPTED, SubmitStatus.RECOVERY_REQUIRED})
        self.assertGreaterEqual(self.bybit.asend_calls + self.okx.asend_calls, 2)

    async def test_kill_switch_blocks_open_only(self) -> None:
        engine = self._engine(readiness=_ready(kill_switch=True))
        opened = await engine.submit(_intent())
        self.assertEqual(opened.reason_code, "kill_switch")
        self._reclaim_fence()
        closer = self._engine(
            readiness=_ready(kill_switch=True),
            state=_open_state(),
            wal=engine._wal,
        )
        closed = await closer.submit(_intent(intent_id=CLOSE_ID, action=IntentAction.CLOSE))
        self.assertNotEqual(closed.status, SubmitStatus.REJECTED)
        self.assertGreaterEqual(self.bybit.asend_calls + self.okx.asend_calls, 2)

    async def test_trade_socket_and_private_stream_gates(self) -> None:
        sock = self._engine(readiness=_ready(bybit_trade_ready=False))
        self.assertEqual((await sock.submit(_intent())).reason_code, "trade_socket_not_ready")
        self._reclaim_fence()
        stream = self._engine(readiness=_ready(okx_private_ready=False), wal=_ready_wal(self.wal_path))
        self.assertEqual((await stream.submit(_intent())).reason_code, "private_stream_not_ready")
        self.assertEqual(self.bybit.asend_calls, 0)

    async def test_disconnected_private_stream_turns_normal_close_into_recovery(self) -> None:
        engine = self._engine(
            readiness=_ready(okx_private_ready=False, okx_generation=2),
            state=_open_state(),
        )
        result = await engine.submit(
            _intent(intent_id=CLOSE_ID, action=IntentAction.CLOSE)
        )
        self.assertEqual(result.status, SubmitStatus.RECOVERY_REQUIRED)
        self.assertEqual(result.reason_code, "private_stream_not_ready")
        self.assertTrue(result.recovery_required)
        self.assertIn(
            engine.state.status,
            {SpreadStatus.RECOVERING, SpreadStatus.EXPOSURE_UNKNOWN},
        )
        self.assertTrue(engine.state.recovery_required)
        self.assertEqual(self.bybit.asend_calls, 0)
        self.assertEqual(self.okx.asend_calls, 0)

    async def test_readiness_change_during_frame_finalize_blocks_both_writes(self) -> None:
        holder: dict[str, ExecutionEngine] = {}
        finalize_calls = 0

        def finalize(*args: Any, **kwargs: Any) -> str:
            nonlocal finalize_calls
            finalize_calls += 1
            text = unsigned_frame_finalizer(*args, **kwargs)
            if finalize_calls == 2:
                holder["engine"].publish_readiness(
                    _ready(bybit_generation=2, okx_generation=2)
                )
            return text

        engine = ExecutionEngine(
            run_id=RUN_ID,
            wal=_ready_wal(self.wal_path),
            transport=self._transport(finalize=finalize),
            plan_resolver=_resolver,
            instrument_cache=_cache("BTC"),
            risk_policy=_policy("BTC"),
            readiness=_ready(),
            ownership=self.fence,
            monotonic_ns=self.clock,
        )
        holder["engine"] = engine
        result = await engine.submit(_intent())
        self.assertEqual(result.status, SubmitStatus.REJECTED)
        self.assertEqual(result.reason_code, "readiness_changed")
        self.assertEqual(engine.state.status, SpreadStatus.IDLE)
        self.assertEqual(self.bybit.asend_calls, 0)
        self.assertEqual(self.okx.asend_calls, 0)

    async def test_disconnect_at_close_send_boundary_starts_recovery_without_writes(self) -> None:
        holder: dict[str, ExecutionEngine] = {}
        finalize_calls = 0

        def finalize(*args: Any, **kwargs: Any) -> str:
            nonlocal finalize_calls
            finalize_calls += 1
            text = unsigned_frame_finalizer(*args, **kwargs)
            if finalize_calls == 2:
                holder["engine"].publish_readiness(
                    _ready(bybit_private_ready=False, bybit_generation=2)
                )
            return text

        engine = ExecutionEngine(
            run_id=RUN_ID,
            wal=_ready_wal(self.wal_path),
            transport=self._transport(finalize=finalize),
            plan_resolver=_resolver,
            instrument_cache=_cache("BTC"),
            risk_policy=_policy("BTC"),
            readiness=_ready(),
            ownership=self.fence,
            monotonic_ns=self.clock,
            state=_open_state(),
        )
        holder["engine"] = engine
        result = await engine.submit(
            _intent(intent_id=CLOSE_ID, action=IntentAction.CLOSE)
        )
        self.assertEqual(result.status, SubmitStatus.RECOVERY_REQUIRED)
        self.assertEqual(result.reason_code, "readiness_changed")
        self.assertTrue(engine.state.recovery_required)
        self.assertEqual(self.bybit.asend_calls, 0)
        self.assertEqual(self.okx.asend_calls, 0)

    async def test_readiness_publish_is_not_starved_by_inflight_submit(self) -> None:
        self.bybit.block = True
        self.okx.block = True
        self.bybit.release.clear()
        self.okx.release.clear()
        engine = self._engine()
        task = asyncio.create_task(engine.submit(_intent()))
        await asyncio.wait_for(
            asyncio.gather(self.bybit.started.wait(), self.okx.started.wait()),
            timeout=1,
        )

        await asyncio.wait_for(
            engine.update_readiness(
                _ready(okx_private_ready=False, bybit_generation=2, okx_generation=2)
            ),
            timeout=0.1,
        )
        self.assertEqual(engine.readiness_revision, 1)
        self.bybit.release.set()
        self.okx.release.set()
        result = await task
        self.assertEqual(result.status, SubmitStatus.ACCEPTED)
        sent = [
            item.event
            for item in engine._wal._queue
            if item.event.event_type is ExecutionEventType.REQUEST_SENT
        ]
        self.assertEqual(len(sent), 2)
        self.assertEqual(
            {event.payload["stream_generation"] for event in sent}, {1}
        )

    async def test_ownership_required(self) -> None:
        engine = self._engine()
        self.fence.release()
        result = await engine.submit(_intent())
        self.assertEqual(result.reason_code, "ownership_not_held")
        self.assertEqual(self.bybit.asend_calls, 0)
        self.fence.acquire()

    async def test_resolver_reacquire_rejects_stale_engine_claim(self) -> None:
        def steal_and_reacquire(intent: TradeIntent) -> tuple[LegPlan, LegPlan]:
            self.fence.release()
            self.fence.acquire()
            return _resolver(intent)

        engine = self._engine(resolver=steal_and_reacquire)
        depth = engine._wal.health().queue_depth
        accepted_before = sum(
            1
            for item in engine._wal._queue
            if item.event.event_type is ExecutionEventType.INTENT_ACCEPTED
        )
        result = await engine.submit(_intent())
        self.assertEqual(result.status, SubmitStatus.REJECTED)
        self.assertEqual(result.reason_code, "ownership_not_held")
        self.assertEqual(self.bybit.asend_calls, 0)
        self.assertEqual(self.okx.asend_calls, 0)
        self.assertEqual(engine._wal.health().queue_depth, depth)
        accepted_after = sum(
            1
            for item in engine._wal._queue
            if item.event.event_type is ExecutionEventType.INTENT_ACCEPTED
        )
        self.assertEqual(accepted_after, accepted_before)

    async def test_stale_metadata_rejects_open(self) -> None:
        stale = InstrumentCache.from_snapshots(
            [
                CachedInstrument(
                    venue=Venue.BYBIT,
                    instrument="BTCUSDT",
                    captured_mono_ns=100,
                    fresh_until_mono_ns=200,
                ),
                CachedInstrument(
                    venue=Venue.OKX,
                    instrument="BTC-USDT-SWAP",
                    captured_mono_ns=100,
                    fresh_until_mono_ns=200,
                    inst_id_code=OKX_INST_ID_CODE,
                ),
            ]
        )
        engine = self._engine(cache=stale)
        result = await engine.submit(_intent())
        self.assertEqual(result.reason_code, "stale_metadata")
        self.assertEqual(self.bybit.asend_calls, 0)

    async def test_opens_not_allowed_from_dispatching_state(self) -> None:
        engine = self._engine()
        first = await engine.submit(_intent())
        self.assertEqual(first.status, SubmitStatus.ACCEPTED)
        second = await engine.submit(_intent(intent_id=INTENT_B, coin="ETH"))
        self.assertEqual(second.reason_code, "opens_not_allowed")
        self.assertEqual(self.bybit.asend_calls, 1)

    async def test_close_allowed_when_wal_blocks_opens_if_tail_capacity_exists(self) -> None:
        wal = _ready_wal(self.wal_path, max_queue=10, reserved_tail=5, mark=False)
        self.assertTrue(wal.health().blocks_opens)
        self.assertTrue(wal.can_admit(SUBMIT_WORST_CASE_EVENTS, open_intent=False))
        engine = self._engine(wal=wal, state=_open_state(), readiness=_ready(pause=True, kill_switch=True))
        result = await engine.submit(_intent(intent_id=CLOSE_ID, action=IntentAction.CLOSE))
        self.assertNotEqual(result.status, SubmitStatus.REJECTED)
        self.assertGreaterEqual(self.bybit.asend_calls + self.okx.asend_calls, 2)

    async def test_wal_capacity_does_not_send(self) -> None:
        wal = _ready_wal(self.wal_path, max_queue=8, reserved_tail=2)
        _fill_wal(wal, 4, start_seq=3)
        self.assertFalse(wal.health().blocks_opens)
        self.assertFalse(wal.can_admit(SUBMIT_WORST_CASE_EVENTS, open_intent=True))
        engine = self._engine(wal=wal)
        depth = wal.health().queue_depth
        result = await engine.submit(_intent())
        self.assertEqual(result.status, SubmitStatus.REJECTED)
        self.assertEqual(result.reason_code, "wal_capacity")
        self.assertEqual(self.bybit.asend_calls, 0)
        self.assertEqual(self.okx.asend_calls, 0)
        self.assertEqual(wal.health().queue_depth, depth)

    async def test_open_resolver_both_reduce_only_rejected(self) -> None:
        def both_reduce_only(intent: TradeIntent) -> tuple[LegPlan, LegPlan]:
            return _plans(intent.intent_id, reduce_only=True, coin=intent.coin)

        engine = self._engine(resolver=both_reduce_only)
        depth = engine._wal.health().queue_depth
        result = await engine.submit(_intent())
        self.assertEqual(result.status, SubmitStatus.REJECTED)
        self.assertEqual(result.reason_code, "invalid_plan_set")
        self.assertEqual(self.bybit.asend_calls, 0)
        self.assertEqual(self.okx.asend_calls, 0)
        self.assertEqual(engine._wal.health().queue_depth, depth)

    async def test_close_resolver_both_not_reduce_only_rejected(self) -> None:
        def both_open_style(intent: TradeIntent) -> tuple[LegPlan, LegPlan]:
            return _plans(intent.intent_id, reduce_only=False, coin=intent.coin)

        engine = self._engine(state=_open_state(), resolver=both_open_style)
        depth = engine._wal.health().queue_depth
        result = await engine.submit(_intent(intent_id=CLOSE_ID, action=IntentAction.CLOSE))
        self.assertEqual(result.status, SubmitStatus.REJECTED)
        self.assertEqual(result.reason_code, "invalid_plan_set")
        self.assertEqual(self.bybit.asend_calls, 0)
        self.assertEqual(self.okx.asend_calls, 0)
        self.assertEqual(engine._wal.health().queue_depth, depth)

    async def test_reserved_tail_keeps_close_batch_admissible(self) -> None:
        wal = _ready_wal(self.wal_path, max_queue=12, reserved_tail=5)
        _fill_wal(wal, 2, start_seq=3)
        self.assertTrue(wal.can_admit(SUBMIT_WORST_CASE_EVENTS, open_intent=True))
        self.assertFalse(
            admission_capacity_ok(
                queue_depth=wal.health().queue_depth + 1,
                max_queue=12,
                reserved_tail=5,
                count=SUBMIT_WORST_CASE_EVENTS,
                open_intent=True,
                scanned=True,
                hard_full=False,
                integrity_unhealthy=False,
            )
        )
        engine = self._engine(wal=wal)
        opened = await engine.submit(_intent())
        self.assertEqual(opened.status, SubmitStatus.ACCEPTED)
        self.assertFalse(wal.can_admit(SUBMIT_WORST_CASE_EVENTS, open_intent=True))
        self.assertTrue(wal.can_admit(SUBMIT_WORST_CASE_EVENTS, open_intent=False))
        self.assertFalse(wal.health().hard_full)
        self._reclaim_fence()
        closer = self._engine(wal=wal, state=_open_state())
        closed = await closer.submit(_intent(intent_id=CLOSE_ID, action=IntentAction.CLOSE))
        self.assertNotEqual(closed.status, SubmitStatus.REJECTED)
        self.assertGreaterEqual(self.bybit.asend_calls + self.okx.asend_calls, 4)

    async def test_update_readiness_observed_by_later_open(self) -> None:
        engine = self._engine()
        await engine.update_readiness(_ready(kill_switch=True, bybit_generation=2, okx_generation=3))
        killed = await engine.submit(_intent())
        self.assertEqual(killed.reason_code, "kill_switch")
        await engine.update_readiness(_ready(pause=True, bybit_generation=4, okx_generation=5))
        paused = await engine.submit(_intent())
        self.assertEqual(paused.reason_code, "pause")
        await engine.update_readiness(
            _ready(okx_trade_ready=False, bybit_generation=6, okx_generation=7)
        )
        trade = await engine.submit(_intent())
        self.assertEqual(trade.reason_code, "trade_socket_not_ready")
        await engine.update_readiness(
            _ready(bybit_private_ready=False, bybit_generation=8, okx_generation=9)
        )
        private = await engine.submit(_intent())
        self.assertEqual(private.reason_code, "private_stream_not_ready")
        self.assertEqual(self.bybit.asend_calls, 0)
        await engine.update_readiness(_ready(bybit_generation=17, okx_generation=19))
        accepted = await engine.submit(_intent())
        self.assertEqual(accepted.status, SubmitStatus.ACCEPTED)
        sent = [
            item.event
            for item in engine._wal._queue
            if item.event.event_type is ExecutionEventType.REQUEST_SENT
        ]
        self.assertEqual(len(sent), 2)
        gens = {item.venue: item.payload["stream_generation"] for item in sent}
        self.assertEqual(gens[Venue.BYBIT], 17)
        self.assertEqual(gens[Venue.OKX], 19)

    async def test_risk_policy_cap_above_twenty_raises(self) -> None:
        with self.assertRaises(EngineError) as ctx:
            RiskPolicy(allowed_coins=frozenset({"BTC"}), max_notional_usdt=Decimal("20.01"))
        self.assertEqual(ctx.exception.reason_code, "notional_exceeds_cap")
        policy = RiskPolicy(allowed_coins=frozenset({"BTC"}), max_notional_usdt=Decimal("10"))
        self.assertEqual(policy.max_notional_usdt, Decimal("10"))
        engine = self._engine(policy=policy, cache=_cache("BTC"))
        over = await engine.submit(_intent(notional=Decimal("10.01")))
        self.assertEqual(over.reason_code, "notional_exceeds_cap")
        self.assertEqual(self.bybit.asend_calls, 0)
        accepted = await engine.submit(_intent(notional=Decimal("10")))
        self.assertEqual(accepted.status, SubmitStatus.ACCEPTED)
        self.assertEqual(self.bybit.asend_calls, 1)

    async def test_second_engine_same_fence_rejected_before_send(self) -> None:
        first = self._engine()
        with self.assertRaises((OwnershipError, EngineError)):
            ExecutionEngine(
                run_id=RUN_ID,
                wal=first._wal,
                transport=self._transport(),
                plan_resolver=_resolver,
                instrument_cache=_cache(),
                risk_policy=_policy(),
                readiness=_ready(),
                ownership=self.fence,
                monotonic_ns=self.clock,
            )
        self.assertEqual(self.bybit.asend_calls, 0)
        self.assertEqual(self.okx.asend_calls, 0)


class TransportMappingTests(EngineHarness):
    async def test_transport_rejected_rolls_back_open(self) -> None:
        def boom(*_args: object, **_kwargs: object) -> str:
            raise RuntimeError("no_frame")

        wal = _ready_wal(self.wal_path)
        engine = ExecutionEngine(
            run_id=RUN_ID,
            wal=wal,
            transport=self._transport(finalize=boom),
            plan_resolver=_resolver,
            instrument_cache=_cache(),
            risk_policy=_policy(),
            readiness=_ready(),
            ownership=self.fence,
            monotonic_ns=self.clock,
        )
        result = await engine.submit(_intent())
        self.assertEqual(result.status, SubmitStatus.REJECTED)
        self.assertEqual(result.reason_code, "transport_rejected")
        self.assertEqual(engine.state.status, SpreadStatus.IDLE)
        self.assertEqual(self.bybit.asend_calls, 0)
        types = [item.event.event_type for item in wal._queue]
        self.assertIn(ExecutionEventType.INTENT_ACCEPTED, types)
        self.assertIn(ExecutionEventType.INTENT_REJECTED, types)

    async def test_partial_write_is_recovery_and_emits_timeout(self) -> None:
        self.okx.error = RuntimeError("peer_down")
        engine = self._engine()
        result = await engine.submit(_intent())
        self.assertEqual(result.status, SubmitStatus.RECOVERY_REQUIRED)
        self.assertTrue(result.recovery_required)
        types = [(item.event.event_type, item.event.venue) for item in engine._wal._queue]
        self.assertIn((ExecutionEventType.REQUEST_SENT, Venue.BYBIT), types)
        self.assertIn((ExecutionEventType.REQUEST_SENT, Venue.OKX), types)
        self.assertIn((ExecutionEventType.ACK_TIMEOUT, Venue.OKX), types)
        self.assertTrue(engine.state.recovery_required)

    async def test_both_failed_started_writes_timeout_both(self) -> None:
        self.bybit.error = RuntimeError("bybit_down")
        self.okx.error = RuntimeError("okx_down")
        engine = self._engine()
        result = await engine.submit(_intent())
        self.assertEqual(result.status, SubmitStatus.RECOVERY_REQUIRED)
        venues = [
            item.event.venue
            for item in engine._wal._queue
            if item.event.event_type is ExecutionEventType.ACK_TIMEOUT
        ]
        self.assertEqual(venues, [Venue.BYBIT, Venue.OKX])

    async def test_cancel_after_schedule_is_recovery(self) -> None:
        self.bybit.block = True
        self.okx.block = True
        engine = self._engine()
        task = asyncio.create_task(engine.submit(_intent()))
        await asyncio.wait_for(self.bybit.started.wait(), timeout=1)
        await asyncio.wait_for(self.okx.started.wait(), timeout=1)
        task.cancel()
        self.bybit.release.set()
        self.okx.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(engine.state.recovery_required)
        types = [item.event.event_type for item in engine._wal._queue]
        self.assertIn(ExecutionEventType.REQUEST_SENT, types)
        self.assertTrue(
            any(
                item.event.event_type
                in {ExecutionEventType.ACK_TIMEOUT, ExecutionEventType.FAULT}
                for item in engine._wal._queue
            )
        )

    async def test_close_transport_reject_is_recovery_not_rollback(self) -> None:
        def boom(*_args: object, **_kwargs: object) -> str:
            raise RuntimeError("no_frame")

        wal = _ready_wal(self.wal_path)
        engine = ExecutionEngine(
            run_id=RUN_ID,
            wal=wal,
            transport=self._transport(finalize=boom),
            plan_resolver=_resolver,
            instrument_cache=_cache(),
            risk_policy=_policy(),
            readiness=_ready(),
            ownership=self.fence,
            monotonic_ns=self.clock,
            state=_open_state(),
        )
        result = await engine.submit(_intent(intent_id=CLOSE_ID, action=IntentAction.CLOSE))
        self.assertEqual(result.status, SubmitStatus.RECOVERY_REQUIRED)
        self.assertNotEqual(engine.state.status, SpreadStatus.OPEN)
        self.assertTrue(engine.state.recovery_required)
        types = [item.event.event_type for item in wal._queue]
        self.assertIn(ExecutionEventType.FAULT, types)
        self.assertNotIn(ExecutionEventType.INTENT_REJECTED, types)


class LivePrivateBridgeTests(EngineHarness):
    def _bridge(self, engine: ExecutionEngine) -> LivePrivateEvidenceBridge:
        return LivePrivateEvidenceBridge(
            engine=engine,
            symbols_by_venue={
                Venue.BYBIT: "BTCUSDT",
                Venue.OKX: "BTC-USDT-SWAP",
            },
        )

    async def test_buffered_private_fills_fold_only_after_request_sent(self) -> None:
        engine = self._engine(durable_prewrite=True)
        bridge = self._bridge(engine)
        intent = _intent()
        bridge.begin_submission(_resolver(intent))
        sent = await engine.submit(intent)
        self.assertEqual(sent.status, SubmitStatus.ACCEPTED)
        earlier_receive = engine.state.last_monotonic_ns - 1
        bridge.observe_trade(
            json.dumps({
                "op": "order.create", "reqId": _plans(INTENT_A)[0].client_id,
                "retCode": 0,
            }),
            earlier_receive,
            venue=Venue.BYBIT,
            generation=1,
        )
        bridge.observe_trade(
            json.dumps({
                "op": "order", "id": _plans(INTENT_A)[1].client_id,
                "code": "0", "data": [{"sCode": "0"}],
            }),
            earlier_receive,
            venue=Venue.OKX,
            generation=1,
        )
        bridge.observe(
            json.dumps({
                "topic": "order",
                "data": [{
                    "symbol": "ETHUSDT", "orderLinkId": "other", "orderStatus": "Filled",
                    "cumExecQty": "1",
                }, {
                    "symbol": "BTCUSDT",
                    "orderLinkId": _plans(INTENT_A)[0].client_id,
                    "orderStatus": "Filled", "cumExecQty": "1",
                    "execTime": "1750000000001",
                }],
            }),
            earlier_receive,
            venue=Venue.BYBIT,
            generation=1,
        )
        bridge.observe(
            json.dumps({
                "arg": {"channel": "orders"},
                "data": [{
                    "instId": "BTC-USDT-SWAP",
                    "clOrdId": _plans(INTENT_A)[1].client_id,
                    "state": "filled", "accFillSz": "1",
                    "fillTime": "1750000000002",
                }],
            }),
            earlier_receive,
            venue=Venue.OKX,
            generation=1,
        )
        self.assertEqual(bridge.pending_count, 4)
        bridge.bind_submitted(
            intent, _resolver(intent), bybit_generation=1, okx_generation=1,
            dispatch=sent.dispatch,
        )
        self.assertIs(engine._adapter, bridge._adapter)
        self.assertEqual(await bridge.drain(), 4)
        self.assertEqual(engine.state.status, SpreadStatus.OPEN)
        milestones = {item.venue: item for item in bridge.chronometry.snapshot()}
        self.assertEqual(milestones[Venue.BYBIT].first_fill_exchange_ms, 1750000000001)
        self.assertEqual(milestones[Venue.OKX].full_fill_exchange_ms, 1750000000002)
        self.assertEqual(milestones[Venue.BYBIT].ack_receive_mono_ns, earlier_receive)
        self.assertIsNotNone(milestones[Venue.OKX].send_done_mono_ns)
        self.assertEqual(engine._wal.health().queue_depth, 0)
        self.assertEqual(engine._wal.replay().state, engine.state)

        close_intent = _intent(intent_id=INTENT_B, action=IntentAction.CLOSE)
        close_plans = _resolver(close_intent)
        bridge.begin_submission(close_plans)
        self.assertEqual((await engine.submit(close_intent)).status, SubmitStatus.ACCEPTED)
        bridge.bind_submitted(
            close_intent, close_plans, bybit_generation=1, okx_generation=1,
        )
        bridge.observe(
            json.dumps({
                "topic": "order",
                "data": [{
                    "symbol": "BTCUSDT", "orderLinkId": close_plans[0].client_id,
                    "orderStatus": "Filled", "cumExecQty": "1",
                }],
            }),
            engine.state.last_monotonic_ns + 1,
            venue=Venue.BYBIT,
            generation=1,
        )
        bridge.observe(
            json.dumps({
                "arg": {"channel": "orders"},
                "data": [{
                    "instId": "BTC-USDT-SWAP", "clOrdId": close_plans[1].client_id,
                    "state": "filled", "accFillSz": "1",
                }],
            }),
            engine.state.last_monotonic_ns + 2,
            venue=Venue.OKX,
            generation=1,
        )
        self.assertEqual(await bridge.drain(), 2)
        self.assertEqual(engine.state.status, SpreadStatus.CLOSING)
        self.assertEqual(engine._wal.replay().state, engine.state)
        for venue in (Venue.BYBIT, Venue.OKX):
            empty = (
                {"retCode": 0, "result": {"list": []}}
                if venue is Venue.BYBIT else {"code": "0", "data": []}
            )
            for source in ("rest_positions", "rest_open_orders"):
                await bridge.ingest_complete_rest_snapshot(
                    empty,
                    venue=venue,
                    source=source,
                    generation=1,
                    receive_mono_ns=engine.state.last_monotonic_ns + 1,
                )
        prove = await engine.plan_recovery()
        self.assertEqual(prove.kind, RecoveryActionKind.PROVE_FLAT)
        result = await engine.apply_recovery_step(prove)
        self.assertEqual(result.status, RecoveryStatus.APPLIED)
        self.assertEqual(engine.state.status, SpreadStatus.FLAT)
        self.assertEqual(engine._wal.replay().state, engine.state)

    async def test_unknown_cap_order_fails_closed(self) -> None:
        engine = self._engine(durable_prewrite=True)
        bridge = self._bridge(engine)
        intent = _intent()
        bridge.begin_submission(_resolver(intent))
        self.assertEqual((await engine.submit(intent)).status, SubmitStatus.ACCEPTED)
        bridge.bind_submitted(
            intent, _resolver(intent), bybit_generation=1, okx_generation=1,
        )
        bridge.observe(
            json.dumps({
                "topic": "order",
                "data": [{
                    "symbol": "BTCUSDT", "orderLinkId": "unknown-client",
                    "orderStatus": "Filled", "cumExecQty": "1",
                }],
            }),
            engine.state.last_monotonic_ns + 1,
            venue=Venue.BYBIT,
            generation=1,
        )
        with self.assertRaises(LivePrivateBridgeError):
            await bridge.drain()
        self.assertTrue(engine.readiness.kill_switch)

    async def test_recovery_ack_tap_requires_bound_reduce_only_client(self) -> None:
        engine = self._engine(durable_prewrite=True)
        bridge = self._bridge(engine)
        intent = _intent()
        plans = _resolver(intent)
        bridge.begin_submission(plans)
        self.assertEqual((await engine.submit(intent)).status, SubmitStatus.ACCEPTED)
        bridge.bind_submitted(intent, plans, bybit_generation=1, okx_generation=1)
        with self.assertRaises(LivePrivateBridgeError):
            bridge.arm_recovery_client(plans[0])
        recovery = LegPlan.build(
            intent_id=intent.intent_id,
            leg_id=plans[0].leg_id,
            venue=Venue.BYBIT,
            instrument=plans[0].instrument,
            side="buy",
            quantity=plans[0].quantity,
            reduce_only=True,
        )
        bridge.arm_recovery_client(recovery)
        bridge.observe_trade(
            json.dumps({"op": "order.create", "reqId": recovery.client_id, "retCode": 0}),
            engine.state.last_monotonic_ns + 1,
            venue=Venue.BYBIT,
            generation=1,
        )
        self.assertEqual(bridge.pending_count, 1)


class AdapterIngestTests(EngineHarness):
    async def test_live_ingest_fsyncs_fill_before_return(self) -> None:
        engine = self._engine()
        await engine.submit(_intent())
        fill = _event(
            ExecutionEventType.FILL,
            intent_id=INTENT_A,
            sequence=engine.state.last_sequence + 1,
            monotonic_ns=engine.state.last_monotonic_ns + 1,
            venue=Venue.BYBIT,
            leg_id="leg_bybit",
            payload={"quantity": "1"},
        )
        result = await engine.ingest_adapter_batch_durable(
            AdapterBatch(schema_version=ADAPTER_SCHEMA, events=(fill,), issues=())
        )
        self.assertTrue(result.accepted)
        self.assertEqual(engine._wal.health().queue_depth, 0)
        self.assertEqual(engine._wal.replay().records[-1].event, fill)

    async def test_live_ingest_wal_failure_latches_kill_switch(self) -> None:
        engine = self._engine()
        await engine.submit(_intent())
        fill = _event(
            ExecutionEventType.FILL,
            intent_id=INTENT_A,
            sequence=engine.state.last_sequence + 1,
            monotonic_ns=engine.state.last_monotonic_ns + 1,
            venue=Venue.BYBIT,
            leg_id="leg_bybit",
            payload={"quantity": "1"},
        )
        with patch.object(engine._wal, "drain_and_prove_last", side_effect=WalError("write_failed")):
            result = await engine.ingest_adapter_batch_durable(
                AdapterBatch(schema_version=ADAPTER_SCHEMA, events=(fill,), issues=())
            )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason_code, "wal_unhealthy")
        self.assertTrue(result.recovery_required)
        self.assertTrue(engine.readiness.kill_switch)
        engine.publish_readiness(_ready())
        self.assertTrue(engine.readiness.kill_switch)

    async def test_adapter_batch_is_atomic(self) -> None:
        engine = self._engine()
        accepted = await engine.submit(_intent())
        self.assertEqual(accepted.status, SubmitStatus.ACCEPTED)
        good = _event(
            ExecutionEventType.FILL,
            intent_id=INTENT_A,
            sequence=engine.state.last_sequence + 1,
            monotonic_ns=engine.state.last_monotonic_ns + 1,
            venue=Venue.BYBIT,
            leg_id="leg_bybit",
            payload={"quantity": "1"},
        )
        bad = _event(
            ExecutionEventType.FILL,
            intent_id=INTENT_B,
            sequence=1,
            monotonic_ns=engine.state.last_monotonic_ns + 2,
            venue=Venue.OKX,
            leg_id="leg_okx",
            payload={"quantity": "1"},
        )
        before = engine.state.last_sequence
        depth = engine._wal.health().queue_depth
        result = await engine.ingest_adapter_batch(
            AdapterBatch(schema_version=ADAPTER_SCHEMA, events=(good, bad), issues=())
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason_code, "adapter_transition")
        self.assertEqual(engine.state.last_sequence, before)
        self.assertEqual(engine._wal.health().queue_depth, depth)

    async def test_adapter_batch_commits_when_whole_fold_fits(self) -> None:
        engine = self._engine()
        await engine.submit(_intent())
        fills = (
            _event(
                ExecutionEventType.FILL,
                intent_id=INTENT_A,
                sequence=engine.state.last_sequence + 1,
                monotonic_ns=engine.state.last_monotonic_ns + 1,
                venue=Venue.BYBIT,
                leg_id="leg_bybit",
                payload={"quantity": "1"},
            ),
            _event(
                ExecutionEventType.FILL,
                intent_id=INTENT_A,
                sequence=engine.state.last_sequence + 2,
                monotonic_ns=engine.state.last_monotonic_ns + 2,
                venue=Venue.OKX,
                leg_id="leg_okx",
                payload={"quantity": "1"},
            ),
        )
        result = await engine.ingest_adapter_batch(
            AdapterBatch(schema_version=ADAPTER_SCHEMA, events=fills, issues=())
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.applied_count, 2)
        self.assertEqual(engine.state.status, SpreadStatus.OPEN)
        seed = engine.adapter_last_sequences()
        self.assertEqual(seed[INTENT_A], engine.state.last_sequence)
        self.assertGreaterEqual(engine.adapter_last_monotonic_ns(), fills[-1].monotonic_ns)

    async def test_adapter_seeds_retain_open_and_close(self) -> None:
        open_state = _open_state()
        engine = self._engine(state=open_state)
        before = engine.adapter_last_sequences()
        self.assertEqual(before[INTENT_A], open_state.last_sequence)
        result = await engine.submit(_intent(intent_id=CLOSE_ID, action=IntentAction.CLOSE))
        self.assertEqual(result.status, SubmitStatus.ACCEPTED)
        seeds = engine.adapter_last_sequences()
        self.assertEqual(seeds[INTENT_A], open_state.last_sequence)
        self.assertEqual(seeds[CLOSE_ID], engine.state.last_sequence)
        self.assertNotEqual(seeds[INTENT_A], seeds[CLOSE_ID])
        with self.assertRaises(TypeError):
            seeds[INTENT_A] = 99  # type: ignore[index]
        self.assertEqual(engine.adapter_last_sequences()[INTENT_A], open_state.last_sequence)
        self.assertEqual(engine.adapter_last_sequences()[CLOSE_ID], engine.state.last_sequence)

    async def test_adapter_capacity_enqueued_nothing(self) -> None:
        wal = _ready_wal(self.wal_path, max_queue=8, reserved_tail=2)
        engine = self._engine(wal=wal)
        await engine.submit(_intent())
        _fill_wal(wal, 8 - wal.health().queue_depth, start_seq=20)
        fills = (
            _event(
                ExecutionEventType.FILL,
                intent_id=INTENT_A,
                sequence=engine.state.last_sequence + 1,
                monotonic_ns=engine.state.last_monotonic_ns + 1,
                venue=Venue.BYBIT,
                leg_id="leg_bybit",
                payload={"quantity": "1"},
            ),
        )
        depth = wal.health().queue_depth
        result = await engine.ingest_adapter_batch(
            AdapterBatch(schema_version=ADAPTER_SCHEMA, events=fills, issues=())
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason_code, "adapter_capacity")
        self.assertEqual(wal.health().queue_depth, depth)
        self.assertNotEqual(engine.state.status, SpreadStatus.OPEN)


class HotPathPurityTests(EngineHarness):
    async def test_submit_does_not_fsync_drain_export_or_log(self) -> None:
        engine = self._engine()
        exporter = InMemoryExporter()
        before_size = self.wal_path.stat().st_size if self.wal_path.exists() else 0
        fsync_calls: list[int] = []
        drain_calls: list[str] = []
        log_calls: list[str] = []

        def no_fsync(fd: int) -> None:
            fsync_calls.append(fd)
            raise AssertionError("fsync")

        def track_drain(*_args: object, **_kwargs: object) -> None:
            drain_calls.append("drain")
            raise AssertionError("drain")

        with patch("os.fsync", side_effect=no_fsync):
            with patch.object(engine._wal, "drain_once", side_effect=track_drain):
                with patch.object(engine._wal, "drain_all", side_effect=track_drain):
                    with patch("logging.Logger.info", side_effect=lambda *a, **k: log_calls.append("info")):
                        with patch("logging.Logger.error", side_effect=lambda *a, **k: log_calls.append("error")):
                            result = await engine.submit(_intent())
        self.assertEqual(result.status, SubmitStatus.ACCEPTED)
        self.assertFalse(fsync_calls)
        self.assertFalse(drain_calls)
        self.assertFalse(log_calls)
        self.assertEqual(exporter.cursor.last_wal_seq, 0)
        self.assertEqual(self.wal_path.stat().st_size, before_size)

    def test_engine_source_has_no_hot_path_io(self) -> None:
        source = inspect.getsource(ExecutionEngine.submit) + inspect.getsource(
            ExecutionEngine._submit_locked
        )
        for banned in ("fsync", "drain_once", "drain_all", "logging", "sentry", "print(", "html"):
            self.assertNotIn(banned, source)

    def test_public_result_is_redacted(self) -> None:
        result = SubmitResult(
            schema_version=ENGINE_SCHEMA,
            status=SubmitStatus.REJECTED,
            intent_id=INTENT_A,
            run_id=RUN_ID,
            reason_code="pause",
            recovery_required=False,
        )
        text = repr(result) + str(result.to_public_dict())
        for marker in FORBIDDEN:
            self.assertNotIn(marker, text)
        ingest = IngestResult(
            schema_version=ENGINE_SCHEMA,
            accepted=False,
            applied_count=0,
            reason_code="adapter_transition",
            recovery_required=True,
        )
        ingest_text = repr(ingest) + str(ingest.to_public_dict())
        for marker in FORBIDDEN:
            self.assertNotIn(marker, ingest_text)

    def test_submit_result_requires_matching_dispatch(self) -> None:
        dispatch = _dispatch_result()
        result = SubmitResult(
            schema_version=ENGINE_SCHEMA,
            status=SubmitStatus.ACCEPTED,
            intent_id=INTENT_A,
            run_id=RUN_ID,
            reason_code=None,
            recovery_required=False,
            dispatch=dispatch,
        )
        public = result.to_public_dict()
        self.assertEqual(public["run_id"], RUN_ID)
        self.assertIsNotNone(public["dispatch"])
        self.assertEqual(public["dispatch"]["status"], "both_completed")
        self.assertEqual(public["dispatch"]["run_id"], RUN_ID)
        self.assertEqual(public["dispatch"]["intent_id"], INTENT_A)
        with self.assertRaises(EngineError):
            SubmitResult(
                schema_version=ENGINE_SCHEMA,
                status=SubmitStatus.ACCEPTED,
                intent_id=INTENT_A,
                run_id=RUN_ID,
                reason_code=None,
                recovery_required=False,
                dispatch=_dispatch_result(intent_id=INTENT_B),
            )
        with self.assertRaises(EngineError):
            SubmitResult(
                schema_version=ENGINE_SCHEMA,
                status=SubmitStatus.ACCEPTED,
                intent_id=INTENT_A,
                run_id=RUN_ID,
                reason_code=None,
                recovery_required=False,
                dispatch=_dispatch_result(run_id=OTHER_RUN),
            )


class WalBatchAtomicityTests(unittest.TestCase):
    def test_enqueue_batch_all_or_nothing_preserves_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            wal = _ready_wal(path, max_queue=16, reserved_tail=5)
            first = _event(
                ExecutionEventType.PAUSE,
                intent_id=INTENT_A,
                sequence=3,
                monotonic_ns=30,
                payload={"pause": True},
            )
            mismatched = _event(
                ExecutionEventType.PAUSE,
                intent_id=INTENT_A,
                sequence=4,
                monotonic_ns=31,
                payload={"pause": True},
            )
            object.__setattr__(mismatched, "run_id", OTHER_RUN)
            before = _wal_cursor(wal)
            with self.assertRaises(WalError):
                wal.enqueue_batch((first, mismatched))
            self.assertEqual(_wal_cursor(wal), before)
            invalid_schema = _event(
                ExecutionEventType.PAUSE,
                intent_id=INTENT_A,
                sequence=5,
                monotonic_ns=32,
                payload={"pause": True},
            )
            object.__setattr__(invalid_schema, "schema_version", "bbot.execution.not_a_contract")
            with self.assertRaises(WalError):
                wal.enqueue_batch((first, invalid_schema))
            self.assertEqual(_wal_cursor(wal), before)
            oversized = tuple(
                _event(
                    ExecutionEventType.PAUSE,
                    intent_id=INTENT_A,
                    sequence=40 + index,
                    monotonic_ns=100 + index,
                    payload={"pause": True},
                )
                for index in range(17)
            )
            nacks = wal.enqueue_batch(oversized)
            self.assertTrue(nacks)
            self.assertTrue(all(not ack.accepted for ack in nacks))
            self.assertEqual(_wal_cursor(wal), before)
            second = _event(
                ExecutionEventType.PAUSE,
                intent_id=INTENT_A,
                sequence=6,
                monotonic_ns=33,
                payload={"pause": True},
            )
            acks = wal.enqueue_batch((first, second))
            self.assertEqual(len(acks), 2)
            self.assertTrue(all(ack.accepted for ack in acks))
            self.assertEqual([ack.wal_seq for ack in acks], [before[1], before[1] + 1])
            after = wal.health()
            self.assertEqual(after.queue_depth, before[0] + 2)
            self.assertEqual(after.next_wal_seq, before[1] + 2)
            self.assertNotEqual(wal._enqueue_prev_hash, before[2])
            self.assertFalse(after.hard_full)
