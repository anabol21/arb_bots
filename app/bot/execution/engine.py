"""Execution-v2 engine and cached risk gate.

Stdlib plus frozen EV2 contracts, FSM, transport, adapters and WAL
admission. Import and construction perform no sockets, disk drain, fsync,
Sentry, logging, REST or live send. ``submit`` awaits only parallel
transport socket writes by default. The opt-in durable-prewrite mode fsyncs
and replays the accepted intent before any transport dispatch.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Sequence

from app.bot.execution.adapters import AdapterBatch, PrivateEventAdapter
from app.bot.execution.contracts import (
    SCHEMA_VERSION as CONTRACT_SCHEMA_VERSION,
    ContractValidationError,
    ExecutionEvent,
    ExecutionEventType,
    IntentAction,
    LegPlan,
    SpreadState,
    SpreadStatus,
    TradeIntent,
    Venue,
    canonical_decimal,
    decimal_to_canonical,
    derive_client_id,
)
from app.bot.execution.ownership import FileOwnershipFence, OwnershipError
from app.bot.execution.recovery import (
    RECOVERY_WORST_CASE_EVENTS,
    RecoveryActionKind,
    RecoveryLegFactory,
    RecoveryPlan,
    RecoveryResult,
    RecoveryStatus,
    RestartLiveSnapshot,
    RestartResult,
    SnapshotPort,
    VenueActionKind,
    VenueActionResult,
    WalDrainPort,
    exposure_qty,
    plan_recovery,
    restart_correlation_id,
)
from app.bot.execution.state_machine import (
    InvalidTransition,
    apply_event,
    initial_spread_state,
    is_proven_flat,
    opens_allowed,
)
from app.bot.execution.transport import (
    DispatchResult,
    DispatchStatus,
    ExecutionTransport,
    InstrumentCache,
    PrewriteAuditResult,
    TransportError,
    VenueWriteEvidence,
    WriteOutcome,
    prepare_dual_leg,
    prepare_venue_action,
)
from app.bot.execution.wal import (
    SUBMIT_WORST_CASE_EVENTS,
    ExecutionWal,
    ReplayResult,
    WalError,
)

SCHEMA_VERSION = "bbot.execution.engine.v1"
MAX_NOTIONAL_USDT = Decimal("20")

ENGINE_REASON_CODES = frozenset(
    {
        "ttl_expired",
        "coin_not_allowed",
        "notional_invalid",
        "notional_exceeds_cap",
        "opens_not_allowed",
        "wal_blocks_opens",
        "wal_capacity",
        "wal_unhealthy",
        "kill_switch",
        "pause",
        "trade_socket_not_ready",
        "private_stream_not_ready",
        "readiness_changed",
        "stale_metadata",
        "ownership_not_held",
        "invalid_plan_set",
        "invalid_intent",
        "close_not_open",
        "close_mismatch",
        "transport_rejected",
        "write_failed",
        "cancelled",
        "ambiguous_write",
        "adapter_invalid",
        "adapter_capacity",
        "adapter_transition",
        "adapter_empty",
        "no_order_only",
        "clock_regression",
        "rejected_before_write",
    }
)

_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "api_key",
        "api_secret",
        "secret",
        "passphrase",
        "password",
        "authorization",
        "cookie",
        "set_cookie",
        "signature",
        "sign",
        "access_token",
        "refresh_token",
        "bearer_token",
        "private_key",
        "client_secret",
        "raw_payload",
        "raw_frame",
        "frame",
        "request_body",
        "response_body",
        "headers",
        "canonical_request",
        "account_id",
        "uid",
        "member_id",
        "wallet_address",
        "exchange_order_id",
        "order_id",
        "client_order_id",
        "clordid",
        "ordid",
        "balance",
        "available_balance",
        "equity",
        "margin",
        "account_value",
        "fill_price",
    }
)

_COIN_RE_OK = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")

PlanResolver = Callable[[TradeIntent], Sequence[LegPlan]]
MonotonicNs = Callable[[], int]


def _norm_key(name: object) -> str:
    return str(name).strip().lower().replace("-", "_")


def _assert_public(node: object) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if _norm_key(key) in _FORBIDDEN_PUBLIC_KEYS:
                raise EngineError("forbidden_field")
            _assert_public(value)
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            _assert_public(item)


def _require_reason(code: Optional[str]) -> Optional[str]:
    if code is None:
        return None
    if code not in ENGINE_REASON_CODES:
        return "invalid_intent"
    return code


class EngineError(ValueError):
    """Fail-closed engine construction error. Public view is redacted."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = _require_reason(reason_code) or "invalid_intent"
        super().__init__(self.reason_code)

    def to_public_dict(self) -> dict[str, Any]:
        out = {"schema_version": SCHEMA_VERSION, "reason_code": self.reason_code}
        _assert_public(out)
        return out

    def __repr__(self) -> str:
        return f"EngineError(reason_code={self.reason_code!r})"


class SubmitStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True)
class RiskPolicy:
    allowed_coins: frozenset[str]
    max_notional_usdt: Decimal
    lot_tolerance: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        coins = self.allowed_coins
        if not isinstance(coins, frozenset):
            coins = frozenset(str(item) for item in coins)
            object.__setattr__(self, "allowed_coins", coins)
        frozen: set[str] = set()
        for coin in coins:
            if not isinstance(coin, str) or not (2 <= len(coin) <= 16 or coin == "H"):
                raise EngineError("coin_not_allowed")
            if any(ch not in _COIN_RE_OK for ch in coin):
                raise EngineError("coin_not_allowed")
            frozen.add(coin)
        object.__setattr__(self, "allowed_coins", frozenset(frozen))
        cap = canonical_decimal(
            self.max_notional_usdt, field="max_notional_usdt", allow_zero=False
        )
        if cap > MAX_NOTIONAL_USDT:
            raise EngineError("notional_exceeds_cap")
        object.__setattr__(self, "max_notional_usdt", cap)
        object.__setattr__(
            self,
            "lot_tolerance",
            canonical_decimal(self.lot_tolerance, field="lot_tolerance"),
        )


@dataclass(frozen=True)
class ReadinessSnapshot:
    bybit_trade_ready: bool
    okx_trade_ready: bool
    bybit_private_ready: bool
    okx_private_ready: bool
    bybit_generation: int
    okx_generation: int
    kill_switch: bool
    pause: bool

    def __post_init__(self) -> None:
        for name in (
            "bybit_trade_ready",
            "okx_trade_ready",
            "bybit_private_ready",
            "okx_private_ready",
            "kill_switch",
            "pause",
        ):
            if not isinstance(getattr(self, name), bool):
                raise EngineError("invalid_intent")
        for name in ("bybit_generation", "okx_generation"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise EngineError("private_stream_not_ready")

    def generation_for(self, venue: Venue) -> int:
        if venue is Venue.BYBIT:
            return self.bybit_generation
        return self.okx_generation

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "bybit_trade_ready": self.bybit_trade_ready,
            "okx_trade_ready": self.okx_trade_ready,
            "bybit_private_ready": self.bybit_private_ready,
            "okx_private_ready": self.okx_private_ready,
            "bybit_generation": self.bybit_generation,
            "okx_generation": self.okx_generation,
            "kill_switch": self.kill_switch,
            "pause": self.pause,
        }
        _assert_public(out)
        return out

    def connectivity_reason(self) -> Optional[str]:
        if not self.bybit_trade_ready or not self.okx_trade_ready:
            return "trade_socket_not_ready"
        if not self.bybit_private_ready or not self.okx_private_ready:
            return "private_stream_not_ready"
        return None


@dataclass(frozen=True)
class ReadinessLease:
    """Immutable permission for one normal dual-venue dispatch."""

    revision: int
    connectivity_revision: int
    snapshot: ReadinessSnapshot
    require_controls: bool


@dataclass(frozen=True)
class RecoveryReadinessLease:
    """Permission for one single-venue cancel or reduce-only recovery write."""

    connectivity_revision: int
    snapshot: ReadinessSnapshot
    venue: Venue


@dataclass(frozen=True)
class NoOrderReadinessLease:
    """Private-only audit lease; never valid for a trade dispatch."""

    revision: int
    snapshot: ReadinessSnapshot


