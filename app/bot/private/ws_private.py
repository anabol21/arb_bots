"""Private WS runtime for live Bybit/OKX (injected sockets only).

Default CLI does not connect. LIVE_ORDERS=1 alone never opens a socket.
Terminal state comes only from private order stream observations or REST
reconciliation — never from trade WS ACK.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from app.bot.private.journal_v1 import (
    PrivateJournalWriter,
    new_opaque_id,
)
from app.bot.private.order_lease import OrderStateSnapshot
from app.bot.private.order_plan import OrderPlan
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.venue import VenueEndpoints, endpoints_for_venue, send_allowed
from app.bot.private.ws_gates import (
    assert_ws_runtime_profile_gate,
    is_live_send_ws_profile_gate,
)
from app.bot.private.ws_messages import (
    WsOutboundMessage,
    build_bybit_ping,
    build_bybit_private_auth,
    build_bybit_private_subscribe,
    build_bybit_trade_cancel,
    build_bybit_trade_place,
    build_okx_ping,
    build_okx_private_login,
    build_okx_private_subscribe,
    build_okx_trade_cancel,
    build_okx_trade_place,
    new_okx_ws_id,
)
from app.bot.private.ws_socket import (
    PrivateWsSocket,
    assert_no_default_ws_socket,
    get_socket_factory,
    open_private_socket,
)

LOG = logging.getLogger("bbot.private.ws")

# Digits-only venue reject codes for public/report surfaces (never sMsg / frames).
_VENUE_CODE_RE = re.compile(r"^[0-9]{1,8}$")


def sanitize_venue_code(raw: object) -> Optional[str]:
    """Return digits-only venue code ``^[0-9]{1,8}$``, else None. Never messages."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        s = str(raw)
    elif isinstance(raw, float):
        if not raw.is_integer():
            return None
        s = str(int(raw))
    else:
        s = str(raw).strip()
    if _VENUE_CODE_RE.fullmatch(s):
        return s
    return None


def new_trade_req_id(*, exchange: str) -> str:
    """Venue-safe trade request correlation id.

    OKX Place/Cancel ``id`` must be alphanumeric (or all digits / all letters),
    case-sensitive, ≤32 chars — no underscore. Bybit keeps ``w4_{hex}``.
    """
    import uuid

    if exchange == "okx":
        return new_okx_ws_id(prefix="w4")
    return f"w4_{uuid.uuid4().hex[:24]}"


def trade_ws_url_for_exchange(
    exchange: str, endpoints: Optional[VenueEndpoints] = None
) -> str:
    """W4 trade socket URL: Bybit ``/v5/trade``; OKX ``/ws/v5/private`` (not business).

    OKX Place order / Cancel order require the private channel after login.
    Business (``/ws/v5/business``) is algo/orders-algo only — login can succeed
    while ``op=order`` is ignored (no ACK, no resting order).
    """
    ep = endpoints if endpoints is not None else endpoints_for_venue("live")
    if exchange == "bybit":
        return ep.bybit_trade_ws
    if exchange == "okx":
        return ep.okx_private_ws
    raise ValueError(f"unsupported exchange for trade ws url: {exchange!r}")


def is_ws_noise_frame(exchange: str, text: str) -> bool:
    """Categorical skip for ping/pong/welcome/nonterminal frames (no payload log)."""
    if text in {"ping", "pong"}:
        return True
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    if exchange == "bybit":
        op = str(data.get("op") or "")
        if op in {"ping", "pong"}:
            return True
        # Welcome / non-auth subscribe echoes without correlating reqId.
        if op == "subscribe" and "success" not in data and "retCode" not in data:
            return True
        return False
    event = str(data.get("event") or "")
    if event in {"ping", "pong", "channel-conn-count", "notice"}:
        return True
    op = str(data.get("op") or "")
    if op in {"ping", "pong"}:
        return True
    return False



class SequenceHealth(str, Enum):
    HEALTHY = "healthy"
    GAP = "gap"
    RESEED_REQUIRED = "reseed_required"


class SubscriptionReadiness(str, Enum):
    NOT_READY = "not_ready"
    READY = "ready"


@dataclass(frozen=True)
class RestReseedResult:
    """Categorical REST reseed outcome — no account/order identifiers."""

    matched: bool
    inconclusive: bool = False


class RestReseedPort(Protocol):
    """REST is seed/reseed/reconcile only — never the live order-state feed."""

    def reseed(
        self,
        *,
        venue: str,
        environment: str,
        reconnect_generation: int,
        symbol_alias: str,
    ) -> RestReseedResult:
        ...


@dataclass
class ParsedStreamEvent:
    """Categorical parse of an inbound private-stream update (no raw ids)."""

    kind: str  # auth_ack | sub_ack | auth_reject | heartbeat | order_update | duplicate | gap | ignored
    symbol_alias: Optional[str] = None
    terminal_state: Optional[str] = None  # filled | cancelled | expired
    working: bool = False
    sequence: Optional[int] = None
    # Correlation fingerprint for ``_order_states`` (not a venue order id).
    event_key: Optional[str] = None
    # Dedupe token: fingerprint + status (+ time hint). Distinct from event_key.
    dedupe_key: Optional[str] = None
    req_id: Optional[str] = None
    ack_ok: Optional[bool] = None


@dataclass
class TradeAckObservation:
    """Trade WS place/cancel ACK — maps to ack_received only, never terminal."""

    req_id: str
    accepted: bool
    ack_state: str  # accepted | received
    # Digits-only venue code (OKX sCode/code, Bybit retCode); never sMsg.
    venue_code: Optional[str] = None


