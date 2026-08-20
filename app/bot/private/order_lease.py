"""Post-only lease state machine + TTL supervisor/recovery (no HTTP by default).

Lease/recovery state is reconstructed only from canonical journal events.
No lease sidecar JSONL. GTC alone does NOT bound a post-only order.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from app.bot.private.journal_v1 import (
    JournalValidationError,
    PrivateJournalWriter,
    find_nonterminal_request_ops,
    scan_all_journal_events,
)
from app.bot.private.order_plan import OrderPlan
from app.bot.private.order_sign import (
    LiveCredentials,
    SignedRequest,
    WsTradeDispatch,
    build_signed_cancel_request,
    is_ws_trade_journal,
)


class LeaseState(str, Enum):
    PREPARED = "prepared"
    WORKING = "working"
    ACKED = "acked"
    TTL_EXPIRED_CANCEL_REQUIRED = "ttl_expired_cancel_required"
    CANCEL_REQUESTED = "cancel_requested"
    INCONCLUSIVE = "inconclusive"
    TERMINAL = "terminal"


class CancelTransportError(RuntimeError):
    pass


class LeaseSupervisorError(RuntimeError):
    pass


@dataclass
class PostOnlyLease:
    """Bounded post-only lease. GTC alone does NOT enforce TTL."""

    plan: OrderPlan
    state: LeaseState = LeaseState.PREPARED
    lease_started_mono_ns: Optional[int] = None
    acked: bool = False
    ttl_sec: int = 0

    def mark_working(self, *, now_mono_ns: Optional[int] = None) -> None:
        self.lease_started_mono_ns = (
            now_mono_ns if now_mono_ns is not None else time.monotonic_ns()
        )
        self.state = LeaseState.WORKING

    def mark_acked(self, *, now_mono_ns: Optional[int] = None) -> None:
        if self.lease_started_mono_ns is None:
            self.lease_started_mono_ns = (
                now_mono_ns if now_mono_ns is not None else time.monotonic_ns()
            )
        self.acked = True
        self.state = LeaseState.ACKED
        self.check_ttl(now_mono_ns=now_mono_ns)

    def check_ttl(self, *, now_mono_ns: Optional[int] = None) -> LeaseState:
        if self.plan.mode != "post_only_limit" and self.ttl_sec <= 0:
            return self.state
        if self.state in {
            LeaseState.TERMINAL,
            LeaseState.CANCEL_REQUESTED,
            LeaseState.INCONCLUSIVE,
        }:
            return self.state
        now = now_mono_ns if now_mono_ns is not None else time.monotonic_ns()
        started = self.lease_started_mono_ns
        if started is None:
            return self.state
        ttl = int(self.ttl_sec or self.plan.ttl_sec)
        ttl_ns = ttl * 1_000_000_000
        if now - started >= ttl_ns:
            self.state = LeaseState.TTL_EXPIRED_CANCEL_REQUIRED
        return self.state

    def mark_cancel_requested(self) -> None:
        self.state = LeaseState.CANCEL_REQUESTED

    def mark_inconclusive(self) -> None:
        self.state = LeaseState.INCONCLUSIVE

    def mark_terminal(self) -> None:
        self.state = LeaseState.TERMINAL

    @property
    def blocks_new_sends(self) -> bool:
        return self.state in {
            LeaseState.TTL_EXPIRED_CANCEL_REQUIRED,
            LeaseState.INCONCLUSIVE,
            LeaseState.CANCEL_REQUESTED,
        }

    def public_dict(self) -> dict[str, object]:
        return {
            "order_attempt_id": self.plan.order_attempt_id,
            "state": self.state.value,
            "acked": self.acked,
            "ttl_requires_cancel": True,
            "gtc_auto_bounded": False,
        }


class OrderStateSnapshot(str, Enum):
    """Observed venue order state. Terminal requires an explicit subtype."""

    WORKING = "working"
    UNKNOWN = "unknown"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    # Observed terminal without a known subtype — must block, never invent cancelled.
    TERMINAL_UNKNOWN = "terminal_unknown"


_TERMINAL_SUBTYPES = frozenset(
    {
        OrderStateSnapshot.FILLED,
        OrderStateSnapshot.CANCELLED,
        OrderStateSnapshot.EXPIRED,
    }
)


def observed_terminal_state(snap: OrderStateSnapshot) -> Optional[str]:
    """Return contract terminal_state only for known provider subtypes."""
    if snap in _TERMINAL_SUBTYPES:
        return snap.value
    return None


def is_known_terminal(snap: OrderStateSnapshot) -> bool:
    return snap in _TERMINAL_SUBTYPES


class OrderStateProvider(Protocol):
    def get(self, plan: OrderPlan) -> OrderStateSnapshot:
        ...


CancelTransportFn = Callable[[SignedRequest], "CancelAck"]


@dataclass(frozen=True)
class CancelAck:
    ok: bool
    cancel_state: str  # accepted | cancelled
    error_code: Optional[str] = None
    ambiguous: bool = False


@dataclass
class ReconstructedLeg:
    operation_id: str
    venue: str
    environment: str
    dual_leg_id: Optional[str]
    leg_id: Optional[str]
    request_fingerprint: Optional[str]
    post_only: bool
    ttl_bucket: Optional[str]
    acked: bool
    terminal: bool
    cancel_requested: bool
    dispatch_ambiguous: bool
    ttl_recovery_inconclusive: bool
    # Market / W5 fields (journal order_prepared + request_sent / terminal).
    reduce_only: bool = False
    order_kind: str = "limit"
    request_sent: bool = False
    terminal_state: Optional[str] = None
    side: str = "buy"


def _latest_post_dispatch_ambiguity(
    ops: Sequence[Mapping[str, Any]],
) -> Optional[Mapping[str, Any]]:
    """Latest post_dispatch_ambiguity recon for an operation (append order)."""
    latest: Optional[Mapping[str, Any]] = None
    for e in ops:
        if (
            e.get("event_type") == "reconciliation"
            and e.get("reconciliation_scope") == "post_dispatch_ambiguity"
        ):
            latest = e
    return latest


def reconstruct_legs_from_events(
    events: Sequence[Mapping[str, Any]],
) -> dict[str, ReconstructedLeg]:
    by_op: dict[str, list[Mapping[str, Any]]] = {}
    for ev in events:
        by_op.setdefault(str(ev["operation_id"]), []).append(ev)
    out: dict[str, ReconstructedLeg] = {}
    for op_id, ops in by_op.items():
        prepared = next((e for e in ops if e.get("event_type") == "order_prepared"), None)
        if prepared is None:
            continue
        types = [str(e["event_type"]) for e in ops]
        pda = _latest_post_dispatch_ambiguity(ops)
        pda_state = str(pda.get("reconciliation_state") or "") if pda is not None else ""
        # Blocking only while the *latest* post_dispatch_ambiguity is inconclusive.
        dispatch_ambiguous = pda_state == "inconclusive"
        pda_resolved = pda_state in {"matched", "mismatch"}
        term_ev = next(
            (e for e in reversed(ops) if e.get("event_type") == "terminal_update"),
            None,
        )
        term_state = (
            str(term_ev.get("terminal_state"))
            if term_ev is not None and term_ev.get("terminal_state")
            else None
        )
        order_kind = str(prepared.get("order_kind") or "limit")
        ack_failed = any(
            e.get("event_type") == "ack_received" and e.get("outcome") == "failure"
            for e in ops
        )
        out[op_id] = ReconstructedLeg(
            operation_id=op_id,
            venue=str(prepared.get("venue") or "bybit"),
            environment=str(prepared.get("environment") or "live"),
            dual_leg_id=str(prepared["dual_leg_id"]) if prepared.get("dual_leg_id") else None,
            leg_id=str(prepared["leg_id"]) if prepared.get("leg_id") else None,
            request_fingerprint=str(prepared["request_fingerprint"])
            if prepared.get("request_fingerprint")
            else None,
            post_only=bool(prepared.get("post_only")),
            ttl_bucket=str(prepared["ttl_bucket"]) if prepared.get("ttl_bucket") else None,
            acked="ack_received" in types,
            terminal=("terminal_update" in types) or pda_resolved or ack_failed,
            cancel_requested="cancel_requested" in types,
            dispatch_ambiguous=dispatch_ambiguous,
            ttl_recovery_inconclusive=any(
                e.get("event_type") == "reconciliation"
                and e.get("reconciliation_scope") == "post_only_ttl_recovery"
                and e.get("reconciliation_state") == "inconclusive"
                for e in ops
            ),
            reduce_only=bool(prepared.get("reduce_only")),
            order_kind=order_kind,
            request_sent="request_sent" in types,
            terminal_state=term_state,
            side=str(prepared.get("side") or "buy"),
        )
    return out


@dataclass
class LeaseSupervisor:
    """Recovery interface. State comes only from canonical journal events."""

    journal: PrivateJournalWriter
    data_root: Path
    order_state_provider: Optional[OrderStateProvider] = None
    cancel_transport: Optional[CancelTransportFn] = None
    _leases: dict[str, PostOnlyLease] = field(default_factory=dict)
    _dispatch_blocking: bool = False

    def register(self, lease: PostOnlyLease) -> None:
        self._leases[lease.plan.order_attempt_id] = lease
        # No sidecar persistence — journal events are the only durable record.

    def get(self, order_attempt_id: str) -> Optional[PostOnlyLease]:
        return self._leases.get(order_attempt_id)

    def mark_process_sends_blocked(self) -> None:
        """Process-global fail-closed block after post-transport journal loss."""
        self._dispatch_blocking = True

    def has_blocking_lease(self, *, now_mono_ns: Optional[int] = None) -> bool:
        if self._dispatch_blocking:
            return True
        for lease in self._leases.values():
            lease.check_ttl(now_mono_ns=now_mono_ns)
            if lease.blocks_new_sends:
                return True
        return False

    def assert_can_send(self, *, now_mono_ns: Optional[int] = None) -> None:
        if self.has_blocking_lease(now_mono_ns=now_mono_ns):
            raise LeaseSupervisorError(
                "overdue or inconclusive post-only/dispatch state blocks new sends"
            )

    def reconstruct_from_journal(self, *, append_missing_recon: bool = True) -> None:
        """Restart-safe reconstruction from canonical events only."""
        events = scan_all_journal_events(self.data_root)
        nonterm = find_nonterminal_request_ops(events)
        self._dispatch_blocking = bool(nonterm)
        if append_missing_recon:
            for item in nonterm:
                if not item["needs_recon_append"]:
                    continue
                sent = item["request_sent"]
                try:
                    self.journal.append(
                        {
                            "event_type": "reconciliation",
                            "operation_id": item["operation_id"],
                            "venue": sent.get("venue") or "bybit",
                            "environment": sent.get("environment") or "live",
                            "outcome": "observed",
                            "dual_leg_id": sent.get("dual_leg_id"),
                            "leg_id": sent.get("leg_id"),
                            "reconciliation_scope": "post_dispatch_ambiguity",
                            "reconciliation_state": "inconclusive",
                            "mismatch_fields": ["state", "timing"],
                        }
                    )
                except (JournalValidationError, OSError, RuntimeError, ValueError) as exc:
                    raise LeaseSupervisorError(
                        f"journal failure during dispatch recovery: {exc}"
                    ) from exc

        legs = reconstruct_legs_from_events(scan_all_journal_events(self.data_root))
        for op_id, leg in legs.items():
            if leg.terminal:
                continue
            if not leg.post_only and leg.order_kind == "market":
                # W5 market: acked or dispatched without terminal → blocking lease.
                if not leg.request_sent and not leg.dispatch_ambiguous:
                    continue
                stub = self._market_stub_plan(leg, op_id=op_id)
                lease = PostOnlyLease(plan=stub, ttl_sec=0)
                if leg.acked:
                    lease.acked = True
                    lease.state = LeaseState.ACKED
                    lease.lease_started_mono_ns = time.monotonic_ns()
                elif leg.request_sent:
                    lease.mark_working()
                # Market ACKED does not TTL-expire; force inconclusive so sends block.
                lease.mark_inconclusive()
                self._leases[op_id] = lease
                self._dispatch_blocking = True
                continue
            if not leg.post_only:
                if leg.dispatch_ambiguous:
                    self._dispatch_blocking = True
                continue
            # Minimal plan stub for recovery — identity fields only.
            from app.bot.private.order_plan import OrderPlan as OP

            stub = OP(
                intent_id=f"intent_recon_{op_id}",
                leg_id=leg.leg_id or f"leg_{op_id}",
                order_attempt_id=op_id,
                venue="bybit_live" if leg.venue == "bybit" else "okx_live",
                symbol="BTCUSDT" if leg.venue == "bybit" else "BTC-USDT-SWAP",
                symbol_alias="BTCUSDT" if leg.venue == "bybit" else "BTC-USDT-SWAP",
                instrument_class="linear_perpetual",
                side="buy",
                mode="post_only_limit",
                qty="0.001",
                price="1",
                max_notional_usd="100",
                time_in_force="post_only",
                ttl_sec=_ttl_from_bucket(leg.ttl_bucket),
                expires_at_utc="2099-01-01T00:00:00.000Z",
                expires_at_monotonic_ns=2**62,
                k_live=1,
                post_only=True,
                reduce_only=False,
                request_fingerprint=leg.request_fingerprint
                or ("fp_" + ("0" * 32)),
                dual_leg_id=leg.dual_leg_id or f"solo_{op_id}",
                quantity_bucket="min_lot",
                notional_bucket="under_100_usd",
            )
            lease = PostOnlyLease(plan=stub, ttl_sec=stub.ttl_sec)
            if leg.acked:
                lease.acked = True
                lease.state = LeaseState.ACKED
                # Treat as overdue until proven terminal — restart cannot trust GTC.
                lease.lease_started_mono_ns = 0
                lease.check_ttl(now_mono_ns=time.monotonic_ns())
            if leg.cancel_requested:
                lease.mark_cancel_requested()
            if leg.ttl_recovery_inconclusive or leg.dispatch_ambiguous:
                lease.mark_inconclusive()
            self._leases[op_id] = lease
            if lease.blocks_new_sends:
                self._dispatch_blocking = True

        # Buy market filled without flatten terminal → open exposure blocks all venues
        # until REST flat or same-venue reduce-only flatten.
        self._register_open_market_exposures(legs)

    def _market_stub_plan(self, leg: ReconstructedLeg, *, op_id: str) -> Any:
        from app.bot.private.order_plan import OrderPlan as OP

        venue_live = "bybit_live" if leg.venue == "bybit" else "okx_live"
        symbol = "BTCUSDT" if leg.venue == "bybit" else "BTC-USDT-SWAP"
        qty = "0.001" if leg.venue == "bybit" else "0.01"
        side = leg.side if leg.side in {"buy", "sell"} else "buy"
        return OP(
            intent_id=f"intent_recon_{op_id}",
            leg_id=leg.leg_id or f"leg_{op_id}",
            order_attempt_id=op_id,
            venue=venue_live,
            symbol=symbol,
            symbol_alias=symbol,
            instrument_class="linear_perpetual",
            side=side,
            mode="market",
            qty=qty,
            price=None,
            max_notional_usd="100",
            time_in_force="ioc",
            ttl_sec=0,
            expires_at_utc="2099-01-01T00:00:00.000Z",
            expires_at_monotonic_ns=2**62,
            k_live=1,
            post_only=False,
            reduce_only=bool(leg.reduce_only),
            request_fingerprint=leg.request_fingerprint or ("fp_" + ("0" * 32)),
            dual_leg_id=leg.dual_leg_id or f"solo_{op_id}",
            quantity_bucket="min_lot",
            notional_bucket="under_100_usd",
        )

    def _register_open_market_exposures(
        self, legs: Mapping[str, ReconstructedLeg]
    ) -> None:
        """Filled market buy without flatten fill → blocking exposure lease."""
        by_dual: dict[str, list[ReconstructedLeg]] = {}
        for leg in legs.values():
            if leg.post_only or leg.order_kind != "market":
                continue
            dual = leg.dual_leg_id or leg.operation_id
            by_dual.setdefault(dual, []).append(leg)

        for dual, group in by_dual.items():
            buy_filled = any(
                (not leg.reduce_only)
                and leg.terminal
                and leg.terminal_state == "filled"
                for leg in group
            )
            flatten_filled = any(
                leg.reduce_only
                and leg.terminal
                and leg.terminal_state == "filled"
                for leg in group
            )
            if not buy_filled or flatten_filled:
                continue
            # Prefer an existing nonterminal flatten lease if present.
            if any(
                leg.reduce_only and not leg.terminal and leg.request_sent
                for leg in group
            ):
                continue
            buy = next(
                leg
                for leg in group
                if (not leg.reduce_only)
                and leg.terminal
                and leg.terminal_state == "filled"
            )
            exposure_id = f"exposure_flatten_{dual}"
            if exposure_id in self._leases:
                continue
            exposure_leg = ReconstructedLeg(
                operation_id=exposure_id,
                venue=buy.venue,
                environment=buy.environment,
                dual_leg_id=dual,
                leg_id=f"leg_exposure_{dual}",
                request_fingerprint=buy.request_fingerprint,
                post_only=False,
                ttl_bucket=None,
                acked=False,
                terminal=False,
                cancel_requested=False,
                dispatch_ambiguous=False,
                ttl_recovery_inconclusive=False,
                reduce_only=True,
                order_kind="market",
                request_sent=False,
                terminal_state=None,
                side="sell",
            )
            stub = self._market_stub_plan(exposure_leg, op_id=exposure_id)
            lease = PostOnlyLease(plan=stub, ttl_sec=0)
            lease.mark_working()
            lease.mark_inconclusive()
            self._leases[exposure_id] = lease
            self._dispatch_blocking = True

    def recover_overdue(
        self,
        plan: OrderPlan,
        credentials: LiveCredentials,
        *,
        now_mono_ns: Optional[int] = None,
        journal_transport: Optional[str] = None,
    ) -> PostOnlyLease:
        """Query order state then cancel via injected providers when TTL expires."""
        lease = self._leases.get(plan.order_attempt_id)
        if lease is None:
            lease = PostOnlyLease(
                plan=plan, state=LeaseState.TTL_EXPIRED_CANCEL_REQUIRED, ttl_sec=plan.ttl_sec
            )
            lease.acked = True
            self._leases[plan.order_attempt_id] = lease
        lease.check_ttl(now_mono_ns=now_mono_ns)
        if lease.state not in {
            LeaseState.TTL_EXPIRED_CANCEL_REQUIRED,
            LeaseState.INCONCLUSIVE,
            LeaseState.ACKED,
        }:
            return lease
        if not lease.acked:
            raise LeaseSupervisorError("cancel recovery requires acknowledged lease")

        if self.order_state_provider is None:
            lease.mark_inconclusive()
            self._journal_recovery(plan, state="inconclusive")
            return lease

        snap = self.order_state_provider.get(plan)
        if is_known_terminal(snap):
            lease.mark_terminal()
            self._journal_recovery(plan, state="matched")
            return lease
        if snap == OrderStateSnapshot.TERMINAL_UNKNOWN:
            lease.mark_inconclusive()
            self._journal_recovery(plan, state="inconclusive")
            return lease
        if snap == OrderStateSnapshot.UNKNOWN:
            lease.mark_inconclusive()
            self._journal_recovery(plan, state="inconclusive")
            return lease

        try:
            cancel_body: dict = {
                "event_type": "cancel_requested",
                "operation_id": plan.order_attempt_id,
                "venue": "okx" if plan.venue.startswith("okx") else "bybit",
                "environment": "live",
                "outcome": "pending",
                "dual_leg_id": plan.dual_leg_id,
                "leg_id": plan.leg_id,
                "request_fingerprint": plan.request_fingerprint,
                "cancel_reason": "post_only_ttl_expired",
                "send_monotonic_ns": time.monotonic_ns(),
            }
            if journal_transport is not None:
                cancel_body["transport"] = journal_transport
            self.journal.append(cancel_body)
        except (JournalValidationError, OSError, RuntimeError, ValueError) as exc:
            raise LeaseSupervisorError(f"journal failure during cancel: {exc}") from exc

        lease.mark_cancel_requested()
        if is_ws_trade_journal(journal_transport):
            cancel_payload: object = WsTradeDispatch(plan=plan, op="cancel")
        else:
            cancel_payload = build_signed_cancel_request(plan, credentials)
        _ = cancel_payload.public_view()  # type: ignore[union-attr]
        if self.cancel_transport is None:
            lease.mark_inconclusive()
            self._journal_recovery(plan, state="inconclusive")
            return lease

        try:
            ack = self.cancel_transport(cancel_payload)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            lease.mark_inconclusive()
            self._journal_recovery(plan, state="inconclusive")
            return lease

        if ack.ambiguous:
            lease.mark_inconclusive()
            self._journal_recovery(plan, state="inconclusive")
            return lease

        if ack.ok:
            # Transport acceptance is NOT terminal — journal cancel_ack(accepted) only.
            try:
                recv = time.monotonic_ns()
                ack_body: dict = {
                    "event_type": "cancel_ack",
                    "operation_id": plan.order_attempt_id,
                    "venue": "okx" if plan.venue.startswith("okx") else "bybit",
                    "environment": "live",
                    "outcome": "success",
                    "dual_leg_id": plan.dual_leg_id,
                    "leg_id": plan.leg_id,
                    "cancel_state": "accepted",
                    "request_fingerprint": plan.request_fingerprint,
                    "receive_monotonic_ns": recv,
                    "event_monotonic_ns": max(recv + 1, time.monotonic_ns()),
                }
                if journal_transport is not None:
                    ack_body["transport"] = journal_transport
                self.journal.append(ack_body)
            except (JournalValidationError, OSError, RuntimeError, ValueError) as exc:
                lease.mark_inconclusive()
                self.mark_process_sends_blocked()
                raise LeaseSupervisorError(
                    f"recovery journal failure after cancel_ack: {exc}"
                ) from exc

            # Terminal only from observed provider subtype (filled/cancelled/expired).
            snap_after = self.order_state_provider.get(plan)
            term = observed_terminal_state(snap_after)
            if term is not None:
                try:
                    recv2 = time.monotonic_ns()
                    self.journal.append(
                        {
                            "event_type": "terminal_update",
                            "operation_id": plan.order_attempt_id,
                            "venue": "okx" if plan.venue.startswith("okx") else "bybit",
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
                except (JournalValidationError, OSError, RuntimeError, ValueError) as exc:
                    lease.mark_inconclusive()
                    self.mark_process_sends_blocked()
                    raise LeaseSupervisorError(
                        f"recovery journal failure after terminal observe: {exc}"
                    ) from exc
                lease.mark_terminal()
                self._journal_matched_recovery(plan)
                return lease

            # Unknown subtype or still working — never invent cancelled; block sends.
            lease.mark_inconclusive()
            self._journal_recovery(plan, state="inconclusive")
            return lease

        lease.mark_inconclusive()
        self._journal_recovery(plan, state="mismatch")
        return lease

    def _journal_matched_recovery(self, plan: OrderPlan) -> None:
        """Matched post-only TTL recovery: recon + latency_summary (fail-closed)."""
        try:
            self.journal.append_post_only_ttl_matched_followup(
                venue="okx" if plan.venue.startswith("okx") else "bybit",
                environment="live",
                operation_id=plan.order_attempt_id,
                dual_leg_id=plan.dual_leg_id,
                leg_id=plan.leg_id,
            )
        except (JournalValidationError, OSError, RuntimeError, ValueError) as exc:
            raise LeaseSupervisorError(
                f"journal failure during matched recovery: {exc}"
            ) from exc

    def _journal_recovery(self, plan: OrderPlan, *, state: str) -> None:
        if state == "matched":
            self._journal_matched_recovery(plan)
            return
        body: dict = {
            "event_type": "reconciliation",
            "operation_id": plan.order_attempt_id,
            "venue": "okx" if plan.venue.startswith("okx") else "bybit",
            "environment": "live",
            "outcome": "failure" if state == "mismatch" else "observed",
            "dual_leg_id": plan.dual_leg_id,
            "leg_id": plan.leg_id,
            "reconciliation_scope": "post_only_ttl_recovery",
            "reconciliation_state": state,
        }
        if state == "mismatch":
            body["error_code"] = "reconciliation_mismatch"
            body["mismatch_fields"] = ["state", "timing"]
        elif state == "inconclusive":
            body["mismatch_fields"] = ["state", "timing"]
        try:
            self.journal.append(body)
        except (JournalValidationError, OSError, RuntimeError, ValueError) as exc:
            raise LeaseSupervisorError(f"journal failure during recovery: {exc}") from exc


def _ttl_from_bucket(bucket: Optional[str]) -> int:
    """Representative seconds for reconstructed leases (bucket → approx TTL)."""
    if bucket == "short":
        return 10
    if bucket == "medium":
        return 30
    if bucket == "long":
        return 60
    return 10


class CancelRequestBuilder:
    """REST signed cancel construction only — never for W4 ws_trade path."""

    def build(self, plan: OrderPlan, credentials: LiveCredentials) -> SignedRequest:
        return build_signed_cancel_request(plan, credentials)


@dataclass
class UnboundCancelTransport:
    def __call__(self, _req: SignedRequest) -> CancelAck:
        raise CancelTransportError("cancel transport unbound")
