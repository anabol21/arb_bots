"""Single-loop Contour B private warm: app ping, place vs recv, reconnect silence."""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest import mock


def _is_app_ping_frame(frame: str) -> bool:
    return frame == "ping" or '"op":"ping"' in frame.replace(" ", "")


class FakeWsCM:
    """Async context manager standing in for ``websockets.connect``."""

    def __init__(self, exchange: str, channel: str) -> None:
        self.exchange = exchange
        self.channel = channel
        self.outbox: list[str] = []
        self._q: Optional[asyncio.Queue[Optional[str]]] = None
        self.closed = False

    async def __aenter__(self) -> "FakeWsCM":
        self._q = asyncio.Queue()
        self.closed = False
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def send(self, text: str) -> None:
        if self._q is None:
            raise RuntimeError("fake ws not entered")
        self.outbox.append(text)
        if text == "ping":
            await self._q.put("pong")
            return
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        op = str(data.get("op") or "")
        if self.exchange == "bybit":
            if op == "auth":
                await self._q.put(
                    json.dumps({"op": "auth", "success": True, "retCode": 0})
                )
            elif op == "subscribe":
                await self._q.put(json.dumps({"op": "subscribe", "success": True}))
            elif op == "ping":
                await self._q.put(json.dumps({"op": "pong"}))
            return
        if op == "login":
            await self._q.put(json.dumps({"event": "login", "code": "0"}))
        elif op == "subscribe":
            await self._q.put(
                json.dumps(
                    {
                        "event": "subscribe",
                        "code": "0",
                        "arg": {"channel": "orders"},
                    }
                )
            )
        elif op == "ping":
            await self._q.put("pong")

    def __aiter__(self) -> "FakeWsCM":
        return self

    async def __anext__(self) -> str:
        if self._q is None:
            raise StopAsyncIteration
        msg = await self._q.get()
        if msg is None:
            raise StopAsyncIteration
        return msg

    async def close(self) -> None:
        self.closed = True
        if self._q is not None:
            await self._q.put(None)

    def push_from_thread(self, text: str) -> None:
        q = self._q
        loop = asyncio.get_event_loop()
        if q is None:
            raise RuntimeError("fake ws not entered")
        loop.call_soon_threadsafe(q.put_nowait, text)


class PrivateWsConnectKwargsTests(unittest.TestCase):
    def test_private_connect_disables_library_ping(self) -> None:
        from app.bot.private.ws_socket import PRIVATE_WS_CONNECT_KWARGS

        self.assertIsNone(PRIVATE_WS_CONNECT_KWARGS["ping_interval"])
        self.assertIsNone(PRIVATE_WS_CONNECT_KWARGS["ping_timeout"])
        self.assertEqual(PRIVATE_WS_CONNECT_KWARGS["max_size"], 2**20)

    def test_loop_native_connect_disables_lib_ping(self) -> None:
        from app.bot.private.ws_socket import PRIVATE_WS_CONNECT_KWARGS
        from app.bot.private.ws_warm_loop import PrivateWarmLoop

        try:
            import websockets
        except ImportError:
            self.skipTest("websockets not installed")

        captured: dict[str, Any] = {}

        class _CM:
            async def __aenter__(self) -> "_CM":
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

        def _connect(url: str, **kwargs: Any) -> _CM:
            captured.update(kwargs)
            captured["url"] = url
            return _CM()

        loop = PrivateWarmLoop()
        with mock.patch.object(websockets, "connect", side_effect=_connect):
            cm = loop._connect_cm("wss://example.test/private")  # noqa: SLF001
        self.assertIsInstance(cm, _CM)
        self.assertEqual(captured.get("url"), "wss://example.test/private")
        self.assertIsNone(captured.get("ping_interval"))
        self.assertIsNone(captured.get("ping_timeout"))
        self.assertEqual(captured.get("max_size"), PRIVATE_WS_CONNECT_KWARGS["max_size"])
        self.assertEqual(
            captured.get("close_timeout"), PRIVATE_WS_CONNECT_KWARGS["close_timeout"]
        )

    def test_compat_client_passes_ping_interval_none(self) -> None:
        try:
            import websockets  # noqa: F401
        except ImportError:
            self.skipTest("websockets not installed")

        from app.bot.private.ws_socket import (
            PRIVATE_WS_CONNECT_KWARGS,
            WebsocketsClientSocket,
        )

        captured: dict[str, Any] = {}

        class _Conn:
            async def send(self, text: str) -> None:
                del text

            async def recv(self) -> str:
                await asyncio.sleep(3600)
                return "x"

            async def close(self) -> None:
                return None

        async def _connect(url: str, **kwargs: Any) -> _Conn:
            captured.update(kwargs)
            captured["url"] = url
            return _Conn()

        sock = WebsocketsClientSocket("wss://example.test/ws")
        with mock.patch("websockets.connect", side_effect=_connect):
            sock.connect()
        sock.close()
        self.assertIsNone(captured.get("ping_interval"))
        self.assertIsNone(captured.get("ping_timeout"))
        self.assertEqual(captured.get("max_size"), PRIVATE_WS_CONNECT_KWARGS["max_size"])