def _order_stream_dedupe_key(
    *,
    correlation: Optional[str],
    status: str,
    time_hint: object = None,
) -> Optional[str]:
    """Dedupe New vs Cancelled separately; never fingerprint-alone."""
    if not correlation:
        return None
    hint = ""
    if time_hint is not None and not isinstance(time_hint, bool):
        hint = str(time_hint)
    return f"{correlation}:{str(status).lower()}:{hint}"


def _journal_venue(plan_venue: str) -> str:
    return "okx" if plan_venue.startswith("okx") else "bybit"


def _safe_log(event: str, **fields: object) -> None:
    # Never pass raw frame / signature / key / passphrase / order id / account values.
    forbidden = {
        "frame",
        "raw",
        "signature",
        "sign",
        "api_key",
        "api_secret",
        "passphrase",
        "order_id",
        "client_order_id",
        "account",
        "balance",
        "text",
        "payload",
    }
    clean = {k: v for k, v in fields.items() if k.lower() not in forbidden}
    LOG.info("ws_%s %s", event, " ".join(f"{k}={v}" for k, v in sorted(clean.items())))


@dataclass
class PrivateStreamRuntime:
    """One-symbol private stream + optional trade socket (both injected)."""

    exchange: str  # bybit | okx
    environment: str  # live | testnet | demo
    symbol_alias: str
    journal: PrivateJournalWriter
    run_id: str
    credentials: LiveCredentials
    private_socket: Optional[PrivateWsSocket] = None
    trade_socket: Optional[PrivateWsSocket] = None
    rest_reseed: Optional[RestReseedPort] = None
    # Env used for profile gate on connect/bind (required for production paths).
    gate_env: Optional[Mapping[str, str]] = None
    # Default W3 read-only gate; W4 runner injects assert_ws_w4_send_gates.
    profile_gate: Any = None
    stream_operation_id: str = field(default_factory=lambda: new_opaque_id("op_stream"))
    reconnect_generation: int = 0
    sequence_state: SequenceHealth = SequenceHealth.RESEED_REQUIRED
    subscription_readiness: SubscriptionReadiness = SubscriptionReadiness.NOT_READY
    authenticated: bool = False
    last_recv_mono_ns: Optional[int] = None
    last_trade_recv_mono_ns: Optional[int] = None
    _last_seq: Optional[int] = None
    _seen_event_keys: set[str] = field(default_factory=set)
    _order_states: dict[str, OrderStateSnapshot] = field(default_factory=dict)
    _sends_blocked: bool = True
    _fingerprint_by_attempt: dict[str, str] = field(default_factory=dict)
    # OKX WS Place/Cancel: positive instIdCode (from instruments). Bybit unused.
    okx_inst_id_code: Optional[int] = None
    # Keepalive may drain trade noise (pong); non-noise frames are stashed so
    # place/ack recv cannot lose them to the supervisor thread.
    _trade_inbound_stash: list[str] = field(default_factory=list)

    @property
    def sends_blocked(self) -> bool:
        return self._sends_blocked or self.sequence_state != SequenceHealth.HEALTHY

    @property
    def reseed_required(self) -> bool:
        return self.sequence_state in {
            SequenceHealth.GAP,
            SequenceHealth.RESEED_REQUIRED,
        }

    def assert_live_profile_allows_ws(self, env: Optional[Mapping[str, str]] = None) -> None:
        """Use profile gate; do not connect on wrong profile."""
        e = env if env is not None else self.gate_env
        gate = self.profile_gate or assert_ws_runtime_profile_gate
        if is_live_send_ws_profile_gate(gate):
            gate(e)
        else:
            gate(e, environment=self.environment)

    def _require_gate(self, env: Optional[Mapping[str, str]] = None) -> None:
        e = env if env is not None else self.gate_env
        gate = self.profile_gate or assert_ws_runtime_profile_gate
        if is_live_send_ws_profile_gate(gate):
            gate(e)
        else:
            gate(e, environment=self.environment)

    def connect_private(self, url: str, *, env: Optional[Mapping[str, str]] = None) -> None:
        self._require_gate(env)
        sock = open_private_socket(url)
        sock.connect()
        self.private_socket = sock
        self.last_recv_mono_ns = time.monotonic_ns()
        _safe_log("connect", exchange=self.exchange, channel="private", gen=self.reconnect_generation)

    def connect_trade(self, url: str, *, env: Optional[Mapping[str, str]] = None) -> None:
        """Trade WS connect — not used by W3 read-only runner."""
        self._require_gate(env)
        sock = open_private_socket(url)
        sock.connect()
        self.trade_socket = sock
        self.last_trade_recv_mono_ns = time.monotonic_ns()
        self._trade_inbound_stash.clear()
        _safe_log("connect", exchange=self.exchange, channel="trade", gen=self.reconnect_generation)

    def bind_sockets(
        self,
        *,
        private: PrivateWsSocket,
        trade: Optional[PrivateWsSocket] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        """Bind already-constructed sockets only after profile gate."""
        self._require_gate(env)
        private.connect()
        self.private_socket = private
        self.last_recv_mono_ns = time.monotonic_ns()
        if trade is not None:
            trade.connect()
            self.trade_socket = trade
            self.last_trade_recv_mono_ns = time.monotonic_ns()
            self._trade_inbound_stash.clear()

    @classmethod
    def create_gated(
        cls,
        *,
        exchange: str,
        symbol_alias: str,
        journal: PrivateJournalWriter,
        credentials: LiveCredentials,
        env: Mapping[str, str],
        rest_reseed: Optional[RestReseedPort] = None,
        profile_gate: Any = None,
    ) -> "PrivateStreamRuntime":
        """Construct a live WS runtime only when profile gate passes (no socket yet)."""
        gate = profile_gate or assert_ws_runtime_profile_gate
        if is_live_send_ws_profile_gate(gate):
            gate(env)
        else:
            gate(env, environment="live")
        return cls(
            exchange=exchange,
            environment="live",
            symbol_alias=symbol_alias,
            journal=journal,
            run_id=journal.run_id,
            credentials=credentials,
            rest_reseed=rest_reseed,
            gate_env=dict(env),
            profile_gate=gate,
        )
    def mark_reconnect(self) -> None:
        self.reconnect_generation += 1
        self.authenticated = False
        self.subscription_readiness = SubscriptionReadiness.NOT_READY
        self.sequence_state = SequenceHealth.RESEED_REQUIRED
        self._sends_blocked = True
        self._last_seq = None
        self._trade_inbound_stash.clear()
        self._journal_stream_recon(
            sequence_state=SequenceHealth.RESEED_REQUIRED,
            observation_source="private_ws",
            outcome="observed",
            recon_state="inconclusive",
        )
        _safe_log("reconnect", exchange=self.exchange, gen=self.reconnect_generation)

    def build_auth_message(self) -> WsOutboundMessage:
        if self.exchange == "bybit":
            return build_bybit_private_auth(self.credentials)
        if self.exchange == "okx":
            return build_okx_private_login(self.credentials)
        raise ValueError(f"unsupported exchange {self.exchange!r}")

    def build_subscribe_message(self) -> WsOutboundMessage:
        if self.exchange == "bybit":
            return build_bybit_private_subscribe()
        if self.exchange == "okx":
            return build_okx_private_subscribe(symbol=self.symbol_alias)
        raise ValueError(f"unsupported exchange {self.exchange!r}")

    def build_heartbeat(self) -> WsOutboundMessage:
        if self.exchange == "bybit":
            return build_bybit_ping()
        return build_okx_ping()

    def send_auth(self) -> None:
        assert self.private_socket is not None
        msg = self.build_auth_message()
        self.private_socket.send_text(msg.text)
        _safe_log("auth_sent", exchange=self.exchange, gen=self.reconnect_generation)

    def journal_auth(self, *, success: bool, error_code: Optional[str] = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "event_type": "auth",
            "operation_id": self.stream_operation_id,
            "venue": self.exchange,
            "environment": self.environment,
            "outcome": "success" if success else "failure",
            "auth_method": "hmac",
            "credential_presence": {"credentials_configured": True},
        }
        if not success:
            body["error_code"] = error_code or "auth_failed"
        return self.journal.append(body)

    def send_subscribe(self) -> dict[str, Any]:
        assert self.private_socket is not None
        if not self.authenticated:
            raise RuntimeError("subscribe requires successful auth")
        msg = self.build_subscribe_message()
        send_mono = time.monotonic_ns()
        self.private_socket.send_text(msg.text)
        self.subscription_readiness = SubscriptionReadiness.NOT_READY
        self._sends_blocked = True
        partial = {
            "event_type": "request_sent",
            "operation_id": self.stream_operation_id,
            "venue": self.exchange,
            "environment": self.environment,
            "outcome": "pending",
            "request_kind": "ws_subscribe",
            "transport_attempt": 1,
            "send_monotonic_ns": send_mono,
            "transport": "ws_trade",
            "reconnect_generation": self.reconnect_generation,
            "subscription_readiness": "not_ready",
        }
        ev = self.journal.append(partial)
        _safe_log("subscribe_sent", exchange=self.exchange, gen=self.reconnect_generation)
        return ev

    def send_heartbeat(self) -> None:
        assert self.private_socket is not None
        msg = self.build_heartbeat()
        self.private_socket.send_text(msg.text)
        _safe_log("heartbeat", exchange=self.exchange, gen=self.reconnect_generation)

    def send_trade_heartbeat(self) -> None:
        """Application ping on the trade socket (idle health; not private-only)."""
        assert self.trade_socket is not None
        msg = self.build_heartbeat()
        self.trade_socket.send_text(msg.text)
        # Successful send proves the trade socket is still writable; idle
        # silence uses this so a ping-only keepalive does not false-trip.
        self.note_trade_activity()
        _safe_log(
            "trade_heartbeat", exchange=self.exchange, gen=self.reconnect_generation
        )

    def silence_exceeded(self, *, silence_timeout_sec: float, now_mono_ns: Optional[int] = None) -> bool:
        if self.last_recv_mono_ns is None:
            return False
        now = now_mono_ns if now_mono_ns is not None else time.monotonic_ns()
        return (now - self.last_recv_mono_ns) >= int(silence_timeout_sec * 1_000_000_000)

    def trade_silence_exceeded(
        self, *, silence_timeout_sec: float, now_mono_ns: Optional[int] = None
    ) -> bool:
        if self.last_trade_recv_mono_ns is None:
            return False
        now = now_mono_ns if now_mono_ns is not None else time.monotonic_ns()
        return (now - self.last_trade_recv_mono_ns) >= int(
            silence_timeout_sec * 1_000_000_000
        )

    def note_trade_activity(self) -> None:
        self.last_trade_recv_mono_ns = time.monotonic_ns()

    def stash_trade_inbound(self, text: str) -> None:
        """Preserve a non-noise trade frame drained by keepalive for place/ack."""
        if not isinstance(text, str):
            raise TypeError("stash_trade_inbound requires str")
        self._trade_inbound_stash.append(text)

    def _pop_trade_inbound(self) -> Optional[str]:
        if not self._trade_inbound_stash:
            return None
        return self._trade_inbound_stash.pop(0)

    def handle_silence_timeout(self) -> None:
        """Heartbeat/silence failure → reconnect generation + reseed block."""
        self.mark_reconnect()
        _safe_log("silence_timeout", exchange=self.exchange, gen=self.reconnect_generation)

    def handle_inbound_text(self, text: str) -> ParsedStreamEvent:
        """Parse one inbound text frame categorically; never log raw text."""
        self.last_recv_mono_ns = time.monotonic_ns()
        parsed = self._parse_inbound(text)
        if parsed.kind == "auth_ack":
            self.authenticated = True
            self.journal_auth(success=True)
        elif parsed.kind == "auth_reject":
            self.authenticated = False
            self.journal_auth(success=False, error_code="auth_failed")
        elif parsed.kind == "sub_ack":
            self._on_subscribe_ack(ok=bool(parsed.ack_ok))
        elif parsed.kind == "gap":
            self._on_sequence_gap()
        elif parsed.kind == "duplicate":
            _safe_log("dup_suppressed", exchange=self.exchange, gen=self.reconnect_generation)
        elif parsed.kind == "order_update":
            self._on_order_update(parsed)
        elif parsed.kind == "heartbeat":
            _safe_log("heartbeat_ack", exchange=self.exchange, gen=self.reconnect_generation)
        return parsed

    def _on_subscribe_ack(self, *, ok: bool) -> None:
        recv = time.monotonic_ns()
        # Late/duplicate successful ACK after matched REST reseed must not
        # re-arm the send gate or downgrade healthy/ready stream state.
        already_reseed_cleared = (
            ok
            and self.sequence_state == SequenceHealth.HEALTHY
            and self.subscription_readiness == SubscriptionReadiness.READY
            and not self._sends_blocked
        )
        if ok:
            self.subscription_readiness = SubscriptionReadiness.READY
            outcome = "success"
            readiness = "ready"
            ack_state = "received"
        else:
            self.subscription_readiness = SubscriptionReadiness.NOT_READY
            outcome = "failure"
            readiness = "not_ready"
            ack_state = "received"
        body: dict[str, Any] = {
            "event_type": "ack_received",
            "operation_id": self.stream_operation_id,
            "venue": self.exchange,
            "environment": self.environment,
            "outcome": outcome,
            "request_kind": "ws_subscribe",
            "ack_state": ack_state,
            "receive_monotonic_ns": recv,
            "transport": "ws_trade",
            "reconnect_generation": self.reconnect_generation,
            "subscription_readiness": readiness,
        }
        if not ok:
            body["error_code"] = "venue_rejected"
        self.journal.append(body)
        if already_reseed_cleared:
            # Preserve healthy/ready/unblocked after matched reseed.
            return
        # First subscribe / reconnect / failed ACK: fail-closed until REST reseed.
        self._sends_blocked = True
        if not ok:
            self.sequence_state = SequenceHealth.RESEED_REQUIRED

    def _on_sequence_gap(self) -> None:
        self.sequence_state = SequenceHealth.GAP
        self.subscription_readiness = SubscriptionReadiness.NOT_READY
        self._sends_blocked = True
        self._journal_stream_recon(
            sequence_state=SequenceHealth.GAP,
            observation_source="private_ws",
            outcome="observed",
            recon_state="inconclusive",
        )
        _safe_log("seq_gap", exchange=self.exchange, gen=self.reconnect_generation)

    def _journal_stream_recon(
        self,
        *,
        sequence_state: SequenceHealth,
        observation_source: str,
        outcome: str,
        recon_state: str,
        transport: Optional[str] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "event_type": "reconciliation",
            "operation_id": self.stream_operation_id,
            "venue": self.exchange,
            "environment": self.environment,
            "outcome": outcome,
            "reconciliation_scope": "private_stream_reseed",
            "reconciliation_state": recon_state,
            "observation_source": observation_source,
            "reconnect_generation": self.reconnect_generation,
            "sequence_state": sequence_state.value,
            "subscription_readiness": self.subscription_readiness.value,
        }
        if transport is not None:
            body["transport"] = transport
        return self.journal.append(body)

    def confirm_rest_reseed(self, result: RestReseedResult) -> dict[str, Any]:
        """Apply REST seed/reseed confirmation. Until matched, provider stays unknown."""
        if result.matched and not result.inconclusive:
            self.sequence_state = SequenceHealth.HEALTHY
            self.subscription_readiness = SubscriptionReadiness.READY
            self._sends_blocked = False
            return self._journal_stream_recon(
                sequence_state=SequenceHealth.HEALTHY,
                observation_source="rest_reconcile",
                outcome="success",
                recon_state="matched",
                transport="rest",
            )
        self.sequence_state = SequenceHealth.RESEED_REQUIRED
        self.subscription_readiness = SubscriptionReadiness.NOT_READY
        self._sends_blocked = True
        return self._journal_stream_recon(
            sequence_state=SequenceHealth.RESEED_REQUIRED,
            observation_source="rest_reconcile",
            outcome="observed",
            recon_state="inconclusive",
            transport="rest",
        )

    def run_rest_reseed(self) -> dict[str, Any]:
        if self.rest_reseed is None:
            raise RuntimeError("REST reseed port unbound")
        result = self.rest_reseed.reseed(
            venue=self.exchange,
            environment=self.environment,
            reconnect_generation=self.reconnect_generation,
            symbol_alias=self.symbol_alias,
        )
        return self.confirm_rest_reseed(result)

    def register_plan_fingerprint(self, plan: OrderPlan) -> None:
        self._fingerprint_by_attempt[plan.order_attempt_id] = plan.request_fingerprint
        self._fingerprint_by_attempt[plan.request_fingerprint] = plan.request_fingerprint
        # Bybit orderLinkId is truncated to 36 chars in trade/stream payloads.
        self._fingerprint_by_attempt[plan.order_attempt_id[:36]] = plan.request_fingerprint
        # OKX clOrdId strips underscores and truncates to 32.
        self._fingerprint_by_attempt[
            plan.order_attempt_id.replace("_", "")[:32]
        ] = plan.request_fingerprint

    def _on_order_update(self, parsed: ParsedStreamEvent) -> None:
        # Resolve correlation fingerprint first (registered plans only).
        corr = parsed.event_key
        if corr and corr in self._fingerprint_by_attempt:
            corr = self._fingerprint_by_attempt[corr]
        registered = corr is not None and corr in set(
            self._fingerprint_by_attempt.values()
        )

        # Unregistered updates stay blocked during reseed/gap; registered
        # in-flight plans still accept working/terminal observations.
        if self.reseed_required and not registered:
            return
        if parsed.symbol_alias and parsed.symbol_alias != self.symbol_alias:
            return

        dedupe = parsed.dedupe_key
        if dedupe and dedupe in self._seen_event_keys:
            return
        if dedupe:
            self._seen_event_keys.add(dedupe)

        # Never +1-gap on order-topic timestamps (creationTime/uTime). Sequence
        # counters on these topics are not trusted for gap detection.

        if not corr:
            return
        if parsed.terminal_state in {"filled", "cancelled", "expired"}:
            snap = {
                "filled": OrderStateSnapshot.FILLED,
                "cancelled": OrderStateSnapshot.CANCELLED,
                "expired": OrderStateSnapshot.EXPIRED,
            }[parsed.terminal_state]
            self._order_states[corr] = snap
        elif parsed.working:
            # Do not downgrade an already-terminal observation.
            prior = self._order_states.get(corr)
            if prior not in {
                OrderStateSnapshot.FILLED,
                OrderStateSnapshot.CANCELLED,
                OrderStateSnapshot.EXPIRED,
            }:
                self._order_states[corr] = OrderStateSnapshot.WORKING

    def journal_terminal_from_stream(
        self,
        plan: OrderPlan,
        *,
        terminal_state: str,
    ) -> dict[str, Any]:
        if self.reseed_required or self.sequence_state != SequenceHealth.HEALTHY:
            raise RuntimeError("terminal from stream blocked until healthy reseed")
        recv = time.monotonic_ns()
        return self.journal.append(
            {
                "event_type": "terminal_update",
                "operation_id": plan.order_attempt_id,
                "venue": _journal_venue(plan.venue),
                "environment": self.environment,
                "outcome": "observed",
                "dual_leg_id": plan.dual_leg_id,
                "leg_id": plan.leg_id,
                "terminal_state": terminal_state,
                "request_fingerprint": plan.request_fingerprint,
                "receive_monotonic_ns": recv,
                "observation_source": "private_ws",
                "reconnect_generation": self.reconnect_generation,
                "sequence_state": "healthy",
            }
        )

    def journal_terminal_from_rest(
        self,
        plan: OrderPlan,
        *,
        terminal_state: str,
    ) -> dict[str, Any]:
        """REST reconciliation terminal — does not require private-stream mapping."""
        recv = time.monotonic_ns()
        return self.journal.append(
            {
                "event_type": "terminal_update",
                "operation_id": plan.order_attempt_id,
                "venue": _journal_venue(plan.venue),
                "environment": self.environment,
                "outcome": "observed",
                "dual_leg_id": plan.dual_leg_id,
                "leg_id": plan.leg_id,
                "terminal_state": terminal_state,
                "request_fingerprint": plan.request_fingerprint,
                "receive_monotonic_ns": recv,
                "observation_source": "rest_reconcile",
            }
        )

    def build_trade_place(self, plan: OrderPlan, *, req_id: str) -> WsOutboundMessage:
        if self.exchange == "bybit":
            return build_bybit_trade_place(plan, self.credentials, req_id=req_id)
        code = (
            plan.inst_id_code
            if plan.inst_id_code is not None
            else self.okx_inst_id_code
        )
        return build_okx_trade_place(plan, req_id=req_id, inst_id_code=code)

    def build_trade_cancel(self, plan: OrderPlan, *, req_id: str) -> WsOutboundMessage:
        if self.exchange == "bybit":
            return build_bybit_trade_cancel(plan, self.credentials, req_id=req_id)
        code = (
            plan.inst_id_code
            if plan.inst_id_code is not None
            else self.okx_inst_id_code
        )
        return build_okx_trade_cancel(plan, req_id=req_id, inst_id_code=code)

    def send_trade_place(self, plan: OrderPlan, *, req_id: str) -> WsOutboundMessage:
        if self.sends_blocked:
            raise RuntimeError("WS trade place blocked until REST reseed confirmation")
        assert self.trade_socket is not None
        msg = self.build_trade_place(plan, req_id=req_id)
        self.trade_socket.send_text(msg.text)
        self.note_trade_activity()
        self.register_plan_fingerprint(plan)
        _safe_log("trade_place_sent", exchange=self.exchange, gen=self.reconnect_generation)
        return msg

    def send_trade_cancel(self, plan: OrderPlan, *, req_id: str) -> WsOutboundMessage:
        if self.sends_blocked:
            raise RuntimeError("WS trade cancel blocked until REST reseed confirmation")
        assert self.trade_socket is not None
        msg = self.build_trade_cancel(plan, req_id=req_id)
        self.trade_socket.send_text(msg.text)
        self.note_trade_activity()
        _safe_log("trade_cancel_sent", exchange=self.exchange, gen=self.reconnect_generation)
        return msg

    def send_trade_auth(self) -> WsOutboundMessage:
        """Authenticate trade socket (Bybit trade auth / OKX private login on trade conn)."""
        assert self.trade_socket is not None
        if self.exchange == "bybit":
            msg = build_bybit_private_auth(self.credentials)
        else:
            msg = build_okx_private_login(self.credentials)
        self.trade_socket.send_text(msg.text)
        self.note_trade_activity()
        _safe_log("trade_auth_sent", exchange=self.exchange, gen=self.reconnect_generation)
        return msg

    def parse_trade_ack_text(self, text: str, *, expect_req_id: str) -> TradeAckObservation:
        """Parse trade WS place/cancel ACK by exact request id. Never terminal."""
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TimeoutError("trade ack malformed") from exc
        if not isinstance(data, Mapping):
            raise TimeoutError("trade ack malformed")
        if self.exchange == "bybit":
            req = str(data.get("reqId") or "")
            if req != expect_req_id:
                raise TimeoutError("trade ack reqId mismatch")
            ok = data.get("retCode") in (0, "0") or data.get("success") is True
            return TradeAckObservation(
                req_id=req,
                accepted=bool(ok),
                ack_state="accepted" if ok else "received",
                venue_code=sanitize_venue_code(data.get("retCode")),
            )
        req = str(data.get("id") or "")
        if req != expect_req_id:
            raise TimeoutError("trade ack id mismatch")
        code_ok = str(data.get("code", "")) == "0"
        rows = data.get("data")
        scode_ok = True
        scode_raw: object = None
        if isinstance(rows, list) and rows and isinstance(rows[0], Mapping):
            scode_raw = rows[0].get("sCode")
            scode_ok = str(scode_raw if scode_raw is not None else "0") == "0"
        ok = code_ok and scode_ok
        # Prefer per-order sCode; fall back to top-level code when sCode absent.
        venue_raw = scode_raw if scode_raw is not None else data.get("code")
        return TradeAckObservation(
            req_id=req,
            accepted=bool(ok),
            ack_state="accepted" if ok else "received",
            venue_code=sanitize_venue_code(venue_raw),
        )

    def recv_trade_ack(
        self,
        *,
        expect_req_id: str,
        timeout_sec: float = 5.0,
    ) -> TradeAckObservation:
        """Bounded loop: ignore ping/welcome/non-matching frames until exact ACK."""
        assert self.trade_socket is not None
        deadline = time.monotonic() + float(timeout_sec)
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            raw = self._pop_trade_inbound()
            if raw is None:
                try:
                    raw = self.trade_socket.recv_text(
                        timeout_sec=min(1.0, remaining)
                    )
                except TimeoutError:
                    continue
            self.note_trade_activity()
            if is_ws_noise_frame(self.exchange, raw):
                continue
            try:
                return self.parse_trade_ack_text(raw, expect_req_id=expect_req_id)
            except TimeoutError:
                # Non-matching or non-ack frame — keep waiting within budget.
                continue
        raise TimeoutError("trade ack timeout")

    def recv_private_handshake_event(
        self,
        *,
        expect_kinds: frozenset[str],
        timeout_sec: float = 5.0,
    ) -> ParsedStreamEvent:
        """Bounded private-socket recv: skip noise until auth/sub ACK/reject."""
        assert self.private_socket is not None
        deadline = time.monotonic() + float(timeout_sec)
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                raw = self.private_socket.recv_text(timeout_sec=min(1.0, remaining))
            except TimeoutError:
                continue
            if is_ws_noise_frame(self.exchange, raw):
                continue
            ev = self.handle_inbound_text(raw)
            if ev.kind in expect_kinds:
                return ev
        raise TimeoutError("private handshake timeout")

    def recv_trade_auth_ack(self, *, timeout_sec: float = 5.0) -> bool:
        """Bounded trade-socket auth: ignore ping/welcome; require matching auth."""
        assert self.trade_socket is not None
        deadline = time.monotonic() + float(timeout_sec)
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                raw = self.trade_socket.recv_text(timeout_sec=min(1.0, remaining))
            except TimeoutError:
                continue
            if is_ws_noise_frame(self.exchange, raw):
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, Mapping):
                continue
            if self.exchange == "bybit":
                if str(obj.get("op") or "") != "auth":
                    continue
                return obj.get("success") is True or str(obj.get("retCode", "")) == "0"
            event = str(obj.get("event") or "")
            if event and event != "login":
                continue
            if event == "login" or "code" in obj:
                return str(obj.get("code", "")) == "0"
        raise TimeoutError("trade auth timeout")

    def observe_trade_ack(self, obs: TradeAckObservation) -> dict[str, Any]:
        """Map trade WS ACK → ack_received. Never emits terminal_update."""
        recv = time.monotonic_ns()
        return {
            "ack_state": "accepted" if obs.accepted else "received",
            "receive_monotonic_ns": recv,
            "transport": "ws_trade",
            "reconnect_generation": self.reconnect_generation,
            "terminal": False,
        }

    def journal_trade_ack(
        self,
        plan: OrderPlan,
        obs: TradeAckObservation,
        *,
        request_kind: str = "place",
    ) -> dict[str, Any]:
        meta = self.observe_trade_ack(obs)
        return self.journal.append(
            {
                "event_type": "ack_received",
                "operation_id": plan.order_attempt_id,
                "venue": _journal_venue(plan.venue),
                "environment": self.environment,
                "outcome": "success" if obs.accepted else "failure",
                "dual_leg_id": plan.dual_leg_id,
                "leg_id": plan.leg_id,
                "request_kind": request_kind,
                "request_fingerprint": plan.request_fingerprint,
                "ack_state": meta["ack_state"],
                "receive_monotonic_ns": meta["receive_monotonic_ns"],
                "transport": "ws_trade",
                "reconnect_generation": self.reconnect_generation,
                **(
                    {"error_code": "order_rejected"}
                    if not obs.accepted
                    else {}
                ),
            }
        )

    def get_order_snapshot(self, plan: OrderPlan) -> OrderStateSnapshot:
        known = self._order_states.get(plan.request_fingerprint)
        if known is not None:
            # Known working/terminal for a registered plan survives send-block /
            # false-gap; waiters must still observe Cancelled after New.
            return known
        if self.sends_blocked or self.reseed_required:
            return OrderStateSnapshot.UNKNOWN
        return OrderStateSnapshot.UNKNOWN

    def _parse_inbound(self, text: str) -> ParsedStreamEvent:
        # Heartbeats first (OKX literal ping/pong).
        if text in {"pong", "ping"}:
            return ParsedStreamEvent(kind="heartbeat")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return ParsedStreamEvent(kind="ignored")
        if not isinstance(data, dict):
            return ParsedStreamEvent(kind="ignored")

        if self.exchange == "bybit":
            return self._parse_bybit(data)
        return self._parse_okx(data)

    def _parse_bybit(self, data: Mapping[str, Any]) -> ParsedStreamEvent:
        op = data.get("op")
        if op == "pong" or data.get("ret_msg") == "pong":
            return ParsedStreamEvent(kind="heartbeat")
        if op == "auth":
            ok = data.get("success") is True or str(data.get("retCode", "")) == "0"
            return ParsedStreamEvent(
                kind="auth_ack" if ok else "auth_reject", ack_ok=ok
            )
        if op == "subscribe":
            ok = data.get("success") is True or str(data.get("retCode", "")) == "0"
            return ParsedStreamEvent(kind="sub_ack", ack_ok=ok)
        topic = str(data.get("topic") or "")
        if topic.startswith("order") or topic.startswith("execution"):
            rows = data.get("data")
            if not isinstance(rows, list) or not rows:
                return ParsedStreamEvent(kind="ignored")
            row = rows[0] if isinstance(rows[0], Mapping) else {}
            symbol = str(row.get("symbol") or "")
            if symbol and symbol != self.symbol_alias:
                return ParsedStreamEvent(kind="ignored", symbol_alias=symbol)
            link = str(row.get("orderLinkId") or "")
            corr = self._fingerprint_by_attempt.get(link) if link else None
            status = str(row.get("orderStatus") or row.get("execType") or "").lower()
            # creationTime / u are timestamps — never use as +1 gap sequences.
            time_hint = data.get("creationTime")
            if time_hint is None:
                time_hint = data.get("u")
            dedupe = _order_stream_dedupe_key(
                correlation=corr, status=status, time_hint=time_hint
            )
            if dedupe and dedupe in self._seen_event_keys:
                return ParsedStreamEvent(
                    kind="duplicate", event_key=corr, dedupe_key=dedupe
                )
            terminal = None
            working = False
            if status in {"filled", "partiallyfilled"}:
                terminal = "filled" if status == "filled" else None
                working = status == "partiallyfilled"
            elif status in {"cancelled", "canceled"}:
                terminal = "cancelled"
            elif status in {"rejected", "deactivated"}:
                terminal = "expired"
            elif status in {"new", "created", "untriggered"}:
                working = True
            return ParsedStreamEvent(
                kind="order_update",
                symbol_alias=symbol or self.symbol_alias,
                terminal_state=terminal,
                working=working,
                sequence=None,
                event_key=corr,
                dedupe_key=dedupe,
            )
        return ParsedStreamEvent(kind="ignored")

    def _parse_okx(self, data: Mapping[str, Any]) -> ParsedStreamEvent:
        event = str(data.get("event") or "")
        if event == "login":
            ok = str(data.get("code", "")) == "0"
            return ParsedStreamEvent(
                kind="auth_ack" if ok else "auth_reject", ack_ok=ok
            )
        if event == "subscribe":
            ok = str(data.get("code", "0")) == "0"
            return ParsedStreamEvent(kind="sub_ack", ack_ok=ok)
        if event == "error":
            # Distinguish auth vs sub via arg channel when present.
            arg = data.get("arg") if isinstance(data.get("arg"), Mapping) else {}
            if not arg:
                return ParsedStreamEvent(kind="auth_reject", ack_ok=False)
            return ParsedStreamEvent(kind="sub_ack", ack_ok=False)
        arg = data.get("arg") if isinstance(data.get("arg"), Mapping) else {}
        channel = str(arg.get("channel") or "")
        if channel in {"orders", "positions"}:
            rows = data.get("data")
            if not isinstance(rows, list) or not rows:
                return ParsedStreamEvent(kind="ignored")
            row = rows[0] if isinstance(rows[0], Mapping) else {}
            inst = str(row.get("instId") or arg.get("instId") or "")
            if inst and inst != self.symbol_alias:
                return ParsedStreamEvent(kind="ignored", symbol_alias=inst)
            cl = str(row.get("clOrdId") or "")
            corr = self._fingerprint_by_attempt.get(cl) if cl else None
            state = str(row.get("state") or "").lower()
            # uTime / timestamps must never drive +1 gap; seqId alone is unused
            # here because venue counters are not trusted on these topics.
            time_hint = row.get("uTime")
            if time_hint is None:
                time_hint = data.get("seqId")
            dedupe = _order_stream_dedupe_key(
                correlation=corr, status=state, time_hint=time_hint
            )
            if dedupe and dedupe in self._seen_event_keys:
                return ParsedStreamEvent(
                    kind="duplicate", event_key=corr, dedupe_key=dedupe
                )
            terminal = None
            working = False
            if state == "filled":
                terminal = "filled"
            elif state in {"canceled", "cancelled"}:
                terminal = "cancelled"
            elif state in {"live", "partially_filled"}:
                working = True
            return ParsedStreamEvent(
                kind="order_update",
                symbol_alias=inst or self.symbol_alias,
                terminal_state=terminal,
                working=working,
                sequence=None,
                event_key=corr,
                dedupe_key=dedupe,
            )
        # Trade ACK frames (id + op/code) — not terminal.
        if "id" in data and ("op" in data or "code" in data):
            ok = str(data.get("code", "")) == "0"
            return ParsedStreamEvent(
                kind="ignored",
                req_id=str(data.get("id")),
                ack_ok=ok,
            )
        return ParsedStreamEvent(kind="ignored")


