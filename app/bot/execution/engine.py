"""Execution-v2 engine and cached risk gate.

Stdlib plus frozen EV2 contracts, FSM, transport, adapters and WAL
admission. Import and construction perform no sockets, disk drain, fsync,
Sentry, logging, REST or live send. ``submit`` awaits only parallel
transport socket writes.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Sequence

from app.bot.execution.adapters import AdapterBatch
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
)
from app.bot.execution.ownership import FileOwnershipFence, OwnershipError
from app.bot.execution.state_machine import (
    InvalidTransition,
    apply_event,
    initial_spread_state,
    opens_allowed,
)
from app.bot.execution.transport import (
    DispatchResult,
    DispatchStatus,
    ExecutionTransport,
    InstrumentCache,
    TransportError,
    VenueWriteEvidence,
    WriteOutcome,
    prepare_dual_leg,
)
from app.bot.execution.wal import (
    SUBMIT_WORST_CASE_EVENTS,
    ExecutionWal,
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
            if not isinstance(coin, str) or not (2 <= len(coin) <= 16):
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
        self._readiness = readiness
        self._ownership = ownership
        self._ownership_claim = ownership.claim_engine()
        self._monotonic_ns = monotonic_ns
        self._state = state
        self._lock = asyncio.Lock()
        self._adapter_seeds: dict[str, int] = {}
        self._note_seeds((), state)

    @property
    def state(self) -> SpreadState:
        return self._state

    @property
    def readiness(self) -> ReadinessSnapshot:
        return self._readiness

    def adapter_last_sequences(self) -> Mapping[str, int]:
        return MappingProxyType(dict(self._adapter_seeds))

    async def update_readiness(self, snapshot: ReadinessSnapshot) -> None:
        async with self._lock:
            if not isinstance(snapshot, ReadinessSnapshot):
                raise EngineError("invalid_intent")
            self._readiness = snapshot

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
            if folded is not None:
                self._commit_events((rejected,), folded)
            return self._reject(intent, reason_code)
        fault = self._fault_event(
            intent, seq, mono, halt=False, reason_code="recovery_required"
        )
        folded = self._fold((fault,))
        if folded is not None:
            self._commit_events((fault,), folded)
        return self._recovery(intent, reason_code)

    def _open_gate(self, intent: TradeIntent) -> Optional[str]:
        now = self._now()
        if now >= intent.expiry_mono_ns:
            return "ttl_expired"
        if intent.coin not in self._policy.allowed_coins:
            return "coin_not_allowed"
        if intent.notional_usdt <= 0:
            return "notional_invalid"
        if intent.notional_usdt > self._policy.max_notional_usdt:
            return "notional_exceeds_cap"
        if self._readiness.pause:
            return "pause"
        if self._readiness.kill_switch:
            return "kill_switch"
        if not self._readiness.bybit_trade_ready or not self._readiness.okx_trade_ready:
            return "trade_socket_not_ready"
        if (
            not self._readiness.bybit_private_ready
            or not self._readiness.okx_private_ready
        ):
            return "private_stream_not_ready"
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
                "stream_generation": self._readiness.generation_for(plan.venue),
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
    ) -> Optional[SpreadState]:
        accepted = self._accepted_event(intent, accept_seq, accept_mono)
        bybit_sent = self._request_sent_event(
            intent, bybit, None, accept_seq + 1, accept_mono
        )
        okx_sent = self._request_sent_event(
            intent, okx, None, accept_seq + 2, accept_mono
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
        if no_starts and intent.action is IntentAction.OPEN:
            seq += 1
            mono = self._mono_at_least(mono)
            events.append(self._rejected_event(intent, seq, mono))
            return events, "transport_rejected"
        if no_starts and intent.action is IntentAction.CLOSE:
            seq += 1
            mono = self._mono_at_least(mono)
            events.append(
                self._fault_event(
                    intent, seq, mono, halt=False, reason_code="recovery_required"
                )
            )
            return events, "transport_rejected"

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
                events.append(self._request_sent_event(intent, plan, evidence, seq, mono))

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

        if intent.action is IntentAction.OPEN:
            reason = self._open_gate(intent)
            if reason is not None:
                return self._reject(intent, reason)
        elif intent.action is IntentAction.CLOSE:
            reason = self._close_gate(intent)
            if reason is not None:
                return self._reject(intent, reason)
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
        if self._prefold_request_shapes(intent, bybit, okx, accept_seq, accept_mono) is None:
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

        try:
            self._ownership.assert_owned(self._ownership_claim)
        except OwnershipError:
            return self._rollback_accepted_without_send(intent, "ownership_not_held")

        try:
            dispatch = await self._transport.dispatch(prepared)
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
            return self._reject(intent, "transport_rejected", dispatch=dispatch)
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
