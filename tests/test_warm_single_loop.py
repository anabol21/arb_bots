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
        self.subscribe_ack_code = "0"
        self.auto_subscribe_ack = True
        self.reverse_subscribe_ack = False

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
            if not self.auto_subscribe_ack:
                return
            args = list(data.get("args", []))
            if self.reverse_subscribe_ack:
                args.reverse()
            for arg in args:
                await self._q.put(json.dumps({
                    "event": "subscribe",
                    "id": data.get("id"),
                    "code": self.subscribe_ack_code,
                    "arg": arg,
                }))
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
            self.assertIs(
                getattr(session.okx_runtime.private_socket, "_wire_transcript", None),
                session.wire,
            )
            self.assertTrue(session.wire.healthy)
            from app.bot.private.wire_transcript import scan_all_wire_events

            intent_id = "capture-owner-intent"
            client_order_id = "o_capture_owner_000000000000000000000000000000"
            session.wire.bind_place_correlation(
                req_id=client_order_id,
                intent_id=intent_id,
                venue="okx",
                phase="open",
            )
            owner_loop = warm_loop.loop
            okx_priv = cms["ws://okx/private"][0]
            self.assertIsNotNone(owner_loop)
            self.assertIsNotNone(okx_priv._q)
            owner_loop.call_soon_threadsafe(
                okx_priv._q.put_nowait,
                json.dumps(
                    {
                        "arg": {"channel": "orders", "instId": "SOL-USDT-SWAP"},
                        "data": [
                            {
                                "clOrdId": client_order_id,
                                "state": "filled",
                                "accFillSz": "0.1",
                                "avgPx": "120.88",
                                "fillTime": "1700000001000",
                            }
                        ],
                    }
                ),
            )
            deadline = time.monotonic() + 2.0
            owner_events = []
            while time.monotonic() < deadline:
                owner_events = [
                    event
                    for event in scan_all_wire_events(Path(env["BBOT_PRIVATE_DATA_ROOT"]))
                    if event.get("capture_stage") == "socket_arrival"
                    and event.get("intent_id") == intent_id
                ]
                if owner_events:
                    break
                time.sleep(0.02)
            self.assertEqual(len(owner_events), 1)
            self.assertEqual(owner_events[0]["venue"], "okx")
            self.assertEqual(owner_events[0]["socket"], "private")
            self.assertEqual(owner_events[0]["phase"], "open")
            self.assertIsNotNone(owner_events[0].get("reconnect_generation"))
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


    def test_startup_okx_ack_burst_is_drained_at_handshake_handoff(self) -> None:
        from app.bot.private.selftest import W2PrivateWsTests
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_warm_session import start_warm_private_session

        warm_loop, _cms, _ = self._factory()
        coins = ("BTC", "ETH", "SOL")
        okx_symbols = tuple(f"{coin}-USDT-SWAP" for coin in coins)
        bybit_symbols = tuple(f"{coin}USDT" for coin in coins)
        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            session = start_warm_private_session(
                env=env,
                bybit_credentials=W2PrivateWsTests()._creds(),
                okx_credentials=W2PrivateWsTests()._creds(okx=True),
                socket_provider=self._provider(warm_loop),
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                coins=coins,
                bybit_symbols=bybit_symbols,
                okx_symbols=okx_symbols,
                attach=True,
                keepalive=True,
                heartbeat_every_sec=0.15,
                silence_timeout_sec=2.0,
            )
            self.assertTrue(session.is_ready())
            self.assertTrue(
                all(
                    session.okx_runtime.okx_symbol_ready(symbol)
                    for symbol in okx_symbols
                ),
                "all startup orders/positions ACKs must survive the handshake queue handoff",
            )
            self.assertEqual(
                len(session.okx_runtime._symbol_subscription_acks), 6  # noqa: SLF001
            )
            session.stop()

    def test_added_okx_coin_ack_is_instrument_scoped_on_owner_loop(self) -> None:
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
            self.assertTrue(session.is_ready())
            cm.subscribe_ack_code = "60000"
            asyncio.run(
                asyncio.to_thread(
                    session.add_coin,
                    "NEW",
                    bybit_symbol="NEWUSDT",
                    okx_symbol="NEW-USDT-SWAP",
                )
            )
            request = next(
                json.loads(frame)
                for frame in reversed(cm.outbox)
                if isinstance(frame, str)
                and frame.startswith("{")
                and json.loads(frame).get("op") == "subscribe"
                and any(a.get("instId") == "NEW-USDT-SWAP" for a in json.loads(frame).get("args", []))
            )
            self.assertTrue(request.get("id"))
            self.assertTrue(session.is_ready(), "extra NACK must not unready healthy base pool")
            self.assertFalse(session.coin_ready(bybit_symbol="NEWUSDT", okx_symbol="NEW-USDT-SWAP"))

            loop = warm_loop.loop
            assert loop is not None and cm._q is not None
            wrong = json.dumps({
                "event": "subscribe", "id": "wrong-request-id", "code": "0",
                "arg": {"channel": "orders", "instId": "NEW-USDT-SWAP"},
            })
            asyncio.run_coroutine_threadsafe(cm._q.put(wrong), loop).result(timeout=2.0)
            time.sleep(0.05)
            self.assertFalse(session.coin_ready(bybit_symbol="NEWUSDT", okx_symbol="NEW-USDT-SWAP"))
            self.assertTrue(session.is_ready())

            # A late positive response cannot undo a NACK for that request/key.
            for channel in ("orders", "positions"):
                frame = json.dumps({
                    "event": "subscribe", "id": request["id"], "code": "0",
                    "arg": {"channel": channel, "instId": "NEW-USDT-SWAP"},
                })
                asyncio.run_coroutine_threadsafe(cm._q.put(frame), loop).result(timeout=2.0)
            time.sleep(0.05)
            self.assertFalse(session.coin_ready(bybit_symbol="NEWUSDT", okx_symbol="NEW-USDT-SWAP"))
            self.assertTrue(session.is_ready())

            no_arg_nack = json.dumps({
                "event": "error", "id": request["id"], "code": "60000",
                "msg": "subscribe denied", "arg": None,
            })
            asyncio.run_coroutine_threadsafe(cm._q.put(no_arg_nack), loop).result(timeout=2.0)
            time.sleep(0.05)
            self.assertTrue(session.is_ready(), "known subscription NACK must not un-auth the base pool")

            cm.subscribe_ack_code = "0"
            cm.auto_subscribe_ack = False
            asyncio.run(
                asyncio.to_thread(
                    session.add_coin,
                    "SECOND",
                    bybit_symbol="SECONDUSDT",
                    okx_symbol="SECOND-USDT-SWAP",
                )
            )
            second_req = next(
                json.loads(frame)
                for frame in reversed(cm.outbox)
                if frame.startswith("{")
                and json.loads(frame).get("op") == "subscribe"
                and any(a.get("instId") == "SECOND-USDT-SWAP" for a in json.loads(frame).get("args", []))
            )
            for arg in (
                {"channel": "orders", "instId": "OTHER-USDT-SWAP"},
                {"channel": "fills", "instId": "SECOND-USDT-SWAP"},
            ):
                frame = json.dumps({
                    "event": "subscribe", "id": second_req["id"], "code": "0", "arg": arg,
                })
                asyncio.run_coroutine_threadsafe(cm._q.put(frame), loop).result(timeout=2.0)
            time.sleep(0.05)
            self.assertFalse(session.coin_ready(
                bybit_symbol="SECONDUSDT", okx_symbol="SECOND-USDT-SWAP"
            ))
            for channel in ("positions", "orders"):
                frame = json.dumps({
                    "event": "subscribe", "id": second_req["id"], "code": "0",
                    "arg": {"channel": channel, "instId": "SECOND-USDT-SWAP"},
                })
                asyncio.run_coroutine_threadsafe(cm._q.put(frame), loop).result(timeout=2.0)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and not session.coin_ready(
                bybit_symbol="SECONDUSDT", okx_symbol="SECOND-USDT-SWAP"
            ):
                time.sleep(0.01)
            self.assertTrue(session.coin_ready(
                bybit_symbol="SECONDUSDT", okx_symbol="SECOND-USDT-SWAP"
            ), "reordered matching channel ACKs should ready the coin")
            session.stop()

    def test_old_okx_request_id_cannot_advance_new_generation_readiness(self) -> None:
        from app.bot.private.selftest import W2PrivateWsTests
        from app.bot.private.ws_private import RestReseedResult, SubscriptionReadiness
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
            rt = session.okx_runtime
            old = next(
                json.loads(frame)
                for frame in cm.outbox
                if frame.startswith("{")
                and json.loads(frame).get("op") == "subscribe"
            )
            cm.auto_subscribe_ack = False
            rt.mark_reconnect()
            rt.authenticated = True  # isolate subscription correlation from login
            rt.send_subscribe()
            current = next(
                json.loads(frame)
                for frame in reversed(cm.outbox)
                if frame.startswith("{")
                and json.loads(frame).get("op") == "subscribe"
            )
            self.assertNotEqual(old["id"], current["id"])

            loop = warm_loop.loop
            assert loop is not None and cm._q is not None
            stale = json.dumps({
                "event": "subscribe", "id": old["id"], "code": "0",
                "arg": {"channel": "orders", "instId": "TRUMP-USDT-SWAP"},
            })
            asyncio.run_coroutine_threadsafe(cm._q.put(stale), loop).result(timeout=2.0)
            time.sleep(0.05)
            self.assertEqual(rt.subscription_readiness, SubscriptionReadiness.NOT_READY)

            fresh = json.dumps({
                "event": "subscribe", "id": current["id"], "code": "0",
                "arg": {"channel": "orders", "instId": "TRUMP-USDT-SWAP"},
            })
            asyncio.run_coroutine_threadsafe(cm._q.put(fresh), loop).result(timeout=2.0)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and rt.subscription_readiness != SubscriptionReadiness.READY:
                time.sleep(0.01)
            self.assertEqual(rt.subscription_readiness, SubscriptionReadiness.READY)
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