class WarmConnectorFakeSessionTests(unittest.TestCase):
    """Connector works on the Fake/thread session (compat) without recv lock."""

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

    def test_connector_ready_and_send_trade(self) -> None:
        from app.bot.private.selftest import W2PrivateWsTests
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_warm_session import (
            WarmConnector,
            WarmSocketBundle,
            get_process_warm_connector,
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
                keepalive=False,
            )
            connector = WarmConnector(session)
            self.assertTrue(connector.ready())
            attached = get_process_warm_connector()
            self.assertIsNotNone(attached)
            assert attached is not None
            self.assertTrue(attached.ready())
            connector.send_trade("bybit", '{"op":"order.create","reqId":"x"}')
            trade = session.bybit_runtime.trade_socket
            assert isinstance(trade, FakePrivateWsSocket)
            self.assertTrue(
                any("order.create" in frame for frame in trade.outbox),
                trade.outbox,
            )
            session.stop()


class SingleLoopWarmTests(unittest.TestCase):
    def tearDown(self) -> None:
        from app.bot.private.ws_warm_loop import clear_process_warm_loop
        from app.bot.private.ws_warm_session import clear_process_warm_session

        clear_process_warm_session(stop=True)
        clear_process_warm_loop()

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

    def _factory(self) -> tuple[Any, dict[str, list[FakeWsCM]], list[dict[str, Any]]]:
        from app.bot.private.ws_socket import PRIVATE_WS_CONNECT_KWARGS
        from app.bot.private.ws_warm_loop import PrivateWarmLoop

        cms: dict[str, list[FakeWsCM]] = {}
        kwargs_seen: list[dict[str, Any]] = []

        def connect_cm_fn(url: str) -> FakeWsCM:
            kwargs_seen.append(dict(PRIVATE_WS_CONNECT_KWARGS))
            body = url.split("://", 1)[-1]
            exchange, _, channel = body.partition("/")
            cm = FakeWsCM(exchange=exchange, channel=channel or "private")
            cms.setdefault(url, []).append(cm)
            return cm

        loop = PrivateWarmLoop(
            connect_cm_fn=connect_cm_fn,
            heartbeat_every_sec=0.15,
            silence_timeout_sec=2.0,
            reconnect_base_sec=0.05,
            reconnect_cap_sec=0.2,
        )
        loop.start()
        return loop, cms, kwargs_seen

    def _provider(self, warm_loop: Any):
        from app.bot.private.ws_warm_session import WarmSocketBundle

        def provider() -> WarmSocketBundle:
            return WarmSocketBundle(
                bybit_private=warm_loop.open(
                    "ws://bybit/private", exchange="bybit", channel="private"
                ),
                bybit_trade=warm_loop.open(
                    "ws://bybit/trade", exchange="bybit", channel="trade"
                ),
                okx_private=warm_loop.open(
                    "ws://okx/private", exchange="okx", channel="private"
                ),
                okx_trade=warm_loop.open(
                    "ws://okx/trade", exchange="okx", channel="trade"
                ),
            )

        return provider

    def test_single_loop_warm_ready_and_app_ping_kwargs(self) -> None:
        from app.bot.private.selftest import W2PrivateWsTests
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_warm_loop import is_loop_owned_socket
        from app.bot.private.ws_warm_session import start_warm_private_session

        warm_loop, cms, kwargs_seen = self._factory()
        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            session = start_warm_private_session(
                env=env,
                bybit_credentials=W2PrivateWsTests()._creds(),
                okx_credentials=W2PrivateWsTests()._creds(okx=True),
                socket_provider=self._provider(warm_loop),
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                attach=True,
                keepalive=True,
                heartbeat_every_sec=0.15,
                silence_timeout_sec=2.0,
                reconnect_base_sec=0.05,
                reconnect_cap_sec=0.2,
            )
            self.assertTrue(session.is_ready())
            self.assertTrue(session.keepalive_running)
            self.assertEqual(session._handshake_count, 1)  # noqa: SLF001
            self.assertTrue(
                is_loop_owned_socket(session.bybit_runtime.private_socket)
            )
            self.assertTrue(kwargs_seen)
            for kw in kwargs_seen:
                self.assertIsNone(kw.get("ping_interval"))
                self.assertIsNone(kw.get("ping_timeout"))
            okx_priv = cms["ws://okx/private"][0]
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not any(
                f == "ping" for f in okx_priv.outbox
            ):
                time.sleep(0.05)
            self.assertTrue(
                any(f == "ping" for f in okx_priv.outbox),
                f"okx private outbox={okx_priv.outbox!r}",
            )
            session.stop()

    def test_place_send_not_blocked_by_recv(self) -> None:
        from app.bot.private.selftest import W2PrivateWsTests
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_warm_session import (
            WarmConnector,
            start_warm_private_session,
        )

        warm_loop, cms, _ = self._factory()
        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            session = start_warm_private_session(
                env=env,
                bybit_credentials=W2PrivateWsTests()._creds(),
                okx_credentials=W2PrivateWsTests()._creds(okx=True),
                socket_provider=self._provider(warm_loop),
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                attach=True,
                keepalive=True,
                heartbeat_every_sec=3600.0,
                silence_timeout_sec=30.0,
            )
            connector = WarmConnector(session)
            self.assertTrue(connector.ready())
            # Listen is blocked in async for (empty inbound). Place send must
            # still complete in milliseconds — no recv lock.
            waits: list[float] = []

            def _place() -> None:
                t0 = time.monotonic()
                with connector.place_io_section():
                    connector.send_trade(
                        "bybit", '{"op":"order.create","reqId":"loop1"}'
                    )
                waits.append(time.monotonic() - t0)

            worker = threading.Thread(target=_place, daemon=True)
            worker.start()
            worker.join(timeout=2.0)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(waits), 1)
            self.assertLess(
                waits[0],
                0.05,
                f"place send waited {waits[0]*1000:.1f}ms behind recv",
            )
            trade_cm = cms["ws://bybit/trade"][0]
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and not any(
                "order.create" in f for f in trade_cm.outbox
            ):
                time.sleep(0.01)
            self.assertTrue(
                any("order.create" in f for f in trade_cm.outbox),
                trade_cm.outbox,
            )
            session.stop()

    def test_okx_literal_ping_replies_pong_on_loop(self) -> None:
        from app.bot.private.selftest import W2PrivateWsTests
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_warm_session import start_warm_private_session

        warm_loop, cms, _ = self._factory()
        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            session = start_warm_private_session(
                env=env,
                bybit_credentials=W2PrivateWsTests()._creds(),
                okx_credentials=W2PrivateWsTests()._creds(okx=True),
                socket_provider=self._provider(warm_loop),
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                attach=True,
                keepalive=True,
                heartbeat_every_sec=3600.0,
                silence_timeout_sec=30.0,
            )
            cm = cms["ws://okx/private"][0]
            before = cm.outbox.count("pong")
            loop = warm_loop.loop
            assert loop is not None
            asyncio.run_coroutine_threadsafe(cm._q.put("ping"), loop).result(  # noqa: SLF001
                timeout=2.0
            )
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and cm.outbox.count("pong") <= before:
                time.sleep(0.05)
            self.assertGreater(cm.outbox.count("pong"), before, cm.outbox)
            session.stop()

    def test_reconnect_does_not_false_trip_silence(self) -> None:
        from app.bot.private.selftest import W2PrivateWsTests
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_warm_session import start_warm_private_session

        warm_loop, _cms, _ = self._factory()
        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            session = start_warm_private_session(
                env=env,
                bybit_credentials=W2PrivateWsTests()._creds(),
                okx_credentials=W2PrivateWsTests()._creds(okx=True),
                socket_provider=self._provider(warm_loop),
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                attach=True,
                keepalive=True,
                heartbeat_every_sec=0.1,
                silence_timeout_sec=0.6,
                reconnect_base_sec=0.05,
                reconnect_cap_sec=0.2,
            )
            self.assertEqual(session._handshake_count, 1)  # noqa: SLF001
            session.note_disconnect(exchange="okx")
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not session.is_ready():
                time.sleep(0.05)
            self.assertTrue(session.is_ready(), "loop must re-handshake after drop")
            hs_after = session._handshake_count  # noqa: SLF001
            self.assertGreaterEqual(hs_after, 2)
            time.sleep(0.9)
            self.assertTrue(session.is_ready())
            self.assertEqual(
                session._handshake_count,  # noqa: SLF001
                hs_after,
                "reconnect must not inherit old silence clock and storm",
            )
            session.stop()


if __name__ == "__main__":
    unittest.main()
