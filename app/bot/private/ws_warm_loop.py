"""Single asyncio loop for Contour B private warm sockets.

Public L1 already runs concurrent ``async with websockets.connect`` tasks
(``app/bot/ws_books.py`` ``_listen_loop``). Private historically used one
dedicated asyncio loop **thread per socket** plus a polling keepalive thread
that called ``recv_text``. That raced place, and the default websockets
library ping produced OKX ``1011 keepalive ping timeout``.

This module:

- owns **one** asyncio loop thread for Bybit/OKX private+trade;
- each slot is a concurrent listen task: connect, pump inbound, app
  heartbeat, reconnect with bounded backoff;
- connects with ``ping_interval=None`` (application text ping only);
- exposes ``LoopOwnedSocket``: ``send_text`` never waits on recv and does
  not share an I/O lock with the listen pump;
- ``recv_text`` pops an inbound queue filled only by the listen task.

Handshake / REST reseed stay on ``PrivateStreamRuntime`` (sync). Listen
pumps the queue so handshake ``recv_text`` does not call ``ws.recv``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.bot.private.ws_socket import PRIVATE_WS_CONNECT_KWARGS

LOG = logging.getLogger("bbot.private.ws_warm_loop")

_CONNECT_WAIT_SEC = 30.0
_SEND_TIMEOUT_SEC = 30.0
_RECONNECT_BASE_SEC = 5.0
_RECONNECT_CAP_SEC = 60.0
_HB_POLL_SEC = 0.05


def reconnect_sleep_sec(
    attempt: int,
    *,
    base: float = _RECONNECT_BASE_SEC,
    cap: float = _RECONNECT_CAP_SEC,
) -> float:
    n = max(0, int(attempt))
    return min(float(cap), float(base) * (2**n))


def _decode_ws_message(message: Any) -> str:
    if isinstance(message, bytes):
        return message.decode("utf-8", errors="replace")
    return str(message)


def is_loop_owned_socket(sock: Any) -> bool:
    return isinstance(sock, LoopOwnedSocket) or bool(
        getattr(sock, "loop_owned", False)
    )


class LoopOwnedSocket:
    """``PrivateWsSocket`` whose ``ws.recv`` is owned by the listen task.

    Cross-thread ``send_text`` submits ``ws.send`` on the shared loop and
    waits only for that send. It does not take a lock that recv holds.
    ``recv_text`` never calls ``ws.recv``.
    """

    loop_owned = True

    def __init__(
        self,
        *,
        url: str,
        exchange: str,
        channel: str,
        owner: "PrivateWarmLoop",
    ) -> None:
        self._url = url
        self.exchange = str(exchange).lower()
        self.channel = str(channel).lower()
        self._owner = owner
        self._ws: Any = None
        self._connected = False
        self._closed = False
        self._permanent_close = False
        self._connected_event = threading.Event()
        self._inbound: queue.Queue[str] = queue.Queue()
        self.handshake_done = False
        self.runtime: Any = None
        self.last_recv_mono_ns: Optional[int] = None

    @property
    def url(self) -> str:
        return self._url

    @property
    def connected(self) -> bool:
        return self._connected and self._ws is not None and not self._permanent_close

    @property
    def closed(self) -> bool:
        return self._closed or self._permanent_close

    def connect(self) -> None:
        if self._permanent_close:
            raise RuntimeError("loop-owned socket already closed")
        if self.connected:
            return
        if not self._connected_event.wait(timeout=_CONNECT_WAIT_SEC):
            raise RuntimeError(
                f"loop-owned socket connect timeout exchange={self.exchange} "
                f"channel={self.channel}"
            )

    def send_text(self, text: str) -> None:
        if not isinstance(text, str):
            raise TypeError("send_text requires str")
        ws = self._ws
        loop = self._owner.loop
        if ws is None or loop is None or not self._connected:
            raise RuntimeError("loop-owned socket not connected")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            raise RuntimeError(
                "LoopOwnedSocket.send_text cannot wait on the owner loop; "
                "use asend() from listen/heartbeat tasks"
            )
        fut = asyncio.run_coroutine_threadsafe(ws.send(text), loop)
        try:
            fut.result(timeout=_SEND_TIMEOUT_SEC)
        except concurrent.futures.TimeoutError as exc:
            fut.cancel()
            raise TimeoutError("loop-owned send timeout") from exc

    async def asend(self, text: str) -> None:
        """Owner-loop send (heartbeat / OKX pong). Does not wait for recv."""
        if not isinstance(text, str):
            raise TypeError("asend requires str")
        ws = self._ws
        if ws is None:
            raise RuntimeError("loop-owned socket not connected")
        await ws.send(text)

    def recv_text(self, *, timeout_sec: Optional[float] = None) -> str:
        if self._permanent_close and self._inbound.empty():
            raise RuntimeError("loop-owned socket not connected")
        try:
            if timeout_sec is None:
                return self._inbound.get()
            return self._inbound.get(timeout=float(timeout_sec))
        except queue.Empty as exc:
            raise TimeoutError("loop-owned recv timeout") from exc

    def push_inbound(self, text: str) -> None:
        """Test / listen helper: enqueue a venue→client frame."""
        if not isinstance(text, str):
            raise TypeError("push_inbound requires str")
        self._inbound.put(text)

    def attach_ws(self, ws: Any) -> None:
        self._ws = ws
        self._connected = True
        self._closed = False
        self.handshake_done = False
        self.last_recv_mono_ns = time.monotonic_ns()
        runtime = self.runtime
        if runtime is not None:
            # New generation: do not inherit the previous socket's silence clock.
            if self.channel == "trade":
                runtime.note_trade_activity()
            else:
                runtime.note_private_activity()
        self._connected_event.set()

    def detach_ws(self) -> None:
        self._ws = None
        self._connected = False
        self.handshake_done = False
        self._connected_event.clear()
        # Drop stale handshake/ack frames so a new generation cannot mix.
        while True:
            try:
                self._inbound.get_nowait()
            except queue.Empty:
                break

    def drop_connection(self) -> None:
        """Close the current websocket; listen may reconnect."""
        self._connected = False
        self.handshake_done = False
        self._connected_event.clear()
        self._schedule_ws_close()

    def close(self) -> None:
        self._permanent_close = True
        self._connected = False
        self._closed = True
        self.handshake_done = False
        self._connected_event.clear()
        self._schedule_ws_close()

    def _schedule_ws_close(self) -> None:
        ws = self._ws
        loop = self._owner.loop
        self._ws = None
        if ws is None or loop is None:
            return

        async def _aclose() -> None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

        try:
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(_aclose(), loop)
        except Exception:  # noqa: BLE001
            pass


@dataclass
class SocketSlot:
    socket: LoopOwnedSocket
    fail_attempt: int = 0
    on_up: Optional[Callable[[LoopOwnedSocket], None]] = None
    on_down: Optional[Callable[[LoopOwnedSocket], None]] = None
    place_inflight_fn: Optional[Callable[[], bool]] = None
    private_frame_observer: Optional[Callable[[str, int], Any]] = None
    trade_frame_observer: Optional[Callable[[str, int], Any]] = None
    heartbeat_every_sec: float = 10.0
    silence_timeout_sec: float = 45.0
    stop_event: Any = None
    _hb_err_logged: bool = field(default=False, repr=False)


class PrivateWarmLoop:
    """One asyncio loop thread; four concurrent private/trade listen tasks."""

    def __init__(
        self,
        *,
        connect_cm_fn: Optional[Callable[[str], Any]] = None,
        reconnect_base_sec: float = _RECONNECT_BASE_SEC,
        reconnect_cap_sec: float = _RECONNECT_CAP_SEC,
        heartbeat_every_sec: float = 10.0,
        silence_timeout_sec: float = 45.0,
    ) -> None:
        self._connect_cm_fn = connect_cm_fn
        self._reconnect_base_sec = float(reconnect_base_sec)
        self._reconnect_cap_sec = float(reconnect_cap_sec)
        self._heartbeat_every_sec = float(heartbeat_every_sec)
        self._silence_timeout_sec = float(silence_timeout_sec)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()
        self._stop = threading.Event()
        self._slots: dict[str, SocketSlot] = {}
        self._external_stop: Any = None

    @property
    def loop(self) -> Optional[asyncio.AbstractEventLoop]:
        return self._loop

    @property
    def running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive() and not self._stop.is_set()

    def set_external_stop(self, stop_event: Any) -> None:
        self._external_stop = stop_event
        for slot in self._slots.values():
            slot.stop_event = stop_event

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._loop_ready.clear()
        t = threading.Thread(
            target=self._run_loop, name="bbot-private-warm-loop", daemon=True
        )
        self._thread = t
        t.start()
        if not self._loop_ready.wait(timeout=5.0):
            raise RuntimeError("private warm loop thread failed to start")

    def stop(self) -> None:
        self._stop.set()
        for slot in list(self._slots.values()):
            try:
                slot.socket.close()
            except Exception:  # noqa: BLE001
                pass
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:  # noqa: BLE001
                pass
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3.0)
        self._thread = None
        self._loop = None
        self._slots.clear()
        self._loop_ready.clear()

    def open(
        self,
        url: str,
        *,
        exchange: str,
        channel: str,
        on_up: Optional[Callable[[LoopOwnedSocket], None]] = None,
        on_down: Optional[Callable[[LoopOwnedSocket], None]] = None,
        place_inflight_fn: Optional[Callable[[], bool]] = None,
    ) -> LoopOwnedSocket:
        if self._loop is None or not self.running:
            raise RuntimeError("private warm loop is not running")
        key = f"{str(exchange).lower()}:{str(channel).lower()}"
        existing = self._slots.get(key)
        if existing is not None:
            if on_up is not None:
                existing.on_up = on_up
            if on_down is not None:
                existing.on_down = on_down
            if place_inflight_fn is not None:
                existing.place_inflight_fn = place_inflight_fn
            return existing.socket
        sock = LoopOwnedSocket(
            url=url, exchange=exchange, channel=channel, owner=self
        )
        slot = SocketSlot(
            socket=sock,
            on_up=on_up,
            on_down=on_down,
            place_inflight_fn=place_inflight_fn,
            heartbeat_every_sec=self._heartbeat_every_sec,
            silence_timeout_sec=self._silence_timeout_sec,
            stop_event=self._external_stop,
        )
        self._slots[key] = slot
        asyncio.run_coroutine_threadsafe(self._listen_slot(slot), self._loop)
        return sock

    def slot(self, exchange: str, channel: str) -> Optional[SocketSlot]:
        return self._slots.get(f"{str(exchange).lower()}:{str(channel).lower()}")

    def set_private_frame_observer(
        self, exchange: str, observer: Optional[Callable[[str, int], Any]]
    ) -> None:
        """Opt-in owner-loop fanout after legacy private parsing.

        The observer must ingest or fail closed; an exception tears down the
        private socket so the normal reconnect/reseed gate blocks sends.
        Trade ACK frames are intentionally not delivered through this hook.
        """
        slot = self.slot(exchange, "private")
        if slot is None or (observer is not None and not callable(observer)):
            raise ValueError("invalid_private_observer")
        slot.private_frame_observer = observer

    def set_trade_frame_observer(
        self, exchange: str, observer: Optional[Callable[[str, int], Any]]
    ) -> None:
        """Opt-in receive-time tap before a trade ACK enters the legacy queue.

        The tap cannot consume or rewrite the ACK.  Failure propagates to the
        normal reconnect path, which invalidates live readiness.
        """
        slot = self.slot(exchange, "trade")
        if slot is None or (observer is not None and not callable(observer)):
            raise ValueError("invalid_trade_observer")
        slot.trade_frame_observer = observer

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._loop_ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                pending = [
                    task for task in asyncio.all_tasks(loop) if not task.done()
                ]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:  # noqa: BLE001
                pass
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    def _stop_requested(self) -> bool:
        if self._stop.is_set():
            return True
        ext = self._external_stop
        if ext is not None and getattr(ext, "is_set", lambda: False)():
            return True
        return False

    def _connect_cm(self, url: str) -> Any:
        if self._connect_cm_fn is not None:
            return self._connect_cm_fn(url)
        import websockets

        return websockets.connect(url, **PRIVATE_WS_CONNECT_KWARGS)

    async def _listen_slot(self, slot: SocketSlot) -> None:
        sock = slot.socket
        while not self._stop_requested() and not sock._permanent_close:  # noqa: SLF001
            if slot.place_inflight_fn is not None and slot.place_inflight_fn():
                await asyncio.sleep(_HB_POLL_SEC)
                continue
            ws = None
            try:
                async with self._connect_cm(sock.url) as ws:
                    sock.attach_ws(ws)
                    slot.fail_attempt = 0
                    if slot.on_up is not None:
                        try:
                            slot.on_up(sock)
                        except Exception:  # noqa: BLE001
                            LOG.warning(
                                "warm_loop_on_up_error exchange=%s channel=%s",
                                sock.exchange,
                                sock.channel,
                            )
                    pump = asyncio.create_task(
                        self._pump(slot, ws), name=f"pump-{sock.exchange}-{sock.channel}"
                    )
                    hb = asyncio.create_task(
                        self._heartbeat(slot),
                        name=f"hb-{sock.exchange}-{sock.channel}",
                    )
                    wd = asyncio.create_task(
                        self._watchdog(slot, ws),
                        name=f"wd-{sock.exchange}-{sock.channel}",
                    )
                    try:
                        done, pending = await asyncio.wait(
                            {pump, hb, wd},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in pending:
                            task.cancel()
                        if pending:
                            await asyncio.gather(*pending, return_exceptions=True)
                        for task in done:
                            if task.cancelled():
                                continue
                            exc = task.exception()
                            if exc is not None:
                                raise exc
                        raise ConnectionError("private ws listen ended")
                    finally:
                        sock.detach_ws()
            except asyncio.CancelledError:
                sock.detach_ws()
                raise
            except Exception as exc:  # noqa: BLE001
                LOG.warning(
                    "warm_loop_disconnect exchange=%s channel=%s err=%s",
                    sock.exchange,
                    sock.channel,
                    type(exc).__name__,
                )
                sock.detach_ws()
                if slot.on_down is not None:
                    try:
                        slot.on_down(sock)
                    except Exception:  # noqa: BLE001
                        pass
                if self._stop_requested() or sock._permanent_close:  # noqa: SLF001
                    return
                while (
                    slot.place_inflight_fn is not None
                    and slot.place_inflight_fn()
                    and not self._stop_requested()
                ):
                    await asyncio.sleep(_HB_POLL_SEC)
                delay = reconnect_sleep_sec(
                    slot.fail_attempt,
                    base=self._reconnect_base_sec,
                    cap=self._reconnect_cap_sec,
                )
                slot.fail_attempt += 1
                LOG.info(
                    "warm_loop_reconnect_backoff exchange=%s channel=%s "
                    "attempt=%s sleep_sec=%.3f",
                    sock.exchange,
                    sock.channel,
                    slot.fail_attempt,
                    delay,
                )
                await asyncio.sleep(delay)

    async def _pump(self, slot: SocketSlot, ws: Any) -> None:
        sock = slot.socket
        async for message in ws:
            if self._stop_requested() or sock._permanent_close:  # noqa: SLF001
                return
            text = _decode_ws_message(message)
            sock.last_recv_mono_ns = time.monotonic_ns()
            runtime = sock.runtime
            if runtime is not None:
                if sock.channel == "trade":
                    runtime.note_trade_activity()
                else:
                    runtime.note_private_activity()
            if sock.exchange == "okx" and text == "ping":
                try:
                    await sock.asend("pong")
                except Exception as exc:  # noqa: BLE001
                    LOG.warning(
                        "warm_loop_okx_pong_failed channel=%s err=%s",
                        sock.channel,
                        type(exc).__name__,
                    )
                    raise
                if runtime is not None:
                    LOG.info(
                        "ws_okx_pong_reply exchange=%s gen=%s",
                        runtime.exchange,
                        runtime.reconnect_generation,
                    )
                if not sock.handshake_done:
                    sock.push_inbound(text)
                continue
            if sock.handshake_done and runtime is not None:
                from app.bot.private.ws_private import is_ws_noise_frame

                if is_ws_noise_frame(sock.exchange, text):
                    continue
                if sock.channel == "private":
                    try:
                        runtime.handle_inbound_text(text)
                    except Exception:  # noqa: BLE001
                        raise
                    observer = slot.private_frame_observer
                    if observer is not None:
                        observed = observer(text, sock.last_recv_mono_ns or 0)
                        if inspect.isawaitable(observed):
                            await observed
                    continue
                observer = slot.trade_frame_observer
                if observer is not None:
                    observed = observer(text, sock.last_recv_mono_ns or 0)
                    if inspect.isawaitable(observed):
                        await observed
            sock.push_inbound(text)

    async def _heartbeat(self, slot: SocketSlot) -> None:
        sock = slot.socket
        while not self._stop_requested() and sock.connected:
            if not sock.handshake_done:
                await asyncio.sleep(_HB_POLL_SEC)
                continue
            runtime = sock.runtime
            if runtime is None:
                await asyncio.sleep(_HB_POLL_SEC)
                continue
            try:
                msg = runtime.build_heartbeat()
                await sock.asend(msg.text)
                if sock.channel == "trade":
                    runtime.note_trade_activity()
                else:
                    runtime.note_private_activity()
            except Exception as exc:  # noqa: BLE001
                LOG.warning(
                    "warm_heartbeat_send_failed phase=loop exchange=%s "
                    "channel=%s err=%s:%s",
                    sock.exchange,
                    sock.channel,
                    type(exc).__name__,
                    " ".join(str(exc).split())[:160],
                )
                raise
            await asyncio.sleep(float(slot.heartbeat_every_sec))

    async def _watchdog(self, slot: SocketSlot, ws: Any) -> None:
        sock = slot.socket
        timeout_ns = int(float(slot.silence_timeout_sec) * 1_000_000_000)
        while not self._stop_requested() and sock.connected:
            await asyncio.sleep(_HB_POLL_SEC)
            if not sock.handshake_done:
                continue
            if slot.place_inflight_fn is not None and slot.place_inflight_fn():
                continue
            runtime = sock.runtime
            last = None
            if runtime is not None:
                if sock.channel == "trade":
                    last = runtime.last_trade_recv_mono_ns
                else:
                    last = runtime.last_recv_mono_ns
            if last is None:
                last = sock.last_recv_mono_ns
            if last is None:
                continue
            if (time.monotonic_ns() - last) < timeout_ns:
                continue
            if runtime is not None:
                runtime.handle_silence_timeout()
            LOG.info(
                "warm_loop_silence_timeout exchange=%s channel=%s",
                sock.exchange,
                sock.channel,
            )
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
            return


_PROCESS_LOOP: Optional[PrivateWarmLoop] = None
_PROCESS_LOOP_LOCK = threading.Lock()


def get_process_warm_loop() -> Optional[PrivateWarmLoop]:
    return _PROCESS_LOOP


def ensure_process_warm_loop(**kwargs: Any) -> PrivateWarmLoop:
    global _PROCESS_LOOP
    with _PROCESS_LOOP_LOCK:
        if _PROCESS_LOOP is not None and _PROCESS_LOOP.running:
            return _PROCESS_LOOP
        loop = PrivateWarmLoop(**kwargs)
        loop.start()
        _PROCESS_LOOP = loop
        return loop


def clear_process_warm_loop() -> None:
    global _PROCESS_LOOP
    with _PROCESS_LOOP_LOCK:
        if _PROCESS_LOOP is not None:
            _PROCESS_LOOP.stop()
        _PROCESS_LOOP = None
