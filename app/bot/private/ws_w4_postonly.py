"""W4 bounded WS post-only runner (explicit ``--ws-w4-post-only`` only).

Immutable profiles only:
  - Bybit BTCUSDT BUY qty 0.001, TTL 10s, price = opposing ask × 0.99
  - OKX BTC-USDT-SWAP BUY qty 0.01, TTL 10s, same price rule

Requires VENUE=live, LIVE_ORDERS=1, BBOT_PRIVATE_W4=1, live credentials, and a
plan-bound one-shot approval. Default/readonly CLI never binds trade transport.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from app.bot.private.journal_v1 import (
    JournalValidationError,
    PrivateJournalWriter,
    new_opaque_id,
)
from app.bot.private.order_approval import ApprovalToken, ApprovalVault
from app.bot.private.order_lease import (
    CancelAck,
    LeaseState,
    OrderStateSnapshot,
    observed_terminal_state,
)
from app.bot.private.order_metadata import MetadataError, MetadataProvider
from app.bot.private.order_plan import OrderPlan, OrderPlanError, build_order_plan
from app.bot.private.order_preflight import PositionModeProvider, PreflightError
from app.bot.private.order_sender import (
    ApprovalBoundSender,
    SendResult,
    TransportAck,
    assert_default_entrypoint_cannot_transport,
)
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.paths import resolve_data_root
from app.bot.private.secrets import load_live_secrets
from app.bot.private.venue import endpoints_for_venue
from app.bot.private.ws_gates import WsProfileGateError, assert_ws_w4_send_gates
from app.bot.private.ws_l1_public import (
    L1Error,
    PublicL1Port,
    PublicL1Quote,
    assert_plan_price_matches_l1,
    limit_price_from_opposing_ask,
)
from app.bot.private.ws_private import (
    PrivateStreamRuntime,
    RestReseedResult,
    WsOrderStateProvider,
)
from app.bot.private.ws_reseed import build_signed_rest_reseed
from app.bot.private.ws_socket import (
    PrivateWsSocket,
    assert_no_default_ws_socket,
    unbind_socket_factory,
)
from app.bot.private.ws_w4_baseline import (
    BaselineError,
    FlatBaselinePort,
    assert_flat,
)

LOG = logging.getLogger("bbot.private.ws_w4")

W4_TTL_SEC = 10
# Fail-closed if cancel issuance wakes later than deadline + this slack.
W4_SCHEDULER_OVERSHOOT_TOLERANCE_NS = 250_000_000  # 250ms
W4_RECOVERY_TERMINAL_WAIT_SEC = 12.0
W4_PROFILES: dict[str, dict[str, Any]] = {
    "bybit": {
        "exchange": "bybit",
        "venue": "bybit_live",
        "symbol": "BTCUSDT",
        "qty": "0.001",
        "side": "buy",
        "mode": "post_only_limit",
        "ttl_sec": W4_TTL_SEC,
    },
    "okx": {
        "exchange": "okx",
        "venue": "okx_live",
        "symbol": "BTC-USDT-SWAP",
        "qty": "0.01",
        "side": "buy",
        "mode": "post_only_limit",
        "ttl_sec": W4_TTL_SEC,
    },
}


class W4ProfileError(ValueError):
    """Reject anything outside the immutable W4 profiles."""


@dataclass
class W4Report:
    status: str
    exchange: str
    symbol: str
    subscription_ready: bool = False
    reseed_matched: bool = False
    sends_blocked: bool = True
    trade_ws_bound: bool = False
    orders_sent: int = 0
    ack_ok: bool = False
    cancel_acked: bool = False
    terminal_observed: bool = False
    reconnect_generation: int = 0
    error_code: Optional[str] = None
    # Digits-only venue reject code for public JSON; never sMsg / frames / IDs.
    venue_code: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def as_public_dict(self) -> dict[str, Any]:
        """Allowlisted public fields only — never exception types/messages/raw extras."""
        from app.bot.private.ws_private import sanitize_venue_code

        out: dict[str, Any] = {
            "status": self.status,
            "exchange": self.exchange,
            "symbol_alias": self.symbol,
            "subscription_ready": self.subscription_ready,
            "reseed_matched": self.reseed_matched,
            "sends_blocked": self.sends_blocked,
            "trade_ws_bound": self.trade_ws_bound,
            "orders_sent": self.orders_sent,
            "ack_ok": self.ack_ok,
            "cancel_acked": self.cancel_acked,
            "terminal_observed": self.terminal_observed,
            "reconnect_generation": self.reconnect_generation,
            "ttl_sec": W4_TTL_SEC,
            "mode": "post_only_limit",
            "side": "buy",
        }
        if self.error_code:
            out["error_code"] = self.error_code
        vc = sanitize_venue_code(self.venue_code)
        if vc is not None:
            out["venue_code"] = vc
        return out


def resolve_w4_profile(venue_flag: str) -> dict[str, Any]:
    key = venue_flag.strip().lower()
    if key not in W4_PROFILES:
        raise W4ProfileError("W4 venue must be bybit or okx")
    return dict(W4_PROFILES[key])


def assert_exact_w4_plan(plan: OrderPlan, profile: Mapping[str, Any]) -> None:
    if plan.venue != profile["venue"]:
        raise W4ProfileError("plan venue outside W4 profile")
    if plan.symbol != profile["symbol"]:
        raise W4ProfileError("plan symbol outside W4 profile")
    if plan.side != "buy":
        raise W4ProfileError("W4 only allows buy")
    if plan.mode != "post_only_limit" or not plan.post_only:
        raise W4ProfileError("W4 only allows post_only_limit")
    if plan.ttl_sec != W4_TTL_SEC:
        raise W4ProfileError("W4 TTL must be exactly 10s")
    if plan.qty != profile["qty"]:
        raise W4ProfileError("W4 qty must match immutable profile")
    if plan.reduce_only:
        raise W4ProfileError("W4 rejects reduce_only")
    if plan.k_live != 1:
        raise W4ProfileError("W4 requires K_live=1")


def _creds_from_live(secrets: Any, exchange: str) -> LiveCredentials:
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


def _new_req_id(*, exchange: str) -> str:
    from app.bot.private.ws_private import new_trade_req_id

    return new_trade_req_id(exchange=exchange)


@dataclass
class _WsTradePlaceTransport:
    runtime: PrivateStreamRuntime
    ack_timeout_sec: float = 5.0
    last_req_id: str = ""
    ttl_deadline_mono_ns: Optional[int] = None
    dispatch_mono_ns: Optional[int] = None

    def __call__(self, payload: Any) -> TransportAck:
        from app.bot.private.order_sign import WsTradeDispatch

        plan = getattr(self, "_plan", None)
        if isinstance(payload, WsTradeDispatch):
            plan = payload.plan
        if plan is None:
            raise RuntimeError("WS trade transport missing plan binding")
        req_id = _new_req_id(exchange=self.runtime.exchange)
        self.last_req_id = req_id
        # Hard 10s deadline starts immediately before real WS place dispatch.
        self.dispatch_mono_ns = time.monotonic_ns()
        self.ttl_deadline_mono_ns = self.dispatch_mono_ns + int(
            W4_TTL_SEC * 1_000_000_000
        )
        self.runtime.send_trade_place(plan, req_id=req_id)
        try:
            obs = self.runtime.recv_trade_ack(
                expect_req_id=req_id, timeout_sec=self.ack_timeout_sec
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


@dataclass
class _WsTradeCancelTransport:
    runtime: PrivateStreamRuntime
    ack_timeout_sec: float = 5.0

    def __call__(self, payload: Any) -> CancelAck:
        from app.bot.private.order_sign import WsTradeDispatch

        plan = getattr(self, "_plan", None)
        if isinstance(payload, WsTradeDispatch):
            plan = payload.plan
        if plan is None:
            raise RuntimeError("WS cancel transport missing plan binding")
        req_id = _new_req_id(exchange=self.runtime.exchange)
        self.runtime.send_trade_cancel(plan, req_id=req_id)
        try:
            obs = self.runtime.recv_trade_ack(
                expect_req_id=req_id, timeout_sec=self.ack_timeout_sec
            )
        except TimeoutError:
            return CancelAck(
                ok=False, cancel_state="accepted", ambiguous=True, error_code="timeout"
            )
        if obs.accepted:
            return CancelAck(ok=True, cancel_state="accepted")
        return CancelAck(ok=False, cancel_state="accepted", error_code="venue_rejected")


def assert_w4_transport_is_ws_trade(transport: Any) -> None:
    """Fail-closed: W4 must never bind REST HTTP place/cancel transports."""
    from app.bot.private.order_transport import is_live_http_order_transport

    if is_live_http_order_transport(transport):
        raise W4ProfileError("W4 refuses HTTP order transport; require trade WS")
    if isinstance(transport, (_WsTradePlaceTransport, _WsTradeCancelTransport)):
        return
    if getattr(transport, "_bbot_ws_trade", False):
        return
    # Unknown callables are refused — only explicit WS adapters are allowed.
    raise W4ProfileError("W4 requires explicit ws_trade transport binding")


def assert_w4_okx_net_mode(
    *,
    exchange: str,
    venue: str,
    position_mode_provider: PositionModeProvider,
) -> None:
    """W4 OKX: verified net/one_way only — reject hedge (never omit posSide as workaround)."""
    if exchange != "okx":
        return
    snap = position_mode_provider.get(venue)
    if not snap.verified:
        raise W4ProfileError("W4 OKX position mode not verified")
    if snap.mode != "one_way":
        raise W4ProfileError("W4 OKX requires net/one_way; hedge rejected")


def _wait_private_terminal(
    runtime: PrivateStreamRuntime,
    provider: WsOrderStateProvider,
    plan: OrderPlan,
    *,
    timeout_sec: float,
    recv_timeout_sec: float = 0.5,
) -> OrderStateSnapshot:
    """Drain private order updates until terminal or timeout.

    Do not abort solely because ``sends_blocked`` / gap — keep applying
    registered-plan updates (New→Cancelled) within the wait budget.
    """
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        snap = provider.get(plan)
        if observed_terminal_state(snap) is not None:
            return snap
        try:
            assert runtime.private_socket is not None
            raw = runtime.private_socket.recv_text(timeout_sec=recv_timeout_sec)
            runtime.handle_inbound_text(raw)
        except TimeoutError:
            continue
    return provider.get(plan)


def _handshake_private_and_trade(
    runtime: PrivateStreamRuntime,
    *,
    exchange: str,
    ack_timeout_sec: float,
) -> Optional[str]:
    """Bounded auth/sub/trade-auth loops. Returns categorical error_code or None."""
    runtime.send_auth()
    try:
        auth_ev = runtime.recv_private_handshake_event(
            expect_kinds=frozenset({"auth_ack", "auth_reject"}),
            timeout_sec=ack_timeout_sec,
        )
    except TimeoutError:
        return "auth_failed"
    if auth_ev.kind != "auth_ack" or not runtime.authenticated:
        return "auth_failed"
    runtime.send_subscribe()
    try:
        sub_ev = runtime.recv_private_handshake_event(
            expect_kinds=frozenset({"sub_ack"}),
            timeout_sec=ack_timeout_sec,
        )
    except TimeoutError:
        return "venue_rejected"
    if sub_ev.ack_ok is False:
        return "venue_rejected"
    reseed_ev = runtime.run_rest_reseed()
    reseed_ok = (
        reseed_ev.get("reconciliation_state") == "matched" and not runtime.reseed_required
    )
    if not reseed_ok:
        return "reseed_required"
    runtime.send_trade_auth()
    try:
        ok = runtime.recv_trade_auth_ack(timeout_sec=ack_timeout_sec)
    except TimeoutError:
        return "auth_failed"
    if not ok:
        return "auth_failed"
    del exchange  # categorical only
    return None


def _unresolved_recovery_leases(sender: ApprovalBoundSender) -> list[Any]:
    """Leases reconstructed from journal that must reach terminal before a new W4."""
    supervisor = sender.lease_supervisor
    out: list[Any] = []
    for lease in list(supervisor._leases.values()):  # noqa: SLF001
        if lease.state == LeaseState.TERMINAL:
            continue
        # Any non-terminal post-only / cancel / overdue / inconclusive needs recovery.
        out.append(lease)
    return out


def _restore_stream_correlation(runtime: PrivateStreamRuntime, plan: OrderPlan) -> None:
    """Rebind opaque client/order ids → request_fingerprint for private-stream updates."""
    runtime.register_plan_fingerprint(plan)


def _commit_recovery_terminal(
    *,
    journal: PrivateJournalWriter,
    runtime: PrivateStreamRuntime,
    lease: Any,
    plan: OrderPlan,
    term: str,
    source: str,
) -> None:
    """Durable recovery close. Raises on journal failure — never marks lease first.

    Ack-present: ``terminal_update`` then post_only TTL matched followup.
    No-ack (place never acked): NEVER ``terminal_update`` — append
    ``post_dispatch_ambiguity`` ``matched`` (REST: observation_source=rest_reconcile,
    transport=rest) and mark lease terminal. Skip TTL matched followup (needs terminal).
    """
    if not lease.acked:
        body: dict[str, Any] = {
            "event_type": "reconciliation",
            "operation_id": plan.order_attempt_id,
            "venue": _journal_venue_from_plan(plan),
            "environment": "live",
            "outcome": "observed",
            "dual_leg_id": plan.dual_leg_id,
            "leg_id": plan.leg_id,
            "reconciliation_scope": "post_dispatch_ambiguity",
            "reconciliation_state": "matched",
        }
        if source == "rest":
            body["observation_source"] = "rest_reconcile"
            body["transport"] = "rest"
        else:
            body["observation_source"] = "private_ws"
        journal.append(body)
        lease.mark_terminal()
        return

    if source == "rest":
        runtime.journal_terminal_from_rest(plan, terminal_state=term)
    else:
        runtime.journal_terminal_from_stream(plan, terminal_state=term)
    journal.append_post_only_ttl_matched_followup(
        venue=_journal_venue_from_plan(plan),
        environment="live",
        operation_id=plan.order_attempt_id,
        dual_leg_id=plan.dual_leg_id,
        leg_id=plan.leg_id,
    )
    # Only after durable appends succeed:
    lease.mark_terminal()


def _journal_venue_from_plan(plan: OrderPlan) -> str:
    return "okx" if str(plan.venue).startswith("okx") else "bybit"


def _plan_matches_runtime(plan: OrderPlan, runtime: PrivateStreamRuntime) -> bool:
    from app.bot.private.ws_w4_baseline import plan_matches_runtime_exchange

    return plan_matches_runtime_exchange(plan, runtime.exchange)


def _recover_inflight_w4(
    *,
    sender: ApprovalBoundSender,
    runtime: PrivateStreamRuntime,
    provider: WsOrderStateProvider,
    cancel_transport: _WsTradeCancelTransport,
    credentials: LiveCredentials,
    env: Mapping[str, str],
    terminal_wait_sec: float,
    journal: PrivateJournalWriter,
    rest_order_recon: Optional[Any] = None,
) -> Optional[str]:
    """Recover unresolved W4 leases to terminal or fail closed.

    WS cancel only when ``plan.venue`` matches ``runtime.exchange``. Cross-venue
    leases use signed REST GET recon only; never cancel on the wrong trade socket.
    Journal append failure never marks terminal or clears block.
    Returns None on success; categorical error code otherwise.
    """
    unresolved = _unresolved_recovery_leases(sender)
    if not unresolved and not sender.lease_supervisor.has_blocking_lease():
        return None
    if not unresolved and sender.lease_supervisor.has_blocking_lease():
        return "recovery_blocked"

    observe_sec = max(0.05, min(float(terminal_wait_sec), 3.0))

    for lease in unresolved:
        plan = lease.plan
        same_venue = _plan_matches_runtime(plan, runtime)
        sender._lease = lease  # noqa: SLF001 — cancel path finds reconstructed lease

        term: Optional[str] = None
        source = "stream"

        # Prefer signed REST GET recon first (cross-venue safe; avoids long silent stream waits).
        if rest_order_recon is not None:
            try:
                snap_r = rest_order_recon.get(plan)
                t = observed_terminal_state(snap_r)
                if t is not None:
                    term = t
                    source = "rest"
            except Exception:  # noqa: BLE001
                pass

        if term is None and same_venue:
            _restore_stream_correlation(runtime, plan)
            snap = _wait_private_terminal(
                runtime, provider, plan, timeout_sec=observe_sec
            )
            term = observed_terminal_state(snap)
            if term is not None:
                source = "stream"
            elif rest_order_recon is not None:
                # Second REST look after stream silence (order may have cleared).
                try:
                    snap_r2 = rest_order_recon.get(plan)
                    t2 = observed_terminal_state(snap_r2)
                    if t2 is not None:
                        term = t2
                        source = "rest"
                except Exception:  # noqa: BLE001
                    pass
        elif term is None and not same_venue:
            # Cross-venue: never wait on the wrong private stream.
            pass

        if term is not None:
            try:
                _commit_recovery_terminal(
                    journal=journal,
                    runtime=runtime,
                    lease=lease,
                    plan=plan,
                    term=term,
                    source=source,
                )
            except (
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                JournalValidationError,
            ):
                sender.lease_supervisor.mark_process_sends_blocked()
                return "recovery_journal_failed"
            continue

        # Still unresolved — never WS-cancel on a mismatched venue trade socket.
        if not same_venue:
            sender.lease_supervisor.mark_process_sends_blocked()
            return "recovery_blocked"

        # Same-venue: correlated WS cancel then wait for terminal.
        if runtime.sends_blocked or runtime.reseed_required:
            sender.lease_supervisor.mark_process_sends_blocked()
            return "recovery_blocked"
        if not lease.acked:
            sender.lease_supervisor.mark_process_sends_blocked()
            return "recovery_blocked"

        cancel_transport._plan = plan  # noqa: SLF001
        cancel_res = sender.request_cancel_interface(
            plan,
            credentials,
            cancel_transport=cancel_transport,
            order_state_provider=provider,
            journal_transport="ws_trade",
            reconnect_generation=runtime.reconnect_generation,
        )
        if cancel_res.status in {
            "ambiguous",
            "recovery_journal_failed",
            "journal_failed",
            "gate_failed",
        }:
            sender.lease_supervisor.mark_process_sends_blocked()
            return "recovery_blocked"

        snap2 = _wait_private_terminal(
            runtime, provider, plan, timeout_sec=float(terminal_wait_sec)
        )
        term2 = observed_terminal_state(snap2)
        source2 = "stream"
        if term2 is None and rest_order_recon is not None:
            try:
                snap_r2 = rest_order_recon.get(plan)
                term2 = observed_terminal_state(snap_r2)
                if term2 is not None:
                    source2 = "rest"
            except Exception:  # noqa: BLE001
                term2 = None
        if term2 is None:
            sender.lease_supervisor.mark_process_sends_blocked()
            return "recovery_blocked"
        try:
            _commit_recovery_terminal(
                journal=journal,
                runtime=runtime,
                lease=lease,
                plan=plan,
                term=term2,
                source=source2,
            )
        except (
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            JournalValidationError,
        ):
            sender.lease_supervisor.mark_process_sends_blocked()
            return "recovery_journal_failed"

    if any(
        lease.state != LeaseState.TERMINAL
        for lease in sender.lease_supervisor._leases.values()  # noqa: SLF001
    ):
        sender.lease_supervisor.mark_process_sends_blocked()
        return "recovery_blocked"
    # All reconstructed leases terminal and journaled — allow new W4 attempt.
    sender.lease_supervisor._dispatch_blocking = False  # noqa: SLF001
    del env
    return None


def _sleep_until_ttl_deadline(
    *,
    deadline_mono_ns: int,
    sleep_fn: Any,
) -> bool:
    """Sleep remaining TTL. Returns True if scheduler overshoot beyond tolerance."""
    now = time.monotonic_ns()
    remaining_ns = deadline_mono_ns - now
    if remaining_ns <= 0:
        # ACK delayed past deadline — caller must cancel immediately.
        return False
    remaining_sec = remaining_ns / 1_000_000_000
    t0 = time.monotonic()
    sleep_fn(remaining_sec)
    after = time.monotonic_ns()
    # Also catch sleep that ran longer than requested wall time.
    slept = time.monotonic() - t0
    if after > deadline_mono_ns + W4_SCHEDULER_OVERSHOOT_TOLERANCE_NS:
        return True
    if slept > remaining_sec + (W4_SCHEDULER_OVERSHOOT_TOLERANCE_NS / 1e9):
        return True
    return False


def run_w4_post_only(
    *,
    venue: str,
    env: Optional[Mapping[str, str]] = None,
    metadata_provider: MetadataProvider,
    position_mode_provider: PositionModeProvider,
    l1: PublicL1Port,
    baseline: FlatBaselinePort,
    private_socket: Optional[PrivateWsSocket] = None,
    trade_socket: Optional[PrivateWsSocket] = None,
    approval_token: Optional[ApprovalToken] = None,
    issue_approval: bool = False,
    credentials: Optional[LiveCredentials] = None,
    load_secrets: bool = True,
    journal: Optional[PrivateJournalWriter] = None,
    data_root: Optional[Path] = None,
    rest_probe_fn: Optional[Any] = None,
    ack_timeout_sec: float = 5.0,
    terminal_wait_sec: float = 12.0,
    sleep_fn: Any = time.sleep,
    rest_order_recon: Optional[Any] = None,
    place_transport_override: Optional[Any] = None,
    cancel_transport_override: Optional[Any] = None,
) -> W4Report:
    """Execute one W4 post-only attempt for a single venue profile."""
    e = dict(env if env is not None else os.environ)
    try:
        assert_ws_w4_send_gates(e)
        profile = resolve_w4_profile(venue)
    except (WsProfileGateError, W4ProfileError):
        return W4Report(
            status="rejected_before_socket",
            exchange=venue,
            symbol="",
            error_code="invalid_request",
        )

    exchange = profile["exchange"]
    symbol = profile["symbol"]
    root = data_root if data_root is not None else resolve_data_root(e)
    j = journal if journal is not None else PrivateJournalWriter(root, run_id=new_opaque_id("run"))

    if credentials is None:
        if not load_secrets:
            return W4Report(
                status="secrets_unavailable",
                exchange=exchange,
                symbol=symbol,
                error_code="auth_unavailable",
            )
        try:
            secrets = load_live_secrets(e, require_complete=True)
            credentials = _creds_from_live(secrets, exchange)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return W4Report(
                status="secrets_unavailable",
                exchange=exchange,
                symbol=symbol,
                error_code="auth_unavailable",
            )
        if rest_order_recon is None:
            from app.bot.private.ws_w4_baseline import build_signed_rest_order_recon

            rest_order_recon = build_signed_rest_order_recon(
                bybit_credentials=_creds_from_live(secrets, "bybit"),
                okx_credentials=_creds_from_live(secrets, "okx"),
                endpoints=endpoints_for_venue("live"),
            )

    # Fresh flat baseline before any trade socket.
    try:
        base = baseline.check(exchange=exchange, symbol=symbol)
        assert_flat(base)
    except BaselineError:
        return W4Report(
            status="baseline_not_flat",
            exchange=exchange,
            symbol=symbol,
            error_code="invalid_request",
        )

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
        profile_gate=assert_ws_w4_send_gates,
    )

    place_transport = place_transport_override or _WsTradePlaceTransport(
        runtime=runtime, ack_timeout_sec=ack_timeout_sec
    )
    cancel_transport = cancel_transport_override or _WsTradeCancelTransport(
        runtime=runtime, ack_timeout_sec=ack_timeout_sec
    )
    try:
        assert_w4_transport_is_ws_trade(place_transport)
        assert_w4_transport_is_ws_trade(cancel_transport)
    except W4ProfileError:
        return W4Report(
            status="http_transport_rejected",
            exchange=exchange,
            symbol=symbol,
            error_code="transport_error",
        )

    vault = ApprovalVault(journal=j, venue=exchange, environment="live")
    sender = ApprovalBoundSender(
        journal=j,
        approval_vault=vault,
        metadata_provider=metadata_provider,
        position_mode_provider=position_mode_provider,
        transport=place_transport,
        data_root=root,
    )
    provider = WsOrderStateProvider(runtime)
    sender.lease_supervisor.order_state_provider = provider
    sender.lease_supervisor.cancel_transport = cancel_transport

    trade_bound = False
    try:
        if private_socket is None or trade_socket is None:
            return W4Report(
                status="sockets_required",
                exchange=exchange,
                symbol=symbol,
                error_code="transport_error",
            )

        runtime.bind_sockets(private=private_socket, trade=trade_socket, env=e)
        trade_bound = runtime.trade_socket is not None

        hs_err = _handshake_private_and_trade(
            runtime, exchange=exchange, ack_timeout_sec=ack_timeout_sec
        )
        if hs_err == "auth_failed":
            # Distinguish private vs trade login by auth state.
            if not runtime.authenticated:
                return W4Report(
                    status="auth_failed",
                    exchange=exchange,
                    symbol=symbol,
                    trade_ws_bound=trade_bound,
                    error_code="auth_failed",
                )
            return W4Report(
                status="trade_login_failed",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                sends_blocked=runtime.sends_blocked,
                error_code="auth_failed",
            )
        if hs_err == "venue_rejected":
            return W4Report(
                status="subscribe_failed",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=trade_bound,
                error_code="venue_rejected",
            )
        if hs_err == "reseed_required":
            return W4Report(
                status="reseed_required",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=trade_bound,
                subscription_ready=True,
                sends_blocked=runtime.sends_blocked,
                error_code="unknown",
            )
        if hs_err is not None:
            return W4Report(
                status="handshake_failed",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=trade_bound,
                error_code="transport_error",
            )

        # Restart/inflight recovery before any new W4 place attempt.
        # OKX WS cancel during recovery needs instIdCode from instruments metadata.
        if exchange == "okx":
            try:
                meta_early = metadata_provider.get(profile["venue"], symbol)
                code = meta_early.inst_id_code
                if (
                    not isinstance(code, int)
                    or isinstance(code, bool)
                    or code <= 0
                ):
                    raise W4ProfileError("okx W4 requires positive instIdCode metadata")
                runtime.okx_inst_id_code = code
            except (MetadataError, W4ProfileError, PreflightError):
                return W4Report(
                    status="l1_or_plan_rejected",
                    exchange=exchange,
                    symbol=symbol,
                    trade_ws_bound=True,
                    subscription_ready=True,
                    reseed_matched=True,
                    sends_blocked=runtime.sends_blocked,
                    error_code="invalid_request",
                )

        recovery_err = _recover_inflight_w4(
            sender=sender,
            runtime=runtime,
            provider=provider,
            cancel_transport=cancel_transport,
            credentials=credentials,
            env=e,
            terminal_wait_sec=float(terminal_wait_sec),
            journal=j,
            rest_order_recon=rest_order_recon,
        )
        if recovery_err is not None:
            return W4Report(
                status=recovery_err
                if recovery_err in {"recovery_blocked", "recovery_journal_failed"}
                else "recovery_blocked",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                sends_blocked=True,
                error_code="unknown",
            )

        # W4 OKX: verified net/one_way only (fail-closed before plan/send).
        try:
            assert_w4_okx_net_mode(
                exchange=exchange,
                venue=profile["venue"],
                position_mode_provider=position_mode_provider,
            )
        except (W4ProfileError, PreflightError):
            return W4Report(
                status="okx_position_mode_rejected",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                error_code="invalid_request",
            )

        # L1 → immutable plan price.
        try:
            quote = l1.snapshot(exchange=exchange, symbol=symbol)
            meta = metadata_provider.get(profile["venue"], symbol)
            if exchange == "okx":
                code = meta.inst_id_code
                if (
                    not isinstance(code, int)
                    or isinstance(code, bool)
                    or code <= 0
                ):
                    raise W4ProfileError("okx W4 requires positive instIdCode metadata")
                runtime.okx_inst_id_code = code
            px = limit_price_from_opposing_ask(quote.best_ask, meta)
            plan = build_order_plan(
                venue=profile["venue"],
                symbol=symbol,
                side="buy",
                mode="post_only_limit",
                metadata_provider=metadata_provider,
                qty=profile["qty"],
                price=str(px),
                ttl_sec=W4_TTL_SEC,
                expires_in_sec=max(30, W4_TTL_SEC + 20),
            )
            assert_exact_w4_plan(plan, profile)
            if exchange == "okx" and (
                not isinstance(plan.inst_id_code, int)
                or isinstance(plan.inst_id_code, bool)
                or plan.inst_id_code <= 0
            ):
                raise W4ProfileError("okx W4 plan missing positive instIdCode")
            # Immediate pre-send revalidation against fresh L1.
            quote2 = l1.snapshot(exchange=exchange, symbol=symbol)
            assert_plan_price_matches_l1(plan_price=str(plan.price), quote=quote2, meta=meta)
        except (L1Error, OrderPlanError, W4ProfileError, MetadataError, PreflightError):
            return W4Report(
                status="l1_or_plan_rejected",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                sends_blocked=runtime.sends_blocked,
                error_code="invalid_request",
            )

        # Fresh baseline again immediately before approval/send.
        try:
            assert_flat(baseline.check(exchange=exchange, symbol=symbol))
        except BaselineError:
            return W4Report(
                status="baseline_not_flat",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                error_code="invalid_request",
            )

        if approval_token is None:
            if not issue_approval:
                return W4Report(
                    status="approval_required",
                    exchange=exchange,
                    symbol=symbol,
                    trade_ws_bound=True,
                    subscription_ready=True,
                    reseed_matched=True,
                    error_code="invalid_request",
                )
            approval_token = vault.issue(plan)

        place_transport._plan = plan  # noqa: SLF001
        cancel_transport._plan = plan  # noqa: SLF001

        # Final L1 revalidation immediately before dispatch.
        try:
            quote3 = l1.snapshot(exchange=exchange, symbol=symbol)
            meta = metadata_provider.get(profile["venue"], symbol)
            assert_plan_price_matches_l1(plan_price=str(plan.price), quote=quote3, meta=meta)
        except L1Error:
            sender.journal_pre_send_gate(plan, gate_kind="price")
            return W4Report(
                status="l1_stale_pre_send",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                error_code="timeout",
            )

        if runtime.reseed_required or runtime.sends_blocked:
            return W4Report(
                status="stream_blocked",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=False,
                sends_blocked=True,
                error_code="unknown",
            )

        result = sender.send_approved(
            plan,
            approval_token,
            credentials,
            e,
            journal_transport="ws_trade",
            reconnect_generation=runtime.reconnect_generation,
        )
        orders_sent = 1 if result.transport_invoked else 0
        if result.status != "ack":
            return W4Report(
                status=f"place_{result.status}",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                sends_blocked=runtime.sends_blocked,
                orders_sent=orders_sent,
                ack_ok=False,
                reconnect_generation=runtime.reconnect_generation,
                error_code=result.error_code,
                venue_code=result.venue_code,
            )

        # Hard dispatch-time TTL: remaining after ACK; cancel by deadline (now if late).
        deadline = place_transport.ttl_deadline_mono_ns
        if deadline is None:
            sender.lease_supervisor.mark_process_sends_blocked()
            return W4Report(
                status="ttl_deadline_missing",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                sends_blocked=True,
                orders_sent=orders_sent,
                ack_ok=True,
                error_code="internal_error",
            )
        scheduler_overshoot = _sleep_until_ttl_deadline(
            deadline_mono_ns=deadline, sleep_fn=sleep_fn
        )
        if sender.lease is not None:
            sender.lease.check_ttl()

        cancel_res = sender.request_cancel_interface(
            plan,
            credentials,
            cancel_transport=cancel_transport,
            order_state_provider=provider,
            journal_transport="ws_trade",
            reconnect_generation=runtime.reconnect_generation,
        )
        cancel_acked = cancel_res.status in {"cancel_acked", "cancel_required", "ack"} or (
            cancel_res.transport_invoked and cancel_res.ack_state == "accepted"
        )
        if cancel_res.status in {"ambiguous", "recovery_journal_failed", "journal_failed"}:
            return W4Report(
                status=f"cancel_{cancel_res.status}",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                sends_blocked=True,
                orders_sent=orders_sent,
                ack_ok=True,
                cancel_acked=False,
                reconnect_generation=runtime.reconnect_generation,
                error_code=cancel_res.error_code or "unknown",
            )

        if scheduler_overshoot:
            # Cancel was issued, but scheduler missed the hard deadline — fail closed.
            sender.lease_supervisor.mark_process_sends_blocked()

        snap = _wait_private_terminal(
            runtime, provider, plan, timeout_sec=terminal_wait_sec
        )
        term = observed_terminal_state(snap)
        if term is None:
            # Fail-closed REST reconciliation scope (no invented terminal).
            try:
                j.append(
                    {
                        "event_type": "reconciliation",
                        "operation_id": plan.order_attempt_id,
                        "venue": exchange,
                        "environment": "live",
                        "outcome": "observed",
                        "dual_leg_id": plan.dual_leg_id,
                        "leg_id": plan.leg_id,
                        "reconciliation_scope": "post_only_ttl_recovery",
                        "reconciliation_state": "inconclusive",
                        "mismatch_fields": ["state", "timing"],
                        "observation_source": "rest_reconcile",
                        "transport": "rest",
                        "reconnect_generation": runtime.reconnect_generation,
                        "sequence_state": runtime.sequence_state.value,
                        "subscription_readiness": runtime.subscription_readiness.value,
                    }
                )
            except (OSError, RuntimeError, ValueError, TypeError, JournalValidationError):
                sender.lease_supervisor.mark_process_sends_blocked()
            return W4Report(
                status="terminal_inconclusive",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                sends_blocked=True,
                orders_sent=orders_sent,
                ack_ok=True,
                cancel_acked=bool(cancel_acked),
                terminal_observed=False,
                reconnect_generation=runtime.reconnect_generation,
                error_code="unknown",
            )

        if scheduler_overshoot:
            # Observed terminal but deadline was missed — still fail closed.
            try:
                runtime.journal_terminal_from_stream(plan, terminal_state=term)
                if sender.lease is not None:
                    sender.lease.mark_terminal()
            except (OSError, RuntimeError, ValueError, TypeError, JournalValidationError):
                pass
            return W4Report(
                status="ttl_scheduler_overshoot",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                sends_blocked=True,
                orders_sent=orders_sent,
                ack_ok=True,
                cancel_acked=bool(cancel_acked),
                terminal_observed=True,
                reconnect_generation=runtime.reconnect_generation,
                error_code="timeout",
            )

        # Journal observed terminal from private stream.
        try:
            runtime.journal_terminal_from_stream(plan, terminal_state=term)
            if sender.lease is not None:
                sender.lease.mark_terminal()
            j.append_post_only_ttl_matched_followup(
                venue=exchange,
                environment="live",
                operation_id=plan.order_attempt_id,
                dual_leg_id=plan.dual_leg_id,
                leg_id=plan.leg_id,
            )
        except (OSError, RuntimeError, ValueError, TypeError, JournalValidationError):
            sender.lease_supervisor.mark_process_sends_blocked()
            return W4Report(
                status="journal_failed",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                sends_blocked=True,
                orders_sent=orders_sent,
                ack_ok=True,
                cancel_acked=bool(cancel_acked),
                terminal_observed=True,
                error_code="internal_error",
            )

        return W4Report(
            status="ok",
            exchange=exchange,
            symbol=symbol,
            subscription_ready=True,
            reseed_matched=True,
            sends_blocked=runtime.sends_blocked,
            trade_ws_bound=True,
            orders_sent=orders_sent,
            ack_ok=True,
            cancel_acked=bool(cancel_acked),
            terminal_observed=True,
            reconnect_generation=runtime.reconnect_generation,
        )
    finally:
        for sock in (runtime.private_socket, runtime.trade_socket):
            if sock is not None:
                try:
                    sock.close()
                except Exception:  # noqa: BLE001
                    pass


def parse_w4_cli_args(argv: Sequence[str]) -> tuple[str, bool]:
    """Parse ``--venue=`` and ``--w4-approve-one-shot`` (W4 CLI only)."""
    venue = ""
    approve = False
    for arg in argv:
        if arg.startswith("--venue="):
            venue = arg.split("=", 1)[1].strip().lower()
        elif arg == "--w4-approve-one-shot":
            approve = True
    return venue, approve


def _public_http_get_json(url: str, headers: Mapping[str, str]) -> Mapping[str, Any]:
    """Stdlib GET JSON for public market metadata (no secrets).

    OKX URLs always carry R1 UA/Accept so urllib default is never used.
    """
    import urllib.request

    from app.bot.private.rest_readonly import (
        OKX_REST_ACCEPT,
        OKX_REST_USER_AGENT,
        okx_public_rest_headers,
    )

    hdrs = dict(headers)
    if "okx.com" in str(url).lower():
        pub = okx_public_rest_headers()
        hdrs.setdefault("Accept", pub["Accept"])
        hdrs.setdefault("User-Agent", pub["User-Agent"])
        # Force R1 identity even if caller passed Accept-only.
        hdrs["Accept"] = OKX_REST_ACCEPT
        hdrs["User-Agent"] = OKX_REST_USER_AGENT
    req = urllib.request.Request(url, headers=hdrs, method="GET")
    with urllib.request.urlopen(req, timeout=15.0) as resp:  # noqa: S310
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, Mapping):
        raise RuntimeError("metadata response malformed")
    return data


@dataclass
class W4RuntimeBindings:
    """Explicit W4 production (or test) bindings — never used by default CLI."""

    credentials: LiveCredentials
    metadata_provider: MetadataProvider
    position_mode_provider: PositionModeProvider
    baseline: FlatBaselinePort
    l1: PublicL1Port
    private_socket: PrivateWsSocket
    trade_socket: PrivateWsSocket
    l1_closer: Optional[Any] = None
    rest_order_recon: Optional[Any] = None


def open_w4_production_bindings(
    *,
    venue: str,
    env: Mapping[str, str],
    credentials: Optional[LiveCredentials] = None,
) -> W4RuntimeBindings:
    """Signed flat baseline before any trade/private/public sockets.

    Callable only after W4 gates. Unbinds nothing — caller must cleanup.
    """
    from app.bot.private.order_preflight import (
        LiveHttpMetadataProvider,
        LiveSignedPositionModeProvider,
    )
    from app.bot.private.ws_l1_public import PublicL1WsAdapter
    from app.bot.private.ws_socket import WebsocketsSocketFactory, bind_socket_factory
    from app.bot.private.ws_w4_baseline import (
        SignedRestFlatBaseline,
        build_signed_rest_order_recon,
    )

    profile = resolve_w4_profile(venue)
    exchange = profile["exchange"]
    symbol = profile["symbol"]
    secrets = load_live_secrets(dict(env), require_complete=True)
    if credentials is None:
        credentials = _creds_from_live(secrets, exchange)

    ep = endpoints_for_venue("live")
    meta = LiveHttpMetadataProvider(http_get_json=_public_http_get_json)
    pos = LiveSignedPositionModeProvider(
        exchange=exchange,
        credentials=credentials,
        bybit_base=ep.bybit_rest,
        okx_base=ep.okx_rest,
        symbol=symbol,
    )
    baseline = SignedRestFlatBaseline(
        exchange=exchange,
        credentials=credentials,
        endpoints=ep,
    )
    # Multi-venue GET recon: Bybit lease during OKX run uses Bybit live creds.
    rest_order_recon = build_signed_rest_order_recon(
        bybit_credentials=_creds_from_live(secrets, "bybit"),
        okx_credentials=_creds_from_live(secrets, "okx"),
        endpoints=ep,
    )
    # Production binding order: signed flat baseline before any WS sockets.
    assert_flat(baseline.check(exchange=exchange, symbol=symbol))
    assert_w4_okx_net_mode(
        exchange=exchange,
        venue=profile["venue"],
        position_mode_provider=pos,
    )

    factory = WebsocketsSocketFactory()
    bind_socket_factory(factory)

    from app.bot.private.ws_private import trade_ws_url_for_exchange

    if exchange == "bybit":
        private_url = ep.bybit_private_ws
        public_url = ep.bybit_public_ws
    else:
        # Stream socket stays on private; trade is a *second* private connection
        # (OKX Place/Cancel require /ws/v5/private — not business).
        private_url = ep.okx_private_ws
        public_url = ep.okx_public_ws
    trade_url = trade_ws_url_for_exchange(exchange, ep)

    # Public L1 only after baseline; private/trade after baseline too.
    private_sock = factory.open(private_url)
    trade_sock = factory.open(trade_url)
    l1_sock = factory.open(public_url)
    l1_adapter = PublicL1WsAdapter(exchange=exchange, symbol=symbol)
    l1_adapter.bind(l1_sock)
    # Must receive a real quote frame — subscribe alone is insufficient.
    l1_adapter.await_fresh_quote(timeout_sec=15.0)

    return W4RuntimeBindings(
        credentials=credentials,
        metadata_provider=meta,
        position_mode_provider=pos,
        baseline=baseline,
        l1=l1_adapter,
        private_socket=private_sock,
        trade_socket=trade_sock,
        l1_closer=l1_adapter,
        rest_order_recon=rest_order_recon,
    )


def main_ws_w4_post_only(
    argv: Optional[Sequence[str]] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    bindings: Optional[W4RuntimeBindings] = None,
) -> int:
    """CLI entry for ``--ws-w4-post-only --venue=bybit|okx``.

    When all live W4 gates pass, binds production sockets/providers (or uses
    injected ``bindings`` in tests) and runs ``run_w4_post_only``.
    ``--w4-approve-one-shot`` is required to issue plan-bound approval after
    L1 plan construction; without it the run rejects before send.
    Default / ``--ws-readonly`` paths never call this binder.
    """
    argv = list(argv or [])
    e = dict(env if env is not None else os.environ)
    venue, approve_one_shot = parse_w4_cli_args(argv)

    def _print_report(report: W4Report) -> None:
        print(json.dumps(report.as_public_dict(), ensure_ascii=False, indent=2, sort_keys=True))

    if venue not in W4_PROFILES:
        _print_report(
            W4Report(
                status="rejected_before_socket",
                exchange=venue or "",
                symbol="",
                error_code="invalid_request",
            )
        )
        return 1

    # Approval switch has no effect outside exact W4 profiles (already gated).
    try:
        assert_ws_w4_send_gates(e)
        resolve_w4_profile(venue)
    except (WsProfileGateError, W4ProfileError):
        _print_report(
            W4Report(
                status="rejected_before_socket",
                exchange=venue,
                symbol="",
                error_code="invalid_request",
            )
        )
        return 1

    if not approve_one_shot:
        _print_report(
            W4Report(
                status="approval_required",
                exchange=venue,
                symbol=W4_PROFILES[venue]["symbol"],
                error_code="invalid_request",
            )
        )
        return 1

    # Order transport must remain unbound; only W4 entrypoint binds WS factory.
    assert_default_entrypoint_cannot_transport()

    owned_bindings = bindings is None
    active: Optional[W4RuntimeBindings] = bindings
    try:
        if active is None:
            # Refuse if a factory was somehow already bound outside this entry.
            try:
                assert_no_default_ws_socket()
            except RuntimeError:
                _print_report(
                    W4Report(
                        status="rejected_before_socket",
                        exchange=venue,
                        symbol="",
                        error_code="transport_error",
                    )
                )
                return 2
            try:
                active = open_w4_production_bindings(venue=venue, env=e)
            except (
                BaselineError,
                L1Error,
                W4ProfileError,
                PreflightError,
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                KeyError,
            ):
                unbind_socket_factory()
                _print_report(
                    W4Report(
                        status="bind_failed",
                        exchange=venue,
                        symbol=W4_PROFILES[venue]["symbol"],
                        error_code="transport_error",
                    )
                )
                return 2

        report = run_w4_post_only(
            venue=venue,
            env=e,
            metadata_provider=active.metadata_provider,
            position_mode_provider=active.position_mode_provider,
            l1=active.l1,
            baseline=active.baseline,
            private_socket=active.private_socket,
            trade_socket=active.trade_socket,
            credentials=active.credentials,
            load_secrets=False,
            issue_approval=True,  # gated by --w4-approve-one-shot above
            rest_order_recon=active.rest_order_recon,
        )
        _print_report(report)
        if report.status == "ok":
            return 0
        if report.status in {"secrets_unavailable", "approval_required", "rejected_before_socket"}:
            return 1
        return 2
    finally:
        if active is not None and active.l1_closer is not None:
            try:
                active.l1_closer.close()
            except Exception:  # noqa: BLE001
                pass
        if owned_bindings:
            unbind_socket_factory()
            assert_default_entrypoint_cannot_transport()
