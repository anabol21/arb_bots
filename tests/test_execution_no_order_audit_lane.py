"""No-order audit lane counts only fsynced, replayed policy attempts."""

from __future__ import annotations

from unittest.mock import patch

from app.bot.execution.engine import ExecutionEngine
from app.bot.execution.no_order_audit_lane import NoOrderAuditLane
from app.bot.execution.transport import ExecutionTransport, NoOrderTradeSocket, unsigned_frame_finalizer
from app.bot.execution.wal import WalError
from tests.test_execution_engine import (
    EngineHarness, INTENT_B, RUN_ID, _cache, _intent, _policy, _ready, _ready_wal,
    _resolver,
)


class NoOrderAuditLaneTests(EngineHarness):
    def make_lane(self, source):
        self.audit_bybit = NoOrderTradeSocket(self.loop)
        self.audit_okx = NoOrderTradeSocket(self.loop)
        self.audit_wal = _ready_wal(self.wal_path)
        transport = ExecutionTransport(
            self.loop,
            bybit_socket=self.audit_bybit,
            okx_socket=self.audit_okx,
            finalize_frame=unsigned_frame_finalizer,
            monotonic_ns=self.clock,
        )
        engine = ExecutionEngine(
            run_id=RUN_ID, wal=self.audit_wal, transport=transport,
            plan_resolver=_resolver,
            instrument_cache=_cache("BTC", "ETH"),
            risk_policy=_policy("BTC", "ETH"),
            readiness=_ready(bybit_trade_ready=False, okx_trade_ready=False,
                             bybit_private_ready=False, okx_private_ready=False),
            ownership=self.fence, monotonic_ns=self.clock,
        )
        return NoOrderAuditLane(engine, self.audit_wal, source)

    async def test_fsync_and_replay_required_for_pass(self) -> None:
        calls = 0

        def source():
            nonlocal calls
            calls += 1
            return _ready(bybit_trade_ready=False, okx_trade_ready=False)

        lane = self.make_lane(source)
        first = await lane.audit(_intent())
        second = await lane.audit(_intent(intent_id=INTENT_B, coin="ETH"))
        self.assertTrue(first.passed, first.to_public_dict())
        self.assertTrue(first.wal_durable)
        self.assertTrue(second.passed)
        self.assertEqual(calls, 2)
        self.assertEqual((self.audit_bybit.write_attempts, self.audit_okx.write_attempts), (0, 0))
        self.assertEqual(self.audit_wal.health().queue_depth, 0)

    async def test_private_loss_is_resampled_and_rejects_second_attempt(self) -> None:
        snapshots = iter((
            _ready(bybit_trade_ready=False, okx_trade_ready=False),
            _ready(bybit_trade_ready=False, okx_trade_ready=False, okx_private_ready=False),
        ))
        lane = self.make_lane(lambda: next(snapshots))
        first = await lane.audit(_intent())
        self.assertTrue(first.passed, first.to_public_dict())
        rejected = await lane.audit(_intent(intent_id=INTENT_B, coin="ETH"))
        self.assertFalse(rejected.passed)
        self.assertEqual(rejected.reason_code, "private_stream_not_ready")
        self.assertFalse(rejected.wal_durable)
        self.assertEqual(self.audit_wal.health().queue_depth, 0)

    async def test_fsync_failure_halts_future_audits(self) -> None:
        lane = self.make_lane(lambda: _ready(bybit_trade_ready=False, okx_trade_ready=False))
        with patch.object(self.audit_wal, "drain_all", side_effect=WalError("write_failed")):
            failed = await lane.audit(_intent())
        self.assertFalse(failed.passed)
        self.assertTrue(failed.halted)
        self.assertEqual(failed.reason_code, "wal_not_durable")
        blocked = await lane.audit(_intent(intent_id=INTENT_B, coin="ETH"))
        self.assertEqual(blocked.reason_code, "lane_halted")
        self.assertEqual((self.audit_bybit.write_attempts, self.audit_okx.write_attempts), (0, 0))

    async def test_trade_ready_source_halts_without_socket_use(self) -> None:
        lane = self.make_lane(_ready)
        result = await lane.audit(_intent())
        self.assertTrue(result.halted)
        self.assertEqual(result.reason_code, "trade_socket_bound")
        self.assertEqual((self.audit_bybit.write_attempts, self.audit_okx.write_attempts), (0, 0))
