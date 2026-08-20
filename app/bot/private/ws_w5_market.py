"""W5 bounded WS market samples (explicit ``--ws-w5-market`` only).

Immutable profiles (same lots as W4):
  - Bybit BTCUSDT BUY qty 0.001 market → SELL reduce_only qty 0.001 market
  - OKX BTC-USDT-SWAP BUY qty 0.01 market → SELL reduce_only qty 0.01 market

Sequential single venue. After fill, flatten immediately. Do not send the other
venue until REST baseline is flat. Requires VENUE=live, LIVE_ORDERS=1,
BBOT_PRIVATE_W5=1, and ``--w5-approve-one-shot``. Default / W3 / W4 CLI never
binds this runner.
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
    derive_latency_intervals_from_op_events,
    new_opaque_id,
    scan_all_journal_events,
)
from app.bot.private.order_approval import ApprovalToken, ApprovalVault
from app.bot.private.order_lease import (
    LeaseState,
    OrderStateSnapshot,
    observed_terminal_state,
)
from app.bot.private.order_metadata import MetadataError, MetadataProvider
from app.bot.private.order_plan import OrderPlan, OrderPlanError, build_order_plan
from app.bot.private.order_preflight import PositionModeProvider, PreflightError
from app.bot.private.order_sender import (
    ApprovalBoundSender,
    TransportAck,
    assert_default_entrypoint_cannot_transport,
)
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.paths import resolve_data_root
from app.bot.private.secrets import load_live_secrets
from app.bot.private.venue import endpoints_for_venue
from app.bot.private.ws_gates import WsProfileGateError, assert_ws_w5_send_gates
from app.bot.private.ws_private import (
    PrivateStreamRuntime,
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
from app.bot.private.ws_w4_postonly import (
    _handshake_private_and_trade,
    _wait_private_terminal,
    assert_w4_okx_net_mode,
)

LOG = logging.getLogger("bbot.private.ws_w5")

W5_TERMINAL_WAIT_SEC = 15.0
W5_PROFILES: dict[str, dict[str, Any]] = {
    "bybit": {
        "exchange": "bybit",
        "venue": "bybit_live",
        "symbol": "BTCUSDT",
        "qty": "0.001",
        "buy_side": "buy",
        "flatten_side": "sell",
        "mode": "market",
    },
    "okx": {
        "exchange": "okx",
        "venue": "okx_live",
        "symbol": "BTC-USDT-SWAP",
        "qty": "0.01",
        "buy_side": "buy",
        "flatten_side": "sell",
        "mode": "market",
    },
}


class W5ProfileError(ValueError):
    """Reject anything outside the immutable W5 market profiles."""


@dataclass
class W5Report:
    status: str
    exchange: str
    symbol: str
    subscription_ready: bool = False
    reseed_matched: bool = False
    sends_blocked: bool = True
    trade_ws_bound: bool = False
    orders_sent: int = 0
    buy_ack_ok: bool = False
    buy_filled: bool = False
    flatten_ack_ok: bool = False
    flatten_filled: bool = False
    flat_after: bool = False
    reconnect_generation: int = 0
    error_code: Optional[str] = None
    venue_code: Optional[str] = None

    def as_public_dict(self) -> dict[str, Any]:
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
            "buy_ack_ok": self.buy_ack_ok,
            "buy_filled": self.buy_filled,
            "flatten_ack_ok": self.flatten_ack_ok,
            "flatten_filled": self.flatten_filled,
            "flat_after": self.flat_after,
            "reconnect_generation": self.reconnect_generation,
            "mode": "market",
            "side": "buy",
        }
        if self.error_code:
            out["error_code"] = self.error_code
        vc = sanitize_venue_code(self.venue_code)
        if vc is not None:
            out["venue_code"] = vc
        return out


def resolve_w5_profile(venue_flag: str) -> dict[str, Any]:
    key = venue_flag.strip().lower()
    if key not in W5_PROFILES:
        raise W5ProfileError("W5 venue must be bybit or okx")
    return dict(W5_PROFILES[key])


def assert_exact_w5_buy_plan(plan: OrderPlan, profile: Mapping[str, Any]) -> None:
    if plan.venue != profile["venue"]:
        raise W5ProfileError("plan venue outside W5 profile")
    if plan.symbol != profile["symbol"]:
        raise W5ProfileError("plan symbol outside W5 profile")
    if plan.side != "buy":
        raise W5ProfileError("W5 buy leg only allows buy")
    if plan.mode != "market" or plan.post_only:
        raise W5ProfileError("W5 only allows market mode")
    if plan.qty != profile["qty"]:
        raise W5ProfileError("W5 qty must match immutable profile")
    if plan.reduce_only:
        raise W5ProfileError("W5 buy rejects reduce_only")
    if plan.price is not None:
        raise W5ProfileError("W5 market must not include price")
    if plan.k_live != 1:
        raise W5ProfileError("W5 requires K_live=1")


def assert_exact_w5_flatten_plan(
    plan: OrderPlan, profile: Mapping[str, Any], *, dual_leg_id: str
) -> None:
    if plan.venue != profile["venue"]:
        raise W5ProfileError("flatten venue outside W5 profile")
    if plan.symbol != profile["symbol"]:
        raise W5ProfileError("flatten symbol outside W5 profile")
    if plan.side != "sell":
        raise W5ProfileError("W5 flatten only allows sell")
    if plan.mode != "market" or plan.post_only:
        raise W5ProfileError("W5 flatten only allows market")
    if plan.qty != profile["qty"]:
        raise W5ProfileError("W5 flatten qty must match profile")
    if not plan.reduce_only:
        raise W5ProfileError("W5 flatten requires reduce_only")
    if plan.dual_leg_id != dual_leg_id:
        raise W5ProfileError("W5 flatten dual_leg_id must link buy attempt")
    if plan.k_live != 1:
        raise W5ProfileError("W5 requires K_live=1")


def assert_w5_okx_net_mode(
    *,
    exchange: str,
    venue: str,
    position_mode_provider: PositionModeProvider,
) -> None:
    """Same net/one_way rule as W4 — hedge rejected."""
    try:
        assert_w4_okx_net_mode(
            exchange=exchange,
            venue=venue,
            position_mode_provider=position_mode_provider,
        )
    except Exception as exc:  # noqa: BLE001 — remap W4ProfileError
        from app.bot.private.ws_w4_postonly import W4ProfileError

        if isinstance(exc, W4ProfileError):
            raise W5ProfileError(str(exc)) from exc
        raise


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


def _journal_venue(plan: OrderPlan) -> str:
    return "okx" if str(plan.venue).startswith("okx") else "bybit"


@dataclass
class _WsTradePlaceTransport:
    """W5 place-only WS trade transport (no TTL deadline)."""

    runtime: PrivateStreamRuntime
    ack_timeout_sec: float = 5.0
    last_req_id: str = ""
    _bbot_ws_trade: bool = True

    def __call__(self, payload: Any) -> TransportAck:
        from app.bot.private.order_sign import WsTradeDispatch

        plan = getattr(self, "_plan", None)
        if isinstance(payload, WsTradeDispatch):
            plan = payload.plan
        if plan is None:
            raise RuntimeError("WS trade transport missing plan binding")
        req_id = _new_req_id(exchange=self.runtime.exchange)
        self.last_req_id = req_id
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


def assert_w5_transport_is_ws_trade(transport: Any) -> None:
    from app.bot.private.order_transport import is_live_http_order_transport

    if is_live_http_order_transport(transport):
        raise W5ProfileError("W5 refuses HTTP order transport; require trade WS")
    if isinstance(transport, _WsTradePlaceTransport):
        return
    if getattr(transport, "_bbot_ws_trade", False):
        return
    raise W5ProfileError("W5 requires explicit ws_trade transport binding")


def _append_leg_latency(
    journal: PrivateJournalWriter,
    plan: OrderPlan,
) -> None:
    op_events = [
        e
        for e in scan_all_journal_events(journal.data_root)
        if str(e.get("operation_id")) == str(plan.order_attempt_id)
    ]
    intervals = derive_latency_intervals_from_op_events(op_events)
    if not intervals:
        raise JournalValidationError("W5 latency_summary requires derivable intervals")
    body = journal.build_latency_summary_event(
        venue=_journal_venue(plan),
        environment="live",
        operation_id=plan.order_attempt_id,
        intervals_ms=intervals,
        latency_basis="monotonic_local",
        sample_count=1,
        dual_leg_id=plan.dual_leg_id,
        leg_id=plan.leg_id,
    )
    journal.append(body)


def _commit_stream_terminal(
    *,
    runtime: PrivateStreamRuntime,
    journal: PrivateJournalWriter,
    lease: Any,
    plan: OrderPlan,
    term: str,
) -> None:
    runtime.journal_terminal_from_stream(plan, terminal_state=term)
    _append_leg_latency(journal, plan)
    lease.mark_terminal()


def _plan_matches_runtime(plan: OrderPlan, runtime: PrivateStreamRuntime) -> bool:
    from app.bot.private.ws_w4_baseline import plan_matches_runtime_exchange

    return plan_matches_runtime_exchange(plan, runtime.exchange)


def _unresolved_market_leases(sender: ApprovalBoundSender) -> list[Any]:
    out: list[Any] = []
    for lease in list(sender.lease_supervisor._leases.values()):  # noqa: SLF001
        if lease.state == LeaseState.TERMINAL:
            continue
        plan = lease.plan
        if getattr(plan, "mode", None) != "market":
            # Non-market unresolved (e.g. W4) still blocks — surface for recovery_blocked.
            out.append(lease)
            continue
        out.append(lease)
    return out


def _plan_exchange_symbol(plan: OrderPlan) -> tuple[str, str]:
    from app.bot.private.ws_w4_baseline import plan_runtime_exchange

    return plan_runtime_exchange(plan), str(plan.symbol)


def _commit_rest_flat_matched(
    *,
    journal: PrivateJournalWriter,
    lease: Any,
    plan: OrderPlan,
    scope: str = "post_dispatch_ambiguity",
) -> None:
    """REST flat / no open order — never invent fill; matched recon only."""
    journal.append(
        {
            "event_type": "reconciliation",
            "operation_id": plan.order_attempt_id,
            "venue": _journal_venue(plan),
            "environment": "live",
            "outcome": "observed",
            "dual_leg_id": plan.dual_leg_id,
            "leg_id": plan.leg_id,
            "reconciliation_scope": scope,
            "reconciliation_state": "matched",
            "observation_source": "rest_reconcile",
            "transport": "rest",
        }
    )
    lease.mark_terminal()


def _clear_dispatch_block_if_idle(sender: ApprovalBoundSender) -> None:
    """After market recovery, clear reconstruction block if no lease still blocks."""
    still = any(
        lease.blocks_new_sends
        for lease in sender.lease_supervisor._leases.values()  # noqa: SLF001
    )
    if not still:
        sender.lease_supervisor._dispatch_blocking = False  # noqa: SLF001


def _recover_inflight_w5(
    *,
    sender: ApprovalBoundSender,
    runtime: PrivateStreamRuntime,
    provider: WsOrderStateProvider,
    place_transport: _WsTradePlaceTransport,
    credentials: LiveCredentials,
    metadata_provider: MetadataProvider,
    profile: Mapping[str, Any],
    journal: PrivateJournalWriter,
    baseline: FlatBaselinePort,
    rest_order_recon: Optional[Any],
    terminal_wait_sec: float,
    vault: ApprovalVault,
    issue_approval: bool,
) -> Optional[str]:
    """Recover unresolved market (or blocking) leases before a new W5 buy.

    - Cross-venue: REST only; never flatten on the wrong trade socket.
    - Ack-without-terminal + REST flat → matched recon (never invent fill).
    - Position still open on same venue → WS reduce-only market flatten.
    """
    unresolved = _unresolved_market_leases(sender)
    if not unresolved and not sender.lease_supervisor.has_blocking_lease():
        return None
    if not unresolved and sender.lease_supervisor.has_blocking_lease():
        return "recovery_blocked"

    observe_sec = max(0.05, min(float(terminal_wait_sec), 5.0))
    run_exchange = profile["exchange"]
    run_symbol = profile["symbol"]

    for lease in unresolved:
        plan = lease.plan
        same_venue = _plan_matches_runtime(plan, runtime)
        plan_ex, plan_sym = _plan_exchange_symbol(plan)
        sender._lease = lease  # noqa: SLF001

        term: Optional[str] = None
        source: Optional[str] = None
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
            runtime.register_plan_fingerprint(plan)
            snap = _wait_private_terminal(
                runtime, provider, plan, timeout_sec=observe_sec
            )
            term = observed_terminal_state(snap)
            if term is not None:
                source = "stream"

        if term is not None and source == "rest" and term in {"cancelled", "expired"}:
            # REST CANCELLED/EXPIRED (+ require_position_flat) ⇒ flat; never invent fill.
            try:
                if str(plan.order_attempt_id).startswith("exposure_flatten_"):
                    # Synthetic exposure stub — no prepared journal op; clear only.
                    lease.mark_terminal()
                else:
                    scope = (
                        "dual_leg_state"
                        if bool(plan.reduce_only)
                        else "post_dispatch_ambiguity"
                    )
                    _commit_rest_flat_matched(
                        journal=journal, lease=lease, plan=plan, scope=scope
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

        if term is not None and source == "rest" and term == "filled":
            # Observed fill via REST — never invent; foreign venue stays blocked.
            if not same_venue:
                sender.lease_supervisor.mark_process_sends_blocked()
                return "recovery_blocked"
            try:
                if lease.acked:
                    runtime.journal_terminal_from_rest(plan, terminal_state="filled")
                    try:
                        _append_leg_latency(journal, plan)
                    except JournalValidationError:
                        pass
                    lease.mark_terminal()
                    if bool(plan.reduce_only):
                        continue
                    # Buy fill observed — same-venue flatten required below.
                else:
                    # No ack → matched recon only (no invent fill terminal).
                    _commit_rest_flat_matched(journal=journal, lease=lease, plan=plan)
                    continue
            except (
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                JournalValidationError,
            ):
                sender.lease_supervisor.mark_process_sends_blocked()
                return "recovery_journal_failed"
            # Fall through to same-venue flatten for filled buy.
            term = None
            source = None

        if term is not None and source == "stream":
            try:
                if term == "filled":
                    _commit_stream_terminal(
                        runtime=runtime,
                        journal=journal,
                        lease=lease,
                        plan=plan,
                        term="filled",
                    )
                else:
                    # Stream cancelled/expired on market — journal terminal, no invent fill.
                    runtime.journal_terminal_from_stream(plan, terminal_state=term)
                    lease.mark_terminal()
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

        # Still unresolved.
        if not same_venue:
            # Foreign venue still open — must not place/flatten on this trade socket.
            sender.lease_supervisor.mark_process_sends_blocked()
            return "recovery_blocked"

        # Same venue: check flat on *plan* symbol (not only runner profile).
        try:
            base = baseline.check(exchange=plan_ex, symbol=plan_sym)
        except BaselineError:
            # Baseline may be single-venue; fall back to REST-only if available.
            if rest_order_recon is not None:
                try:
                    snap_b = rest_order_recon.get(plan)
                    if observed_terminal_state(snap_b) is not None:
                        try:
                            _commit_rest_flat_matched(
                                journal=journal, lease=lease, plan=plan
                            )
                            continue
                        except (
                            OSError,
                            RuntimeError,
                            ValueError,
                            TypeError,
                            JournalValidationError,
                        ):
                            sender.lease_supervisor.mark_process_sends_blocked()
                            return "recovery_journal_failed"
                except Exception:  # noqa: BLE001
                    pass
            sender.lease_supervisor.mark_process_sends_blocked()
            return "recovery_blocked"
        if base.ok:
            # Flat with unresolved lease — matched recon (never invent fill).
            try:
                _commit_rest_flat_matched(journal=journal, lease=lease, plan=plan)
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

        # Position / opens not flat → WS reduce-only flatten on this venue only.
        if runtime.sends_blocked or runtime.reseed_required:
            sender.lease_supervisor.mark_process_sends_blocked()
            return "recovery_blocked"
        # Flatten must use the *plan* venue profile, not a mismatched runner.
        if plan_ex != run_exchange:
            sender.lease_supervisor.mark_process_sends_blocked()
            return "recovery_blocked"
        try:
            flatten = build_order_plan(
                venue=profile["venue"],
                symbol=run_symbol,
                side="sell",
                mode="market",
                metadata_provider=metadata_provider,
                qty=profile["qty"],
                reduce_only=True,
                dual_leg_id=plan.dual_leg_id,
                expires_in_sec=60,
            )
            assert_exact_w5_flatten_plan(
                flatten, profile, dual_leg_id=plan.dual_leg_id
            )
        except (OrderPlanError, W5ProfileError, MetadataError):
            sender.lease_supervisor.mark_process_sends_blocked()
            return "recovery_blocked"

        if not issue_approval:
            sender.lease_supervisor.mark_process_sends_blocked()
            return "recovery_blocked"
        token = vault.issue(flatten)
        runtime.register_plan_fingerprint(flatten)
        place_transport._plan = flatten  # noqa: SLF001
        sender.transport = place_transport
        res = sender.send_approved(
            flatten,
            token,
            credentials,
            {"VENUE": "live", "LIVE_ORDERS": "1", "BBOT_PRIVATE_W5": "1"},
            journal_transport="ws_trade",
            reconnect_generation=runtime.reconnect_generation,
        )
        if res.status != "ack":
            sender.lease_supervisor.mark_process_sends_blocked()
            return "recovery_blocked"
        snap2 = _wait_private_terminal(
            runtime, provider, flatten, timeout_sec=float(terminal_wait_sec)
        )
        term2 = observed_terminal_state(snap2)
        if term2 != "filled":
            sender.lease_supervisor.mark_process_sends_blocked()
            return "recovery_blocked"
        try:
            fl = sender.lease
            assert fl is not None
            _commit_stream_terminal(
                runtime=runtime,
                journal=journal,
                lease=fl,
                plan=flatten,
                term="filled",
            )
            base2 = baseline.check(exchange=plan_ex, symbol=plan_sym)
            assert_flat(base2)
            if lease.state != LeaseState.TERMINAL:
                _commit_rest_flat_matched(
                    journal=journal,
                    lease=lease,
                    plan=plan,
                    scope="dual_leg_state",
                )
        except (
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            JournalValidationError,
            BaselineError,
        ):
            sender.lease_supervisor.mark_process_sends_blocked()
            return "recovery_journal_failed"

    _clear_dispatch_block_if_idle(sender)
    if sender.lease_supervisor.has_blocking_lease():
        return "recovery_blocked"
    return None


def run_w5_market(
    *,
    venue: str,
    env: Optional[Mapping[str, str]] = None,
    metadata_provider: MetadataProvider,
    position_mode_provider: PositionModeProvider,
    baseline: FlatBaselinePort,
    private_socket: Optional[PrivateWsSocket] = None,
    trade_socket: Optional[PrivateWsSocket] = None,
    issue_approval: bool = False,
    credentials: Optional[LiveCredentials] = None,
    load_secrets: bool = True,
    journal: Optional[PrivateJournalWriter] = None,
    data_root: Optional[Path] = None,
    rest_probe_fn: Optional[Any] = None,
    ack_timeout_sec: float = 5.0,
    terminal_wait_sec: float = W5_TERMINAL_WAIT_SEC,
    rest_order_recon: Optional[Any] = None,
    place_transport_override: Optional[Any] = None,
    fill_inject_fn: Optional[Any] = None,
) -> W5Report:
    """Execute one W5 market buy + reduce-only flatten for a single venue."""
    e = dict(env if env is not None else os.environ)
    try:
        assert_ws_w5_send_gates(e)
        profile = resolve_w5_profile(venue)
    except (WsProfileGateError, W5ProfileError):
        return W5Report(
            status="rejected_before_socket",
            exchange=venue,
            symbol="",
            error_code="invalid_request",
        )

    exchange = profile["exchange"]
    symbol = profile["symbol"]
    root = data_root if data_root is not None else resolve_data_root(e)
    j = journal if journal is not None else PrivateJournalWriter(
        root, run_id=new_opaque_id("run")
    )

    if credentials is None:
        if not load_secrets:
            return W5Report(
                status="secrets_unavailable",
                exchange=exchange,
                symbol=symbol,
                error_code="auth_unavailable",
            )
        try:
            secrets = load_live_secrets(e, require_complete=True)
            credentials = _creds_from_live(secrets, exchange)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return W5Report(
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
                require_position_flat=True,
            )

    try:
        base = baseline.check(exchange=exchange, symbol=symbol)
        assert_flat(base)
    except BaselineError:
        return W5Report(
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
        profile_gate=assert_ws_w5_send_gates,
    )

    place_transport = place_transport_override or _WsTradePlaceTransport(
        runtime=runtime, ack_timeout_sec=ack_timeout_sec
    )
    try:
        assert_w5_transport_is_ws_trade(place_transport)
    except W5ProfileError:
        return W5Report(
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

    trade_bound = False
    orders_sent = 0
    try:
        if private_socket is None or trade_socket is None:
            return W5Report(
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
        if hs_err is not None:
            status_map = {
                "auth_failed": "auth_failed",
                "venue_rejected": "subscribe_failed",
                "reseed_required": "reseed_required",
            }
            return W5Report(
                status=status_map.get(hs_err, "handshake_failed"),
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=trade_bound,
                subscription_ready=hs_err not in {"auth_failed"},
                reseed_matched=hs_err is None,
                sends_blocked=runtime.sends_blocked,
                error_code="auth_failed" if "auth" in hs_err else "unknown",
            )

        if exchange == "okx":
            try:
                meta_early = metadata_provider.get(profile["venue"], symbol)
                code = meta_early.inst_id_code
                if (
                    not isinstance(code, int)
                    or isinstance(code, bool)
                    or code <= 0
                ):
                    raise W5ProfileError("okx W5 requires positive instIdCode")
                runtime.okx_inst_id_code = code
            except (MetadataError, W5ProfileError):
                return W5Report(
                    status="plan_rejected",
                    exchange=exchange,
                    symbol=symbol,
                    trade_ws_bound=True,
                    subscription_ready=True,
                    reseed_matched=True,
                    error_code="invalid_request",
                )

        recovery_err = _recover_inflight_w5(
            sender=sender,
            runtime=runtime,
            provider=provider,
            place_transport=place_transport,
            credentials=credentials,
            metadata_provider=metadata_provider,
            profile=profile,
            journal=j,
            baseline=baseline,
            rest_order_recon=rest_order_recon,
            terminal_wait_sec=float(terminal_wait_sec),
            vault=vault,
            issue_approval=issue_approval,
        )
        if recovery_err is not None:
            return W5Report(
                status=recovery_err,
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                sends_blocked=True,
                error_code="unknown",
            )

        try:
            assert_w5_okx_net_mode(
                exchange=exchange,
                venue=profile["venue"],
                position_mode_provider=position_mode_provider,
            )
        except (W5ProfileError, PreflightError):
            return W5Report(
                status="okx_position_mode_rejected",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                error_code="invalid_request",
            )

        try:
            buy_plan = build_order_plan(
                venue=profile["venue"],
                symbol=symbol,
                side="buy",
                mode="market",
                metadata_provider=metadata_provider,
                qty=profile["qty"],
                reduce_only=False,
                expires_in_sec=60,
            )
            assert_exact_w5_buy_plan(buy_plan, profile)
            if exchange == "okx":
                runtime.okx_inst_id_code = buy_plan.inst_id_code
        except (OrderPlanError, W5ProfileError, MetadataError):
            return W5Report(
                status="plan_rejected",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                error_code="invalid_request",
            )

        if not issue_approval:
            return W5Report(
                status="approval_required",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                error_code="invalid_request",
            )

        # Re-check flat immediately before buy dispatch.
        try:
            assert_flat(baseline.check(exchange=exchange, symbol=symbol))
        except BaselineError:
            return W5Report(
                status="baseline_not_flat",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                error_code="invalid_request",
            )

        if runtime.reseed_required or runtime.sends_blocked:
            return W5Report(
                status="stream_blocked",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=False,
                sends_blocked=True,
                error_code="unknown",
            )

        buy_token = vault.issue(buy_plan)
        runtime.register_plan_fingerprint(buy_plan)
        place_transport._plan = buy_plan  # noqa: SLF001
        sender.transport = place_transport
        buy_res = sender.send_approved(
            buy_plan,
            buy_token,
            credentials,
            e,
            journal_transport="ws_trade",
            reconnect_generation=runtime.reconnect_generation,
        )
        orders_sent = 1 if buy_res.transport_invoked else 0
        if buy_res.status != "ack":
            return W5Report(
                status=f"buy_{buy_res.status}",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                sends_blocked=runtime.sends_blocked
                or buy_res.status in {"ambiguous", "journal_failed"},
                orders_sent=orders_sent,
                buy_ack_ok=False,
                reconnect_generation=runtime.reconnect_generation,
                error_code=buy_res.error_code,
                venue_code=buy_res.venue_code,
            )

        if fill_inject_fn is not None:
            try:
                fill_inject_fn("buy", buy_plan)
            except Exception:  # noqa: BLE001
                pass

        buy_snap = _wait_private_terminal(
            runtime, provider, buy_plan, timeout_sec=float(terminal_wait_sec)
        )
        buy_term = observed_terminal_state(buy_snap)
        if buy_term != "filled":
            sender.lease_supervisor.mark_process_sends_blocked()
            return W5Report(
                status="buy_terminal_inconclusive"
                if buy_term is None
                else f"buy_{buy_term}",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                sends_blocked=True,
                orders_sent=orders_sent,
                buy_ack_ok=True,
                buy_filled=False,
                reconnect_generation=runtime.reconnect_generation,
                error_code="unknown",
            )

        try:
            assert sender.lease is not None
            _commit_stream_terminal(
                runtime=runtime,
                journal=j,
                lease=sender.lease,
                plan=buy_plan,
                term="filled",
            )
        except (
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            JournalValidationError,
        ):
            sender.lease_supervisor.mark_process_sends_blocked()
            return W5Report(
                status="journal_failed",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                sends_blocked=True,
                orders_sent=orders_sent,
                buy_ack_ok=True,
                buy_filled=True,
                error_code="internal_error",
            )

        # Immediate flatten — reduce-only market sell, same dual_leg_id.
        try:
            flatten_plan = build_order_plan(
                venue=profile["venue"],
                symbol=symbol,
                side="sell",
                mode="market",
                metadata_provider=metadata_provider,
                qty=profile["qty"],
                reduce_only=True,
                dual_leg_id=buy_plan.dual_leg_id,
                expires_in_sec=60,
            )
            assert_exact_w5_flatten_plan(
                flatten_plan, profile, dual_leg_id=buy_plan.dual_leg_id
            )
        except (OrderPlanError, W5ProfileError, MetadataError):
            sender.lease_supervisor.mark_process_sends_blocked()
            return W5Report(
                status="flatten_plan_rejected",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                sends_blocked=True,
                orders_sent=orders_sent,
                buy_ack_ok=True,
                buy_filled=True,
                error_code="invalid_request",
            )

        flatten_token = vault.issue(flatten_plan)
        runtime.register_plan_fingerprint(flatten_plan)
        place_transport._plan = flatten_plan  # noqa: SLF001
        flatten_res = sender.send_approved(
            flatten_plan,
            flatten_token,
            credentials,
            e,
            journal_transport="ws_trade",
            reconnect_generation=runtime.reconnect_generation,
        )
        if flatten_res.transport_invoked:
            orders_sent += 1
        if flatten_res.status != "ack":
            sender.lease_supervisor.mark_process_sends_blocked()
            return W5Report(
                status="flatten_incomplete",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                sends_blocked=True,
                orders_sent=orders_sent,
                buy_ack_ok=True,
                buy_filled=True,
                flatten_ack_ok=False,
                reconnect_generation=runtime.reconnect_generation,
                error_code=flatten_res.error_code or "unknown",
                venue_code=flatten_res.venue_code,
            )

        if fill_inject_fn is not None:
            try:
                fill_inject_fn("flatten", flatten_plan)
            except Exception:  # noqa: BLE001
                pass

        flat_snap = _wait_private_terminal(
            runtime, provider, flatten_plan, timeout_sec=float(terminal_wait_sec)
        )
        flat_term = observed_terminal_state(flat_snap)
        if flat_term != "filled":
            sender.lease_supervisor.mark_process_sends_blocked()
            return W5Report(
                status="flatten_incomplete",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                sends_blocked=True,
                orders_sent=orders_sent,
                buy_ack_ok=True,
                buy_filled=True,
                flatten_ack_ok=True,
                flatten_filled=False,
                reconnect_generation=runtime.reconnect_generation,
                error_code="unknown",
            )

        try:
            assert sender.lease is not None
            _commit_stream_terminal(
                runtime=runtime,
                journal=j,
                lease=sender.lease,
                plan=flatten_plan,
                term="filled",
            )
        except (
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            JournalValidationError,
        ):
            sender.lease_supervisor.mark_process_sends_blocked()
            return W5Report(
                status="journal_failed",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                sends_blocked=True,
                orders_sent=orders_sent,
                buy_ack_ok=True,
                buy_filled=True,
                flatten_ack_ok=True,
                flatten_filled=True,
                error_code="internal_error",
            )

        try:
            after = baseline.check(exchange=exchange, symbol=symbol)
            assert_flat(after)
        except BaselineError:
            sender.lease_supervisor.mark_process_sends_blocked()
            return W5Report(
                status="flatten_incomplete",
                exchange=exchange,
                symbol=symbol,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                sends_blocked=True,
                orders_sent=orders_sent,
                buy_ack_ok=True,
                buy_filled=True,
                flatten_ack_ok=True,
                flatten_filled=True,
                flat_after=False,
                error_code="unknown",
            )

        return W5Report(
            status="ok",
            exchange=exchange,
            symbol=symbol,
            subscription_ready=True,
            reseed_matched=True,
            sends_blocked=runtime.sends_blocked,
            trade_ws_bound=True,
            orders_sent=orders_sent,
            buy_ack_ok=True,
            buy_filled=True,
            flatten_ack_ok=True,
            flatten_filled=True,
            flat_after=True,
            reconnect_generation=runtime.reconnect_generation,
        )
    finally:
        for sock in (runtime.private_socket, runtime.trade_socket):
            if sock is not None:
                try:
                    sock.close()
                except Exception:  # noqa: BLE001
                    pass


def parse_w5_cli_args(argv: Sequence[str]) -> tuple[str, bool]:
    venue = ""
    approve = False
    for arg in argv:
        if arg.startswith("--venue="):
            venue = arg.split("=", 1)[1].strip().lower()
        elif arg == "--w5-approve-one-shot":
            approve = True
    return venue, approve


@dataclass
class W5RuntimeBindings:
    credentials: LiveCredentials
    metadata_provider: MetadataProvider
    position_mode_provider: PositionModeProvider
    baseline: FlatBaselinePort
    private_socket: PrivateWsSocket
    trade_socket: PrivateWsSocket
    rest_order_recon: Optional[Any] = None


def _public_http_get_json(url: str, headers: Mapping[str, str]):
    from app.bot.private.ws_w4_postonly import _public_http_get_json as _w4_get

    return _w4_get(url, headers)


def open_w5_production_bindings(
    *,
    venue: str,
    env: Mapping[str, str],
    credentials: Optional[LiveCredentials] = None,
) -> W5RuntimeBindings:
    """Signed flat baseline before any private/trade sockets. No L1 (market)."""
    from app.bot.private.order_preflight import (
        LiveHttpMetadataProvider,
        LiveSignedPositionModeProvider,
    )
    from app.bot.private.ws_private import trade_ws_url_for_exchange
    from app.bot.private.ws_socket import WebsocketsSocketFactory, bind_socket_factory
    from app.bot.private.ws_w4_baseline import (
        SignedRestFlatBaseline,
        build_signed_rest_order_recon,
    )

    profile = resolve_w5_profile(venue)
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
    rest_order_recon = build_signed_rest_order_recon(
        bybit_credentials=_creds_from_live(secrets, "bybit"),
        okx_credentials=_creds_from_live(secrets, "okx"),
        endpoints=ep,
        require_position_flat=True,
    )
    assert_flat(baseline.check(exchange=exchange, symbol=symbol))
    assert_w5_okx_net_mode(
        exchange=exchange,
        venue=profile["venue"],
        position_mode_provider=pos,
    )

    factory = WebsocketsSocketFactory()
    bind_socket_factory(factory)
    if exchange == "bybit":
        private_url = ep.bybit_private_ws
    else:
        private_url = ep.okx_private_ws
    trade_url = trade_ws_url_for_exchange(exchange, ep)
    private_sock = factory.open(private_url)
    trade_sock = factory.open(trade_url)
    return W5RuntimeBindings(
        credentials=credentials,
        metadata_provider=meta,
        position_mode_provider=pos,
        baseline=baseline,
        private_socket=private_sock,
        trade_socket=trade_sock,
        rest_order_recon=rest_order_recon,
    )


def main_ws_w5_market(
    argv: Optional[Sequence[str]] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    bindings: Optional[W5RuntimeBindings] = None,
) -> int:
    """CLI entry for ``--ws-w5-market --venue=bybit|okx``."""
    argv = list(argv or [])
    e = dict(env if env is not None else os.environ)
    venue, approve_one_shot = parse_w5_cli_args(argv)

    def _print(report: W5Report) -> None:
        print(json.dumps(report.as_public_dict(), ensure_ascii=False, indent=2, sort_keys=True))

    if venue not in W5_PROFILES:
        _print(
            W5Report(
                status="rejected_before_socket",
                exchange=venue or "",
                symbol="",
                error_code="invalid_request",
            )
        )
        return 1

    try:
        assert_ws_w5_send_gates(e)
        resolve_w5_profile(venue)
    except (WsProfileGateError, W5ProfileError):
        _print(
            W5Report(
                status="rejected_before_socket",
                exchange=venue,
                symbol="",
                error_code="invalid_request",
            )
        )
        return 1

    if not approve_one_shot:
        _print(
            W5Report(
                status="approval_required",
                exchange=venue,
                symbol=W5_PROFILES[venue]["symbol"],
                error_code="invalid_request",
            )
        )
        return 1

    assert_default_entrypoint_cannot_transport()

    owned = bindings is None
    active: Optional[W5RuntimeBindings] = bindings
    try:
        if active is None:
            try:
                assert_no_default_ws_socket()
            except RuntimeError:
                _print(
                    W5Report(
                        status="rejected_before_socket",
                        exchange=venue,
                        symbol="",
                        error_code="transport_error",
                    )
                )
                return 2
            try:
                active = open_w5_production_bindings(venue=venue, env=e)
            except (
                BaselineError,
                W5ProfileError,
                PreflightError,
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                KeyError,
            ):
                unbind_socket_factory()
                _print(
                    W5Report(
                        status="bind_failed",
                        exchange=venue,
                        symbol=W5_PROFILES[venue]["symbol"],
                        error_code="transport_error",
                    )
                )
                return 2

        report = run_w5_market(
            venue=venue,
            env=e,
            metadata_provider=active.metadata_provider,
            position_mode_provider=active.position_mode_provider,
            baseline=active.baseline,
            private_socket=active.private_socket,
            trade_socket=active.trade_socket,
            credentials=active.credentials,
            load_secrets=False,
            issue_approval=True,
            rest_order_recon=active.rest_order_recon,
        )
        _print(report)
        if report.status == "ok":
            return 0
        if report.status in {
            "secrets_unavailable",
            "approval_required",
            "rejected_before_socket",
        }:
            return 1
        return 2
    finally:
        if owned:
            unbind_socket_factory()
            assert_default_entrypoint_cannot_transport()