class DualReadinessFence:
    """Same-loop readiness publication with generation/revision invalidation.

    Publication is synchronous so a disconnect callback can invalidate an
    outstanding lease even while ``ExecutionEngine.submit`` is awaiting socket
    writes under its lifecycle lock. Identical health refreshes do not churn the
    revision.
    """

    def __init__(self, snapshot: ReadinessSnapshot) -> None:
        if not isinstance(snapshot, ReadinessSnapshot):
            raise EngineError("invalid_intent")
        self._snapshot = snapshot
        self._revision = 0
        self._connectivity_revision = 0

    @property
    def snapshot(self) -> ReadinessSnapshot:
        return self._snapshot

    @property
    def revision(self) -> int:
        return self._revision

    def publish(self, snapshot: ReadinessSnapshot) -> bool:
        if not isinstance(snapshot, ReadinessSnapshot):
            raise EngineError("invalid_intent")
        if snapshot == self._snapshot:
            return False
        prior_connectivity = self._connectivity_key(self._snapshot)
        next_connectivity = self._connectivity_key(snapshot)
        self._snapshot = snapshot
        self._revision += 1
        if next_connectivity != prior_connectivity:
            self._connectivity_revision += 1
        return True

    @staticmethod
    def _connectivity_key(
        snapshot: ReadinessSnapshot,
    ) -> tuple[bool, bool, bool, bool, int, int]:
        return (
            snapshot.bybit_trade_ready,
            snapshot.okx_trade_ready,
            snapshot.bybit_private_ready,
            snapshot.okx_private_ready,
            snapshot.bybit_generation,
            snapshot.okx_generation,
        )

    def acquire(self, *, require_controls: bool) -> tuple[Optional[ReadinessLease], Optional[str]]:
        snapshot = self._snapshot
        if require_controls:
            if snapshot.pause:
                return None, "pause"
            if snapshot.kill_switch:
                return None, "kill_switch"
        reason = snapshot.connectivity_reason()
        if reason is not None:
            return None, reason
        return ReadinessLease(
            self._revision,
            self._connectivity_revision,
            snapshot,
            require_controls,
        ), None

    def validate(self, lease: ReadinessLease) -> bool:
        if not isinstance(lease, ReadinessLease):
            return False
        if lease.connectivity_revision != self._connectivity_revision:
            return False
        if self._snapshot.connectivity_reason() is not None:
            return False
        if lease.require_controls:
            return (
                lease.revision == self._revision
                and not self._snapshot.pause
                and not self._snapshot.kill_switch
            )
        return True

    def acquire_no_order(self) -> tuple[Optional[NoOrderReadinessLease], Optional[str]]:
        snapshot = self._snapshot
        if snapshot.bybit_trade_ready or snapshot.okx_trade_ready:
            return None, "no_order_only"
        if snapshot.pause:
            return None, "pause"
        if snapshot.kill_switch:
            return None, "kill_switch"
        if not snapshot.bybit_private_ready or not snapshot.okx_private_ready:
            return None, "private_stream_not_ready"
        return NoOrderReadinessLease(self._revision, snapshot), None

    def validate_no_order(self, lease: NoOrderReadinessLease) -> bool:
        if not isinstance(lease, NoOrderReadinessLease):
            return False
        current, reason = self.acquire_no_order()
        return current is not None and reason is None and current.revision == lease.revision

    def acquire_recovery(
        self, venue: Venue
    ) -> tuple[Optional[RecoveryReadinessLease], Optional[str]]:
        """Admit one target-venue recovery write with both private views fresh.

        Recovery deliberately ignores pause/kill-switch and does not require the
        peer trade socket: cancel/flatten is single-venue risk reduction. Both
        private streams remain mandatory so exposure evidence cannot be stale.
        """
        if not isinstance(venue, Venue):
            return None, "trade_socket_not_ready"
        snapshot = self._snapshot
        trade_ready = (
            snapshot.bybit_trade_ready
            if venue is Venue.BYBIT
            else snapshot.okx_trade_ready
        )
        if not trade_ready:
            return None, "trade_socket_not_ready"
        if not snapshot.bybit_private_ready or not snapshot.okx_private_ready:
            return None, "stream_blocked"
        return RecoveryReadinessLease(
            connectivity_revision=self._connectivity_revision,
            snapshot=snapshot,
            venue=venue,
        ), None

    def validate_recovery(self, lease: RecoveryReadinessLease) -> bool:
        if not isinstance(lease, RecoveryReadinessLease):
            return False
        if lease.connectivity_revision != self._connectivity_revision:
            return False
        current, reason = self.acquire_recovery(lease.venue)
        return current is not None and reason is None


@dataclass(frozen=True)
class SubmitResult:
    schema_version: str
    status: SubmitStatus
    intent_id: str
    run_id: str
    reason_code: Optional[str]
    recovery_required: bool
    dispatch: Optional[DispatchResult] = None

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise EngineError("invalid_intent")
        object.__setattr__(
            self,
            "status",
            self.status if isinstance(self.status, SubmitStatus) else SubmitStatus(self.status),
        )
        object.__setattr__(self, "reason_code", _require_reason(self.reason_code))
        if not isinstance(self.run_id, str) or not self.run_id:
            raise EngineError("invalid_intent")
        if not isinstance(self.recovery_required, bool):
            raise EngineError("invalid_intent")
        if self.status is SubmitStatus.RECOVERY_REQUIRED and not self.recovery_required:
            raise EngineError("invalid_intent")
        if self.dispatch is not None:
            if not isinstance(self.dispatch, DispatchResult):
                raise EngineError("invalid_intent")
            if self.dispatch.intent_id != self.intent_id or self.dispatch.run_id != self.run_id:
                raise EngineError("invalid_intent")

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "intent_id": self.intent_id,
            "run_id": self.run_id,
            "reason_code": self.reason_code,
            "recovery_required": self.recovery_required,
            "dispatch": None if self.dispatch is None else self.dispatch.to_public_dict(),
        }
        _assert_public(out)
        return out

    def __repr__(self) -> str:
        return (
            "SubmitResult("
            f"status={self.status.value!r}, intent_id={self.intent_id!r}, "
            f"run_id={self.run_id!r}, reason_code={self.reason_code!r})"
        )


@dataclass(frozen=True)
class NoOrderAuditResult:
    """One audit attempt; WAL admission is not a durable fsync claim."""

    intent_id: str
    run_id: str
    prewrite: Optional[PrewriteAuditResult]
    wal_accepted: bool
    reason_code: Optional[str]

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": SCHEMA_VERSION,
            "intent_id": self.intent_id,
            "run_id": self.run_id,
            "prewrite": None if self.prewrite is None else self.prewrite.to_public_dict(),
            "wal_accepted": self.wal_accepted,
            "wal_durable": False,
            "reason_code": self.reason_code,
            "orders_sent": 0,
            "trade_socket_bound": False,
        }
        _assert_public(out)
        return out


@dataclass(frozen=True)
class IngestResult:
    schema_version: str
    accepted: bool
    applied_count: int
    reason_code: Optional[str]
    recovery_required: bool

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise EngineError("invalid_intent")
        object.__setattr__(self, "reason_code", _require_reason(self.reason_code))
        if isinstance(self.applied_count, bool) or not isinstance(self.applied_count, int):
            raise EngineError("adapter_invalid")
        if self.applied_count < 0:
            raise EngineError("adapter_invalid")

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": self.schema_version,
            "accepted": self.accepted,
            "applied_count": self.applied_count,
            "reason_code": self.reason_code,
            "recovery_required": self.recovery_required,
        }
        _assert_public(out)
        return out

    def __repr__(self) -> str:
        return (
            "IngestResult("
            f"accepted={self.accepted}, applied_count={self.applied_count}, "
            f"reason_code={self.reason_code!r})"
        )


def _event_id(
    intent_id: str,
    event_type: str,
    venue: Optional[str],
    leg_id: Optional[str],
    sequence: int,
    dedupe: str,
) -> str:
    digest = hashlib.sha256(
        f"{intent_id}|{event_type}|{venue or '-'}|{leg_id or '-'}|{sequence}|{dedupe}".encode(
            "utf-8"
        )
    ).hexdigest()
    return "e" + digest[:31]


def _split_plans(intent: TradeIntent, plans: Sequence[LegPlan]) -> tuple[LegPlan, LegPlan]:
    if not isinstance(plans, Sequence) or isinstance(plans, (str, bytes)) or len(plans) != 2:
        raise EngineError("invalid_plan_set")
    bybit: Optional[LegPlan] = None
    okx: Optional[LegPlan] = None
    seen: set[Venue] = set()
    for plan in plans:
        if not isinstance(plan, LegPlan):
            raise EngineError("invalid_plan_set")
        if plan.intent_id != intent.intent_id:
            raise EngineError("invalid_plan_set")
        if plan.venue in seen:
            raise EngineError("invalid_plan_set")
        seen.add(plan.venue)
        if plan.venue is Venue.BYBIT:
            bybit = plan
        elif plan.venue is Venue.OKX:
            okx = plan
        else:
            raise EngineError("invalid_plan_set")
    if bybit is None or okx is None:
        raise EngineError("invalid_plan_set")
    return bybit, okx


def _write_representable(evidence: VenueWriteEvidence) -> bool:
    return (
        evidence.outcome is WriteOutcome.WRITE_COMPLETED
        or evidence.asend_start_mono_ns is not None
    )


def _write_uncertain(evidence: VenueWriteEvidence) -> bool:
    return evidence.outcome in {WriteOutcome.WRITE_FAILED, WriteOutcome.CANCELLED}


def _pending_cancellation(task: Optional[asyncio.Task[Any]]) -> bool:
    if task is None:
        return False
    cancelling = getattr(task, "cancelling", None)
    if callable(cancelling):
        try:
            return int(cancelling()) > 0
        except (TypeError, ValueError):
            return bool(cancelling())
    return True


