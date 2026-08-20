"""Explicit private WS read-only runner (not default CLI).

Invoke only via ``--ws-readonly`` with ``VENUE=live`` and ``LIVE_ORDERS=0``.
No trade WS place/cancel, no order sender. Profile gate runs before any socket.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.order_symbols import allowed_native_symbol
from app.bot.private.paths import resolve_data_root
from app.bot.private.secrets import LiveSecrets, load_live_secrets
from app.bot.private.venue import endpoints_for_venue
from app.bot.private.ws_gates import (
    WsProfileGateError,
    assert_ws_readonly_cli_gates,
)
from app.bot.private.ws_private import PrivateStreamRuntime
from app.bot.private.ws_reseed import build_signed_rest_reseed
from app.bot.private.ws_socket import (
    PrivateWsSocket,
    SocketFactory,
    WebsocketsSocketFactory,
    bind_socket_factory,
    unbind_socket_factory,
)

LOG = logging.getLogger("bbot.private.ws_readonly")

DEFAULT_SILENCE_TIMEOUT_SEC = 30.0
DEFAULT_RECV_TIMEOUT_SEC = 5.0
DEFAULT_HEARTBEAT_EVERY_SEC = 15.0


@dataclass
class WsReadonlyReport:
    status: str
    exchange: str
    symbol_alias: str
    reconnect_generation: int = 0
    authenticated: bool = False
    subscription_ready: bool = False
    reseed_matched: bool = False
    sends_blocked: bool = True
    cycles: int = 0
    silence_timeouts: int = 0
    journal_events: int = 0
    error_code: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def as_public_dict(self) -> dict[str, Any]:
        out = {
            "status": self.status,
            "exchange": self.exchange,
            "symbol_alias": self.symbol_alias,
            "reconnect_generation": self.reconnect_generation,
            "authenticated": self.authenticated,
            "subscription_ready": self.subscription_ready,
            "reseed_matched": self.reseed_matched,
            "sends_blocked": self.sends_blocked,
            "cycles": self.cycles,
            "silence_timeouts": self.silence_timeouts,
            "journal_events": self.journal_events,
            "orders_sent": 0,
            "trade_ws_bound": False,
        }
        if self.error_code:
            out["error_code"] = self.error_code
        out.update(self.extras)
        return out


def _creds_from_live(secrets: LiveSecrets, exchange: str) -> LiveCredentials:
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


def _private_ws_url(exchange: str) -> str:
    ep = endpoints_for_venue("live")
    if exchange == "bybit":
        return ep.bybit_private_ws
    return ep.okx_private_ws


def run_ws_readonly_preflight(
    *,
    exchange: str,
    env: Optional[Mapping[str, str]] = None,
    socket_factory: Optional[SocketFactory] = None,
    private_socket: Optional[PrivateWsSocket] = None,
    rest_probe_fn: Optional[Any] = None,
    max_cycles: int = 3,
    silence_timeout_sec: float = DEFAULT_SILENCE_TIMEOUT_SEC,
    recv_timeout_sec: float = DEFAULT_RECV_TIMEOUT_SEC,
    heartbeat_every_sec: float = DEFAULT_HEARTBEAT_EVERY_SEC,
    data_root: Optional[Path] = None,
    load_secrets: bool = True,
    credentials: Optional[LiveCredentials] = None,
    journal: Optional[PrivateJournalWriter] = None,
) -> WsReadonlyReport:
    """Auth → one-symbol subscribe → REST reseed → heartbeat/silence loop.

    No trade WS. No order send. Gates before any socket open.
    """
    e = dict(env if env is not None else os.environ)
    exchange = exchange.strip().lower()
    if exchange not in {"bybit", "okx"}:
        raise ValueError(f"exchange must be bybit or okx, got {exchange!r}")

    try:
        assert_ws_readonly_cli_gates(e)
    except WsProfileGateError as exc:
        return WsReadonlyReport(
            status="rejected_before_socket",
            exchange=exchange,
            symbol_alias="",
            error_code="invalid_request",
            extras={"gate_error": type(exc).__name__},
        )

    symbol = allowed_native_symbol(f"{exchange}_live")
    root = data_root if data_root is not None else resolve_data_root(e)
    j = journal if journal is not None else PrivateJournalWriter(root, run_id=new_opaque_id("run"))

    if credentials is None:
        if not load_secrets:
            return WsReadonlyReport(
                status="secrets_unavailable",
                exchange=exchange,
                symbol_alias=symbol,
                error_code="auth_unavailable",
            )
        try:
            secrets = load_live_secrets(e, require_complete=True)
        except Exception:  # noqa: BLE001
            return WsReadonlyReport(
                status="secrets_unavailable",
                exchange=exchange,
                symbol_alias=symbol,
                error_code="auth_unavailable",
            )
        credentials = _creds_from_live(secrets, exchange)

    reseed_port = build_signed_rest_reseed(
        exchange=exchange,
        credentials=credentials,
        endpoints=endpoints_for_venue("live"),
        probe_fn=rest_probe_fn,
    )
    runtime = PrivateStreamRuntime.create_gated(
        exchange=exchange,
        symbol_alias=symbol,
        journal=j,
        credentials=credentials,
        env=e,
        rest_reseed=reseed_port,
    )

    bound_factory = False
    try:
        if private_socket is not None:
            runtime.bind_sockets(private=private_socket, env=e)
        else:
            factory = socket_factory if socket_factory is not None else WebsocketsSocketFactory()
            bind_socket_factory(factory)
            bound_factory = True
            runtime.connect_private(_private_ws_url(exchange), env=e)

        # Auth
        runtime.send_auth()
        try:
            auth_raw = runtime.private_socket.recv_text(timeout_sec=recv_timeout_sec)  # type: ignore[union-attr]
        except TimeoutError:
            return _report_from_runtime(
                runtime,
                status="auth_timeout",
                error_code="timeout",
                journal=j,
            )
        auth_ev = runtime.handle_inbound_text(auth_raw)
        if auth_ev.kind != "auth_ack" or not runtime.authenticated:
            return _report_from_runtime(
                runtime,
                status="auth_failed",
                error_code="auth_failed",
                journal=j,
            )

        # Subscribe one-symbol order/position topics
        runtime.send_subscribe()
        try:
            sub_raw = runtime.private_socket.recv_text(timeout_sec=recv_timeout_sec)  # type: ignore[union-attr]
        except TimeoutError:
            return _report_from_runtime(
                runtime,
                status="subscribe_timeout",
                error_code="timeout",
                journal=j,
            )
        sub_ev = runtime.handle_inbound_text(sub_raw)
        if sub_ev.kind != "sub_ack" or sub_ev.ack_ok is False:
            return _report_from_runtime(
                runtime,
                status="subscribe_failed",
                error_code="venue_rejected",
                journal=j,
            )

        # REST seed/reseed (categorical)
        reseed_ev = runtime.run_rest_reseed()
        reseed_matched = (
            reseed_ev.get("reconciliation_state") == "matched"
            and not runtime.reseed_required
        )
        if not reseed_matched:
            return _report_from_runtime(
                runtime,
                status="reseed_required",
                error_code="unknown",
                journal=j,
                reseed_matched=False,
            )

        # Heartbeat / silence loop (read-only; no trade WS)
        silence_timeouts = 0
        cycles = 0
        last_hb = time.monotonic_ns()
        while cycles < max_cycles:
            cycles += 1
            now = time.monotonic_ns()
            if (now - last_hb) >= int(heartbeat_every_sec * 1_000_000_000):
                runtime.send_heartbeat()
                last_hb = now
            try:
                assert runtime.private_socket is not None
                raw = runtime.private_socket.recv_text(timeout_sec=recv_timeout_sec)
                runtime.handle_inbound_text(raw)
            except TimeoutError:
                if runtime.silence_exceeded(silence_timeout_sec=silence_timeout_sec):
                    silence_timeouts += 1
                    runtime.handle_silence_timeout()
                    # After silence → reconnect block until REST reseed again.
                    return _report_from_runtime(
                        runtime,
                        status="silence_timeout_reseed_required",
                        error_code="timeout",
                        journal=j,
                        reseed_matched=False,
                        cycles=cycles,
                        silence_timeouts=silence_timeouts,
                    )
            if runtime.reseed_required:
                return _report_from_runtime(
                    runtime,
                    status="reseed_required",
                    error_code="unknown",
                    journal=j,
                    reseed_matched=False,
                    cycles=cycles,
                    silence_timeouts=silence_timeouts,
                )

        return _report_from_runtime(
            runtime,
            status="ok",
            journal=j,
            reseed_matched=True,
            cycles=cycles,
            silence_timeouts=silence_timeouts,
        )
    finally:
        if runtime.private_socket is not None:
            try:
                runtime.private_socket.close()
            except Exception:  # noqa: BLE001
                pass
        if bound_factory:
            unbind_socket_factory()


def _report_from_runtime(
    runtime: PrivateStreamRuntime,
    *,
    status: str,
    journal: PrivateJournalWriter,
    error_code: Optional[str] = None,
    reseed_matched: Optional[bool] = None,
    cycles: int = 0,
    silence_timeouts: int = 0,
) -> WsReadonlyReport:
    matched = (
        (not runtime.reseed_required and not runtime.sends_blocked)
        if reseed_matched is None
        else reseed_matched
    )
    return WsReadonlyReport(
        status=status,
        exchange=runtime.exchange,
        symbol_alias=runtime.symbol_alias,
        reconnect_generation=runtime.reconnect_generation,
        authenticated=runtime.authenticated,
        subscription_ready=runtime.subscription_readiness.value == "ready",
        reseed_matched=bool(matched),
        sends_blocked=runtime.sends_blocked,
        cycles=cycles,
        silence_timeouts=silence_timeouts,
        journal_events=int(getattr(journal, "_seq", 0) or 0),
        error_code=error_code,
    )


def main_ws_readonly(
    argv: Optional[Sequence[str]] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> int:
    """CLI entry for ``--ws-readonly`` (exchange via ``--exchange=bybit|okx``)."""
    argv = list(argv or [])
    exchange = "bybit"
    for arg in argv:
        if arg.startswith("--exchange="):
            exchange = arg.split("=", 1)[1].strip().lower()
    report = run_ws_readonly_preflight(exchange=exchange, env=env)
    print(json.dumps(report.as_public_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    if report.status == "ok":
        return 0
    if report.status in {"secrets_unavailable", "rejected_before_socket"}:
        return 1
    return 2
