"""Process-lifetime private WS supervisor (OKX + Bybit).

Public L1 sockets already stay up for the life of the bot process
(``app/bot/ws_books.py``). Private historically cold-started auth + subscribe
+ REST reseed (+ operator approval setup) on every dual-leg send. This module
holds one warm private session per process so a signal does not pay that
round-trip.

Policy (same as public reconnect):
- connect and handshake at private-live / unit startup;
- re-auth / re-subscribe / REST reseed only on disconnect, auth failure, or
  explicit reconnect;
- live send reuses the same ``run_id`` / journal (no ``event_seq=1`` auth storm).

``LIVE_ORDERS=1`` alone still never opens a socket — an explicit
``start_warm_private_session`` (or bot private-live startup that calls it)
is required. Once started, private connections are on by default (no
default-off warm switch).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from app.bot.private.journal_v1 import (
    PrivateJournalWriter,
    new_opaque_id,
    scan_all_journal_events,
)
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.paths import resolve_data_root
from app.bot.private.venue import endpoints_for_venue
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
    _started: bool = False
    _stopped: bool = False
    _handshake_count: int = 0

    @property
    def run_id(self) -> str:
        return str(self.journal.run_id)

    def is_ready(self) -> bool:
        if self._stopped or not self._started:
            return False
        for rt in (self.bybit_runtime, self.okx_runtime):
            if rt.private_socket is None or rt.trade_socket is None:
                return False
            if not getattr(rt.private_socket, "connected", False):
                return False
            if not getattr(rt.trade_socket, "connected", False):
                return False
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

    def note_disconnect(self, *, exchange: Optional[str] = None) -> None:
        """Mark reconnect required after socket drop (public-policy analogue)."""
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
        if self._stopped:
            raise RuntimeError("warm session already stopped")
        if self.is_ready():
            return
        self._bind_fresh_sockets()
        self._handshake_both()
        self._started = True
        LOG.info(
            "warm_started run_id=%s handshake_count=%s ready=%s",
            self.run_id,
            self._handshake_count,
            self.is_ready(),
        )

    def ensure_ready(self) -> None:
        """Reuse healthy session; reconnect+handshake only when needed."""
        if self._stopped:
            raise RuntimeError("warm session already stopped")
        if self.is_ready():
            return
        if self._started:
            # Disconnect / auth failure path — same policy as public L1.
            self.note_disconnect()
        self.start()

    def stop(self) -> None:
        """Close sockets. Does not delete journal history."""
        self._stopped = True
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
) -> PrivateWarmSession:
    """Start (or reuse) the process-lifetime private WS session.

    Call at bot / private-live unit startup — before any signal/send.
    Private sockets are enabled by default once this runs; there is no
    default-off warm flag.
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
    )
    session.start()
    if attach:
        attach_process_warm_session(session)
    return session


def ensure_process_warm_session(
    **kwargs: Any,
) -> PrivateWarmSession:
    """Alias for startup: ensure a warm private session is attached and ready."""
    return start_warm_private_session(**kwargs)
