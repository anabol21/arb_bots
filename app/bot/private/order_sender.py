"""Approval-bound order sender with fail-closed v1 journal (R3 hardened).

Default CLI has no transport. Tests inject fakes only. No real I/O without
an explicitly passed transport.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from app.bot.private.journal_v1 import (
    JournalValidationError,
    PrivateJournalWriter,
)
from app.bot.private.order_approval import (
    ApprovalError,
    ApprovalToken,
    ApprovalVault,
    assert_plan_unmutated,
)
from app.bot.private.order_lease import (
    LeaseState,
    LeaseSupervisor,
    LeaseSupervisorError,
    PostOnlyLease,
)
from app.bot.private.order_metadata import MetadataProvider
from app.bot.private.order_plan import (
    OrderPlan,
    OrderPlanError,
    revalidate_order_plan,
    ttl_bucket_for_sec,
)
from app.bot.private.order_preflight import (
    PositionModeProvider,
    PreflightError,
    assert_preflight_ready,
)
from app.bot.private.order_sign import (
    LiveCredentials,
    SignedRequest,
    WsTradeDispatch,
    build_signed_cancel_request,
    build_signed_place_request,
    is_ws_trade_journal,
)
from app.bot.private.order_symbols import ORDER_VENUES
from app.bot.private.secrets import resolve_private_profile
from app.bot.private.venue import live_orders_enabled, resolve_venue, send_allowed


class SendGateError(RuntimeError):
    """Authorization / profile / flag gate failure before signing."""


class TransportNotBoundError(RuntimeError):
    """Raised when no transport is explicitly bound (default / CLI path)."""


@dataclass(frozen=True)
class TransportAck:
    kind: str  # accepted | rejected | ambiguous
    ack_state: str  # accepted | received
    error_code: Optional[str] = None
    ambiguous: bool = False
    # Digits-only venue code for public report; never journaled as schema field.
    venue_code: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.kind == "accepted"


TransportFn = Callable[[Any], TransportAck]


def _refuse_http_order_for_ws_trade(transport: Any, journal_transport: Optional[str]) -> bool:
    """True when ws_trade journal is paired with a REST HTTP order transport."""
    if not is_ws_trade_journal(journal_transport):
        return False
    from app.bot.private.order_transport import is_live_http_order_transport

    return is_live_http_order_transport(transport)


class _NeverTransport:
    def __call__(self, _req: Any) -> TransportAck:
        raise TransportNotBoundError(
            "live order transport is not bound; default entrypoints cannot send"
        )


_RUNTIME_TRANSPORT: Optional[TransportFn] = None


def bind_runtime_transport(fn: TransportFn) -> None:
    global _RUNTIME_TRANSPORT
    _RUNTIME_TRANSPORT = fn


def unbind_runtime_transport() -> None:
    global _RUNTIME_TRANSPORT
    _RUNTIME_TRANSPORT = None


def get_runtime_transport() -> Optional[TransportFn]:
    return _RUNTIME_TRANSPORT


def assert_default_entrypoint_cannot_transport() -> None:
    if _RUNTIME_TRANSPORT is not None:
        raise RuntimeError("runtime transport unexpectedly bound in default context")


def assert_no_default_live_transport() -> None:
    assert_default_entrypoint_cannot_transport()


def orders_runtime_armed(env: Mapping[str, str]) -> bool:
    """True only when flags allow send AND a transport is explicitly bound."""
    return bool(send_allowed(env) and get_runtime_transport() is not None)


def orders_code_present() -> bool:
    return True


def assert_send_gates(
    env: Mapping[str, str],
    plan: OrderPlan,
    *,
    require_live_profile: bool = True,
) -> None:
    venue = resolve_venue(env)
    if venue != "live":
        raise SendGateError("VENUE must be live for order send")
    if not live_orders_enabled(env):
        raise SendGateError("LIVE_ORDERS=1 required for order send")
    if not send_allowed(env):
        raise SendGateError("send_allowed is false")
    if plan.venue not in ORDER_VENUES:
        raise SendGateError("plan venue not allowlisted")
    if plan.k_live != 1:
        raise SendGateError("K_live must be 1")
    if require_live_profile:
        profile = resolve_private_profile(env)
        if profile.name != "live":
            raise SendGateError("credential profile must be live")
        if not profile.live_orders_flag:
            raise SendGateError("live profile LIVE_ORDERS flag required")


@dataclass
class SendResult:
    status: str
    # gate_failed | journal_failed | prepared_not_dispatched | ack | rejected |
    # expired | cancel_required | ambiguous
    plan_summary: dict[str, Any]
    journal_ok: bool
    transport_invoked: bool
    ack_state: Optional[str] = None
    error_code: Optional[str] = None
    lease_state: Optional[str] = None
    venue_code: Optional[str] = None
    # Set only after request_sent is durable and immediately before transport().
    dispatch_monotonic_ns: Optional[int] = None


class ApprovalBoundSender:
    """Preflight → revalidate → consume → journal → transport → terminal journal."""

    def __init__(
        self,
        *,
        journal: PrivateJournalWriter,
        approval_vault: ApprovalVault,
        metadata_provider: MetadataProvider,
        position_mode_provider: PositionModeProvider,
        transport: Optional[TransportFn] = None,
        lease_supervisor: Optional[LeaseSupervisor] = None,
        data_root: Optional[Path] = None,
    ) -> None:
        self._journal = journal
        self._vault = approval_vault
        self._meta = metadata_provider
        self._position = position_mode_provider
        self._transport_explicit = transport is not None
        self._transport: Optional[TransportFn] = transport
        self._data_root = data_root or journal.data_root
        self._lease_supervisor = lease_supervisor or LeaseSupervisor(
            journal=journal, data_root=self._data_root
        )
        self._lease: Optional[PostOnlyLease] = None
        # Restart-safe: reconstruct nonterminal dispatches / post-only leases.
        try:
            self._lease_supervisor.reconstruct_from_journal(append_missing_recon=True)
        except LeaseSupervisorError:
            # Leave blocking state set if reconstruction partially applied.
            self._lease_supervisor._dispatch_blocking = True  # noqa: SLF001

    @property
    def lease(self) -> Optional[PostOnlyLease]:
        return self._lease

    @property
    def lease_supervisor(self) -> LeaseSupervisor:
        return self._lease_supervisor

    def journal_pre_send_gate(
        self,
        plan: OrderPlan,
        *,
        gate_kind: str,
    ) -> dict[str, Any]:
        """Durable rest/price/preflight block before consume/prepare/dispatch."""
        return self._journal.append_pre_send_gate(
            venue=_jvenue(plan),
            environment="live",
            gate_kind=gate_kind,
            operation_id=plan.order_attempt_id,
        )

    def send_approved(
        self,
        plan: OrderPlan,
        token: ApprovalToken,
        credentials: LiveCredentials,
        env: Mapping[str, str],
        *,
        canonical_plan: Optional[OrderPlan] = None,
        now: Optional[datetime] = None,
        now_mono_ns: Optional[int] = None,
        now_mono_ns_post_fsync: Optional[int] = None,
        dispatch_transport: bool = True,
        journal_transport: Optional[str] = None,
        reconnect_generation: Optional[int] = None,
        dispatch_barrier: Optional[Any] = None,
    ) -> SendResult:
        summary = plan.public_summary()
        mono = now_mono_ns if now_mono_ns is not None else time.monotonic_ns()

        # Mandatory preflight + revalidation BEFORE approval consumption / journal.
        try:
            assert_send_gates(env, plan)
            self._lease_supervisor.assert_can_send(now_mono_ns=mono)
            assert_preflight_ready(
                metadata_provider=self._meta,
                position_mode_provider=self._position,
                venue=plan.venue,
                symbol=plan.symbol,
                now_mono_ns=mono,
            )
            revalidate_order_plan(plan, self._meta)
            ref = canonical_plan if canonical_plan is not None else plan
            assert_plan_unmutated(ref, plan)
            if plan.is_expired(now_utc=now, now_mono_ns=mono):
                raise ApprovalError("approval token expired")
        except (
            SendGateError,
            ApprovalError,
            OrderPlanError,
            PreflightError,
            LeaseSupervisorError,
        ) as exc:
            gate_kind = _pre_send_gate_kind(exc)
            journal_ok = False
            if gate_kind is not None:
                try:
                    self.journal_pre_send_gate(plan, gate_kind=gate_kind)
                    journal_ok = True
                except (JournalValidationError, OSError, RuntimeError, ValueError):
                    journal_ok = False
            return SendResult(
                status="gate_failed" if "expired" not in str(exc).lower() else "expired",
                plan_summary=summary,
                journal_ok=journal_ok,
                transport_invoked=False,
                error_code=_gate_error_code(exc),
            )

        # Known unbound transport is a pre-send reject — no request_sent.
        if dispatch_transport and self._transport is None:
            return SendResult(
                status="rejected",
                plan_summary=summary,
                journal_ok=False,
                transport_invoked=False,
                error_code="transport_error",
            )

        # W4: ws_trade journal must never bind REST HTTP order APIs.
        if dispatch_transport and _refuse_http_order_for_ws_trade(
            self._transport, journal_transport
        ):
            return SendResult(
                status="rejected",
                plan_summary=summary,
                journal_ok=False,
                transport_invoked=False,
                error_code="transport_error",
            )

        try:
            self._vault.consume(plan, token, now=now, now_mono_ns=mono)
        except ApprovalError as exc:
            return SendResult(
                status="gate_failed" if "expired" not in str(exc).lower() else "expired",
                plan_summary=summary,
                journal_ok=False,
                transport_invoked=False,
                error_code=_gate_error_code(exc),
            )
        except (JournalValidationError, OSError, RuntimeError, ValueError):
            return SendResult(
                status="journal_failed",
                plan_summary=summary,
                journal_ok=False,
                transport_invoked=False,
                error_code="internal_error",
            )

        try:
            self._journal_prepared(plan)
        except (JournalValidationError, OSError, RuntimeError, ValueError):
            return SendResult(
                status="journal_failed",
                plan_summary=summary,
                journal_ok=False,
                transport_invoked=False,
                error_code="internal_error",
            )

        # REST signed place only when not ws_trade (W4 never constructs order HTTP paths).
        dispatch_payload: Any
        if is_ws_trade_journal(journal_transport):
            dispatch_payload = WsTradeDispatch(plan=plan, op="place")
        else:
            try:
                dispatch_payload = build_signed_place_request(plan, credentials)
            except OrderPlanError:
                self._journal_reject(plan, stage="prepare", error_code="invalid_request")
                return SendResult(
                    status="rejected",
                    plan_summary=summary,
                    journal_ok=True,
                    transport_invoked=False,
                    error_code="invalid_request",
                )

        mono2 = (
            now_mono_ns_post_fsync
            if now_mono_ns_post_fsync is not None
            else (now_mono_ns if now_mono_ns is not None else time.monotonic_ns())
        )
        if plan.is_expired(now_utc=now, now_mono_ns=mono2):
            self._journal_reject(plan, stage="prepare", error_code="timeout")
            return SendResult(
                status="expired",
                plan_summary=summary,
                journal_ok=True,
                transport_invoked=False,
                error_code="timeout",
            )

        if not dispatch_transport:
            self._lease = PostOnlyLease(plan=plan, state=LeaseState.PREPARED)
            return SendResult(
                status="prepared_not_dispatched",
                plan_summary=summary,
                journal_ok=True,
                transport_invoked=False,
                lease_state=LeaseState.PREPARED.value,
            )

        assert self._transport is not None
        send_ns = time.monotonic_ns()
        try:
            self._journal_request_sent(
                plan,
                send_monotonic_ns=send_ns,
                transport=journal_transport,
                reconnect_generation=reconnect_generation,
            )
        except (JournalValidationError, OSError, RuntimeError, ValueError):
            return SendResult(
                status="journal_failed",
                plan_summary=summary,
                journal_ok=False,
                transport_invoked=False,
                error_code="internal_error",
            )

        self._lease = PostOnlyLease(plan=plan)
        self._lease.mark_working(now_mono_ns=mono2)

        dispatch_ns = time.monotonic_ns()
        if dispatch_barrier is not None:
            try:
                dispatch_barrier.wait(timeout=30.0)
            except threading.BrokenBarrierError:
                try:
                    self._journal_reject(
                        plan, stage="send", error_code="dual_leg_aborted"
                    )
                    self._lease.mark_terminal()
                except (JournalValidationError, OSError, RuntimeError, ValueError):
                    return SendResult(
                        status="journal_failed",
                        plan_summary=summary,
                        journal_ok=False,
                        transport_invoked=False,
                        error_code="internal_error",
                        lease_state=self._lease.state.value,
                    )
                return SendResult(
                    status="rejected",
                    plan_summary=summary,
                    journal_ok=True,
                    transport_invoked=False,
                    error_code="dual_leg_aborted",
                    lease_state=self._lease.state.value,
                )
            dispatch_ns = time.monotonic_ns()

        try:
            ack = self._transport(dispatch_payload)
        except TransportNotBoundError:
            # Should not happen after pre-check; treat as pre-dispatch if somehow.
            self._journal_reject(plan, stage="send", error_code="transport_error")
            return SendResult(
                status="rejected",
                plan_summary=summary,
                journal_ok=True,
                transport_invoked=False,
                error_code="transport_error",
                lease_state=self._lease.state.value,
            )
        except TimeoutError:
            return self._finish_post_dispatch_ambiguous(
                plan, summary, error_code="timeout", transport_invoked=True
            )
        except Exception:  # noqa: BLE001
            # Unknown exception after request_sent → ambiguous, not reject.
            return self._finish_post_dispatch_ambiguous(
                plan, summary, error_code="unknown", transport_invoked=True
            )

        _ = dispatch_payload.public_view()
        recv_ns = time.monotonic_ns()

        if ack.ambiguous or ack.kind == "ambiguous":
            return self._finish_post_dispatch_ambiguous(
                plan,
                summary,
                error_code=ack.error_code or "timeout",
                transport_invoked=True,
            )

        if ack.ok:
            try:
                self._journal_ack(
                    plan,
                    ack_state=ack.ack_state,
                    receive_monotonic_ns=recv_ns,
                    transport=journal_transport,
                    reconnect_generation=reconnect_generation,
                )
            except (JournalValidationError, OSError, RuntimeError, ValueError):
                return self._fail_post_transport_journal(
                    plan, summary, ack_state=ack.ack_state
                )
            self._lease.mark_acked(now_mono_ns=recv_ns)
            if plan.mode == "post_only_limit":
                self._lease_supervisor.register(self._lease)
                st = self._lease.check_ttl(now_mono_ns=recv_ns)
                if st == LeaseState.TTL_EXPIRED_CANCEL_REQUIRED:
                    # Evidence that cancel is required — GTC does not auto-bound.
                    try:
                        self._journal_ttl_cancel_required_hint(plan)
                    except (JournalValidationError, OSError, RuntimeError, ValueError):
                        return self._fail_post_transport_journal(
                            plan, summary, ack_state=ack.ack_state
                        )
            return SendResult(
                status="ack",
                plan_summary=summary,
                journal_ok=True,
                transport_invoked=True,
                ack_state=ack.ack_state,
                lease_state=self._lease.state.value,
                dispatch_monotonic_ns=dispatch_ns,
            )

        try:
            self._journal_ack(
                plan,
                ack_state=ack.ack_state or "received",
                receive_monotonic_ns=recv_ns,
                ok=False,
                error_code=ack.error_code or "venue_rejected",
                transport=journal_transport,
                reconnect_generation=reconnect_generation,
            )
        except (JournalValidationError, OSError, RuntimeError, ValueError):
            return self._fail_post_transport_journal(
                plan,
                summary,
                ack_state=ack.ack_state,
            )
        self._lease.mark_terminal()
        return SendResult(
            status="rejected",
            plan_summary=summary,
            journal_ok=True,
            transport_invoked=True,
            ack_state=ack.ack_state,
            error_code=ack.error_code or "venue_rejected",
            lease_state=self._lease.state.value,
            venue_code=getattr(ack, "venue_code", None),
            dispatch_monotonic_ns=dispatch_ns,
        )

    def request_cancel_interface(
        self,
        plan: OrderPlan,
        credentials: LiveCredentials,
        *,
        cancel_transport: Optional[Callable[[SignedRequest], Any]] = None,
        order_state_provider: Optional[Any] = None,
        journal_transport: Optional[str] = None,
        reconnect_generation: Optional[int] = None,
    ) -> SendResult:
        """Cancel only for acknowledged leases; journal failure ≠ success.

        Cancel transport acceptance journals ``cancel_ack(accepted)`` only.
        ``terminal_update`` requires a state-provider observation of cancelled.
        """
        summary = plan.public_summary()
        lease = self._lease or self._lease_supervisor.get(plan.order_attempt_id)
        if lease is None or not lease.acked:
            return SendResult(
                status="gate_failed",
                plan_summary=summary,
                journal_ok=False,
                transport_invoked=False,
                error_code="invalid_request",
                lease_state=lease.state.value if lease else None,
            )
        lease.check_ttl()
        self._lease = lease

        try:
            cancel_body: dict[str, Any] = {
                "event_type": "cancel_requested",
                "operation_id": plan.order_attempt_id,
                "venue": _jvenue(plan),
                "environment": "live",
                "outcome": "pending",
                "dual_leg_id": plan.dual_leg_id,
                "leg_id": plan.leg_id,
                "request_fingerprint": plan.request_fingerprint,
                "cancel_reason": "post_only_ttl_expired"
                if lease.state == LeaseState.TTL_EXPIRED_CANCEL_REQUIRED
                else "timeout_guard",
                "send_monotonic_ns": time.monotonic_ns(),
            }
            if journal_transport is not None:
                cancel_body["transport"] = journal_transport
            if reconnect_generation is not None:
                cancel_body["reconnect_generation"] = int(reconnect_generation)
            self._journal.append(cancel_body)
        except (JournalValidationError, OSError, RuntimeError, ValueError):
            return SendResult(
                status="journal_failed",
                plan_summary=summary,
                journal_ok=False,
                transport_invoked=False,
                error_code="internal_error",
                lease_state=lease.state.value,
            )

        lease.mark_cancel_requested()
        # REST signed cancel only outside ws_trade — W4 cancel is trade WS only.
        if is_ws_trade_journal(journal_transport):
            if cancel_transport is not None and _refuse_http_order_for_ws_trade(
                cancel_transport, journal_transport
            ):
                try:
                    self._journal_reject(plan, stage="cancel", error_code="transport_error")
                except (JournalValidationError, OSError, RuntimeError, ValueError):
                    return SendResult(
                        status="journal_failed",
                        plan_summary=summary,
                        journal_ok=False,
                        transport_invoked=False,
                        error_code="internal_error",
                        lease_state=lease.state.value,
                    )
                return SendResult(
                    status="rejected",
                    plan_summary=summary,
                    journal_ok=True,
                    transport_invoked=False,
                    error_code="transport_error",
                    lease_state=lease.state.value,
                )
            cancel_payload: Any = WsTradeDispatch(plan=plan, op="cancel")
        else:
            cancel_payload = build_signed_cancel_request(plan, credentials)
        _ = cancel_payload.public_view()

        if cancel_transport is None:
            # Never dispatched — definite pre-send reject for cancel path.
            try:
                self._journal_reject(plan, stage="cancel", error_code="transport_error")
            except (JournalValidationError, OSError, RuntimeError, ValueError):
                return SendResult(
                    status="journal_failed",
                    plan_summary=summary,
                    journal_ok=False,
                    transport_invoked=False,
                    error_code="internal_error",
                    lease_state=lease.state.value,
                )
            return SendResult(
                status="rejected",
                plan_summary=summary,
                journal_ok=True,
                transport_invoked=False,
                error_code="transport_error",
                lease_state=lease.state.value,
            )

        try:
            ack = cancel_transport(cancel_payload)
        except Exception:  # noqa: BLE001
            # Possible dispatch — inconclusive, not definite reject.
            return self._finish_cancel_ambiguous(
                plan, summary, lease, error_code="unknown", transport_invoked=True
            )

        if bool(getattr(ack, "ambiguous", False)):
            return self._finish_cancel_ambiguous(
                plan,
                summary,
                lease,
                error_code=getattr(ack, "error_code", None) or "timeout",
                transport_invoked=True,
            )

        ok = bool(getattr(ack, "ok", False))
        if ok:
            try:
                recv = time.monotonic_ns()
                ack_body: dict[str, Any] = {
                    "event_type": "cancel_ack",
                    "operation_id": plan.order_attempt_id,
                    "venue": _jvenue(plan),
                    "environment": "live",
                    "outcome": "success",
                    "dual_leg_id": plan.dual_leg_id,
                    "leg_id": plan.leg_id,
                    # Acceptance of cancel request — not observed terminal.
                    "cancel_state": "accepted",
                    "request_fingerprint": plan.request_fingerprint,
                    "receive_monotonic_ns": recv,
                    "event_monotonic_ns": max(recv + 1, time.monotonic_ns()),
                }
                if journal_transport is not None:
                    ack_body["transport"] = journal_transport
                if reconnect_generation is not None:
                    ack_body["reconnect_generation"] = int(reconnect_generation)
                self._journal.append(ack_body)
            except (JournalValidationError, OSError, RuntimeError, ValueError):
                return self._fail_post_transport_journal(
                    plan, summary, lease=lease, ack_state="accepted"
                )

            provider = order_state_provider or self._lease_supervisor.order_state_provider
            if provider is None:
                lease.mark_inconclusive()
                self._lease_supervisor.register(lease)
                try:
                    self._journal_ttl_cancel_required_hint(plan)
                except (JournalValidationError, OSError, RuntimeError, ValueError):
                    return self._fail_post_transport_journal(
                        plan, summary, lease=lease, ack_state="accepted"
                    )
                return SendResult(
                    status="cancel_acked",
                    plan_summary=summary,
                    journal_ok=True,
                    transport_invoked=True,
                    ack_state="accepted",
                    error_code=None,
                    lease_state=lease.state.value,
                )

            from app.bot.private.order_lease import (
                OrderStateSnapshot,
                observed_terminal_state,
            )

            snap = provider.get(plan)
            term = observed_terminal_state(snap)
            if term is not None:
                try:
                    recv2 = time.monotonic_ns()
                    self._journal.append(
                        {
                            "event_type": "terminal_update",
                            "operation_id": plan.order_attempt_id,
                            "venue": _jvenue(plan),
                            "environment": "live",
                            "outcome": "observed",
                            "dual_leg_id": plan.dual_leg_id,
                            "leg_id": plan.leg_id,
                            "terminal_state": term,
                            "request_fingerprint": plan.request_fingerprint,
                            "receive_monotonic_ns": recv2,
                            "event_monotonic_ns": max(recv2 + 1, time.monotonic_ns()),
                        }
                    )
                except (JournalValidationError, OSError, RuntimeError, ValueError):
                    return self._fail_post_transport_journal(
                        plan, summary, lease=lease, ack_state="accepted"
                    )
                lease.mark_terminal()
                try:
                    self._journal.append_post_only_ttl_matched_followup(
                        venue=_jvenue(plan),
                        environment="live",
                        operation_id=plan.order_attempt_id,
                        dual_leg_id=plan.dual_leg_id,
                        leg_id=plan.leg_id,
                    )
                except (JournalValidationError, OSError, RuntimeError, ValueError):
                    return self._fail_post_transport_journal(
                        plan, summary, lease=lease, ack_state="accepted"
                    )
                return SendResult(
                    status="ack",
                    plan_summary=summary,
                    journal_ok=True,
                    transport_invoked=True,
                    ack_state="accepted",
                    lease_state=lease.state.value,
                )

            # Unknown terminal subtype or non-terminal — never invent cancelled.
            lease.mark_inconclusive()
            self._lease_supervisor.register(lease)
            try:
                self._journal_ttl_cancel_required_hint(plan)
            except (JournalValidationError, OSError, RuntimeError, ValueError):
                return self._fail_post_transport_journal(
                    plan, summary, lease=lease, ack_state="accepted"
                )
            return SendResult(
                status="cancel_acked",
                plan_summary=summary,
                journal_ok=True,
                transport_invoked=True,
                ack_state="accepted",
                lease_state=lease.state.value,
            )

        try:
            self._journal_reject(
                plan,
                stage="cancel",
                error_code=getattr(ack, "error_code", None) or "cancel_rejected",
            )
        except (JournalValidationError, OSError, RuntimeError, ValueError):
            return self._fail_post_transport_journal(plan, summary, lease=lease)
        return SendResult(
            status="rejected",
            plan_summary=summary,
            journal_ok=True,
            transport_invoked=True,
            error_code=getattr(ack, "error_code", None) or "cancel_rejected",
            lease_state=lease.state.value,
        )

    def _fail_post_transport_journal(
        self,
        plan: OrderPlan,
        summary: dict[str, Any],
        *,
        lease: Optional[PostOnlyLease] = None,
        ack_state: Optional[str] = None,
    ) -> SendResult:
        """Fail-closed after transport: block all subsequent sends in-process."""
        active = lease if lease is not None else self._lease
        if active is not None:
            active.mark_inconclusive()
            self._lease_supervisor.register(active)
            self._lease = active
        self._lease_supervisor.mark_process_sends_blocked()
        return SendResult(
            status="post_transport_journal_failed",
            plan_summary=summary,
            journal_ok=False,
            transport_invoked=True,
            ack_state=ack_state,
            error_code="internal_error",
            lease_state=active.state.value if active else None,
        )

    def _finish_post_dispatch_ambiguous(
        self,
        plan: OrderPlan,
        summary: dict[str, Any],
        *,
        error_code: str,
        transport_invoked: bool,
    ) -> SendResult:
        if self._lease is not None:
            self._lease.mark_inconclusive()
            self._lease_supervisor.register(self._lease)
        try:
            self._journal_ambiguous(plan)
        except (JournalValidationError, OSError, RuntimeError, ValueError):
            return SendResult(
                status="recovery_journal_failed",
                plan_summary=summary,
                journal_ok=False,
                transport_invoked=transport_invoked,
                error_code="internal_error",
                lease_state=self._lease.state.value if self._lease else None,
            )
        return SendResult(
            status="ambiguous",
            plan_summary=summary,
            journal_ok=True,
            transport_invoked=transport_invoked,
            error_code=error_code,
            lease_state=self._lease.state.value if self._lease else None,
        )

    def _finish_cancel_ambiguous(
        self,
        plan: OrderPlan,
        summary: dict[str, Any],
        lease: PostOnlyLease,
        *,
        error_code: str,
        transport_invoked: bool,
    ) -> SendResult:
        lease.mark_inconclusive()
        self._lease_supervisor.register(lease)
        try:
            self._journal_ambiguous(plan)
        except (JournalValidationError, OSError, RuntimeError, ValueError):
            return SendResult(
                status="recovery_journal_failed",
                plan_summary=summary,
                journal_ok=False,
                transport_invoked=transport_invoked,
                error_code="internal_error",
                lease_state=lease.state.value,
            )
        return SendResult(
            status="ambiguous",
            plan_summary=summary,
            journal_ok=True,
            transport_invoked=transport_invoked,
            error_code=error_code,
            lease_state=lease.state.value,
        )

    def _journal_prepared(self, plan: OrderPlan) -> None:
        order_kind = "limit" if plan.mode == "post_only_limit" else "market"
        body: dict[str, Any] = {
            "event_type": "order_prepared",
            "operation_id": plan.order_attempt_id,
            "venue": _jvenue(plan),
            "environment": "live",
            "outcome": "pending",
            "dual_leg_id": plan.dual_leg_id,
            "leg_id": plan.leg_id,
            "instrument_class": plan.instrument_class,
            "symbol_alias": plan.symbol_alias,
            "side": plan.side,
            "order_kind": order_kind,
            "quantity_bucket": plan.quantity_bucket,
            "notional_bucket": plan.notional_bucket,
            "reduce_only": plan.reduce_only,
            "post_only": plan.post_only,
            "request_fingerprint": plan.request_fingerprint,
        }
        if plan.post_only:
            body["ttl_bucket"] = ttl_bucket_for_sec(plan.ttl_sec)
        self._journal.append(body)

    def _journal_request_sent(
        self,
        plan: OrderPlan,
        *,
        send_monotonic_ns: int,
        transport: Optional[str] = None,
        reconnect_generation: Optional[int] = None,
    ) -> None:
        event_mono = max(send_monotonic_ns + 1, time.monotonic_ns())
        body: dict[str, Any] = {
            "event_type": "request_sent",
            "operation_id": plan.order_attempt_id,
            "venue": _jvenue(plan),
            "environment": "live",
            "outcome": "pending",
            "event_monotonic_ns": event_mono,
            "dual_leg_id": plan.dual_leg_id,
            "leg_id": plan.leg_id,
            "request_kind": "place",
            "request_fingerprint": plan.request_fingerprint,
            "transport_attempt": 1,
            "send_monotonic_ns": send_monotonic_ns,
        }
        if transport is not None:
            body["transport"] = transport
        if reconnect_generation is not None:
            body["reconnect_generation"] = int(reconnect_generation)
        self._journal.append(body)

    def _journal_ack(
        self,
        plan: OrderPlan,
        *,
        ack_state: str,
        receive_monotonic_ns: int,
        ok: bool = True,
        error_code: Optional[str] = None,
        transport: Optional[str] = None,
        reconnect_generation: Optional[int] = None,
    ) -> None:
        event_mono = max(receive_monotonic_ns + 1, time.monotonic_ns())
        body: dict[str, Any] = {
            "event_type": "ack_received",
            "operation_id": plan.order_attempt_id,
            "venue": _jvenue(plan),
            "environment": "live",
            "outcome": "success" if ok else "failure",
            "event_monotonic_ns": event_mono,
            "dual_leg_id": plan.dual_leg_id,
            "leg_id": plan.leg_id,
            "request_kind": "place",
            "request_fingerprint": plan.request_fingerprint,
            "ack_state": ack_state if ack_state in {"accepted", "received"} else "received",
            "receive_monotonic_ns": receive_monotonic_ns,
        }
        if transport is not None:
            body["transport"] = transport
        if reconnect_generation is not None:
            body["reconnect_generation"] = int(reconnect_generation)
        if not ok:
            body["error_code"] = error_code or "venue_rejected"
        self._journal.append(body)

    def _journal_ambiguous(self, plan: OrderPlan) -> None:
        """Fail-closed: callers must treat append errors as recovery_journal_failed."""
        self._journal.append(
            {
                "event_type": "reconciliation",
                "operation_id": plan.order_attempt_id,
                "venue": _jvenue(plan),
                "environment": "live",
                "outcome": "observed",
                "dual_leg_id": plan.dual_leg_id,
                "leg_id": plan.leg_id,
                "reconciliation_scope": "post_dispatch_ambiguity",
                "reconciliation_state": "inconclusive",
                "mismatch_fields": ["state", "timing"],
            }
        )

    def _journal_ttl_cancel_required_hint(self, plan: OrderPlan) -> None:
        """Fail-closed post-only TTL recovery evidence (inconclusive until terminal)."""
        self._journal.append(
            {
                "event_type": "reconciliation",
                "operation_id": plan.order_attempt_id,
                "venue": _jvenue(plan),
                "environment": "live",
                "outcome": "observed",
                "dual_leg_id": plan.dual_leg_id,
                "leg_id": plan.leg_id,
                "reconciliation_scope": "post_only_ttl_recovery",
                "reconciliation_state": "inconclusive",
                "mismatch_fields": ["timing", "state"],
            }
        )

    def _journal_reject(self, plan: OrderPlan, *, stage: str, error_code: str) -> None:
        self._journal.append(
            {
                "event_type": "reject",
                "operation_id": plan.order_attempt_id,
                "venue": _jvenue(plan),
                "environment": "live",
                "outcome": "failure",
                "dual_leg_id": plan.dual_leg_id,
                "leg_id": plan.leg_id,
                "request_kind": "place",
                "request_fingerprint": plan.request_fingerprint,
                "reject_stage": stage
                if stage in {"auth", "prepare", "send", "ack", "cancel"}
                else "send",
                "error_code": error_code,
            }
        )


def _jvenue(plan: OrderPlan) -> str:
    return "bybit" if plan.venue.startswith("bybit") else "okx"


def _pre_send_gate_kind(exc: BaseException) -> Optional[str]:
    """Map pre-dispatch failures to canonical gate_kind, or None if not a gate."""
    if isinstance(exc, PreflightError):
        msg = str(exc).lower()
        if "rest" in msg or "post_only" in msg or "would take" in msg:
            return "rest"
        return "price"
    if isinstance(exc, OrderPlanError):
        msg = str(exc).lower()
        if "rest" in msg or "post_only" in msg:
            return "rest"
        if "price" in msg or "tick" in msg or "mark" in msg or "notional" in msg:
            return "price"
        # Revalidation / metadata / qty failures are preflight-class price gates.
        return "price"
    return None


def _gate_error_code(exc: BaseException) -> str:
    msg = str(exc).lower()
    if "expired" in msg or "stale" in msg:
        return "timeout"
    if "consumed" in msg or "already" in msg:
        return "invalid_request"
    if "forged" in msg or "revalidation" in msg or "fingerprint" in msg:
        return "invalid_request"
    if "mutation" in msg or "mismatch" in msg or "binding" in msg:
        return "invalid_request"
    if "preflight" in msg or "position mode" in msg or "metadata" in msg or "mark" in msg:
        return "invalid_request"
    if "lease" in msg or "overdue" in msg:
        return "invalid_request"
    if (
        "venue" in msg
        or "live_orders" in msg
        or "profile" in msg
        or "send_allowed" in msg
    ):
        return "auth_failed"
    return "invalid_request"