@dataclass
class WsOrderStateProvider:
    """OrderStateProvider backed by private stream; REST only for reseed."""

    runtime: PrivateStreamRuntime

    def get(self, plan: OrderPlan) -> OrderStateSnapshot:
        return self.runtime.get_order_snapshot(plan)


def build_ws_trade_transport(
    runtime: PrivateStreamRuntime,
    *,
    op: str = "place",
    ack_timeout_sec: float = 5.0,
) -> Callable[[Any], Any]:
    """TransportFn using trade WS — real frame send + correlated ACK.

    ACK is never terminal. ``payload`` may be ``WsTradeDispatch``, an ``OrderPlan``,
    or a transport with ``_plan`` bound (W4). Never POSTs REST order APIs.
    OKX trade socket is private WS (second connection); Bybit uses ``/v5/trade``.
    """
    from app.bot.private.order_plan import OrderPlan
    from app.bot.private.order_sender import TransportAck
    from app.bot.private.order_sign import WsTradeDispatch

    if op not in {"place", "cancel"}:
        raise ValueError(f"unsupported ws trade op {op!r}")

    def _resolve_plan(payload: Any) -> OrderPlan:
        if isinstance(payload, WsTradeDispatch):
            return payload.plan
        if isinstance(payload, OrderPlan):
            return payload
        bound = getattr(_send, "_plan", None)
        if isinstance(bound, OrderPlan):
            return bound
        raise RuntimeError("WS trade transport missing plan binding")

    def _send(payload: Any) -> TransportAck:
        if runtime.sends_blocked:
            return TransportAck(
                kind="rejected",
                ack_state="received",
                error_code="transport_error",
            )
        plan = _resolve_plan(payload)
        req_id = new_trade_req_id(exchange=runtime.exchange)
        if op == "place":
            runtime.send_trade_place(plan, req_id=req_id)
        else:
            runtime.send_trade_cancel(plan, req_id=req_id)
        try:
            obs = runtime.recv_trade_ack(
                expect_req_id=req_id, timeout_sec=float(ack_timeout_sec)
            )
        except TimeoutError:
            return TransportAck(
                kind="ambiguous",
                ack_state="received",
                ambiguous=True,
                error_code="timeout",
            )
        if obs.accepted:
            return TransportAck(kind="accepted", ack_state="accepted")
        return TransportAck(
            kind="rejected",
            ack_state="received",
            error_code="venue_rejected",
            venue_code=obs.venue_code,
        )

    _send._bbot_ws_trade = True  # type: ignore[attr-defined]
    _send._bbot_ws_trade_op = op  # type: ignore[attr-defined]
    return _send


