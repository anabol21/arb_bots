"""Injected private WebSocket socket abstraction.

Default CLI never binds a factory and never opens a network socket.
Tests inject ``FakePrivateWsSocket`` only. Production may bind
``WebsocketsSocketFactory`` (lazy ``websockets`` import) only after gates.
"""

from __future__ import annotations

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
    """In-memory duplex queue. Never opens a real network connection."""

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

    def connect(self) -> None:
        if self._closed:
            raise RuntimeError("fake socket already closed")
        self._connected = True

    def send_text(self, text: str) -> None:
        if not self._connected:
            raise RuntimeError("fake socket not connected")
        if not isinstance(text, str):
            raise TypeError("send_text requires str")
        self._outbox.append(text)
        if self._auto_trade_ack:
            self._maybe_auto_trade_ack(text)

    def recv_text(self, *, timeout_sec: Optional[float] = None) -> str:
        del timeout_sec  # fake: empty inbox is always a timeout
        if not self._connected:
            raise RuntimeError("fake socket not connected")
        if not self._inbox:
            raise TimeoutError("fake socket inbox empty")
        return self._inbox.popleft()

    def push_inbound(self, text: str) -> None:
        """Test helper: enqueue a venue→client frame (already a text payload)."""
        if not isinstance(text, str):
            raise TypeError("push_inbound requires str")
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
        return list(self._outbox)

    def clear_outbox(self) -> None:
        self._outbox.clear()


class WebsocketsClientSocket:
    """Production private WS client. Lazily imports ``websockets`` on connect."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._conn: Any = None
        self._connected = False
        self._loop: Any = None

    def connect(self) -> None:
        import asyncio

        try:
            import websockets  # lazy: optional at import time of this module
        except ImportError as exc:
            raise RuntimeError(
                "websockets package required for production private WS client"
            ) from exc

        async def _open() -> Any:
            return await websockets.connect(self._url, max_size=2**20)

        self._loop = asyncio.new_event_loop()
        self._conn = self._loop.run_until_complete(_open())
        self._connected = True

    def send_text(self, text: str) -> None:
        if not self._connected or self._conn is None or self._loop is None:
            raise RuntimeError("websockets client not connected")
        self._loop.run_until_complete(self._conn.send(text))

    def recv_text(self, *, timeout_sec: Optional[float] = None) -> str:
        if not self._connected or self._conn is None or self._loop is None:
            raise RuntimeError("websockets client not connected")
        import asyncio

        async def _recv() -> str:
            if timeout_sec is None:
                msg = await self._conn.recv()
            else:
                msg = await asyncio.wait_for(self._conn.recv(), timeout=timeout_sec)
            if isinstance(msg, bytes):
                return msg.decode("utf-8", errors="replace")
            return str(msg)

        try:
            return self._loop.run_until_complete(_recv())
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

    def close(self) -> None:
        if self._conn is not None and self._loop is not None:
            try:
                self._loop.run_until_complete(self._conn.close())
            except Exception:  # noqa: BLE001
                pass
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001
                pass
        self._conn = None
        self._loop = None
        self._connected = False

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
