"""Hermetic tests: thread-safe private WS I/O + warm keepalive vs place.

Covers the production failure mode where warm keepalive + W6 parallel place
raced ``WebsocketsClientSocket`` asyncio loops / stole trade ACK frames,
mapping to post_dispatch_ambiguity ``unknown`` within ~1ms of place send.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import io
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

    def test_close_cancels_pending_keepalive_like_tasks(self) -> None:
        """close() must cancel leftover loop tasks before loop.stop.

        Production Sentry: asyncio «Task was destroyed but it is pending!»
        on websockets Connection.keepalive / Connection.close during warm
        teardowns. send/recv path is unchanged.
        """
        try:
            import websockets
        except ImportError:
            self.skipTest("websockets not installed")

        ready = threading.Event()
        done = threading.Event()
        cancelled = threading.Event()
        hang_started = threading.Event()

        async def _echo(ws) -> None:
            async for msg in ws:
                await ws.send(msg)

        async def _serve() -> None:
            async with websockets.serve(_echo, "127.0.0.1", 0) as server:
                ready.port = server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]
                ready.set()
                await asyncio.get_running_loop().run_in_executor(None, done.wait)

        srv = threading.Thread(target=lambda: asyncio.run(_serve()), daemon=True)
        srv.start()
        self.assertTrue(ready.wait(timeout=5.0))
        from app.bot.private.ws_socket import WebsocketsClientSocket

        sock = WebsocketsClientSocket(f"ws://127.0.0.1:{int(ready.port)}")  # type: ignore[attr-defined]
        sock.connect()
        loop = sock._loop  # noqa: SLF001
        self.assertIsNotNone(loop)

        async def _hang() -> None:
            hang_started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        asyncio.run_coroutine_threadsafe(_hang(), loop)
        self.assertTrue(hang_started.wait(timeout=2.0))

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            sock.close()
            gc.collect()
        done.set()
        srv.join(timeout=2.0)

        self.assertTrue(
            cancelled.is_set(),
            "close must cancel leftover loop tasks before stopping the loop",
        )
        self.assertNotIn(
            "Task was destroyed but it is pending",
            buf.getvalue(),
        )


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


class PrivateHeartbeatSilenceTests(unittest.TestCase):
    """Private silence clock must follow trade-side heartbeat accounting."""

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

    def _provider(self):
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_warm_session import WarmSocketBundle

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

        return provider

    def test_private_heartbeat_send_refreshes_silence_clock(self) -> None:
        from app.bot.private.selftest import W2PrivateWsTests

        with tempfile.TemporaryDirectory() as td:
            journal = W2PrivateWsTests()._journal(td)
            rt, priv, _trade = W2PrivateWsTests()._runtime(journal)
            stale = time.monotonic_ns() - int(60 * 1_000_000_000)
            rt.last_recv_mono_ns = stale
            self.assertTrue(rt.silence_exceeded(silence_timeout_sec=45.0))
            rt.send_heartbeat()
            self.assertFalse(rt.silence_exceeded(silence_timeout_sec=45.0))
            self.assertGreater(rt.last_recv_mono_ns, stale)
            self.assertTrue(
                any("ping" in frame.replace(" ", "") for frame in priv.outbox),
                "private heartbeat must send an application ping",
            )

    def test_silence_still_trips_without_heartbeat_or_inbound(self) -> None:
        from app.bot.private.selftest import W2PrivateWsTests

        with tempfile.TemporaryDirectory() as td:
            journal = W2PrivateWsTests()._journal(td)
            rt, _priv, _trade = W2PrivateWsTests()._runtime(journal)
            rt.last_recv_mono_ns = time.monotonic_ns() - int(60 * 1_000_000_000)
            self.assertTrue(
                rt.silence_exceeded(silence_timeout_sec=45.0),
                "dead/idle private socket with no heartbeat must still trip silence",
            )
            self.assertFalse(
                rt.silence_exceeded(
                    silence_timeout_sec=45.0,
                    now_mono_ns=rt.last_recv_mono_ns + 1,
                )
            )

    def test_quiet_private_with_heartbeat_does_not_reconnect(self) -> None:
        """Quiet private inbox + outbound ping must not storm warm_reconnected."""
        from app.bot.private.selftest import W2PrivateWsTests
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_warm_session import start_warm_private_session

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            session = start_warm_private_session(
                env=env,
                bybit_credentials=W2PrivateWsTests()._creds(),
                okx_credentials=W2PrivateWsTests()._creds(okx=True),
                socket_provider=self._provider(),
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                attach=True,
                keepalive=True,
                poll_sec=0.05,
                heartbeat_every_sec=0.1,
                silence_timeout_sec=1.5,
                reconnect_base_sec=0.05,
                reconnect_cap_sec=0.2,
            )
            self.assertEqual(session._handshake_count, 1)  # noqa: SLF001
            time.sleep(2.0)
            self.assertTrue(session.is_ready())
            self.assertEqual(
                session._handshake_count,  # noqa: SLF001
                1,
                "quiet private + heartbeat must not false-trip silence reconnect",
            )
            session.stop()

    def test_idle_private_without_heartbeat_still_silence_reconnects(self) -> None:
        from app.bot.private.selftest import W2PrivateWsTests
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_warm_session import start_warm_private_session

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            session = start_warm_private_session(
                env=env,
                bybit_credentials=W2PrivateWsTests()._creds(),
                okx_credentials=W2PrivateWsTests()._creds(okx=True),
                socket_provider=self._provider(),
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                attach=True,
                keepalive=True,
                poll_sec=0.05,
                heartbeat_every_sec=3600.0,
                silence_timeout_sec=0.3,
                reconnect_base_sec=0.05,
                reconnect_cap_sec=0.2,
            )
            self.assertEqual(session._handshake_count, 1)  # noqa: SLF001
            deadline = time.time() + 3.0
            while (
                time.time() < deadline
                and session._handshake_count < 2  # noqa: SLF001
            ):
                time.sleep(0.05)
            self.assertGreaterEqual(
                session._handshake_count,  # noqa: SLF001
                2,
                "real idle (no heartbeat, no inbound) must still silence-reconnect",
            )
            session.stop()


def _is_app_ping_frame(frame: str) -> bool:
    return frame == "ping" or '"op":"ping"' in frame.replace(" ", "")


class WarmPostHandshakeHeartbeatTests(unittest.TestCase):
    """OKX ~30s idle close: ping immediately after handshake, log send failures."""

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

    def _make_bundle(self):
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_warm_session import WarmSocketBundle

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

    def _assert_bundle_has_immediate_pings(self, bundle, *, msg: str) -> None:
        sockets = (
            ("bybit_private", bundle.bybit_private),
            ("bybit_trade", bundle.bybit_trade),
            ("okx_private", bundle.okx_private),
            ("okx_trade", bundle.okx_trade),
        )
        for name, sock in sockets:
            pings = [f for f in sock.outbox if _is_app_ping_frame(f)]
            self.assertGreaterEqual(
                len(pings),
                1,
                f"{msg}: {name} outbox={list(sock.outbox)!r}",
            )

    def test_default_heartbeat_interval_under_okx_idle(self) -> None:
        from app.bot.private.ws_warm_session import _DEFAULT_HEARTBEAT_EVERY_SEC

        self.assertEqual(_DEFAULT_HEARTBEAT_EVERY_SEC, 10.0)
        self.assertLess(_DEFAULT_HEARTBEAT_EVERY_SEC, 30.0)

    def test_start_sends_immediate_private_and_trade_heartbeats(self) -> None:
        from app.bot.private.selftest import W2PrivateWsTests
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_warm_session import start_warm_private_session

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            bundles: list = []

            def provider():
                bundle = self._make_bundle()
                bundles.append(bundle)
                return bundle

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
            self.assertEqual(len(bundles), 1)
            self._assert_bundle_has_immediate_pings(
                bundles[0],
                msg="start/handshake must ping without waiting heartbeat_every_sec",
            )
            okx_priv_pings = [
                f for f in bundles[0].okx_private.outbox if f == "ping"
            ]
            self.assertGreaterEqual(len(okx_priv_pings), 1)
            self.assertEqual(session._handshake_count, 1)  # noqa: SLF001
            session.stop()

    def test_recover_sends_immediate_heartbeats(self) -> None:
        from app.bot.private.selftest import W2PrivateWsTests
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_warm_session import start_warm_private_session

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            bundles: list = []

            def provider():
                bundle = self._make_bundle()
                bundles.append(bundle)
                return bundle

            session = start_warm_private_session(
                env=env,
                bybit_credentials=W2PrivateWsTests()._creds(),
                okx_credentials=W2PrivateWsTests()._creds(okx=True),
                socket_provider=provider,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                attach=True,
                keepalive=False,
                heartbeat_every_sec=3600.0,
                reconnect_base_sec=0.01,
                reconnect_cap_sec=0.05,
                silence_timeout_sec=30.0,
            )
            self.assertEqual(session._handshake_count, 1)  # noqa: SLF001
            session.note_disconnect()
            self.assertFalse(session.is_ready())
            session._fail_attempt = 0  # noqa: SLF001
            session._recover_with_backoff()  # noqa: SLF001
            self.assertTrue(session.is_ready())
            self.assertEqual(session._handshake_count, 2)  # noqa: SLF001
            self.assertEqual(len(bundles), 2)
            self._assert_bundle_has_immediate_pings(
                bundles[1],
                msg="recover handshake must ping immediately",
            )
            session.stop()

    def test_ensure_ready_after_disconnect_sends_immediate_heartbeats(self) -> None:
        from app.bot.private.selftest import W2PrivateWsTests
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_warm_session import start_warm_private_session

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            bundles: list = []

            def provider():
                bundle = self._make_bundle()
                bundles.append(bundle)
                return bundle

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
            session.note_disconnect()
            session.ensure_ready()
            self.assertEqual(len(bundles), 2)
            self._assert_bundle_has_immediate_pings(
                bundles[1],
                msg="ensure_ready handshake must ping immediately",
            )
            session.stop()

    def test_keepalive_heartbeat_failure_logs_and_disconnects_that_venue(self) -> None:
        from app.bot.private.selftest import W2PrivateWsTests
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_warm_session import start_warm_private_session

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            session = start_warm_private_session(
                env=env,
                bybit_credentials=W2PrivateWsTests()._creds(),
                okx_credentials=W2PrivateWsTests()._creds(okx=True),
                socket_provider=lambda: self._make_bundle(),
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                attach=True,
                keepalive=False,
                heartbeat_every_sec=0.01,
                silence_timeout_sec=30.0,
            )
            okx_priv = session.okx_runtime.private_socket
            bybit_priv = session.bybit_runtime.private_socket
            assert isinstance(okx_priv, FakePrivateWsSocket)
            assert isinstance(bybit_priv, FakePrivateWsSocket)
            orig = okx_priv.send_text

            def boom(text: str) -> None:
                if text == "ping":
                    raise ConnectionError("okx private idle close")
                orig(text)

            okx_priv.send_text = boom  # type: ignore[method-assign]
            session._last_hb_mono = 0.0  # noqa: SLF001
            with self.assertLogs("bbot.private.ws_warm", level="WARNING") as cm:
                session._keepalive_tick()  # noqa: SLF001
            joined = "\n".join(cm.output)
            self.assertIn("warm_heartbeat_send_failed", joined)
            self.assertIn("ConnectionError", joined)
            self.assertIn("okx private idle close", joined)
            self.assertIn("exchange=okx", joined)
            self.assertIsNone(session.okx_runtime.private_socket)
            self.assertIsNotNone(session.bybit_runtime.private_socket)
            self.assertTrue(bybit_priv.connected)
            session.stop()

    def test_okx_inbound_literal_ping_replies_pong(self) -> None:
        from app.bot.private.selftest import W2PrivateWsTests

        with tempfile.TemporaryDirectory() as td:
            journal = W2PrivateWsTests()._journal(td)
            rt, priv, trade = W2PrivateWsTests()._runtime(
                journal, exchange="okx", symbol="BTC-USDT-SWAP"
            )
            parsed = rt.handle_inbound_text("ping")
            self.assertEqual(parsed.kind, "heartbeat")
            self.assertIn("pong", priv.outbox)
            self.assertNotIn("pong", trade.outbox)
            self.assertTrue(rt.consume_ws_noise("ping", trade=True))
            self.assertIn("pong", trade.outbox)

    def test_keepalive_trade_drain_pongs_okx_literal_ping(self) -> None:
        from app.bot.private.selftest import W2PrivateWsTests
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_warm_session import start_warm_private_session

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            session = start_warm_private_session(
                env=env,
                bybit_credentials=W2PrivateWsTests()._creds(),
                okx_credentials=W2PrivateWsTests()._creds(okx=True),
                socket_provider=lambda: self._make_bundle(),
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                attach=True,
                keepalive=False,
                heartbeat_every_sec=3600.0,
                silence_timeout_sec=30.0,
            )
            trade = session.okx_runtime.trade_socket
            assert isinstance(trade, FakePrivateWsSocket)
            before = list(trade.outbox)
            trade.push_inbound("ping")
            session._keepalive_tick()  # noqa: SLF001
            self.assertIn("pong", trade.outbox)
            self.assertEqual(before.count("pong"), 0)
            session.stop()


if __name__ == "__main__":
    unittest.main()
