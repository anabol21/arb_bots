"""EV2-12B3b: private-only no-order engine audit, never a send."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

from app.bot.execution.contracts import (
    ContractValidationError,
    ExecutionEventType,
    IntentAction,
    SpreadStatus,
)
from app.bot.execution.engine import ExecutionEngine
from app.bot.execution.transport import (
    ExecutionTransport,
    NoOrderTradeSocket,
    unsigned_frame_finalizer,
)
from app.bot.execution.wal import WalError
from tests.test_execution_engine import (
    EngineHarness,
    INTENT_B,
    RUN_ID,
    _cache,
    _intent,
    _policy,
    _ready,
    _ready_wal,
    _resolver,
)


class NoOrderEngineAuditTests(EngineHarness):
    def audit_engine(self, *, readiness=None, finalizer=unsigned_frame_finalizer):
        self.audit_bybit = NoOrderTradeSocket(self.loop)
        self.audit_okx = NoOrderTradeSocket(self.loop)
        self.audit_wal = _ready_wal(self.wal_path)
        transport = ExecutionTransport(
            self.loop,
            bybit_socket=self.audit_bybit,
            okx_socket=self.audit_okx,
            finalize_frame=finalizer,
            monotonic_ns=self.clock,
        )
        return ExecutionEngine(
            run_id=RUN_ID, wal=self.audit_wal, transport=transport,
            plan_resolver=_resolver, instrument_cache=_cache("BTC", "ETH"),
            risk_policy=_policy("BTC", "ETH"),
            readiness=readiness or _ready(
                bybit_trade_ready=False, okx_trade_ready=False,
            ),
            ownership=self.fence, monotonic_ns=self.clock,
        )

    async def test_audits_are_wal_recorded_without_sent_or_exposure(self) -> None:
        engine = self.audit_engine()
        first = await engine.audit_intent(_intent())
        self.assertTrue(first.wal_accepted)
        self.assertTrue(first.prewrite.ready)
        self.assertIsNone(first.reason_code)
        self.assertEqual(engine.state.status, SpreadStatus.IDLE)
        self.assertEqual((self.audit_bybit.write_attempts, self.audit_okx.write_attempts), (0, 0))
        events = [item.event for item in self.audit_wal._queue]
        self.assertEqual([event.event_type for event in events], [
            ExecutionEventType.INTENT_ACCEPTED,
            ExecutionEventType.INTENT_REJECTED,
        ])
        self.assertEqual(events[-1].payload["audit_mode"], "no_order_prewrite")
        self.assertTrue(events[-1].payload["prewrite_passed"])
        with self.assertRaisesRegex(ContractValidationError, "audit marker invalid"):
            replace(events[-1], payload={
                "reason_code": "intent_rejected",
                "action": "open",
                "audit_mode": "no_order_prewrite",
            })
        self.assertEqual(first.to_public_dict()["wal_durable"], False)
        self.assertEqual(first.to_public_dict()["orders_sent"], 0)

        second = await engine.audit_intent(_intent(intent_id=INTENT_B, coin="ETH"))
        self.assertTrue(second.wal_accepted)
        self.assertEqual(engine.state.status, SpreadStatus.IDLE)
        self.audit_wal.drain_all()
        replay = self.audit_wal.replay()
        self.assertEqual(replay.state.status, SpreadStatus.IDLE)
        self.assertFalse(any(
            record.event.event_type is ExecutionEventType.REQUEST_SENT
            for record in replay.records
        ))

    async def test_trade_submit_still_requires_real_trade_readiness(self) -> None:
        engine = self.audit_engine()
        rejected = await engine.submit(_intent())
        self.assertEqual(rejected.reason_code, "trade_socket_not_ready")
        self.assertEqual((self.audit_bybit.write_attempts, self.audit_okx.write_attempts), (0, 0))
        self.assertEqual(len(self.audit_wal._queue), 0)

        engine.publish_readiness(_ready())
        rejected = await engine.submit(_intent())
        self.assertEqual(rejected.reason_code, "transport_rejected")
        self.assertEqual((self.audit_bybit.write_attempts, self.audit_okx.write_attempts), (0, 0))
        self.assertFalse(any(
            item.event.event_type is ExecutionEventType.REQUEST_SENT
            for item in self.audit_wal._queue
        ))

    async def test_private_disconnect_or_trade_ready_blocks_audit(self) -> None:
        engine = self.audit_engine(readiness=_ready(
            bybit_trade_ready=False, okx_trade_ready=False,
            okx_private_ready=False,
        ))
        rejected = await engine.audit_intent(_intent())
        self.assertEqual(rejected.reason_code, "private_stream_not_ready")
        self.assertFalse(rejected.wal_accepted)
        self.assertEqual(len(self.audit_wal._queue), 0)

        engine.publish_readiness(_ready())
        rejected = await engine.audit_intent(_intent())
        self.assertEqual(rejected.reason_code, "no_order_only")
        self.assertEqual(len(self.audit_wal._queue), 0)

    async def test_pause_blocks_audit_before_wal_or_prewrite(self) -> None:
        engine = self.audit_engine(readiness=_ready(
            bybit_trade_ready=False, okx_trade_ready=False, pause=True,
        ))
        rejected = await engine.audit_intent(_intent())
        self.assertEqual(rejected.reason_code, "pause")
        self.assertIsNone(rejected.prewrite)
        self.assertEqual(len(self.audit_wal._queue), 0)
        self.assertEqual((self.audit_bybit.write_attempts, self.audit_okx.write_attempts), (0, 0))

    async def test_wal_append_failure_never_publishes_audited_state(self) -> None:
        engine = self.audit_engine()
        with patch.object(self.audit_wal, "enqueue_batch", side_effect=WalError("writer_unhealthy")):
            result = await engine.audit_intent(_intent())
        self.assertFalse(result.wal_accepted)
        self.assertEqual(result.reason_code, "wal_capacity")
        self.assertTrue(result.prewrite.ready)
        self.assertEqual(engine.state.status, SpreadStatus.IDLE)
        self.assertEqual(len(self.audit_wal._queue), 0)
        self.assertEqual((self.audit_bybit.write_attempts, self.audit_okx.write_attempts), (0, 0))

    async def test_failed_prewrite_is_recorded_as_no_send_and_id_cannot_repeat(self) -> None:
        def broken_finalizer(*args, **kwargs):
            raise RuntimeError("finalizer unavailable")

        engine = self.audit_engine(finalizer=broken_finalizer)
        result = await engine.audit_intent(_intent())
        self.assertTrue(result.wal_accepted)
        self.assertFalse(result.prewrite.ready)
        self.assertEqual(result.reason_code, "rejected_before_write")
        self.assertEqual(engine.state.status, SpreadStatus.IDLE)
        self.assertFalse(self.audit_wal._queue[-1].event.payload["prewrite_passed"])
        self.assertEqual((self.audit_bybit.write_attempts, self.audit_okx.write_attempts), (0, 0))

        repeated = await engine.audit_intent(_intent())
        self.assertFalse(repeated.wal_accepted)
        self.assertEqual(repeated.reason_code, "opens_not_allowed")
        self.assertEqual(len(self.audit_wal._queue), 2)

    async def test_readiness_changed_during_finalization_records_no_audit(self) -> None:
        holder = {}
        calls = 0

        def finalizer(*args, **kwargs):
            nonlocal calls
            calls += 1
            text = unsigned_frame_finalizer(*args, **kwargs)
            if calls == 2:
                holder["engine"].publish_readiness(_ready(
                    bybit_trade_ready=False, okx_trade_ready=False,
                    okx_private_ready=False, okx_generation=2,
                ))
                holder["engine"].publish_readiness(_ready(
                    bybit_trade_ready=False, okx_trade_ready=False,
                    okx_generation=2,
                ))
            return text

        engine = self.audit_engine(finalizer=finalizer)
        holder["engine"] = engine
        rejected = await engine.audit_intent(_intent())
        self.assertEqual(rejected.reason_code, "readiness_changed")
        self.assertFalse(rejected.wal_accepted)
        self.assertEqual(len(self.audit_wal._queue), 0)
        self.assertEqual((self.audit_bybit.write_attempts, self.audit_okx.write_attempts), (0, 0))

    async def test_no_order_cannot_audit_close_or_real_socket(self) -> None:
        engine = self.audit_engine()
        result = await engine.audit_intent(_intent(action=IntentAction.CLOSE))
        self.assertEqual(result.reason_code, "no_order_only")
        self.assertEqual(len(self.audit_wal._queue), 0)
        self._reclaim_fence()
        real_engine = self._engine(wal=self.audit_wal)
        result = await real_engine.audit_intent(_intent())
        self.assertEqual(result.reason_code, "no_order_only")
        self.assertEqual((self.bybit.asend_calls, self.okx.asend_calls), (0, 0))


if __name__ == "__main__":
    import unittest

    unittest.main()
