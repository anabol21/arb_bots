"""Hermetic tests: thread-safe private WS I/O + warm keepalive vs place.

Covers the production failure mode where warm keepalive + W6 parallel place
raced ``WebsocketsClientSocket`` asyncio loops / stole trade ACK frames,
mapping to post_dispatch_ambiguity ``unknown`` within ~1ms of place send.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path


class WebsocketsClientSocketThreadSafeTests(unittest.TestCase):
    """Local echo server: connect on one thread, place I/O on workers."""

    def test_cross_thread_send_recv_via_owner_loop(self) -> None:
        try:
            import websockets
        except ImportError:
            self.skipTest("websockets not installed")

        received: list[str] = []
        errors: list[BaseException] = []
        ready = threading.Event()
        done = threading.Event()

        async def _echo(ws) -> None:
            async for msg in ws:
                await ws.send(msg)

        async def _serve() -> None:
            async with websockets.serve(_echo, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                ready.port = port  # type: ignore[attr-defined]
                ready.set()
                await asyncio.get_running_loop().run_in_executor(None, done.wait)

        def _server_thread() -> None:
            asyncio.run(_serve())

        srv = threading.Thread(target=_server_thread, daemon=True)
        srv.start()
        self.assertTrue(ready.wait(timeout=5.0))
        port = int(ready.port)  # type: ignore[attr-defined]

        from app.bot.private.ws_socket import WebsocketsClientSocket

        # Connect on "warm/keepalive" thread.
        sock = WebsocketsClientSocket(f"ws://127.0.0.1:{port}")
        sock.connect()
        self.assertTrue(sock.connected)

        def worker(n: int) -> None:
            try:
                payload = f"ping-{n}"
                sock.send_text(payload)
                got = sock.recv_text(timeout_sec=2.0)
                received.append(got)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        sock.close()
        done.set()
        srv.join(timeout=2.0)

        self.assertEqual(errors, [], msg=repr(errors))
        self.assertEqual(sorted(received), [f"ping-{i}" for i in range(4)])

    def test_recv_timeout_maps_to_timeout_error(self) -> None:
        try:
            import websockets
        except ImportError:
            self.skipTest("websockets not installed")

        ready = threading.Event()
        done = threading.Event()

        async def _hold(ws) -> None:
            # Never send; client recv must time out.
            await asyncio.get_running_loop().run_in_executor(None, done.wait)
            await ws.close()

        async def _serve() -> None:
            async with websockets.serve(_hold, "127.0.0.1", 0) as server:
                ready.port = server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]
                ready.set()
                await asyncio.get_running_loop().run_in_executor(None, done.wait)

        srv = threading.Thread(target=lambda: asyncio.run(_serve()), daemon=True)
        srv.start()
        self.assertTrue(ready.wait(timeout=5.0))
        from app.bot.private.ws_socket import WebsocketsClientSocket

        sock = WebsocketsClientSocket(f"ws://127.0.0.1:{int(ready.port)}")  # type: ignore[attr-defined]
        sock.connect()
        with self.assertRaises(TimeoutError):
            sock.recv_text(timeout_sec=0.2)
        sock.close()
        done.set()
        srv.join(timeout=2.0)


class WarmPlaceIoGuardTests(unittest.TestCase):
    """Keepalive must not steal trade ACK while place_io_section is held."""

    def tearDown(self) -> None:
        from app.bot.private.ws_warm_session import clear_process_warm_session

        clear_process_warm_session(stop=True)

    def _live_env(self, td: str) -> dict:
        from app.bot.private.secrets import LIVE_KEY_NAMES

        live_env = Path(td) / "bbot-private-live.env"
        live_env.write_text(
            "\n".join(f"{n}=v{i}" for i, n in enumerate(LIVE_KEY_NAMES)) + "\n",
            encoding="utf-8",
        )
        return {
            "VENUE": "live",
            "LIVE_ORDERS": "1",
            "BBOT_PRIVATE_ENV_FILE": str(live_env),
            "BBOT_PRIVATE_DATA_ROOT": str(Path(td) / "data"),
        }

    def _push_hs(self, priv, trade, *, okx: bool) -> None:
        if okx:
            priv.push_inbound(json.dumps({"event": "login", "code": "0"}))
            priv.push_inbound(
                json.dumps(
                    {"event": "subscribe", "code": "0", "arg": {"channel": "orders"}}
                )
            )
            trade.push_inbound(json.dumps({"event": "login", "code": "0"}))
        else:
            priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            trade.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))

    def test_place_io_pauses_keepalive_and_preserves_trade_ack(self) -> None:
        from app.bot.private.selftest import W2PrivateWsTests
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_warm_session import (
            WarmSocketBundle,
            start_warm_private_session,
        )

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)

            def provider() -> WarmSocketBundle:
                bpriv = FakePrivateWsSocket()
                btrade = FakePrivateWsSocket(exchange="bybit")
                opriv = FakePrivateWsSocket()
                otrade = FakePrivateWsSocket(exchange="okx")
                self._push_hs(bpriv, btrade, okx=False)
                self._push_hs(opriv, otrade, okx=True)
                return WarmSocketBundle(
                    bybit_private=bpriv,
                    bybit_trade=btrade,
                    okx_private=opriv,
                    okx_trade=otrade,
                )

            session = start_warm_private_session(
                env=env,
                bybit_credentials=W2PrivateWsTests()._creds(),
                okx_credentials=W2PrivateWsTests()._creds(okx=True),
                socket_provider=provider,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                attach=True,
                keepalive=True,
                poll_sec=0.05,
                heartbeat_every_sec=0.05,
                silence_timeout_sec=30.0,
            )
            self.assertTrue(session.keepalive_running)
            trade = session.bybit_runtime.trade_socket
            assert isinstance(trade, FakePrivateWsSocket)

            ack = json.dumps(
                {"reqId": "req_place_1", "op": "order.create", "retCode": 0, "success": True}
            )
            with session.place_io_section():
                self.assertTrue(session.place_inflight)
                # Would be stolen by keepalive drain without the guard.
                trade.push_inbound(ack)
                # Give keepalive several poll intervals while place is held.
                time.sleep(0.25)
                self.assertEqual(list(trade._inbox), [ack])  # noqa: SLF001
                # Disconnect must not tear sockets down mid-place.
                gen = session.bybit_runtime.reconnect_generation
                session.note_disconnect()
                self.assertEqual(session.bybit_runtime.reconnect_generation, gen)
                self.assertTrue(session.is_ready())
                got = trade.recv_text(timeout_sec=0.1)
                self.assertEqual(got, ack)

            self.assertFalse(session.place_inflight)
            session.stop()

    def test_keepalive_tick_does_not_hold_lock_during_blocking_recv(self) -> None:
        """place_io_section must enter while keepalive is blocked in recv_text.

        Old design held ``_lock`` across ``recv_text`` (0.2s × sockets), so
        Contour B waited ~500ms to bump ``_place_inflight`` before ws.send.
        """
        from app.bot.private.selftest import W2PrivateWsTests
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_warm_session import (
            WarmSocketBundle,
            start_warm_private_session,
        )

        class _HoldRecvSocket:
            """Delegates to Fake except recv, which blocks until released."""

            def __init__(
                self,
                inner: FakePrivateWsSocket,
                entered: threading.Event,
                release: threading.Event,
            ) -> None:
                self._inner = inner
                self._entered = entered
                self._release = release

            def recv_text(self, *, timeout_sec=None) -> str:
                del timeout_sec
                self._entered.set()
                if not self._release.wait(timeout=5.0):
                    raise TimeoutError("hold-recv not released")
                raise TimeoutError("hold-recv released empty")

            def send_text(self, text: str) -> None:
                self._inner.send_text(text)

            def close(self) -> None:
                self._inner.close()

            def connect(self) -> None:
                self._inner.connect()

            @property
            def connected(self) -> bool:
                return self._inner.connected

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)

            def provider() -> WarmSocketBundle:
                bpriv = FakePrivateWsSocket()
                btrade = FakePrivateWsSocket(exchange="bybit")
                opriv = FakePrivateWsSocket()
                otrade = FakePrivateWsSocket(exchange="okx")
                self._push_hs(bpriv, btrade, okx=False)
                self._push_hs(opriv, otrade, okx=True)
                return WarmSocketBundle(
                    bybit_private=bpriv,
                    bybit_trade=btrade,
                    okx_private=opriv,
                    okx_trade=otrade,
                )

            session = start_warm_private_session(
                env=env,
                bybit_credentials=W2PrivateWsTests()._creds(),
                okx_credentials=W2PrivateWsTests()._creds(okx=True),
                socket_provider=provider,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                attach=True,
                keepalive=False,
                heartbeat_every_sec=3600.0,
                silence_timeout_sec=30.0,
            )
            entered_recv = threading.Event()
            release_recv = threading.Event()
            inner = session.bybit_runtime.private_socket
            assert isinstance(inner, FakePrivateWsSocket)
            session.bybit_runtime.private_socket = _HoldRecvSocket(
                inner, entered_recv, release_recv
            )
            trade = session.bybit_runtime.trade_socket
            assert isinstance(trade, FakePrivateWsSocket)
            ack = json.dumps(
                {
                    "reqId": "req_place_hold",
                    "op": "order.create",
                    "retCode": 0,
                    "success": True,
                }
            )

            tick_done = threading.Event()
            tick_err: list[BaseException] = []

            def _tick() -> None:
                try:
                    session._keepalive_tick()  # noqa: SLF001
                except BaseException as exc:  # noqa: BLE001
                    tick_err.append(exc)
                finally:
                    tick_done.set()

            ticker = threading.Thread(target=_tick, name="keepalive-tick", daemon=True)
            ticker.start()
            self.assertTrue(
                entered_recv.wait(timeout=2.0),
                "keepalive tick must reach recv_text",
            )

            place_wait_sec: list[float] = []
            place_err: list[BaseException] = []

            def _place() -> None:
                try:
                    t0 = time.monotonic()
                    with session.place_io_section():
                        place_wait_sec.append(time.monotonic() - t0)
                        trade.push_inbound(ack)
                        release_recv.set()
                        self.assertTrue(tick_done.wait(timeout=2.0))
                        # Tick must yield after private recv and not steal trade ACK.
                        self.assertEqual(list(trade._inbox), [ack])  # noqa: SLF001
                except BaseException as exc:  # noqa: BLE001
                    place_err.append(exc)
                    release_recv.set()

            placer = threading.Thread(target=_place, name="place-io", daemon=True)
            placer.start()
            placer.join(timeout=1.0)
            if placer.is_alive():
                release_recv.set()
                ticker.join(timeout=2.0)
                placer.join(timeout=2.0)
                self.fail(
                    "place_io_section stayed blocked while keepalive recv held "
                    "(old lock-across-recv design)"
                )
            ticker.join(timeout=2.0)
            self.assertEqual(tick_err, [], msg=repr(tick_err))
            self.assertEqual(place_err, [], msg=repr(place_err))
            self.assertEqual(len(place_wait_sec), 1)
            self.assertLess(
                place_wait_sec[0],
                0.05,
                f"place_io_section waited {place_wait_sec[0]*1000:.1f}ms for keepalive lock",
            )
            session.stop()

    def test_trade_heartbeat_sent_on_keepalive_tick(self) -> None:
        from app.bot.private.selftest import W2PrivateWsTests
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_warm_session import (
            WarmSocketBundle,
            start_warm_private_session,
        )

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)

            def provider() -> WarmSocketBundle:
                bpriv = FakePrivateWsSocket()
                btrade = FakePrivateWsSocket(exchange="bybit")
                opriv = FakePrivateWsSocket()
                otrade = FakePrivateWsSocket(exchange="okx")
                self._push_hs(bpriv, btrade, okx=False)
                self._push_hs(opriv, otrade, okx=True)
                return WarmSocketBundle(
                    bybit_private=bpriv,
                    bybit_trade=btrade,
                    okx_private=opriv,
                    okx_trade=otrade,
                )

            session = start_warm_private_session(
                env=env,
                bybit_credentials=W2PrivateWsTests()._creds(),
                okx_credentials=W2PrivateWsTests()._creds(okx=True),
                socket_provider=provider,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                attach=True,
                keepalive=True,
                poll_sec=0.05,
                heartbeat_every_sec=0.05,
                silence_timeout_sec=30.0,
            )
            deadline = time.time() + 2.0
            saw_trade_ping = False
            while time.time() < deadline and not saw_trade_ping:
                for rt in (session.bybit_runtime, session.okx_runtime):
                    sock = rt.trade_socket
                    assert isinstance(sock, FakePrivateWsSocket)
                    for frame in sock.outbox:
                        if frame == "ping" or '"op":"ping"' in frame.replace(" ", ""):
                            saw_trade_ping = True
                            break
                time.sleep(0.05)
            self.assertTrue(
                saw_trade_ping, "keepalive must ping trade sockets, not only private"
            )
            session.stop()

    def test_keepalive_stashes_non_noise_trade_frame(self) -> None:
        from app.bot.private.selftest import W2PrivateWsTests
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_warm_session import (
            WarmSocketBundle,
            start_warm_private_session,
        )

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)

            def provider() -> WarmSocketBundle:
                bpriv = FakePrivateWsSocket()
                btrade = FakePrivateWsSocket(exchange="bybit")
                opriv = FakePrivateWsSocket()
                otrade = FakePrivateWsSocket(exchange="okx")
                self._push_hs(bpriv, btrade, okx=False)
                self._push_hs(opriv, otrade, okx=True)
                return WarmSocketBundle(
                    bybit_private=bpriv,
                    bybit_trade=btrade,
                    okx_private=opriv,
                    okx_trade=otrade,
                )

            session = start_warm_private_session(
                env=env,
                bybit_credentials=W2PrivateWsTests()._creds(),
                okx_credentials=W2PrivateWsTests()._creds(okx=True),
                socket_provider=provider,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                attach=True,
                keepalive=True,
                poll_sec=0.05,
                heartbeat_every_sec=60.0,
                silence_timeout_sec=30.0,
            )
            trade = session.bybit_runtime.trade_socket
            assert isinstance(trade, FakePrivateWsSocket)
            ack = json.dumps(
                {"reqId": "late", "op": "order.create", "retCode": 0, "success": True}
            )
            trade.push_inbound(ack)
            deadline = time.time() + 2.0
            while time.time() < deadline and not session.bybit_runtime._trade_inbound_stash:  # noqa: SLF001
                time.sleep(0.05)
            self.assertEqual(session.bybit_runtime._trade_inbound_stash, [ack])  # noqa: SLF001
            # Place/ack path must see stashed frame first.
            got = session.bybit_runtime.recv_trade_ack(
                expect_req_id="late", timeout_sec=0.5
            )
            self.assertTrue(got.accepted)
            self.assertEqual(session.bybit_runtime._trade_inbound_stash, [])  # noqa: SLF001
            session.stop()


if __name__ == "__main__":
    unittest.main()