class ExecutionEngine:
    """Single-account execution gate. Sole WAL enqueue owner."""

    def __init__(
        self,
        *,
        run_id: str,
        wal: ExecutionWal,
        transport: ExecutionTransport,
        plan_resolver: PlanResolver,
        instrument_cache: InstrumentCache,
        risk_policy: RiskPolicy,
        readiness: ReadinessSnapshot,
        ownership: FileOwnershipFence,
        monotonic_ns: MonotonicNs,
        state: Optional[SpreadState] = None,
        recovery_factory: Optional[RecoveryLegFactory] = None,
        adapter: Optional[PrivateEventAdapter] = None,
        wal_drain: Optional[WalDrainPort] = None,
        snapshot_provider: Optional[SnapshotPort] = None,
        durable_prewrite: bool = False,
    ) -> None:
        if not isinstance(wal, ExecutionWal):
            raise EngineError("invalid_intent")
        if not isinstance(transport, ExecutionTransport):
            raise EngineError("invalid_intent")
        if not callable(plan_resolver):
            raise EngineError("invalid_plan_set")
        if not isinstance(instrument_cache, InstrumentCache):
            raise EngineError("stale_metadata")
        if not isinstance(risk_policy, RiskPolicy):
            raise EngineError("invalid_intent")
        if not isinstance(readiness, ReadinessSnapshot):
            raise EngineError("invalid_intent")
        if not isinstance(ownership, FileOwnershipFence):
            raise EngineError("ownership_not_held")
        if not callable(monotonic_ns):
            raise EngineError("invalid_intent")
        if wal.run_id != run_id:
            raise EngineError("invalid_intent")
        if state is None:
            state = initial_spread_state(
                run_id=run_id, lot_tolerance=risk_policy.lot_tolerance
            )
        if not isinstance(state, SpreadState) or state.run_id != run_id:
            raise EngineError("invalid_intent")
        self._run_id = run_id
        self._wal = wal
        self._transport = transport
        self._plan_resolver = plan_resolver
        self._cache = instrument_cache
        self._policy = risk_policy
        self._readiness_fence = DualReadinessFence(readiness)
        self._ownership = ownership
        self._ownership_claim = ownership.claim_engine()
        self._monotonic_ns = monotonic_ns
        if recovery_factory is not None and not callable(recovery_factory):
            raise EngineError("invalid_plan_set")
        if adapter is not None and not isinstance(adapter, PrivateEventAdapter):
            raise EngineError("adapter_invalid")
        if wal_drain is not None and not callable(wal_drain):
            raise EngineError("invalid_intent")
        if snapshot_provider is not None and not callable(snapshot_provider):
            raise EngineError("invalid_intent")
        if not isinstance(durable_prewrite, bool):
            raise EngineError("invalid_intent")
        self._state = state
        self._lock = asyncio.Lock()
        self._adapter_seeds: dict[str, int] = {}
        self._recovery_factory = recovery_factory
        self._adapter = adapter
        self._wal_drain = wal_drain
        self._snapshot_provider = snapshot_provider
        self._durable_prewrite = durable_prewrite
        self._recovery_attempts = 0
        self._last_intent: Optional[TradeIntent] = None
        self._primary_by_venue: dict[Venue, LegPlan] = {}
        self._restart_unproven = False
        self._note_seeds((), state)

    @property
    def state(self) -> SpreadState:
        return self._state

    @property
    def readiness(self) -> ReadinessSnapshot:
        return self._readiness_fence.snapshot

    @property
    def readiness_revision(self) -> int:
        return self._readiness_fence.revision

    def adapter_last_sequences(self) -> Mapping[str, int]:
        return MappingProxyType(dict(self._adapter_seeds))

    async def update_readiness(self, snapshot: ReadinessSnapshot) -> None:
        self.publish_readiness(snapshot)

    def publish_readiness(self, snapshot: ReadinessSnapshot) -> bool:
        """Publish from a same-loop socket callback without waiting on submit."""
        return self._readiness_fence.publish(snapshot)

    def _note_seeds(
        self, events: Sequence[ExecutionEvent], state: Optional[SpreadState]
    ) -> None:
        for event in events:
            self._adapter_seeds[event.intent_id] = event.sequence
        if state is not None and state.intent_id is not None:
            self._adapter_seeds[state.intent_id] = state.last_sequence

    def adapter_last_monotonic_ns(self) -> int:
        return self._state.last_monotonic_ns

    def _now(self) -> int:
        value = self._monotonic_ns()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return self._state.last_monotonic_ns
        return value

    def _mono_at_least(self, candidate: Optional[int]) -> int:
        last = self._state.last_monotonic_ns
        if candidate is None or isinstance(candidate, bool) or candidate < last:
            nxt = self._now()
            return last if nxt < last else nxt
        return candidate

    def _reject(
        self,
        intent: TradeIntent,
        reason_code: str,
        *,
        dispatch: Optional[DispatchResult] = None,
    ) -> SubmitResult:
        return SubmitResult(
            schema_version=SCHEMA_VERSION,
            status=SubmitStatus.REJECTED,
            intent_id=intent.intent_id,
            run_id=intent.run_id,
            reason_code=_require_reason(reason_code),
            recovery_required=False,
            dispatch=dispatch,
        )

    def _recovery(
        self,
        intent: TradeIntent,
        reason_code: str,
        *,
        dispatch: Optional[DispatchResult] = None,
    ) -> SubmitResult:
        return SubmitResult(
            schema_version=SCHEMA_VERSION,
            status=SubmitStatus.RECOVERY_REQUIRED,
            intent_id=intent.intent_id,
            run_id=intent.run_id,
            reason_code=_require_reason(reason_code),
            recovery_required=True,
            dispatch=dispatch,
        )

    def _accepted(
        self,
        intent: TradeIntent,
        *,
        dispatch: Optional[DispatchResult] = None,
    ) -> SubmitResult:
        return SubmitResult(
            schema_version=SCHEMA_VERSION,
            status=SubmitStatus.ACCEPTED,
            intent_id=intent.intent_id,
            run_id=intent.run_id,
            reason_code=None,
            recovery_required=False,
            dispatch=dispatch,
        )

    def _rollback_accepted_without_send(
        self, intent: TradeIntent, reason_code: str
    ) -> SubmitResult:
        seq = self._state.last_sequence + 1
        mono = self._mono_at_least(None)
        if intent.action is IntentAction.OPEN:
            rejected = self._rejected_event(intent, seq, mono)
            folded = self._fold((rejected,))
            if folded is None or not self._commit_events((rejected,), folded):
                return self._recovery(intent, "wal_unhealthy")
            return self._reject(intent, reason_code)
        fault = self._fault_event(
            intent, seq, mono, halt=False, reason_code="recovery_required"
        )
        folded = self._fold((fault,))
        if folded is None or not self._commit_events((fault,), folded):
            return self._recovery(intent, "wal_unhealthy")
        return self._recovery(intent, reason_code)

    def _open_gate(
        self, intent: TradeIntent, readiness: ReadinessSnapshot,
        *, require_trade_socket: bool = True,
    ) -> Optional[str]:
        now = self._now()
        if now >= intent.expiry_mono_ns:
            return "ttl_expired"
        if intent.coin not in self._policy.allowed_coins:
            return "coin_not_allowed"
        if intent.notional_usdt <= 0:
            return "notional_invalid"
        if intent.notional_usdt > self._policy.max_notional_usdt:
            return "notional_exceeds_cap"
        if readiness.pause:
            return "pause"
        if readiness.kill_switch:
            return "kill_switch"
        if require_trade_socket:
            connectivity_reason = readiness.connectivity_reason()
        elif not readiness.bybit_private_ready or not readiness.okx_private_ready:
            connectivity_reason = "private_stream_not_ready"
        else:
            connectivity_reason = None
        if connectivity_reason is not None:
            return connectivity_reason
        if self._restart_unproven:
            return "opens_not_allowed"
        if not opens_allowed(self._state):
            return "opens_not_allowed"
        health = self._wal.health()
        if health.integrity_unhealthy or health.hard_full:
            return "wal_unhealthy"
        if health.blocks_opens:
            return "wal_blocks_opens"
        if not self._wal.can_admit(SUBMIT_WORST_CASE_EVENTS, open_intent=True):
            return "wal_capacity"
        return None

    def _no_order_audit_gate(
        self, intent: TradeIntent, readiness: ReadinessSnapshot,
    ) -> Optional[str]:
        """Risk/private/WAL gate for prewrite evidence with no venue exposure.

        This gate is only reachable through ``audit_intent`` and a
        network-incapable transport. It does not relax ``submit``: live opens
        still require current venue reconciliation.
        """
        now = self._now()
        if now >= intent.expiry_mono_ns:
            return "ttl_expired"
        if intent.coin not in self._policy.allowed_coins:
            return "coin_not_allowed"
        if intent.notional_usdt <= 0:
            return "notional_invalid"
        if intent.notional_usdt > self._policy.max_notional_usdt:
            return "notional_exceeds_cap"
        if readiness.pause:
            return "pause"
        if readiness.kill_switch:
            return "kill_switch"
        if not readiness.bybit_private_ready or not readiness.okx_private_ready:
            return "private_stream_not_ready"
        if self._restart_unproven or not opens_allowed(self._state):
            return "opens_not_allowed"
        health = self._wal.health()
        if health.writer_unhealthy or health.integrity_unhealthy or health.hard_full:
            return "wal_unhealthy"
        if not self._wal.can_admit_no_order_audit(2):
            return "wal_capacity" if health.queue_depth else "wal_blocks_opens"
        return None

    def _close_gate(self, intent: TradeIntent) -> Optional[str]:
        if self._state.status is not SpreadStatus.OPEN:
            return "close_not_open"
        if intent.coin != self._state.coin or intent.spread_direction is not self._state.direction:
            return "close_mismatch"
        if not self._wal.can_admit(SUBMIT_WORST_CASE_EVENTS, open_intent=False):
            if self._wal.health().integrity_unhealthy or self._wal.health().hard_full:
                return "wal_unhealthy"
            return "wal_capacity"
        return None

    def _build_event(
        self,
        *,
        intent: TradeIntent,
        event_type: ExecutionEventType,
        sequence: int,
        monotonic_ns: int,
        venue: Optional[Venue],
        leg_id: Optional[str],
        payload: Mapping[str, Any],
        dedupe: str,
    ) -> ExecutionEvent:
        return ExecutionEvent(
            schema_version=CONTRACT_SCHEMA_VERSION,
            event_id=_event_id(
                intent.intent_id,
                event_type.value,
                None if venue is None else venue.value,
                leg_id,
                sequence,
                dedupe,
            ),
            event_type=event_type,
            intent_id=intent.intent_id,
            run_id=intent.run_id,
            sequence=sequence,
            monotonic_ns=monotonic_ns,
            venue=venue,
            leg_id=leg_id,
            payload=payload,
        )

    def _accepted_event(self, intent: TradeIntent, seq: int, mono: int) -> ExecutionEvent:
        return self._build_event(
            intent=intent,
            event_type=ExecutionEventType.INTENT_ACCEPTED,
            sequence=seq,
            monotonic_ns=mono,
            venue=None,
            leg_id=None,
            payload={
                "action": intent.action.value,
                "coin": intent.coin,
                "spread_direction": intent.spread_direction.value,
                "lot_tolerance": decimal_to_canonical(self._policy.lot_tolerance),
            },
            dedupe="intent_accepted",
        )

    def _request_sent_event(
        self,
        intent: TradeIntent,
        plan: LegPlan,
        evidence: Optional[VenueWriteEvidence],
        seq: int,
        mono: int,
        readiness: ReadinessSnapshot,
    ) -> ExecutionEvent:
        return self._build_event(
            intent=intent,
            event_type=ExecutionEventType.REQUEST_SENT,
            sequence=seq,
            monotonic_ns=mono,
            venue=plan.venue,
            leg_id=plan.leg_id,
            payload={
                "quantity": decimal_to_canonical(plan.quantity),
                "reduce_only": plan.reduce_only,
                "instrument": plan.instrument,
                "side": plan.side,
                "client_id": plan.client_id,
                "stream_generation": readiness.generation_for(plan.venue),
            },
            dedupe=f"request_sent:{plan.venue.value}:{plan.leg_id}",
        )

    def _timeout_event(
        self, intent: TradeIntent, plan: LegPlan, seq: int, mono: int
    ) -> ExecutionEvent:
        return self._build_event(
            intent=intent,
            event_type=ExecutionEventType.ACK_TIMEOUT,
            sequence=seq,
            monotonic_ns=mono,
            venue=plan.venue,
            leg_id=plan.leg_id,
            payload={"reason_code": "ack_timeout"},
            dedupe=f"ack_timeout:{plan.venue.value}:{plan.leg_id}",
        )

    def _rejected_event(self, intent: TradeIntent, seq: int, mono: int) -> ExecutionEvent:
        return self._build_event(
            intent=intent,
            event_type=ExecutionEventType.INTENT_REJECTED,
            sequence=seq,
            monotonic_ns=mono,
            venue=None,
            leg_id=None,
            payload={"reason_code": "intent_rejected", "action": intent.action.value},
            dedupe="intent_rejected",
        )

    def _fault_event(
        self,
        intent: TradeIntent,
        seq: int,
        mono: int,
        *,
        halt: bool,
        reason_code: str,
    ) -> ExecutionEvent:
        payload_reason = reason_code if reason_code in {
            "ack_timeout",
            "unknown_correlation",
            "stream_generation_mismatch",
            "venue_rejected",
            "fault",
            "halt",
            "qty_mismatch",
            "open_order_remains",
            "recovery_required",
            "intent_rejected",
            "pause",
        } else "fault"
        return self._build_event(
            intent=intent,
            event_type=ExecutionEventType.FAULT,
            sequence=seq,
            monotonic_ns=mono,
            venue=None,
            leg_id=None,
            payload={"halt": halt, "reason_code": payload_reason},
            dedupe=f"fault:{payload_reason}:{int(halt)}",
        )

    def _commit_events(
        self, events: Sequence[ExecutionEvent], next_state: SpreadState
    ) -> bool:
        if not events:
            self._state = next_state
            self._note_seeds((), next_state)
            return True
        try:
            acks = self._wal.enqueue_batch(tuple(events))
        except WalError:
            return False
        if not acks or any(not ack.accepted for ack in acks):
            return False
        if len(acks) != len(events):
            return False
        self._state = next_state
        self._note_seeds(events, next_state)
        return True

    def _accepted_is_durable(self, accepted_event: ExecutionEvent) -> bool:
        """Opt-in pre-dispatch proof; failure leaves ARMED for reconciliation.

        Keep this synchronous under the engine lock: cancellation must not race
        an outstanding WAL writer with a subsequent submit or a socket write.
        """
        if not self._durable_prewrite:
            return True
        try:
            self._wal.drain_all()
            replay = self._wal.replay()
            health = self._wal.health()
        except Exception:
            return False
        return (
            not health.writer_unhealthy
            and not health.integrity_unhealthy
            and not health.torn_tail
            and health.queue_depth == 0
            and health.durable_lag == 0
            and bool(replay.records)
            and replay.integrity_ok
            and not replay.torn_tail
            and replay.durable_watermark == health.durable_wal_seq
            and replay.records[-1].event == accepted_event
            and replay.state == self._state
        )

    def _validate_plan_reduce_only(
        self, intent: TradeIntent, bybit: LegPlan, okx: LegPlan
    ) -> Optional[str]:
        required = intent.action is IntentAction.CLOSE
        if bybit.reduce_only is not required or okx.reduce_only is not required:
            return "invalid_plan_set"
        return None

    def _prefold_request_shapes(
        self,
        intent: TradeIntent,
        bybit: LegPlan,
        okx: LegPlan,
        accept_seq: int,
        accept_mono: int,
        readiness: ReadinessSnapshot,
    ) -> Optional[SpreadState]:
        accepted = self._accepted_event(intent, accept_seq, accept_mono)
        bybit_sent = self._request_sent_event(
            intent, bybit, None, accept_seq + 1, accept_mono, readiness
        )
        okx_sent = self._request_sent_event(
            intent, okx, None, accept_seq + 2, accept_mono, readiness
        )
        return self._fold((accepted, bybit_sent, okx_sent))

    def _commit_derived(
        self, derived: Sequence[ExecutionEvent], intent: TradeIntent
    ) -> bool:
        if not derived:
            return True
        folded = self._fold(derived)
        if folded is not None:
            return self._commit_events(derived, folded)
        proven = [
            event
            for event in derived
            if event.event_type is ExecutionEventType.REQUEST_SENT
        ]
        if not proven:
            fault = self._fault_event(
                intent,
                self._state.last_sequence + 1,
                self._mono_at_least(None),
                halt=True,
                reason_code="halt",
            )
            fault_state = self._fold((fault,))
            if fault_state is not None:
                self._commit_events((fault,), fault_state)
            return False
        proven_state = self._fold(proven)
        if proven_state is None:
            return False
        fault = self._fault_event(
            intent,
            proven[-1].sequence + 1,
            self._mono_at_least(proven[-1].monotonic_ns),
            halt=True,
            reason_code="halt",
        )
        with_fault = self._fold((*proven, fault))
        if with_fault is not None:
            return self._commit_events((*proven, fault), with_fault)
        return self._commit_events(proven, proven_state)

    def _fold(self, events: Sequence[ExecutionEvent]) -> Optional[SpreadState]:
        tentative = self._state
        try:
            for event in events:
                tentative = apply_event(tentative, event)
        except (InvalidTransition, ContractValidationError):
            return None
        return tentative

    def _derive_lifecycle(
        self,
        intent: TradeIntent,
        bybit: LegPlan,
        okx: LegPlan,
        result: DispatchResult,
        start_seq: int,
        last_mono: int,
        readiness: ReadinessSnapshot,
    ) -> tuple[list[ExecutionEvent], str]:
        seq = start_seq
        mono = last_mono
        events: list[ExecutionEvent] = []
        plans = ((Venue.BYBIT, bybit, result.bybit), (Venue.OKX, okx, result.okx))
        no_starts = (
            result.status is DispatchStatus.REJECTED
            and result.bybit.asend_start_mono_ns is None
            and result.okx.asend_start_mono_ns is None
            and result.bybit.outcome is WriteOutcome.NOT_ATTEMPTED
            and result.okx.outcome is WriteOutcome.NOT_ATTEMPTED
        )
        no_start_reason = (
            "readiness_changed"
            if result.reason_code == "readiness_changed"
            else "transport_rejected"
        )
        if no_starts and intent.action is IntentAction.OPEN:
            seq += 1
            mono = self._mono_at_least(mono)
            events.append(self._rejected_event(intent, seq, mono))
            return events, no_start_reason
        if no_starts and intent.action is IntentAction.CLOSE:
            seq += 1
            mono = self._mono_at_least(mono)
            events.append(
                self._fault_event(
                    intent, seq, mono, halt=False, reason_code="recovery_required"
                )
            )
            return events, no_start_reason

        ambiguous = False
        for _venue, _plan, evidence in plans:
            if result.status is DispatchStatus.CANCELLED and not _write_representable(
                evidence
            ):
                ambiguous = True
            elif (
                evidence.outcome is WriteOutcome.WRITE_FAILED
                and not _write_representable(evidence)
            ):
                ambiguous = True
            elif (
                evidence.outcome is WriteOutcome.NOT_ATTEMPTED
                and result.status is not DispatchStatus.REJECTED
            ):
                ambiguous = True

        for _venue, plan, evidence in plans:
            if _write_representable(evidence):
                seq += 1
                stamp = evidence.asend_start_mono_ns
                mono = stamp if stamp is not None and stamp >= mono else self._mono_at_least(mono)
                if stamp is not None and stamp >= mono:
                    mono = stamp
                events.append(
                    self._request_sent_event(
                        intent, plan, evidence, seq, mono, readiness
                    )
                )

        for _venue, plan, evidence in plans:
            if _write_representable(evidence) and _write_uncertain(evidence):
                seq += 1
                stamp = evidence.asend_done_mono_ns
                mono = stamp if stamp is not None and stamp >= mono else self._mono_at_least(mono)
                if stamp is not None and stamp >= mono:
                    mono = stamp
                events.append(self._timeout_event(intent, plan, seq, mono))

        if ambiguous:
            seq += 1
            mono = self._mono_at_least(mono)
            events.append(
                self._fault_event(intent, seq, mono, halt=True, reason_code="halt")
            )
            return events, "ambiguous_write"
        if result.status is DispatchStatus.CANCELLED:
            return events, "cancelled"
        if result.status in {DispatchStatus.PARTIAL, DispatchStatus.BOTH_FAILED}:
            return events, "write_failed"
        if result.status is DispatchStatus.BOTH_COMPLETED:
            return events, "accepted"
        return events, "write_failed"

    async def submit(self, intent: TradeIntent) -> SubmitResult:
        async with self._lock:
            return await self._submit_locked(intent)

    async def audit_intent(self, intent: TradeIntent) -> NoOrderAuditResult:
        """No-order OPEN probe through risk, owner, private readiness and WAL.

        A successful audit stages an atomic accepted/rejected pair, never a
        REQUEST_SENT. WAL enqueue is not fsync; consumers must drain and
        verify durable replay before counting this as canary evidence.
        """
        async with self._lock:
            def result(
                reason: Optional[str], *, prewrite: Optional[PrewriteAuditResult] = None,
                wal_accepted: bool = False,
            ) -> NoOrderAuditResult:
                return NoOrderAuditResult(
                    intent_id=intent.intent_id if isinstance(intent, TradeIntent) else "invalid",
                    run_id=self._run_id,
                    prewrite=prewrite,
                    wal_accepted=wal_accepted,
                    reason_code=reason,
                )

            if not isinstance(intent, TradeIntent) or intent.run_id != self._run_id:
                return result("invalid_intent")
            if intent.action is not IntentAction.OPEN:
                return result("no_order_only")
            if not self._transport.no_order_audit_capable:
                return result("no_order_only")
            if self._state.status is not SpreadStatus.IDLE:
                return result("opens_not_allowed")
            if intent.intent_id in self._state.accepted_intent_ids:
                return result("opens_not_allowed")
            try:
                self._ownership.assert_owned(self._ownership_claim)
            except OwnershipError:
                return result("ownership_not_held")
            lease, lease_reason = self._readiness_fence.acquire_no_order()
            if lease is None:
                return result(lease_reason or "readiness_changed")
            gate_reason = self._no_order_audit_gate(intent, lease.snapshot)
            if gate_reason is not None:
                return result(gate_reason)
            try:
                bybit, okx = _split_plans(intent, self._plan_resolver(intent))
            except EngineError as exc:
                return result(exc.reason_code)
            except (TypeError, ValueError, ContractValidationError):
                return result("invalid_plan_set")
            if self._validate_plan_reduce_only(intent, bybit, okx) is not None:
                return result("invalid_plan_set")
            try:
                prepared = prepare_dual_leg(
                    intent, (bybit, okx), self._cache, now_mono_ns=self._now(),
                )
            except TransportError as exc:
                return result(
                    "stale_metadata" if exc.reason_code in {
                        "stale_metadata", "missing_metadata", "invalid_metadata",
                    } else "invalid_plan_set"
                )
            try:
                self._ownership.assert_owned(self._ownership_claim)
            except OwnershipError:
                return result("ownership_not_held")
            if self._now() >= intent.expiry_mono_ns:
                return result("ttl_expired")
            if not self._readiness_fence.validate_no_order(lease):
                return result("readiness_changed")
            try:
                prewrite = self._transport.audit_prewrite(
                    prepared,
                    pre_send_guard=lambda: self._readiness_fence.validate_no_order(lease),
                )
            except TransportError:
                return result("transport_rejected")
            if not self._readiness_fence.validate_no_order(lease):
                return result("readiness_changed", prewrite=prewrite)
            try:
                self._ownership.assert_owned(self._ownership_claim)
            except OwnershipError:
                return result("ownership_not_held", prewrite=prewrite)

            mono = self._mono_at_least(None)
            accepted = self._accepted_event(intent, 1, mono)
            rejected = self._build_event(
                intent=intent,
                event_type=ExecutionEventType.INTENT_REJECTED,
                sequence=2,
                monotonic_ns=self._mono_at_least(mono),
                venue=None,
                leg_id=None,
                payload={
                    "reason_code": "intent_rejected",
                    "action": IntentAction.OPEN.value,
                    "audit_mode": "no_order_prewrite",
                    "prewrite_passed": prewrite.ready,
                },
                dedupe="no_order_prewrite",
            )
            folded = self._fold((accepted, rejected))
            if folded is None or folded.status is not SpreadStatus.IDLE:
                return result("opens_not_allowed", prewrite=prewrite)
            if not self._wal.can_admit(2, open_intent=True):
                return result("wal_capacity", prewrite=prewrite)
            if not self._commit_events((accepted, rejected), folded):
                return result("wal_capacity", prewrite=prewrite)
            return result(
                None if prewrite.ready else prewrite.reason_code or "transport_rejected",
                prewrite=prewrite, wal_accepted=True,
            )

    async def _submit_locked(self, intent: TradeIntent) -> SubmitResult:
        if not isinstance(intent, TradeIntent):
            return SubmitResult(
                schema_version=SCHEMA_VERSION,
                status=SubmitStatus.REJECTED,
                intent_id="invalid",
                run_id=self._run_id,
                reason_code="invalid_intent",
                recovery_required=False,
            )
        if intent.run_id != self._run_id:
            return self._reject(intent, "invalid_intent")
        try:
            self._ownership.assert_owned(self._ownership_claim)
        except OwnershipError:
            return self._reject(intent, "ownership_not_held")

        readiness_lease: Optional[ReadinessLease] = None
        readiness_reason: Optional[str] = None
        if intent.action is IntentAction.OPEN:
            readiness_lease, readiness_reason = self._readiness_fence.acquire(
                require_controls=True
            )
            reason = self._open_gate(intent, self.readiness)
            if reason is not None:
                return self._reject(intent, reason)
        elif intent.action is IntentAction.CLOSE:
            reason = self._close_gate(intent)
            if reason is not None:
                return self._reject(intent, reason)
            readiness_lease, readiness_reason = self._readiness_fence.acquire(
                require_controls=False
            )
        else:
            return self._reject(intent, "invalid_intent")

        try:
            plans = self._plan_resolver(intent)
            bybit, okx = _split_plans(intent, plans)
        except EngineError as exc:
            return self._reject(intent, exc.reason_code)
        except (TypeError, ValueError, ContractValidationError):
            return self._reject(intent, "invalid_plan_set")

        plan_reason = self._validate_plan_reduce_only(intent, bybit, okx)
        if plan_reason is not None:
            return self._reject(intent, plan_reason)

        prepare_now = self._now()
        try:
            prepared = prepare_dual_leg(
                intent, (bybit, okx), self._cache, now_mono_ns=prepare_now
            )
        except TransportError as exc:
            mapped = (
                "stale_metadata"
                if exc.reason_code
                in {"stale_metadata", "missing_metadata", "invalid_metadata"}
                else "invalid_plan_set"
            )
            return self._reject(intent, mapped)

        accept_seq = 1 if (
            (intent.action is IntentAction.OPEN and self._state.status in {SpreadStatus.IDLE, SpreadStatus.FLAT})
            or (intent.action is IntentAction.CLOSE and self._state.status is SpreadStatus.OPEN)
        ) else self._state.last_sequence + 1
        try:
            self._ownership.assert_owned(self._ownership_claim)
        except OwnershipError:
            return self._reject(intent, "ownership_not_held")
        now = self._now()
        if now >= intent.expiry_mono_ns:
            return self._reject(intent, "ttl_expired")
        last_mono = self._state.last_monotonic_ns
        accept_mono = last_mono if now < last_mono else now
        accepted_event = self._accepted_event(intent, accept_seq, accept_mono)
        readiness_for_events = (
            readiness_lease.snapshot if readiness_lease is not None else self.readiness
        )
        if self._prefold_request_shapes(
            intent,
            bybit,
            okx,
            accept_seq,
            accept_mono,
            readiness_for_events,
        ) is None:
            return self._reject(intent, "invalid_plan_set")
        accepted_state = self._fold((accepted_event,))
        if accepted_state is None:
            return self._reject(
                intent,
                "opens_not_allowed" if intent.action is IntentAction.OPEN else "close_not_open",
            )
        if not self._wal.can_admit(SUBMIT_WORST_CASE_EVENTS, open_intent=intent.action is IntentAction.OPEN):
            return self._reject(intent, "wal_capacity")
        if not self._commit_events((accepted_event,), accepted_state):
            return self._reject(intent, "wal_capacity")
        if intent.action is IntentAction.OPEN:
            self._remember_primary_plans(bybit, okx)
        self._last_intent = intent

        if not self._accepted_is_durable(accepted_event):
            return self._recovery(intent, "wal_unhealthy")
        if self._now() >= intent.expiry_mono_ns:
            return self._rollback_accepted_without_send(intent, "ttl_expired")

        try:
            self._ownership.assert_owned(self._ownership_claim)
        except OwnershipError:
            return self._rollback_accepted_without_send(intent, "ownership_not_held")

        if readiness_lease is None:
            return self._rollback_accepted_without_send(
                intent, readiness_reason or "readiness_changed"
            )
        if not self._readiness_fence.validate(readiness_lease):
            current_reason = self.readiness.connectivity_reason()
            return self._rollback_accepted_without_send(
                intent, current_reason or "readiness_changed"
            )

        try:
            dispatch = await self._transport.dispatch(
                prepared,
                pre_send_guard=lambda: self._readiness_fence.validate(readiness_lease),
            )
        except asyncio.CancelledError:
            fault = self._fault_event(
                intent,
                self._state.last_sequence + 1,
                self._mono_at_least(None),
                halt=True,
                reason_code="halt",
            )
            folded = self._fold((fault,))
            if folded is not None:
                self._commit_events((fault,), folded)
            raise
        except Exception:
            fault = self._fault_event(
                intent,
                self._state.last_sequence + 1,
                self._mono_at_least(None),
                halt=True,
                reason_code="fault",
            )
            folded = self._fold((fault,))
            if folded is not None:
                self._commit_events((fault,), folded)
            return self._recovery(intent, "ambiguous_write")

        derived, derived_reason = self._derive_lifecycle(
            intent,
            bybit,
            okx,
            dispatch,
            start_seq=self._state.last_sequence,
            last_mono=self._state.last_monotonic_ns,
            readiness=readiness_lease.snapshot,
        )
        if derived:
            if not self._commit_derived(derived, intent):
                if dispatch.status is DispatchStatus.CANCELLED and _pending_cancellation(
                    asyncio.current_task()
                ):
                    raise asyncio.CancelledError
                return self._recovery(intent, "wal_capacity", dispatch=dispatch)
        elif dispatch.status is DispatchStatus.CANCELLED:
            if _pending_cancellation(asyncio.current_task()):
                raise asyncio.CancelledError
            return self._recovery(intent, derived_reason, dispatch=dispatch)
        elif dispatch.status is DispatchStatus.BOTH_COMPLETED:
            return self._accepted(intent, dispatch=dispatch)
        else:
            return self._recovery(intent, derived_reason, dispatch=dispatch)

        if dispatch.status is DispatchStatus.CANCELLED and _pending_cancellation(
            asyncio.current_task()
        ):
            raise asyncio.CancelledError

        if dispatch.status is DispatchStatus.BOTH_COMPLETED and not self._state.recovery_required:
            return self._accepted(intent, dispatch=dispatch)
        if (
            dispatch.status is DispatchStatus.REJECTED
            and intent.action is IntentAction.OPEN
            and not self._state.recovery_required
        ):
            return self._reject(intent, derived_reason, dispatch=dispatch)
        return self._recovery(intent, derived_reason, dispatch=dispatch)

    async def ingest_adapter_batch(self, batch: AdapterBatch) -> IngestResult:
        async with self._lock:
            return self._ingest_locked(batch)

    def _ingest_locked(self, batch: AdapterBatch) -> IngestResult:
        if not isinstance(batch, AdapterBatch):
            return IngestResult(
                schema_version=SCHEMA_VERSION,
                accepted=False,
                applied_count=0,
                reason_code="adapter_invalid",
                recovery_required=self._state.recovery_required,
            )
        events = batch.events
        if not events:
            return IngestResult(
                schema_version=SCHEMA_VERSION,
                accepted=True,
                applied_count=0,
                reason_code="adapter_empty",
                recovery_required=self._state.recovery_required,
            )
        tentative = self._fold(events)
        if tentative is None:
            return IngestResult(
                schema_version=SCHEMA_VERSION,
                accepted=False,
                applied_count=0,
                reason_code="adapter_transition",
                recovery_required=self._state.recovery_required,
            )
        if not self._wal.can_admit(len(events), open_intent=False):
            return IngestResult(
                schema_version=SCHEMA_VERSION,
                accepted=False,
                applied_count=0,
                reason_code="adapter_capacity",
                recovery_required=self._state.recovery_required,
            )
        if not self._commit_events(events, tentative):
            return IngestResult(
                schema_version=SCHEMA_VERSION,
                accepted=False,
                applied_count=0,
                reason_code="adapter_capacity",
                recovery_required=self._state.recovery_required,
            )
        return IngestResult(
            schema_version=SCHEMA_VERSION,
            accepted=True,
            applied_count=len(events),
            reason_code=None,
            recovery_required=self._state.recovery_required,
        )

    def _recovery_result(
        self,
        status: RecoveryStatus,
        action: RecoveryActionKind,
        reason_code: str,
        *,
        dispatch: Optional[VenueActionResult] = None,
    ) -> RecoveryResult:
        halted = self._state.status is SpreadStatus.HALTED
        return RecoveryResult(
            status=status,
            reason_code=reason_code,
            recovery_required=bool(self._state.recovery_required or halted),
            halted=halted,
            action=action,
            dispatch=dispatch,
        )

    def _both_private_ready(self) -> bool:
        return self.readiness.bybit_private_ready and self.readiness.okx_private_ready

    def _recovery_event(
        self,
        *,
        intent_id: str,
        event_type: ExecutionEventType,
        sequence: int,
        monotonic_ns: int,
        venue: Optional[Venue],
        leg_id: Optional[str],
        payload: Mapping[str, Any],
        dedupe: str,
    ) -> ExecutionEvent:
        return ExecutionEvent(
            schema_version=CONTRACT_SCHEMA_VERSION,
            event_id=_event_id(
                intent_id,
                event_type.value,
                None if venue is None else venue.value,
                leg_id,
                sequence,
                dedupe,
            ),
            event_type=event_type,
            intent_id=intent_id,
            run_id=self._run_id,
            sequence=sequence,
            monotonic_ns=monotonic_ns,
            venue=venue,
            leg_id=leg_id,
            payload=payload,
        )

    def _plan_recovery_locked(self) -> RecoveryPlan:
        return plan_recovery(
            self._state,
            self.readiness,
            attempts=self._recovery_attempts,
            wal_blocks_opens=self._wal.health().blocks_opens or self._restart_unproven,
        )

    async def plan_recovery(self) -> RecoveryPlan:
        async with self._lock:
            return self._plan_recovery_locked()

    async def apply_recovery_step(self, plan: RecoveryPlan) -> RecoveryResult:
        async with self._lock:
            return await self._apply_recovery_locked(plan)

    async def begin_restart(self, replay: ReplayResult) -> RestartResult:
        async with self._lock:
            if not isinstance(replay, ReplayResult):
                raise EngineError("invalid_intent")
            if replay.opens_allowed:
                raise EngineError("opens_not_allowed")
            state = replay.state
            if not isinstance(state, SpreadState) or state.run_id != self._run_id:
                raise EngineError("invalid_intent")
            if self._state.last_sequence > state.last_sequence:
                raise EngineError("adapter_transition")
            self._state = state
            self._adapter_seeds = {}
            self._note_seeds((), state)
            self._recovery_attempts = 0
            self._last_intent = None
            self._primary_by_venue = {}
            self._restart_unproven = True
            self._wal.begin_restart()
            if self._adapter is not None:
                self._adapter.reset_for_restart(
                    last_sequences=dict(self._adapter_seeds),
                    last_monotonic_ns=state.last_monotonic_ns,
                )
            return RestartResult(
                replayed_status=state.status,
                opens_allowed=False,
                requires_reseed=True,
                restart_intent_id=restart_correlation_id(state, self._run_id),
                reason_code="restart_unproven",
            )

    async def acknowledge_reconciliation(self, token: str) -> None:
        async with self._lock:
            if self._wal_drain is not None:
                self._wal_drain()
            else:
                self._wal.drain_all()
            self._wal.mark_venue_reconciled(token)
            if self._wal.health().venue_reconciliation_complete:
                self._restart_unproven = False

    async def _apply_recovery_locked(self, plan: RecoveryPlan) -> RecoveryResult:
        if not isinstance(plan, RecoveryPlan):
            return self._recovery_result(
                RecoveryStatus.BLOCKED,
                RecoveryActionKind.WAIT_RESEED,
                "ambiguous_exposure",
            )
        try:
            self._ownership.assert_owned(self._ownership_claim)
        except OwnershipError:
            return self._recovery_result(
                RecoveryStatus.BLOCKED, plan.kind, "ownership_not_held"
            )
        current = self._plan_recovery_locked()
        if current.kind is not plan.kind or current.venue is not plan.venue:
            return self._recovery_result(
                RecoveryStatus.BLOCKED, current.kind, current.reason_code
            )
        if current.kind is RecoveryActionKind.WAIT_RESEED:
            return self._apply_wait_reseed_locked()
        if current.kind is RecoveryActionKind.CANCEL_PEER:
            return await self._apply_cancel_locked(current)
        if current.kind is RecoveryActionKind.FLATTEN_FILLED:
            return await self._apply_flatten_locked(current)
        if current.kind is RecoveryActionKind.PROVE_FLAT:
            return self._apply_prove_flat_locked()
        if current.kind is RecoveryActionKind.HALT:
            return self._apply_halt_locked()
        return self._recovery_result(
            RecoveryStatus.APPLIED, RecoveryActionKind.NOTHING, "nothing_to_do"
        )

    def _apply_wait_reseed_locked(self) -> RecoveryResult:
        self._recovery_attempts += 1
        idle_or_flat = (
            self._state.status is SpreadStatus.IDLE and not self._state.legs
        ) or (
            self._state.status is SpreadStatus.FLAT and is_proven_flat(self._state)
        )
        if idle_or_flat:
            snapshot = None
            if self._snapshot_provider is not None:
                try:
                    snapshot = self._snapshot_provider()
                except Exception:
                    snapshot = None
            if isinstance(snapshot, RestartLiveSnapshot) and snapshot.watch_set_flat:
                emitted = self._emit_restart_recon_locked()
                if emitted is not None:
                    return emitted
                return self._recovery_result(
                    RecoveryStatus.BLOCKED,
                    RecoveryActionKind.WAIT_RESEED,
                    "reseed_failed",
                )
            return self._recovery_result(
                RecoveryStatus.BLOCKED,
                RecoveryActionKind.WAIT_RESEED,
                "restart_unproven" if snapshot is not None else "wait_reseed",
            )
        return self._recovery_result(
            RecoveryStatus.BLOCKED,
            RecoveryActionKind.WAIT_RESEED,
            "wait_reseed",
        )

    def _emit_restart_recon_locked(self) -> Optional[RecoveryResult]:
        intent_id = restart_correlation_id(self._state, self._run_id)
        if not self._wal.can_admit(2, open_intent=False):
            return self._recovery_result(
                RecoveryStatus.BLOCKED, RecoveryActionKind.WAIT_RESEED, "wal_capacity"
            )
        seq = self._state.last_sequence
        events: list[ExecutionEvent] = []
        for venue in (Venue.BYBIT, Venue.OKX):
            seq += 1
            mono = self._mono_at_least(None)
            events.append(
                self._recovery_event(
                    intent_id=intent_id,
                    event_type=ExecutionEventType.RECONCILIATION,
                    sequence=seq,
                    monotonic_ns=mono,
                    venue=venue,
                    leg_id=None,
                    payload={"matched": True},
                    dedupe=f"restart_recon:{venue.value}:{intent_id}",
                )
            )
        folded = self._fold(events)
        if folded is None:
            return None
        if not self._commit_events(events, folded):
            return self._recovery_result(
                RecoveryStatus.BLOCKED, RecoveryActionKind.WAIT_RESEED, "wal_capacity"
            )
        return self._recovery_result(
            RecoveryStatus.APPLIED, RecoveryActionKind.WAIT_RESEED, "wait_reseed"
        )

    def _remember_primary_plans(self, bybit: LegPlan, okx: LegPlan) -> None:
        if bybit.reduce_only or okx.reduce_only:
            return
        self._primary_by_venue = {Venue.BYBIT: bybit, Venue.OKX: okx}

    def _trusted_primary(self, venue: Venue) -> Optional[LegPlan]:
        if self._adapter is not None:
            plan = self._adapter.primary_plan(venue)
            if isinstance(plan, LegPlan) and not plan.reduce_only:
                return plan
        stored = self._primary_by_venue.get(venue)
        if isinstance(stored, LegPlan) and not stored.reduce_only:
            return stored
        return None

    def _cancel_matches_primary(self, factory: LegPlan, primary: LegPlan) -> bool:
        return (
            factory.venue is primary.venue
            and factory.leg_id == primary.leg_id
            and factory.instrument == primary.instrument
            and factory.side == primary.side
            and factory.client_id == primary.client_id
            and not factory.reduce_only
        )

    def _flatten_matches_primary(
        self, factory: LegPlan, primary: LegPlan, qty: Decimal
    ) -> bool:
        opposite = "sell" if primary.side == "buy" else "buy"
        expected_cid = derive_client_id(
            primary.intent_id, primary.venue, reduce_only=True
        )
        return (
            factory.venue is primary.venue
            and factory.leg_id == primary.leg_id
            and factory.instrument == primary.instrument
            and factory.side == opposite
            and factory.reduce_only
            and factory.client_id == expected_cid
            and factory.quantity == qty
        )

    def _factory_plan(self, venue: Venue, kind: VenueActionKind) -> Optional[LegPlan]:
        if self._recovery_factory is None:
            return None
        try:
            plan = self._recovery_factory(self._state, venue, kind)
        except (TypeError, ValueError, ContractValidationError, EngineError):
            return None
        if not isinstance(plan, LegPlan):
            return None
        if plan.venue is not venue:
            return None
        if self._state.intent_id is not None and plan.intent_id != self._state.intent_id:
            return None
        return plan

    async def _apply_cancel_locked(self, plan: RecoveryPlan) -> RecoveryResult:
        if plan.venue is None:
            return self._recovery_result(
                RecoveryStatus.BLOCKED, RecoveryActionKind.CANCEL_PEER, "cancel_failed"
            )
        readiness_lease, readiness_reason = self._readiness_fence.acquire_recovery(
            plan.venue
        )
        if readiness_lease is None:
            self._recovery_attempts += 1
            return self._recovery_result(
                RecoveryStatus.BLOCKED,
                RecoveryActionKind.CANCEL_PEER,
                readiness_reason or "wait_reseed",
            )
        primary = self._trusted_primary(plan.venue)
        if primary is None:
            return self._recovery_result(
                RecoveryStatus.BLOCKED, RecoveryActionKind.CANCEL_PEER, "cancel_failed"
            )
        factory_plan = self._factory_plan(plan.venue, VenueActionKind.CANCEL)
        if factory_plan is None or not self._cancel_matches_primary(factory_plan, primary):
            return self._recovery_result(
                RecoveryStatus.BLOCKED, RecoveryActionKind.CANCEL_PEER, "cancel_failed"
            )
        peer = None
        for leg in self._state.legs:
            if leg.venue is plan.venue:
                peer = leg
                break
        if peer is None or factory_plan.leg_id != peer.leg_id:
            return self._recovery_result(
                RecoveryStatus.BLOCKED, RecoveryActionKind.CANCEL_PEER, "cancel_failed"
            )
        intent_id = self._state.intent_id or factory_plan.intent_id
        try:
            prepared = prepare_venue_action(
                factory_plan,
                self._cache,
                kind=VenueActionKind.CANCEL,
                now_mono_ns=self._now(),
                run_id=self._run_id,
                intent=self._last_intent,
            )
        except TransportError:
            return self._recovery_result(
                RecoveryStatus.BLOCKED, RecoveryActionKind.CANCEL_PEER, "cancel_failed"
            )
        if not self._wal.can_admit(RECOVERY_WORST_CASE_EVENTS, open_intent=False):
            return self._recovery_result(
                RecoveryStatus.BLOCKED, RecoveryActionKind.CANCEL_PEER, "wal_capacity"
            )
        seq = self._state.last_sequence + 1
        requested = self._recovery_event(
            intent_id=intent_id,
            event_type=ExecutionEventType.CANCEL_REQUESTED,
            sequence=seq,
            monotonic_ns=self._mono_at_least(None),
            venue=plan.venue,
            leg_id=peer.leg_id,
            payload={},
            dedupe=f"cancel_requested:{plan.venue.value}:{peer.leg_id}",
        )
        folded = self._fold((requested,))
        if folded is None:
            return self._recovery_result(
                RecoveryStatus.BLOCKED, RecoveryActionKind.CANCEL_PEER, "cancel_failed"
            )
        if not self._commit_events((requested,), folded):
            return self._recovery_result(
                RecoveryStatus.BLOCKED, RecoveryActionKind.CANCEL_PEER, "wal_capacity"
            )
        try:
            self._ownership.assert_owned(self._ownership_claim)
        except OwnershipError:
            return self._recovery_result(
                RecoveryStatus.BLOCKED,
                RecoveryActionKind.CANCEL_PEER,
                "ownership_not_held",
            )
        self._recovery_attempts += 1
        try:
            result = await self._transport.dispatch_action(
                prepared,
                pre_send_guard=lambda: self._readiness_fence.validate_recovery(
                    readiness_lease
                ),
            )
        except asyncio.CancelledError:
            self._commit_started_uncertainty(
                intent_id, plan.venue, peer.leg_id, started=False, halt=True
            )
            raise
        except Exception:
            return self._recovery_result(
                RecoveryStatus.BLOCKED, RecoveryActionKind.CANCEL_PEER, "cancel_failed"
            )
        evidence = result.evidence
        started = _write_representable(evidence)
        if started and _write_uncertain(evidence):
            self._commit_timeout(intent_id, plan.venue, peer.leg_id)
            return self._recovery_result(
                RecoveryStatus.BLOCKED,
                RecoveryActionKind.CANCEL_PEER,
                "cancel_failed",
                dispatch=result,
            )
        if not started:
            return self._recovery_result(
                RecoveryStatus.BLOCKED,
                RecoveryActionKind.CANCEL_PEER,
                "cancel_failed",
                dispatch=result,
            )
        return self._recovery_result(
            RecoveryStatus.APPLIED,
            RecoveryActionKind.CANCEL_PEER,
            "peer_working",
            dispatch=result,
        )

    async def _apply_flatten_locked(self, plan: RecoveryPlan) -> RecoveryResult:
        if plan.venue is None:
            return self._recovery_result(
                RecoveryStatus.BLOCKED,
                RecoveryActionKind.FLATTEN_FILLED,
                "flatten_failed",
            )
        readiness_lease, readiness_reason = self._readiness_fence.acquire_recovery(
            plan.venue
        )
        if readiness_lease is None:
            self._recovery_attempts += 1
            return self._recovery_result(
                RecoveryStatus.BLOCKED,
                RecoveryActionKind.FLATTEN_FILLED,
                readiness_reason or "wait_reseed",
            )
        filled = None
        for leg in self._state.legs:
            if leg.venue is plan.venue:
                filled = leg
                break
        if filled is None or filled.reduce_only:
            return self._recovery_result(
                RecoveryStatus.BLOCKED,
                RecoveryActionKind.FLATTEN_FILLED,
                "flatten_failed",
            )
        qty = exposure_qty(filled, self._state.lot_tolerance)
        if qty is None or qty <= 0:
            return self._recovery_result(
                RecoveryStatus.BLOCKED,
                RecoveryActionKind.FLATTEN_FILLED,
                "ambiguous_exposure",
            )
        primary = self._trusted_primary(plan.venue)
        if primary is None:
            return self._recovery_result(
                RecoveryStatus.BLOCKED,
                RecoveryActionKind.FLATTEN_FILLED,
                "flatten_failed",
            )
        factory_plan = self._factory_plan(plan.venue, VenueActionKind.PLACE)
        if factory_plan is None or not self._flatten_matches_primary(
            factory_plan, primary, qty
        ):
            return self._recovery_result(
                RecoveryStatus.BLOCKED,
                RecoveryActionKind.FLATTEN_FILLED,
                "flatten_failed",
            )
        if factory_plan.leg_id != filled.leg_id:
            return self._recovery_result(
                RecoveryStatus.BLOCKED,
                RecoveryActionKind.FLATTEN_FILLED,
                "flatten_failed",
            )
        if self._adapter is not None:
            try:
                self._adapter.bind_recovery_plan(factory_plan)
            except Exception:
                return self._recovery_result(
                    RecoveryStatus.BLOCKED,
                    RecoveryActionKind.FLATTEN_FILLED,
                    "flatten_failed",
                )
        intent_id = self._state.intent_id or factory_plan.intent_id
        try:
            prepared = prepare_venue_action(
                factory_plan,
                self._cache,
                kind=VenueActionKind.PLACE,
                now_mono_ns=self._now(),
                run_id=self._run_id,
                intent=self._last_intent,
            )
        except TransportError:
            return self._recovery_result(
                RecoveryStatus.BLOCKED,
                RecoveryActionKind.FLATTEN_FILLED,
                "flatten_failed",
            )
        seq = self._state.last_sequence + 1
        sent = self._request_sent_from_plan(intent_id, factory_plan, seq)
        folded = self._fold((sent,))
        if folded is None:
            return self._recovery_result(
                RecoveryStatus.BLOCKED,
                RecoveryActionKind.FLATTEN_FILLED,
                "flatten_failed",
            )
        if not self._wal.can_admit(RECOVERY_WORST_CASE_EVENTS, open_intent=False):
            return self._recovery_result(
                RecoveryStatus.BLOCKED,
                RecoveryActionKind.FLATTEN_FILLED,
                "wal_capacity",
            )
        # Enqueue is admission, not durability. A process crash before drain
        # still depends on mandatory restart REST reseed and the deterministic
        # reduce-only client id. Do not fsync or drain here.
        if not self._commit_events((sent,), folded):
            return self._recovery_result(
                RecoveryStatus.BLOCKED,
                RecoveryActionKind.FLATTEN_FILLED,
                "wal_capacity",
            )
        try:
            self._ownership.assert_owned(self._ownership_claim)
        except OwnershipError:
            return self._recovery_result(
                RecoveryStatus.BLOCKED,
                RecoveryActionKind.FLATTEN_FILLED,
                "ownership_not_held",
            )
        try:
            result = await self._transport.dispatch_action(
                prepared,
                pre_send_guard=lambda: self._readiness_fence.validate_recovery(
                    readiness_lease
                ),
            )
        except asyncio.CancelledError:
            self._commit_started_uncertainty(
                intent_id, plan.venue, filled.leg_id, started=False, halt=True
            )
            raise
        except Exception:
            self._recovery_attempts += 1
            return self._recovery_result(
                RecoveryStatus.BLOCKED,
                RecoveryActionKind.WAIT_RESEED,
                "wait_reseed",
            )
        evidence = result.evidence
        started = _write_representable(evidence)
        self._recovery_attempts += 1
        if started and _write_uncertain(evidence):
            self._commit_timeout(intent_id, plan.venue, filled.leg_id)
            return self._recovery_result(
                RecoveryStatus.BLOCKED,
                RecoveryActionKind.FLATTEN_FILLED,
                "flatten_failed",
                dispatch=result,
            )
        if not started:
            return self._recovery_result(
                RecoveryStatus.BLOCKED,
                RecoveryActionKind.WAIT_RESEED,
                "wait_reseed",
                dispatch=result,
            )
        return self._recovery_result(
            RecoveryStatus.APPLIED,
            RecoveryActionKind.FLATTEN_FILLED,
            "flatten_required",
            dispatch=result,
        )

    def _request_sent_from_plan(
        self, intent_id: str, plan: LegPlan, sequence: int
    ) -> ExecutionEvent:
        return self._recovery_event(
            intent_id=intent_id,
            event_type=ExecutionEventType.REQUEST_SENT,
            sequence=sequence,
            monotonic_ns=self._mono_at_least(None),
            venue=plan.venue,
            leg_id=plan.leg_id,
            payload={
                "quantity": decimal_to_canonical(plan.quantity),
                "reduce_only": plan.reduce_only,
                "instrument": plan.instrument,
                "side": plan.side,
                "client_id": plan.client_id,
                "stream_generation": self.readiness.generation_for(plan.venue),
            },
            dedupe=f"request_sent:{plan.venue.value}:{plan.leg_id}:recovery",
        )

    def _commit_timeout(self, intent_id: str, venue: Venue, leg_id: str) -> None:
        event = self._recovery_event(
            intent_id=intent_id,
            event_type=ExecutionEventType.ACK_TIMEOUT,
            sequence=self._state.last_sequence + 1,
            monotonic_ns=self._mono_at_least(None),
            venue=venue,
            leg_id=leg_id,
            payload={"reason_code": "ack_timeout"},
            dedupe=f"ack_timeout:{venue.value}:{leg_id}:recovery",
        )
        folded = self._fold((event,))
        if folded is not None:
            self._commit_events((event,), folded)

    def _halt_event(self, intent_id: str) -> ExecutionEvent:
        return self._recovery_event(
            intent_id=intent_id,
            event_type=ExecutionEventType.FAULT,
            sequence=self._state.last_sequence + 1,
            monotonic_ns=self._mono_at_least(None),
            venue=None,
            leg_id=None,
            payload={"halt": True, "reason_code": "halt"},
            dedupe="fault:halt:1:recovery",
        )

    def _commit_started_uncertainty(
        self,
        intent_id: str,
        venue: Venue,
        leg_id: str,
        *,
        started: bool,
        halt: bool,
    ) -> None:
        del venue, leg_id, started
        if not halt:
            return
        event = self._halt_event(intent_id)
        folded = self._fold((event,))
        if folded is not None:
            self._commit_events((event,), folded)

    def _apply_prove_flat_locked(self) -> RecoveryResult:
        if self._state.status is SpreadStatus.HALTED:
            return self._recovery_result(
                RecoveryStatus.HALTED, RecoveryActionKind.NOTHING, "halt"
            )
        if not self._both_private_ready() or not self._state.stream_generation_ok:
            self._recovery_attempts += 1
            return self._recovery_result(
                RecoveryStatus.BLOCKED, RecoveryActionKind.PROVE_FLAT, "stream_blocked"
            )
        intent_id = self._state.intent_id or restart_correlation_id(
            self._state, self._run_id
        )
        event = self._recovery_event(
            intent_id=intent_id,
            event_type=ExecutionEventType.FLATNESS_PROVEN,
            sequence=self._state.last_sequence + 1,
            monotonic_ns=self._mono_at_least(None),
            venue=None,
            leg_id=None,
            payload={"positions_flat": True, "open_orders_flat": True},
            dedupe="flatness_proven",
        )
        folded = self._fold((event,))
        if folded is None:
            self._recovery_attempts += 1
            return self._recovery_result(
                RecoveryStatus.BLOCKED, RecoveryActionKind.PROVE_FLAT, "reseed_failed"
            )
        if not self._wal.can_admit(1, open_intent=False):
            return self._recovery_result(
                RecoveryStatus.BLOCKED, RecoveryActionKind.PROVE_FLAT, "wal_capacity"
            )
        if not self._commit_events((event,), folded):
            return self._recovery_result(
                RecoveryStatus.BLOCKED, RecoveryActionKind.PROVE_FLAT, "wal_capacity"
            )
        self._recovery_attempts = 0
        return self._recovery_result(
            RecoveryStatus.APPLIED, RecoveryActionKind.PROVE_FLAT, "prove_flat"
        )

    def _apply_halt_locked(self) -> RecoveryResult:
        if self._state.status is SpreadStatus.HALTED:
            return self._recovery_result(
                RecoveryStatus.HALTED, RecoveryActionKind.NOTHING, "halt"
            )
        intent_id = self._state.intent_id or restart_correlation_id(
            self._state, self._run_id
        )
        event = self._halt_event(intent_id)
        folded = self._fold((event,))
        if folded is None:
            return self._recovery_result(
                RecoveryStatus.BLOCKED, RecoveryActionKind.HALT, "ambiguous_exposure"
            )
        if not self._wal.can_admit(1, open_intent=False):
            return self._recovery_result(
                RecoveryStatus.BLOCKED, RecoveryActionKind.HALT, "wal_capacity"
            )
        if not self._commit_events((event,), folded):
            return self._recovery_result(
                RecoveryStatus.BLOCKED, RecoveryActionKind.HALT, "wal_capacity"
            )
        return self._recovery_result(
            RecoveryStatus.HALTED, RecoveryActionKind.HALT, "halt"
        )

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self._run_id,
            "status": self._state.status.value,
            "intent_id": self._state.intent_id,
            "last_sequence": self._state.last_sequence,
            "recovery_required": self._state.recovery_required,
        }
        _assert_public(out)
        return out

    def __repr__(self) -> str:
        return (
            "ExecutionEngine("
            f"run_id={self._run_id!r}, status={self._state.status.value!r}, "
            f"intent_id={self._state.intent_id!r})"
        )
