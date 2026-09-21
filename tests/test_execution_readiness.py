"""EV2-09D warm-session readiness publisher and reconnect fault matrix."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any, Callable, Optional

from app.bot.execution.engine import DualReadinessFence, ReadinessSnapshot
from app.bot.execution.readiness import (
    WarmSessionReadinessBridge,
    snapshot_from_warm_session,
)
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.ws_private import PrivateStreamRuntime


class FakeSocket:
    def __init__(
        self, connected: bool = False, frames: Optional[list[str]] = None
    ) -> None:
        self.connected = connected
        self._frames = list(frames or [])

    def recv_text(self, *, timeout_sec: Optional[float] = None) -> str:
        del timeout_sec
        if not self._frames:
            raise TimeoutError("empty")
        return self._frames.pop(0)


class FakeRuntime:
    def __init__(self) -> None:
        self.private_socket = FakeSocket()
        self.trade_socket = FakeSocket()
        self.authenticated = False
        self.trade_authenticated = False
        self.subscription_readiness = "not_ready"
        self.sequence_state = "reseed_required"
        self._sends_blocked = True
        self.reconnect_generation = 0
        self.listeners: list[Callable[[], None]] = []

    @property
    def sends_blocked(self) -> bool:
        return self._sends_blocked

    @property
    def reseed_required(self) -> bool:
        return self.sequence_state != "healthy"

    def add_readiness_listener(self, listener: Callable[[], None]) -> None:
        if listener not in self.listeners:
            self.listeners.append(listener)

    def remove_readiness_listener(self, listener: Callable[[], None]) -> None:
        if listener in self.listeners:
            self.listeners.remove(listener)

    def fire(self) -> None:
        for listener in tuple(self.listeners):
            listener()

    def healthy(self) -> None:
        self.private_socket.connected = True
        self.trade_socket.connected = True
        self.authenticated = True
        self.trade_authenticated = True
        self.subscription_readiness = "ready"
        self.sequence_state = "healthy"
        self._sends_blocked = False
        self.fire()


class FakeJournal:
    run_id = "run_readiness_test"

    def append(self, body: dict[str, Any]) -> dict[str, Any]:
        return dict(body)


def _session() -> SimpleNamespace:
    return SimpleNamespace(bybit_runtime=FakeRuntime(), okx_runtime=FakeRuntime())


class SnapshotTests(unittest.TestCase):
    def test_snapshot_requires_physical_protocol_and_reseed_readiness(self) -> None:
        session = _session()
        first = snapshot_from_warm_session(session)
        self.assertFalse(first.bybit_trade_ready)
        self.assertFalse(first.bybit_private_ready)

        session.bybit_runtime.private_socket.connected = True
        session.bybit_runtime.trade_socket.connected = True
        session.bybit_runtime.authenticated = True
        session.bybit_runtime.trade_authenticated = True
        session.bybit_runtime.subscription_readiness = "ready"
        session.bybit_runtime.sequence_state = "healthy"
        session.bybit_runtime._sends_blocked = True
        blocked = snapshot_from_warm_session(session)
        self.assertTrue(blocked.bybit_trade_ready)
        self.assertFalse(blocked.bybit_private_ready)

        session.bybit_runtime._sends_blocked = False
        ready = snapshot_from_warm_session(session)
        self.assertTrue(ready.bybit_trade_ready)
        self.assertTrue(ready.bybit_private_ready)

    def test_missing_or_malformed_runtime_fails_closed(self) -> None:
        snapshot = snapshot_from_warm_session(SimpleNamespace())
        self.assertEqual(
            snapshot,
            ReadinessSnapshot(
                bybit_trade_ready=False,
                okx_trade_ready=False,
                bybit_private_ready=False,
                okx_private_ready=False,
                bybit_generation=0,
                okx_generation=0,
                kill_switch=False,
                pause=False,
            ),
        )


class BridgeFaultMatrixTests(unittest.TestCase):
    def test_disconnect_reconnect_stages_and_generation_invalidate_old_lease(self) -> None:
        session = _session()
        initial = snapshot_from_warm_session(session)
        fence = DualReadinessFence(initial)
        bridge = WarmSessionReadinessBridge(session, fence.publish)
        try:
            session.bybit_runtime.healthy()
            session.okx_runtime.healthy()
            lease, reason = fence.acquire(require_controls=True)
            assert lease is not None
            self.assertIsNone(reason)

            # Physical trade drop closes the gate before protocol state mutates.
            session.okx_runtime.trade_socket.connected = False
            session.okx_runtime.fire()
            self.assertFalse(fence.validate(lease))
            blocked, reason = fence.acquire(require_controls=True)
            self.assertIsNone(blocked)
            self.assertEqual(reason, "trade_socket_not_ready")

            # Reconnect generation: sockets up alone are insufficient.
            okx = session.okx_runtime
            okx.reconnect_generation += 1
            okx.private_socket.connected = True
            okx.trade_socket.connected = True
            okx.authenticated = False
            okx.trade_authenticated = False
            okx.subscription_readiness = "not_ready"
            okx.sequence_state = "reseed_required"
            okx._sends_blocked = True
            okx.fire()
            self.assertEqual(
                fence.acquire(require_controls=True)[1], "trade_socket_not_ready"
            )

            # Trade auth alone still cannot bypass private reseed.
            okx.trade_authenticated = True
            okx.fire()
            self.assertEqual(
                fence.acquire(require_controls=True)[1], "private_stream_not_ready"
            )

            okx.authenticated = True
            okx.subscription_readiness = "ready"
            okx.sequence_state = "healthy"
            okx._sends_blocked = False
            okx.fire()
            replacement, reason = fence.acquire(require_controls=True)
            assert replacement is not None
            self.assertIsNone(reason)
            self.assertTrue(fence.validate(replacement))
            self.assertFalse(fence.validate(lease))
        finally:
            bridge.close()

    def test_private_gap_closes_gate_and_identical_refresh_does_not_churn(self) -> None:
        session = _session()
        session.bybit_runtime.healthy()
        session.okx_runtime.healthy()
        fence = DualReadinessFence(snapshot_from_warm_session(session))
        bridge = WarmSessionReadinessBridge(session, fence.publish)
        try:
            revision = fence.revision
            session.bybit_runtime.fire()
            self.assertEqual(fence.revision, revision)

            session.bybit_runtime.sequence_state = "gap"
            session.bybit_runtime._sends_blocked = True
            session.bybit_runtime.fire()
            self.assertEqual(
                fence.acquire(require_controls=True)[1], "private_stream_not_ready"
            )
        finally:
            bridge.close()
        self.assertEqual(session.bybit_runtime.listeners, [])
        self.assertEqual(session.okx_runtime.listeners, [])


class RuntimeNotificationTests(unittest.TestCase):
    def _runtime(self) -> PrivateStreamRuntime:
        return PrivateStreamRuntime(
            exchange="bybit",
            environment="live",
            symbol_alias="BTCUSDT",
            journal=FakeJournal(),  # type: ignore[arg-type]
            run_id=FakeJournal.run_id,
            credentials=LiveCredentials(api_key="key", api_secret="secret"),
        )

    def test_trade_auth_and_reconnect_notify_and_reset(self) -> None:
        runtime = self._runtime()
        notifications = 0

        def observed() -> None:
            nonlocal notifications
            notifications += 1

        runtime.add_readiness_listener(observed)
        runtime.trade_socket = FakeSocket(
            connected=True,
            frames=['{"op":"auth","success":true}'],
        )  # type: ignore[assignment]
        self.assertTrue(runtime.recv_trade_auth_ack(timeout_sec=0.1))
        self.assertTrue(runtime.trade_authenticated)
        self.assertGreaterEqual(notifications, 1)

        before = notifications
        runtime.mark_reconnect()
        self.assertFalse(runtime.trade_authenticated)
        self.assertEqual(runtime.reconnect_generation, 1)
        self.assertGreater(notifications, before)


class OrderedLoopPublicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_publishes_latest_state_on_owner_loop(self) -> None:
        session = _session()
        published: list[ReadinessSnapshot] = []
        bridge = WarmSessionReadinessBridge(
            session,
            published.append,
            owner_loop=asyncio.get_running_loop(),
        )
        try:
            session.bybit_runtime.healthy()
            session.okx_runtime.healthy()
            await asyncio.sleep(0)
            self.assertTrue(published[-1].bybit_private_ready)
            self.assertTrue(published[-1].okx_private_ready)

            session.bybit_runtime.private_socket.connected = False
            session.bybit_runtime.fire()
            session.bybit_runtime.reconnect_generation += 1
            session.bybit_runtime.private_socket.connected = True
            session.bybit_runtime.authenticated = False
            session.bybit_runtime.fire()
            await asyncio.sleep(0)
            self.assertEqual(published[-1].bybit_generation, 1)
            self.assertFalse(published[-1].bybit_private_ready)
        finally:
            bridge.close()


if __name__ == "__main__":
    unittest.main()
