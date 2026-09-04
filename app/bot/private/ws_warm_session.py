"""Process-lifetime private WS supervisor (OKX + Bybit).

Public L1 sockets already stay up for the life of the bot process
(``app/bot/ws_books.py``). Private historically cold-started auth + subscribe
+ REST reseed (+ operator approval setup) on every dual-leg send. This module
holds one warm private session per process so a signal does not pay that
round-trip.

Policy (same as public reconnect):
- connect and handshake at private-live / bot startup;
- background keep-alive: venue heartbeat/ping, detect drop while idle;
- auto-reconnect with bounded exponential backoff — NOT lazily on the next
  send; a send that finds a dead socket is a bug;
- re-auth / re-subscribe / REST reseed only on disconnect, auth failure, or
  explicit reconnect;
- live send reuses the same ``run_id`` / journal (no ``event_seq=1`` auth storm
  on a healthy recovered session).

Production ``python -m app.bot`` with ``VENUE=live`` + ``LIVE_ORDERS=1`` starts
warm private sockets by default before the signal loop (see
``start_warm_private_for_bot_process``). Default ``python -m app.bot.private``
CLI still does not auto-connect. No default-off warm flag.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from app.bot.private.journal_v1 import (
    PrivateJournalWriter,
    new_opaque_id,
    scan_all_journal_events,
)
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.paths import resolve_data_root
from app.bot.private.venue import endpoints_for_venue, live_orders_enabled, resolve_venue
from app.bot.private.ws_gates import (
    WsProfileGateError,
    assert_ws_warm_private_gates,
)
from app.bot.private.ws_private import (
    PrivateStreamRuntime,
    SequenceHealth,
    SubscriptionReadiness,
    trade_ws_url_for_exchange,
)
from app.bot.private.ws_reseed import build_signed_rest_reseed
from app.bot.private.ws_socket import PrivateWsSocket
from app.bot.private.ws_w4_postonly import _handshake_private_and_trade

LOG = logging.getLogger("bbot.private.ws_warm")

# Default symbols match the immutable W6 dual-leg harness; callers may override.
_DEFAULT_BYBIT_SYMBOL = "TRUMPUSDT"
_DEFAULT_OKX_SYMBOL = "TRUMP-USDT-SWAP"

# Public-style bounded exponential backoff (see app/bot/ws_books.py).
_RECONNECT_BASE_SEC = 5.0
_RECONNECT_CAP_SEC = 60.0
_DEFAULT_HEARTBEAT_EVERY_SEC = 15.0
_DEFAULT_POLL_SEC = 1.0
_DEFAULT_SILENCE_TIMEOUT_SEC = 45.0
_DEFAULT_RECV_TIMEOUT_SEC = 0.2


def reconnect_sleep_sec(
    attempt: int,
    *,
    base: float = _RECONNECT_BASE_SEC,
    cap: float = _RECONNECT_CAP_SEC,
) -> float:
    """base * 2^n capped. attempt starts at 0 after first failure."""
    n = max(0, int(attempt))
    return min(float(cap), float(base) * (2**n))


@dataclass(frozen=True)
class WarmSocketBundle:
    """One private + trade socket pair per venue."""

    bybit_private: PrivateWsSocket
    bybit_trade: PrivateWsSocket
    okx_private: PrivateWsSocket
    okx_trade: PrivateWsSocket


WarmSocketProvider = Callable[[], WarmSocketBundle]


@dataclass
class PrivateWarmSession:
    """Long-lived dual-venue private+trade WS session for one process."""

    journal: PrivateJournalWriter
    bybit_runtime: PrivateStreamRuntime
    okx_runtime: PrivateStreamRuntime
    bybit_credentials: LiveCredentials
    okx_credentials: LiveCredentials
    env: dict[str, str]
    socket_provider: WarmSocketProvider
    rest_probe_fn: Optional[Any] = None
    ack_timeout_sec: float = 5.0
    bybit_symbol: str = _DEFAULT_BYBIT_SYMBOL
    okx_symbol: str = _DEFAULT_OKX_SYMBOL
    heartbeat_every_sec: float = _DEFAULT_HEARTBEAT_EVERY_SEC
    poll_sec: float = _DEFAULT_POLL_SEC
    silence_timeout_sec: float = _DEFAULT_SILENCE_TIMEOUT_SEC
    reconnect_base_sec: float = _RECONNECT_BASE_SEC
    reconnect_cap_sec: float = _RECONNECT_CAP_SEC
    _started: bool = False
    _stopped: bool = False
    _handshake_count: int = 0
    _fail_attempt: int = 0
    _reconnect_attempt_times: list[float] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _keepalive_stop: threading.Event = field(default_factory=threading.Event)
    _keepalive_thread: Optional[threading.Thread] = None
    _external_stop: Any = None
    _last_hb_mono: float = 0.0
    # >0 while W6/W7 place+ack (or flatten) holds trade I/O; keepalive must not
    # recv/close sockets or steal trade ACK frames during that window.
    _place_inflight: int = 0

    @property
    def run_id(self) -> str:
        return str(self.journal.run_id)

    @property
    def keepalive_running(self) -> bool:
        t = self._keepalive_thread
        return t is not None and t.is_alive()

    @property
    def place_inflight(self) -> bool:
        return self._place_inflight > 0

    @contextmanager
    def place_io_section(self) -> Iterator[None]:
        """Serialize warm keepalive against in-flight trade place/ack/recv.

        Acquires the session lock only to bump the counter (so a mid-tick
        keepalive finishes first), then releases so parallel W6 workers can
        place on both venues concurrently. Keepalive skips all socket I/O and
        reconnect while ``place_inflight``.
        """
        with self._lock:
            if self._stopped:
                raise RuntimeError("warm session already stopped")
            self._place_inflight += 1
        try:
            yield
        finally:
            with self._lock:
                self._place_inflight = max(0, int(self._place_inflight) - 1)

    def is_ready(self) -> bool:
        if self._stopped or not self._started:
            return False
        if self._any_socket_dead():
            return False
        for rt in (self.bybit_runtime, self.okx_runtime):
            if not rt.authenticated:
                return False
            if rt.sequence_state != SequenceHealth.HEALTHY:
                return False
            if rt.subscription_readiness != SubscriptionReadiness.READY:
                return False
            if rt.sends_blocked or rt.reseed_required:
                return False
        return True

    def auth_success_events(self) -> list[dict[str, Any]]:
        """Journal auth successes for this run (hermetic accounting)."""
        out: list[dict[str, Any]] = []
        for ev in scan_all_journal_events(self.journal.data_root):
            if str(ev.get("run_id") or "") != self.run_id:
                continue
            if ev.get("event_type") != "auth":
                continue
            if ev.get("outcome") != "success":
                continue
            out.append(ev)
        return out

    def _any_socket_dead(self) -> bool:
        for rt in (self.bybit_runtime, self.okx_runtime):
            for sock in (rt.private_socket, rt.trade_socket):
                if sock is None:
                    return True
                if not getattr(sock, "connected", False):
                    return True
        return False

    def note_disconnect(self, *, exchange: Optional[str] = None) -> None:
        """Mark reconnect required after socket drop (public-policy analogue)."""
        with self._lock:
            if self._place_inflight > 0:
                # Never tear down sockets under an in-flight place/ack; the
                # place path fail-closes ambiguously if transport errors.
                LOG.warning(
                    "warm_disconnect_deferred_place_inflight exchange=%s run_id=%s",
                    exchange or "both",
                    self.run_id,
                )
                return
            targets: Sequence[PrivateStreamRuntime]
            if exchange is None:
                targets = (self.bybit_runtime, self.okx_runtime)
            elif exchange == "bybit":
                targets = (self.bybit_runtime,)
            elif exchange == "okx":
                targets = (self.okx_runtime,)
            else:
                raise ValueError(f"exchange must be bybit|okx, got {exchange!r}")
            for rt in targets:
                rt.mark_reconnect()
                for sock in (rt.private_socket, rt.trade_socket):
                    if sock is not None:
                        try:
                            sock.close()
                        except Exception:  # noqa: BLE001
                            pass
                rt.private_socket = None
                rt.trade_socket = None
            LOG.info(
                "warm_disconnect exchange=%s run_id=%s",
                exchange or "both",
                self.run_id,
            )

    def start(self) -> None:
        """Connect and handshake both venues. No-op when already ready."""
        with self._lock:
            if self._stopped:
                raise RuntimeError("warm session already stopped")
            if self.is_ready():
                return
            self._bind_fresh_sockets()
            self._handshake_both()
            self._started = True
            self._last_hb_mono = time.monotonic()
            LOG.info(
                "warm_started run_id=%s handshake_count=%s ready=%s",
                self.run_id,
                self._handshake_count,
                self.is_ready(),
            )

    def ensure_ready(self) -> None:
        """Reuse healthy session; reconnect+handshake only when needed.

        Prefer background keep-alive recovery. Callers on the send path should
        normally find the session already ready after an idle drop.
        """
        with self._lock:
            if self._stopped:
                raise RuntimeError("warm session already stopped")
            if self.is_ready():
                return
            if self._started:
                self.note_disconnect()
            self._bind_fresh_sockets()
            self._handshake_both()
            self._started = True
            self._last_hb_mono = time.monotonic()
            self._fail_attempt = 0

    def start_keepalive(self, *, stop_event: Any = None) -> None:
        """Start background heartbeat + reconnect supervisor (public L1 analogue).

        Reconnects while idle — not lazily on the next send.
        """
        with self._lock:
            if self._stopped:
                raise RuntimeError("warm session already stopped")
            if self.keepalive_running:
                return
            self._external_stop = stop_event
            self._keepalive_stop.clear()
            t = threading.Thread(
                target=self._keepalive_loop,
                name=f"bbot-private-warm-{self.run_id[:12]}",
                daemon=True,
            )
            self._keepalive_thread = t
            t.start()
            LOG.info("warm_keepalive_started run_id=%s", self.run_id)

    def stop_keepalive(self, *, join_timeout_sec: float = 2.0) -> None:
        """Stop the background supervisor thread."""
        self._keepalive_stop.set()
        t = self._keepalive_thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=float(join_timeout_sec))
        self._keepalive_thread = None

    def stop(self) -> None:
        """Close sockets and stop keep-alive. Does not delete journal history."""
        self._stopped = True
        self.stop_keepalive()
        with self._lock:
            for rt in (self.bybit_runtime, self.okx_runtime):
                for sock in (rt.private_socket, rt.trade_socket):
                    if sock is not None:
                        try:
                            sock.close()
                        except Exception:  # noqa: BLE001
                            pass
                rt.private_socket = None
                rt.trade_socket = None
        LOG.info("warm_stopped run_id=%s", self.run_id)

    def _stop_requested(self) -> bool:
        if self._keepalive_stop.is_set() or self._stopped:
            return True
        ext = self._external_stop
        if ext is not None and getattr(ext, "is_set", lambda: False)():
            return True
        return False

    def _keepalive_loop(self) -> None:
        while not self._stop_requested():
            try:
                # Skip all I/O / reconnect while place+ack holds trade sockets.
                with self._lock:
                    placing = self._place_inflight > 0
                if placing:
                    self._keepalive_stop.wait(timeout=float(self.poll_sec))
                    continue
                if not self.is_ready():
                    self._recover_with_backoff()
                else:
                    self._keepalive_tick()
            except Exception as exc:  # noqa: BLE001
                LOG.warning(
                    "warm_keepalive_tick_error run_id=%s err=%s",
                    self.run_id,
                    type(exc).__name__,
                )
                try:
                    self.note_disconnect()
                except Exception:  # noqa: BLE001
                    pass
            # Interruptible idle poll (also spaces healthy ticks).
            self._keepalive_stop.wait(timeout=float(self.poll_sec))

    def _recover_with_backoff(self) -> None:
        if self._stop_requested():
            return
        with self._lock:
            if self._place_inflight > 0:
                return
        delay = reconnect_sleep_sec(
            self._fail_attempt,
            base=float(self.reconnect_base_sec),
            cap=float(self.reconnect_cap_sec),
        )
        self._fail_attempt += 1
        self._reconnect_attempt_times.append(time.monotonic())
        LOG.info(
            "warm_reconnect_backoff run_id=%s attempt=%s sleep_sec=%.3f",
            self.run_id,
            self._fail_attempt,
            delay,
        )
        # Sleep outside the reconnect work; stoppable.
        if self._keepalive_stop.wait(timeout=delay):
            return
        if self._stop_requested():
            return
        try:
            with self._lock:
                if self._stopped:
                    return
                if self._place_inflight > 0:
                    return
                if self.is_ready():
                    self._fail_attempt = 0
                    return
                if self._started or self._any_socket_dead():
                    # Clear dead sockets / auth state before rebinding.
                    for rt in (self.bybit_runtime, self.okx_runtime):
                        if rt.private_socket is not None or rt.trade_socket is not None:
                            rt.mark_reconnect()
                        for sock in (rt.private_socket, rt.trade_socket):
                            if sock is not None:
                                try:
                                    sock.close()
                                except Exception:  # noqa: BLE001
                                    pass
                        rt.private_socket = None
                        rt.trade_socket = None
                self._bind_fresh_sockets()
                self._handshake_both()
                self._started = True
                self._last_hb_mono = time.monotonic()
                self._fail_attempt = 0
                LOG.info(
                    "warm_reconnected run_id=%s handshake_count=%s",
                    self.run_id,
                    self._handshake_count,
                )
        except Exception as exc:  # noqa: BLE001
            LOG.warning(
                "warm_reconnect_failed run_id=%s err=%s",
                self.run_id,
                type(exc).__name__,
            )

    def _keepalive_tick(self) -> None:
        with self._lock:
            if self._stopped or self._place_inflight > 0 or not self.is_ready():
                return
            now = time.monotonic()
            if (now - self._last_hb_mono) >= float(self.heartbeat_every_sec):
                for rt in (self.bybit_runtime, self.okx_runtime):
                    try:
                        rt.send_heartbeat()
                        rt.send_trade_heartbeat()
                    except Exception:  # noqa: BLE001
                        self.note_disconnect()
                        return
                self._last_hb_mono = now
            # Drain private inboxes (heartbeat ack / order updates).
            for rt in (self.bybit_runtime, self.okx_runtime):
                sock = rt.private_socket
                if sock is None:
                    self.note_disconnect()
                    return
                try:
                    raw = sock.recv_text(timeout_sec=_DEFAULT_RECV_TIMEOUT_SEC)
                except TimeoutError:
                    if rt.silence_exceeded(
                        silence_timeout_sec=float(self.silence_timeout_sec)
                    ):
                        rt.handle_silence_timeout()
                        self.note_disconnect()
                        return
                    # Fall through to trade drain / silence below.
                except Exception:  # noqa: BLE001
                    self.note_disconnect()
                    return
                else:
                    try:
                        rt.handle_inbound_text(raw)
                    except Exception:  # noqa: BLE001
                        self.note_disconnect()
                        return
            # Drain trade inboxes for pong/noise only; stash anything else so
            # place/ack cannot lose frames to keepalive.
            from app.bot.private.ws_private import is_ws_noise_frame

            for rt in (self.bybit_runtime, self.okx_runtime):
                sock = rt.trade_socket
                if sock is None:
                    self.note_disconnect()
                    return
                try:
                    raw = sock.recv_text(timeout_sec=_DEFAULT_RECV_TIMEOUT_SEC)
                except TimeoutError:
                    if rt.trade_silence_exceeded(
                        silence_timeout_sec=float(self.silence_timeout_sec)
                    ):
                        rt.handle_silence_timeout()
                        self.note_disconnect()
                        return
                    continue
                except Exception:  # noqa: BLE001
                    self.note_disconnect()
                    return
                rt.note_trade_activity()
                if is_ws_noise_frame(rt.exchange, raw):
                    continue
                rt.stash_trade_inbound(raw)
            if self._any_socket_dead():
                self.note_disconnect()

    def _bind_fresh_sockets(self) -> None:
        bundle = self.socket_provider()
        self.bybit_runtime.bind_sockets(
            private=bundle.bybit_private,
            trade=bundle.bybit_trade,
            env=self.env,
        )
        self.okx_runtime.bind_sockets(
            private=bundle.okx_private,
            trade=bundle.okx_trade,
            env=self.env,
        )

    def _handshake_both(self) -> None:
        for runtime, exchange in (
            (self.bybit_runtime, "bybit"),
            (self.okx_runtime, "okx"),
        ):
            err = _handshake_private_and_trade(
                runtime,
                exchange=exchange,
                ack_timeout_sec=float(self.ack_timeout_sec),
            )
            if err is not None:
                raise RuntimeError(f"warm handshake failed exchange={exchange} err={err}")
        self._handshake_count += 1


_PROCESS_SESSION: Optional[PrivateWarmSession] = None


def get_process_warm_session() -> Optional[PrivateWarmSession]:
    """Return the process-attached warm session, if any."""
    return _PROCESS_SESSION


def attach_process_warm_session(session: PrivateWarmSession) -> PrivateWarmSession:
    """Attach a warm session for subsequent live sends in this process."""
    global _PROCESS_SESSION
    if _PROCESS_SESSION is not None and _PROCESS_SESSION is not session:
        if not _PROCESS_SESSION._stopped:  # noqa: SLF001
            _PROCESS_SESSION.stop()
    _PROCESS_SESSION = session
    return session


def clear_process_warm_session(*, stop: bool = True) -> None:
    """Detach (and optionally stop) the process warm session."""
    global _PROCESS_SESSION
    if _PROCESS_SESSION is not None and stop and not _PROCESS_SESSION._stopped:  # noqa: SLF001
        _PROCESS_SESSION.stop()
    _PROCESS_SESSION = None


def live_private_send_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """True when production private-live send is armed (VENUE=live + LIVE_ORDERS=1)."""
    e = dict(env if env is not None else os.environ)
    try:
        return resolve_venue(e) == "live" and live_orders_enabled(e)
    except ValueError:
        return False


def _creds_from_live_secrets(secrets: Any, exchange: str) -> LiveCredentials:
    if exchange == "bybit":
        return LiveCredentials(
            api_key=secrets.bybit_api_key,
            api_secret=secrets.bybit_api_secret,
        )
    return LiveCredentials(
        api_key=secrets.okx_api_key,
        api_secret=secrets.okx_api_secret,
        passphrase=secrets.okx_passphrase,
    )


def _production_socket_provider() -> WarmSocketProvider:
    """Bind websockets factory if needed and open private+trade sockets."""
    from app.bot.private.ws_socket import (
        WebsocketsSocketFactory,
        bind_socket_factory,
        get_socket_factory,
    )

    factory = get_socket_factory()
    if factory is None:
        factory = WebsocketsSocketFactory()
        bind_socket_factory(factory)
    ep = endpoints_for_venue("live")

    def _open() -> WarmSocketBundle:
        return WarmSocketBundle(
            bybit_private=factory.open(ep.bybit_private_ws),
            bybit_trade=factory.open(trade_ws_url_for_exchange("bybit", ep)),
            okx_private=factory.open(ep.okx_private_ws),
            okx_trade=factory.open(trade_ws_url_for_exchange("okx", ep)),
        )

    return _open


def _build_runtime(
    *,
    exchange: str,
    symbol: str,
    journal: PrivateJournalWriter,
    credentials: LiveCredentials,
    env: Mapping[str, str],
    rest_probe_fn: Optional[Any],
    profile_gate: Any,
) -> PrivateStreamRuntime:
    reseed = build_signed_rest_reseed(
        exchange=exchange,
        credentials=credentials,
        endpoints=endpoints_for_venue("live"),
        probe_fn=rest_probe_fn,
    )
    return PrivateStreamRuntime.create_gated(
        exchange=exchange,
        symbol_alias=symbol,
        journal=journal,
        credentials=credentials,
        env=env,
        rest_reseed=reseed,
        profile_gate=profile_gate,
    )


def start_warm_private_session(
    *,
    env: Optional[Mapping[str, str]] = None,
    bybit_credentials: LiveCredentials,
    okx_credentials: LiveCredentials,
    socket_provider: WarmSocketProvider,
    journal: Optional[PrivateJournalWriter] = None,
    data_root: Optional[Path] = None,
    rest_probe_fn: Optional[Any] = None,
    ack_timeout_sec: float = 5.0,
    bybit_symbol: str = _DEFAULT_BYBIT_SYMBOL,
    okx_symbol: str = _DEFAULT_OKX_SYMBOL,
    profile_gate: Any = None,
    attach: bool = True,
    keepalive: bool = False,
    stop_event: Any = None,
    heartbeat_every_sec: float = _DEFAULT_HEARTBEAT_EVERY_SEC,
    poll_sec: float = _DEFAULT_POLL_SEC,
    silence_timeout_sec: float = _DEFAULT_SILENCE_TIMEOUT_SEC,
    reconnect_base_sec: float = _RECONNECT_BASE_SEC,
    reconnect_cap_sec: float = _RECONNECT_CAP_SEC,
) -> PrivateWarmSession:
    """Start (or reuse) the process-lifetime private WS session.

    Call at bot / private-live unit startup — before any signal/send.
    Private sockets are enabled by default once this runs; there is no
    default-off warm flag. Set ``keepalive=True`` for background
    heartbeat/reconnect (production bot path).
    """
    e = dict(env if env is not None else os.environ)
    gate = profile_gate if profile_gate is not None else assert_ws_warm_private_gates
    try:
        gate(e)
    except WsProfileGateError:
        raise

    existing = get_process_warm_session()
    if (
        existing is not None
        and not existing._stopped  # noqa: SLF001
        and existing.bybit_symbol == bybit_symbol
        and existing.okx_symbol == okx_symbol
    ):
        existing.ensure_ready()
        if keepalive and not existing.keepalive_running:
            existing.start_keepalive(stop_event=stop_event)
        return existing

    root = data_root if data_root is not None else resolve_data_root(e)
    j = journal if journal is not None else PrivateJournalWriter(
        root, run_id=new_opaque_id("run")
    )
    bybit_rt = _build_runtime(
        exchange="bybit",
        symbol=bybit_symbol,
        journal=j,
        credentials=bybit_credentials,
        env=e,
        rest_probe_fn=rest_probe_fn,
        profile_gate=gate,
    )
    okx_rt = _build_runtime(
        exchange="okx",
        symbol=okx_symbol,
        journal=j,
        credentials=okx_credentials,
        env=e,
        rest_probe_fn=rest_probe_fn,
        profile_gate=gate,
    )
    session = PrivateWarmSession(
        journal=j,
        bybit_runtime=bybit_rt,
        okx_runtime=okx_rt,
        bybit_credentials=bybit_credentials,
        okx_credentials=okx_credentials,
        env=e,
        socket_provider=socket_provider,
        rest_probe_fn=rest_probe_fn,
        ack_timeout_sec=float(ack_timeout_sec),
        bybit_symbol=bybit_symbol,
        okx_symbol=okx_symbol,
        heartbeat_every_sec=float(heartbeat_every_sec),
        poll_sec=float(poll_sec),
        silence_timeout_sec=float(silence_timeout_sec),
        reconnect_base_sec=float(reconnect_base_sec),
        reconnect_cap_sec=float(reconnect_cap_sec),
    )
    session.start()
    if attach:
        attach_process_warm_session(session)
    if keepalive:
        session.start_keepalive(stop_event=stop_event)
    return session


def ensure_process_warm_session(
    **kwargs: Any,
) -> PrivateWarmSession:
    """Alias for startup: ensure a warm private session is attached and ready."""
    return start_warm_private_session(**kwargs)


def start_warm_private_for_bot_process(
    *,
    env: Optional[Mapping[str, str]] = None,
    socket_provider: Optional[WarmSocketProvider] = None,
    bybit_credentials: Optional[LiveCredentials] = None,
    okx_credentials: Optional[LiveCredentials] = None,
    rest_probe_fn: Optional[Any] = None,
    data_root: Optional[Path] = None,
    journal: Optional[PrivateJournalWriter] = None,
    ack_timeout_sec: float = 5.0,
    bybit_symbol: str = _DEFAULT_BYBIT_SYMBOL,
    okx_symbol: str = _DEFAULT_OKX_SYMBOL,
    attach: bool = True,
    stop_event: Any = None,
    heartbeat_every_sec: float = _DEFAULT_HEARTBEAT_EVERY_SEC,
    poll_sec: float = _DEFAULT_POLL_SEC,
    silence_timeout_sec: float = _DEFAULT_SILENCE_TIMEOUT_SEC,
    reconnect_base_sec: float = _RECONNECT_BASE_SEC,
    reconnect_cap_sec: float = _RECONNECT_CAP_SEC,
) -> Optional[PrivateWarmSession]:
    """Production ``app.bot`` hook: warm private WS when live private send is on.

    Returns ``None`` when ``VENUE``/``LIVE_ORDERS`` do not arm live private send
    (stub / would_send). When armed, starts private sockets + background
    keep-alive by default before the signal loop — no opt-in warm flag.
    Fail-closed on handshake/secret errors so the process does not enter the
    signal loop cold.
    """
    e = dict(env if env is not None else os.environ)
    if not live_private_send_enabled(e):
        return None

    assert_ws_warm_private_gates(e)

    if bybit_credentials is None or okx_credentials is None:
        from app.bot.private.secrets import load_live_secrets

        secrets = load_live_secrets(e, require_complete=True)
        if bybit_credentials is None:
            bybit_credentials = _creds_from_live_secrets(secrets, "bybit")
        if okx_credentials is None:
            okx_credentials = _creds_from_live_secrets(secrets, "okx")

    provider = (
        socket_provider if socket_provider is not None else _production_socket_provider()
    )
    return start_warm_private_session(
        env=e,
        bybit_credentials=bybit_credentials,
        okx_credentials=okx_credentials,
        socket_provider=provider,
        journal=journal,
        data_root=data_root,
        rest_probe_fn=rest_probe_fn,
        ack_timeout_sec=float(ack_timeout_sec),
        bybit_symbol=bybit_symbol,
        okx_symbol=okx_symbol,
        profile_gate=assert_ws_warm_private_gates,
        attach=attach,
        keepalive=True,
        stop_event=stop_event,
        heartbeat_every_sec=float(heartbeat_every_sec),
        poll_sec=float(poll_sec),
        silence_timeout_sec=float(silence_timeout_sec),
        reconnect_base_sec=float(reconnect_base_sec),
        reconnect_cap_sec=float(reconnect_cap_sec),
    )