def build_ws_trade_cancel_transport(
    runtime: PrivateStreamRuntime,
    *,
    ack_timeout_sec: float = 5.0,
) -> Callable[[Any], Any]:
    """Cancel variant of ``build_ws_trade_transport`` (trade WS only)."""
    from app.bot.private.order_lease import CancelAck
    from app.bot.private.order_plan import OrderPlan
    from app.bot.private.order_sign import WsTradeDispatch

    def _resolve_plan(payload: Any) -> OrderPlan:
        if isinstance(payload, WsTradeDispatch):
            return payload.plan
        if isinstance(payload, OrderPlan):
            return payload
        bound = getattr(_send, "_plan", None)
        if isinstance(bound, OrderPlan):
            return bound
        raise RuntimeError("WS cancel transport missing plan binding")

    def _send(payload: Any) -> CancelAck:
        if runtime.sends_blocked:
            return CancelAck(
                ok=False, cancel_state="accepted", error_code="transport_error"
            )
        plan = _resolve_plan(payload)
        req_id = new_trade_req_id(exchange=runtime.exchange)
        runtime.send_trade_cancel(plan, req_id=req_id)
        try:
            obs = runtime.recv_trade_ack(
                expect_req_id=req_id, timeout_sec=float(ack_timeout_sec)
            )
        except TimeoutError:
            return CancelAck(
                ok=False, cancel_state="accepted", ambiguous=True, error_code="timeout"
            )
        if obs.accepted:
            return CancelAck(ok=True, cancel_state="accepted")
        return CancelAck(ok=False, cancel_state="accepted", error_code="venue_rejected")

    _send._bbot_ws_trade = True  # type: ignore[attr-defined]
    _send._bbot_ws_trade_op = "cancel"  # type: ignore[attr-defined]
    return _send


def assert_default_cli_has_no_ws() -> None:
    """Invariant: default entrypoint has no socket factory and no auto-connect."""
    assert_no_default_ws_socket()
    if get_socket_factory() is not None:
        raise RuntimeError("socket factory bound in default CLI context")


def live_orders_must_not_auto_connect_ws(env: Optional[Mapping[str, str]] = None) -> None:
    """LIVE_ORDERS=1 must not imply a WS connection by itself."""
    if send_allowed(env) and get_socket_factory() is None:
        return
    if send_allowed(env) and get_socket_factory() is not None:
        # Binding is explicit — still not auto-connect. Presence of factory alone OK.
        return
    assert_no_default_ws_socket()


def private_ws_urls_for_live() -> dict[str, str]:
    ep = endpoints_for_venue("live")
    return {
        "bybit_private": ep.bybit_private_ws,
        "bybit_trade": ep.bybit_trade_ws,
        "okx_private": ep.okx_private_ws,
        "okx_business": ep.okx_business_ws,
    }