class WarmHandshakeReseedOrderingTests(unittest.TestCase):
    def test_all_trade_auth_precedes_slow_reseed_and_failure_stays_blocked(self) -> None:
        from types import SimpleNamespace

        from app.bot.private.ws_private import SequenceHealth, SubscriptionReadiness
        from app.bot.private.ws_warm_session import PrivateWarmSession

        events: list[tuple[str, str]] = []

        class Socket:
            connected = True
            handshake_done = False

            def drop_connection(self) -> None:
                self.connected = False

        class Runtime:
            def __init__(self, exchange: str, fail_reseed: bool = False) -> None:
                self.exchange = exchange
                self.private_socket = Socket()
                self.trade_socket = Socket()
                self.authenticated = False
                self.trade_authenticated = False
                self.sequence_state = SequenceHealth.RESEED_REQUIRED
                self.subscription_readiness = SubscriptionReadiness.NOT_READY
                self.sends_blocked = True
                self.reseed_required = True
                self.fail_reseed = fail_reseed
                self.base_coins: tuple[str, ...] = ()

            def send_auth(self) -> None:
                events.append(("private_auth", self.exchange))

            def recv_private_handshake_event(self, *, expect_kinds, timeout_sec):
                del timeout_sec
                if "auth_ack" in expect_kinds:
                    self.authenticated = True
                    events.append(("private_auth_ack", self.exchange))
                    return SimpleNamespace(kind="auth_ack", ack_ok=True)
                self.subscription_readiness = SubscriptionReadiness.READY
                events.append(("private_sub_ack", self.exchange))
                return SimpleNamespace(kind="sub_ack", ack_ok=True)

            def send_subscribe(self) -> None:
                events.append(("private_subscribe", self.exchange))

            def send_trade_auth(self) -> None:
                events.append(("trade_auth", self.exchange))

            def recv_trade_auth_ack(self, *, timeout_sec: float) -> bool:
                del timeout_sec
                self.trade_authenticated = True
                events.append(("trade_auth_ack", self.exchange))
                return True

            def send_heartbeat(self) -> None:
                events.append(("private_ping", self.exchange))

            def send_trade_heartbeat(self) -> None:
                events.append(("trade_ping", self.exchange))

            def publish_private_leg_state(self) -> None:
                return None

            def run_rest_reseed(self) -> dict[str, str]:
                # Model the 54-symbol startup/reconnect pool while keeping the
                # test offline and proving no REST phase starts before both
                # trade sockets are authenticated and owner heartbeats enabled.
                self.assert_two_phase_ready()
                events.append(("reseed_start", self.exchange))
                for _ in range(54):
                    time.sleep(0.001)
                if self.fail_reseed:
                    events.append(("reseed_failed", self.exchange))
                    return {"reconciliation_state": "inconclusive"}
                self.sequence_state = SequenceHealth.HEALTHY
                self.sends_blocked = False
                self.reseed_required = False
                events.append(("reseed_matched", self.exchange))
                return {"reconciliation_state": "matched"}

            def assert_two_phase_ready(self) -> None:
                self_outer = session
                assert self_outer is not None
                if not all(
                    rt.trade_authenticated
                    and rt.private_socket.handshake_done
                    and rt.trade_socket.handshake_done
                    for rt in (self_outer.bybit_runtime, self_outer.okx_runtime)
                ):
                    raise AssertionError("reseed began before all four socket handshakes")
                if self_outer.is_ready():
                    raise AssertionError("send readiness opened during REST reseed")

        def make_session(*, fail_okx_reseed: bool = False):
            nonlocal session
            session = object.__new__(PrivateWarmSession)
            session.bybit_runtime = Runtime("bybit")
            session.okx_runtime = Runtime("okx", fail_reseed=fail_okx_reseed)
            session.journal = SimpleNamespace(run_id="offline-run")
            session.coins = ("C0", "C1")
            session.bybit_symbols = ("C0USDT", "C1USDT")
            session.okx_symbols = ("C0-USDT-SWAP", "C1-USDT-SWAP")
            session.bybit_symbol = "C0USDT"
            session.okx_symbol = "C0-USDT-SWAP"
            session.ack_timeout_sec = 0.1
            session._started = True
            session._stopped = False
            session._handshake_count = 0
            session._last_hb_mono = 0.0
            session._fail_attempt = 0
            session._venue_hs_lock = {
                "bybit": threading.Lock(),
                "okx": threading.Lock(),
            }
            return session

        session = None
        session = make_session()
        session._handshake_both()
        self.assertTrue(session.is_ready())
        first_reseed = next(i for i, event in enumerate(events) if event[0] == "reseed_start")
        self.assertEqual(
            [event for event in events[:first_reseed] if event[0] == "trade_auth_ack"],
            [("trade_auth_ack", "bybit"), ("trade_auth_ack", "okx")],
        )
        self.assertEqual(session._handshake_count, 1)

        # A single-venue reconnect follows the same auth/heartbeat-before-REST
        # order while the healthy venue remains unchanged.
        events.clear()
        rt = session.okx_runtime
        rt.authenticated = False
        rt.trade_authenticated = False
        rt.sequence_state = SequenceHealth.RESEED_REQUIRED
        rt.subscription_readiness = SubscriptionReadiness.NOT_READY
        rt.sends_blocked = True
        rt.reseed_required = True
        rt.private_socket.handshake_done = False
        rt.trade_socket.handshake_done = False
        rt.private_socket.connected = True
        rt.trade_socket.connected = True
        session._handshake_venue_if_ready("okx")
        self.assertTrue(session.is_ready())
        self.assertEqual(session._handshake_count, 2)

        # Reseed failure cannot make the session send-ready even though the
        # loop handshake flags are true for receive/heartbeat work.
        events.clear()
        rt.authenticated = False
        rt.trade_authenticated = False
        rt.sequence_state = SequenceHealth.RESEED_REQUIRED
        rt.subscription_readiness = SubscriptionReadiness.NOT_READY
        rt.sends_blocked = True
        rt.reseed_required = True
        rt.fail_reseed = True
        rt.private_socket.handshake_done = False
        rt.trade_socket.handshake_done = False
        rt.private_socket.connected = True
        rt.trade_socket.connected = True
        session._handshake_venue_if_ready("okx")
        self.assertFalse(session.is_ready())
        self.assertTrue(rt.reseed_required)
        self.assertTrue(rt.sends_blocked)
        self.assertTrue(rt.private_socket.handshake_done)
        self.assertTrue(rt.trade_socket.handshake_done)


if __name__ == "__main__":
    unittest.main()
