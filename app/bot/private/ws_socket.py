"""Injected private WebSocket socket abstraction.

Default CLI never binds a factory and never opens a network socket.
Tests inject ``FakePrivateWsSocket`` only. Production may bind
``WebsocketsSocketFactory`` (lazy ``websockets`` import) only after gates.

``WebsocketsClientSocket`` owns a dedicated asyncio loop thread per socket.
All send/recv/close coroutines are submitted via
``asyncio.run_coroutine_threadsafe`` under a per-socket lock so warm
keepalive and W6 parallel-place worker threads never call
``loop.run_until_complete`` across threads (unsafe with asyncio/websockets).
"""

from __future__ import annotations

import concurrent.futures
import threading
from collections import deque
from typing import Any, Deque, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class PrivateWsSocket(Protocol):
    """Minimal duplex text socket used by private WS runtime."""

    def connect(self) -> None:
        ...

    def send_text(self, text: str) -> None:
        ...

    def recv_text(self, *, timeout_sec: Optional[float] = None) -> str:
        ...

    def close(self) -> None:
        ...

    @property
    def connected(self) -> bool:
        ...


class FakePrivateWsSocket:
    """In-memory duplex queue. Never opens a real network connection.

    Methods are serialized with an RLock so hermetic tests can exercise
    concurrent keepalive + place workers without deque corruption.
    """

    def __init__(
        self,
        *,
        auto_trade_ack: bool = False,
        exchange: str = "bybit",
    ) -> None:
        self._inbox: Deque[str] = deque()
        self._outbox: List[str] = []
        self._connected = False
        self._closed = False
        self._auto_trade_ack = bool(auto_trade_ack)
        self._exchange = str(exchange).lower()
        self._lock = threading.RLock()
        self._call_threads: List[int] = []

    def connect(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("fake socket already closed")
            self._connected = True
            self._call_threads.append(threading.get_ident())

    def send_text(self, text: str) -> None:
        with self._lock:
            self._call_threads.append(threading.get_ident())
            if not self._connected:
                raise RuntimeError("fake socket not connected")
            if not isinstance(text, str):
                raise TypeError("send_text requires str")
            self._outbox.append(text)
            if self._auto_trade_ack:
                self._maybe_auto_trade_ack(text)

    def recv_text(self, *, timeout_sec: Optional[float] = None) -> str:
        del timeout_sec  # fake: empty inbox is always a timeout
        with self._lock:
            self._call_threads.append(threading.get_ident())
            if not self._connected:
                raise RuntimeError("fake socket not connected")
            if not self._inbox:
                raise TimeoutError("fake socket inbox empty")
            return self._inbox.popleft()

    def push_inbound(self, text: str) -> None:
        """Test helper: enqueue a venue→client frame (already a text payload)."""
        if not isinstance(text, str):
            raise TypeError("push_inbound requires str")
        with self._lock:
            self._inbox.append(text)

    def _maybe_auto_trade_ack(self, text: str) -> None:
        """Correlate real reqId/id from place/cancel outbox — no monkeypatch."""
        import json

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        if self._exchange == "bybit":
            op = str(data.get("op") or "")
            req = str(data.get("reqId") or "")
            if req and op in {"order.create", "order.cancel"}:
                self._inbox.append(
                    json.dumps(
                        {"reqId": req, "op": op, "retCode": 0, "success": True}
                    )
                )
            return
        op = str(data.get("op") or "")
        req = str(data.get("id") or "")
        if req and op in {"order", "cancel-order"}:
            self._inbox.append(
                json.dumps(
                    {
                        "id": req,
                        "op": op,
                        "code": "0",
                        "data": [{"sCode": "0"}],
                    }
                )
            )

    def close(self) -> None:
        with self._lock:
            self._call_threads.append(threading.get_ident())
            self._connected = False
            self._closed = True

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def outbox(self) -> List[str]:
        with self._lock:
            return list(self._outbox)

    def clear_outbox(self) -> None:
        with self._lock:
            self._outbox.clear()


class WebsocketsClientSocket:
    """Production private WS client with a dedicated asyncio loop thread.

    Lazily imports ``websockets`` on connect. Cross-thread send/recv/close are
    safe: coroutines always run on the owner loop via
    ``run_coroutine_threadsafe``, serialized by ``_io_lock``.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._conn: Any = None
        self._connected = False
        self._loop: Any = None
        self._loop_thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()
        self._io_lock = threading.RLock()
        self._closed = False

    def _start_loop_thread(self) -> None:
        import asyncio

        if self._loop_thread is not None and self._loop_thread.is_alive():
            return
        self._loop_ready.clear()
        loop = asyncio.new_event_loop()
        self._loop = loop

        def _run() -> None:
            asyncio.set_event_loop(loop)
            self._loop_ready.set()
            loop.run_forever()
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

        t = threading.Thread(
            target=_run,
            name=f"bbot-ws-io-{id(self) & 0xFFFF:x}",
            daemon=True,
        )
        self._loop_thread = t
        t.start()
        if not self._loop_ready.wait(timeout=5.0):
            raise RuntimeError("websockets client loop thread failed to start")

    def _stop_loop_thread(self) -> None:
        loop = self._loop
        t = self._loop_thread
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:  # noqa: BLE001
                pass
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._loop_thread = None
        self._loop = None
        self._loop_ready.clear()

    def _run_on_loop(self, coro: Any, *, timeout_sec: Optional[float]) -> Any:
        """Submit ``coro`` to the owner loop; map futures timeout → TimeoutError."""
        import asyncio

        loop = self._loop
        if loop is None:
            raise RuntimeError("websockets client loop not running")
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return fut.result(timeout=timeout_sec)
        except concurrent.futures.TimeoutError as exc:
            fut.cancel()
            raise TimeoutError("websockets operation timeout") from exc
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, TimeoutError) or type(exc).__name__ in {
                "TimeoutError",
                "CancelledError",
            }:
                raise TimeoutError("websockets recv timeout") from exc
            if hasattr(asyncio, "TimeoutError") and isinstance(
                exc, asyncio.TimeoutError
            ):
                raise TimeoutError("websockets recv timeout") from exc
            raise

    def connect(self) -> None:
        import asyncio

        try:
            import websockets  # lazy: optional at import time of this module
        except ImportError as exc:
            raise RuntimeError(
                "websockets package required for production private WS client"
            ) from exc

        with self._io_lock:
            if self._closed:
                raise RuntimeError("websockets client already closed")
            if self._connected:
                return
            self._start_loop_thread()

            async def _open() -> Any:
                return await websockets.connect(self._url, max_size=2**20)

            self._conn = self._run_on_loop(_open(), timeout_sec=30.0)
            self._connected = True

    def send_text(self, text: str) -> None:
        with self._io_lock:
            if not self._connected or self._conn is None or self._loop is None:
                raise RuntimeError("websockets client not connected")
            if not isinstance(text, str):
                raise TypeError("send_text requires str")
            self._run_on_loop(self._conn.send(text), timeout_sec=30.0)

    def recv_text(self, *, timeout_sec: Optional[float] = None) -> str:
        with self._io_lock:
            if not self._connected or self._conn is None or self._loop is None:
                raise RuntimeError("websockets client not connected")
            import asyncio

            async def _recv() -> str:
                if timeout_sec is None:
                    msg = await self._conn.recv()
                else:
                    msg = await asyncio.wait_for(
                        self._conn.recv(), timeout=timeout_sec
                    )
                if isinstance(msg, bytes):
                    return msg.decode("utf-8", errors="replace")
                return str(msg)

            # Outer future timeout: recv budget + small slack for scheduling.
            outer = None if timeout_sec is None else float(timeout_sec) + 1.0
            return self._run_on_loop(_recv(), timeout_sec=outer)

    def close(self) -> None:
        with self._io_lock:
            conn = self._conn
            loop = self._loop
            if conn is not None and loop is not None:
                try:
                    self._run_on_loop(conn.close(), timeout_sec=5.0)
                except Exception:  # noqa: BLE001
                    pass
            self._conn = None
            self._connected = False
            self._closed = True
            self._stop_loop_thread()

    @property
    def connected(self) -> bool:
        return self._connected


class SocketFactory(Protocol):
    def open(self, url: str) -> PrivateWsSocket:
        ...


class UnboundSocketFactory:
    """Default factory: refuse any connect (CLI / LIVE_ORDERS alone must not open WS)."""

    def open(self, url: str) -> PrivateWsSocket:
        del url
        raise RuntimeError(
            "private WS socket factory unbound; default entrypoints cannot connect"
        )


class WebsocketsSocketFactory:
    """Production factory — bind only after WS profile gates pass."""

    def open(self, url: str) -> PrivateWsSocket:
        return WebsocketsClientSocket(url)


_RUNTIME_SOCKET_FACTORY: Optional[SocketFactory] = None


def bind_socket_factory(factory: SocketFactory) -> None:
    global _RUNTIME_SOCKET_FACTORY
    _RUNTIME_SOCKET_FACTORY = factory


def unbind_socket_factory() -> None:
    global _RUNTIME_SOCKET_FACTORY
    _RUNTIME_SOCKET_FACTORY = None


def get_socket_factory() -> Optional[SocketFactory]:
    return _RUNTIME_SOCKET_FACTORY


def assert_no_default_ws_socket() -> None:
    if _RUNTIME_SOCKET_FACTORY is not None:
        raise RuntimeError("private WS socket factory unexpectedly bound")


def open_private_socket(url: str) -> PrivateWsSocket:
    """Open via explicitly bound factory only. Never falls back to network."""
    factory = _RUNTIME_SOCKET_FACTORY
    if factory is None:
        raise RuntimeError(
            "private WS socket factory unbound; default entrypoints cannot connect"
        )
    return factory.open(url)
