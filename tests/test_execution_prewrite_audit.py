"""EV2-12E1: final prewrite audit is not a venue write or fill."""

from __future__ import annotations

import asyncio
import unittest

from app.bot.execution.transport import (
    DispatchResult,
    ExecutionTransport,
    NoOrderTradeSocket,
    TransportError,
    unsigned_frame_finalizer,
)
from tests.test_execution_transport import FakeSocket, _prepare


class PrewriteAuditTests(unittest.IsolatedAsyncioTestCase):
    def make_transport(self, *, clock=None, finalizer=unsigned_frame_finalizer):
        loop = asyncio.get_running_loop()
        bybit = NoOrderTradeSocket(loop)
        okx = NoOrderTradeSocket(loop)
        transport = ExecutionTransport(
            loop, bybit_socket=bybit, okx_socket=okx,
            finalize_frame=finalizer,
            monotonic_ns=clock or iter((1_500, 1_700)).__next__,
            wall_ms=lambda: 1_700_000_000_000,
        )
        return transport, bybit, okx

    async def test_audit_finalizes_both_legs_without_scheduling_asend(self) -> None:
        calls = []

        def finalizer(frame, *, timestamp_ms, request_id, client_id):
            calls.append(frame.venue.value)
            return unsigned_frame_finalizer(
                frame, timestamp_ms=timestamp_ms,
                request_id=request_id, client_id=client_id,
            )

        transport, bybit, okx = self.make_transport(finalizer=finalizer)
        guards = []

        def guard():
            guards.append(True)
            return True

        result = transport.audit_prewrite(_prepare(), pre_send_guard=guard)
        await asyncio.sleep(0)
        self.assertTrue(result.ready)
        self.assertNotIsInstance(result, DispatchResult)
        self.assertEqual(result.signal_to_prewrite_ns, 1_200)
        self.assertGreater(result.bybit_payload_bytes, 0)
        self.assertGreater(result.okx_payload_bytes, 0)
        self.assertEqual(calls, ["bybit", "okx"])
        self.assertEqual(guards, [True])
        self.assertEqual((bybit.write_attempts, okx.write_attempts), (0, 0))
        public = result.to_public_dict()
        self.assertEqual(public["orders_sent"], 0)
        self.assertFalse(public["trade_socket_bound"])
        self.assertNotIn("raw_frame", public)
        self.assertNotIn("signature", public)

    async def test_readiness_or_staleness_rejects_without_asend(self) -> None:
        transport, bybit, okx = self.make_transport()
        result = transport.audit_prewrite(_prepare(), pre_send_guard=lambda: False)
        self.assertFalse(result.ready)
        self.assertEqual(result.reason_code, "readiness_changed")
        self.assertIsNone(result.signal_to_prewrite_ns)
        self.assertEqual((bybit.write_attempts, okx.write_attempts), (0, 0))

        transport, bybit, okx = self.make_transport(clock=lambda: 20_000)
        result = transport.audit_prewrite(_prepare(), pre_send_guard=lambda: True)
        self.assertFalse(result.ready)
        self.assertEqual(result.reason_code, "stale_metadata")
        self.assertEqual((bybit.write_attempts, okx.write_attempts), (0, 0))

        transport, bybit, okx = self.make_transport()

        def broken_guard():
            raise RuntimeError("private status unavailable")

        result = transport.audit_prewrite(_prepare(), pre_send_guard=broken_guard)
        self.assertFalse(result.ready)
        self.assertEqual(result.reason_code, "readiness_changed")
        self.assertEqual((bybit.write_attempts, okx.write_attempts), (0, 0))

    async def test_finalizer_and_clock_failure_do_not_create_send_tasks(self) -> None:
        def broken_finalizer(*args, **kwargs):
            raise RuntimeError("signer unavailable")

        transport, bybit, okx = self.make_transport(finalizer=broken_finalizer)
        result = transport.audit_prewrite(_prepare(), pre_send_guard=lambda: True)
        self.assertFalse(result.ready)
        self.assertEqual(result.reason_code, "rejected_before_write")
        self.assertEqual((bybit.write_attempts, okx.write_attempts), (0, 0))

        transport, bybit, okx = self.make_transport(
            clock=iter((1_500, 1_400)).__next__,
        )
        result = transport.audit_prewrite(_prepare(), pre_send_guard=lambda: True)
        self.assertFalse(result.ready)
        self.assertEqual(result.reason_code, "clock_regression")
        self.assertEqual((bybit.write_attempts, okx.write_attempts), (0, 0))

    async def test_real_socket_and_missing_guard_cannot_claim_no_order(self) -> None:
        loop = asyncio.get_running_loop()
        transport = ExecutionTransport(
            loop, bybit_socket=FakeSocket(loop), okx_socket=FakeSocket(loop),
            finalize_frame=unsigned_frame_finalizer,
        )
        with self.assertRaisesRegex(TransportError, "invalid_socket"):
            transport.audit_prewrite(_prepare(), pre_send_guard=lambda: True)
        transport, bybit, okx = self.make_transport()
        with self.assertRaisesRegex(TransportError, "readiness_changed"):
            transport.audit_prewrite(_prepare(), pre_send_guard=None)
        self.assertEqual((bybit.write_attempts, okx.write_attempts), (0, 0))

    async def test_accidental_dispatch_on_sentinels_cannot_reach_network(self) -> None:
        transport, bybit, okx = self.make_transport(clock=lambda: 1_500)
        result = await transport.dispatch(_prepare(), pre_send_guard=lambda: True)
        self.assertNotEqual(result.status.value, "both_completed")
        self.assertEqual((bybit.write_attempts, okx.write_attempts), (1, 1))
        with self.assertRaisesRegex(TransportError, "rejected_before_write"):
            transport.audit_prewrite(_prepare(), pre_send_guard=lambda: True)


if __name__ == "__main__":
    unittest.main()
