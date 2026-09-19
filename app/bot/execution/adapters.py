"""Pure execution-v2 private event adapters. Stdlib only. No I/O.

Converts already-decoded OKX/Bybit ACK, order, execution, position and
complete REST snapshots into EV2 ``ExecutionEvent`` values. Venue data is
evidence only. Import and construction perform no network, file, env,
socket, thread, task, journal or live-order action.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Optional, Sequence

from app.bot.execution.contracts import (
    SCHEMA_VERSION as CONTRACT_SCHEMA_VERSION,
    ContractValidationError,
    ExecutionEvent,
    ExecutionEventType,
    LegPlan,
    TradeIntent,
    Venue,
    canonical_decimal,
    decimal_to_canonical,
    derive_client_id,
)

SCHEMA_VERSION = "bbot.execution.adapters.v1"

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
        "orderid",
        "client_order_id",
        "clordid",
        "ordid",
        "execid",
        "exec_id",
        "tradeid",
        "fillid",
        "billid",
        "balance",
        "available_balance",
        "equity",
        "margin",
        "account_value",
        "fill_price",
        "reqid",
        "orderlinkid",
    }
)

_ISSUE_REASON_CODES = frozenset(
    {
        "stale_generation",
        "unknown_correlation",
        "ambiguous_correlation",
        "stale_quantity",
        "malformed_quantity",
        "missing_quantity",
        "conflicting_position",
        "conflicting_identity",
        "incomplete_snapshot",
        "snapshot_failed",
        "paginated_snapshot",
        "blocked_until_reseed",
        "malformed_frame",
        "invalid_source",
        "id_less_ack_unbound",
        "negative_quantity",
        "float_rejected",
    }
)

_SOURCE_ALIASES = {
    "trade_ack": "trade_ack",
    "ack": "trade_ack",
    "order": "order",
    "orders": "order",
    "execution": "execution",
    "fills": "execution",
    "fill": "execution",
    "position": "position",
    "positions": "position",
    "rest_positions": "rest_positions",
    "rest_position": "rest_positions",
    "rest_open_orders": "rest_open_orders",
    "rest_orders": "rest_open_orders",
}

_ORDINARY_SOURCES = frozenset({"trade_ack", "order", "execution", "position"})
_REST_SOURCES = frozenset({"rest_positions", "rest_open_orders"})

_BYBIT_TERMINAL_FILLED = frozenset({"filled"})
_BYBIT_CANCELLED = frozenset({"cancelled", "canceled"})
_BYBIT_REJECTED = frozenset({"rejected", "deactivated"})
_OKX_TERMINAL_FILLED = frozenset({"filled"})
_OKX_CANCELLED = frozenset({"canceled", "cancelled"})
_OKX_REJECTED = frozenset({"rejected"})

_LONG_SIDES = frozenset({"buy", "long"})
_SHORT_SIDES = frozenset({"sell", "short"})


class AdapterError(ValueError):
    """Fail-closed adapter construction/registration error. Redacted."""

    def __init__(self, reason_code: str, *, intent_id: Optional[str] = None) -> None:
        if reason_code not in _ISSUE_REASON_CODES and reason_code not in {
            "invalid_registration",
            "duplicate_binding",
            "intent_mismatch",
            "client_id_mismatch",
            "too_many_intents",
            "invalid_generation",
        }:
            reason_code = "invalid_registration"
        self.reason_code = reason_code
        self.intent_id = intent_id
        super().__init__(reason_code)

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": SCHEMA_VERSION,
            "reason_code": self.reason_code,
            "intent_id": self.intent_id,
        }
        _assert_public(out)
        return out

    def __repr__(self) -> str:
        return (
            f"AdapterError(reason_code={self.reason_code!r}, "
            f"intent_id={self.intent_id!r})"
        )


class AdapterSource(str, Enum):
    TRADE_ACK = "trade_ack"
    ORDER = "order"
    EXECUTION = "execution"
    POSITION = "position"
    REST_POSITIONS = "rest_positions"
    REST_OPEN_ORDERS = "rest_open_orders"


@dataclass(frozen=True)
class AdapterIssue:
    schema_version: str
    reason_code: str
    venue: Optional[Venue]
    source: Optional[str]
    generation: Optional[int]
    intent_id: Optional[str]
    leg_id: Optional[str]

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise AdapterError("invalid_registration")
        if self.reason_code not in _ISSUE_REASON_CODES:
            raise AdapterError("invalid_registration")
        if self.venue is not None and not isinstance(self.venue, Venue):
            raise AdapterError("invalid_registration")

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": self.schema_version,
            "reason_code": self.reason_code,
            "venue": None if self.venue is None else self.venue.value,
            "source": self.source,
            "generation": self.generation,
            "intent_id": self.intent_id,
            "leg_id": self.leg_id,
        }
        _assert_public(out)
        return out

    def __repr__(self) -> str:
        return f"AdapterIssue({self.to_public_dict()!r})"


@dataclass(frozen=True)
class AdapterBatch:
    schema_version: str
    events: tuple[ExecutionEvent, ...]
    issues: tuple[AdapterIssue, ...]

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise AdapterError("invalid_registration")
        if not isinstance(self.events, tuple):
            raise AdapterError("invalid_registration")
        if not isinstance(self.issues, tuple):
            raise AdapterError("invalid_registration")
        for event in self.events:
            if not isinstance(event, ExecutionEvent):
                raise AdapterError("invalid_registration")
        for issue in self.issues:
            if not isinstance(issue, AdapterIssue):
                raise AdapterError("invalid_registration")

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": self.schema_version,
            "events": [event.to_public_dict() for event in self.events],
            "issues": [issue.to_public_dict() for issue in self.issues],
        }
        _assert_public(out)
        return out

    def __repr__(self) -> str:
        return f"AdapterBatch({self.to_public_dict()!r})"


@dataclass(frozen=True)
class _BoundLeg:
    intent: TradeIntent
    plan: LegPlan


@dataclass
class _IntentSeq:
    last_sequence: int
    last_monotonic_ns: int


@dataclass
class _VenueFence:
    expected: int
    blocked: bool
    mismatch_emitted: set[tuple[str, int]]
    rest_positions_ok: bool
    rest_orders_ok: bool
    rest_positions_dirty: bool
    rest_orders_dirty: bool
    observed_position_legs: set[str]
    observed_order_legs: set[str]


class PrivateEventAdapter:
    """Correlate decoded venue evidence onto registered EV2 intents."""

    def __init__(
        self,
        *,
        expected_generations: Optional[Mapping[Venue, int]] = None,
        last_sequences: Optional[Mapping[str, int]] = None,
        last_monotonic_ns: int = 0,
    ) -> None:
        if not isinstance(last_monotonic_ns, int) or isinstance(last_monotonic_ns, bool):
            raise AdapterError("invalid_generation")
        if last_monotonic_ns < 0:
            raise AdapterError("invalid_generation")
        self._expected_seed: dict[Venue, int] = {Venue.BYBIT: 0, Venue.OKX: 0}
        if expected_generations is not None:
            if not isinstance(expected_generations, Mapping):
                raise AdapterError("invalid_generation")
            for venue, generation in expected_generations.items():
                venue_e = _coerce_venue(venue)
                if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
                    raise AdapterError("invalid_generation")
                self._expected_seed[venue_e] = generation
        self._seq_seed: dict[str, int] = {}
        if last_sequences is not None:
            if not isinstance(last_sequences, Mapping):
                raise AdapterError("invalid_registration")
            for intent_id, sequence in last_sequences.items():
                if not isinstance(intent_id, str) or not intent_id:
                    raise AdapterError("invalid_registration")
                if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
                    raise AdapterError("invalid_registration")
                self._seq_seed[intent_id] = sequence
        self._mono_seed = last_monotonic_ns
        self._legs_by_client: dict[str, _BoundLeg] = {}
        self._legs_by_instrument: dict[tuple[Venue, str], _BoundLeg] = {}
        self._intents: dict[str, TradeIntent] = {}
        self._seq: dict[str, _IntentSeq] = {}
        self._fences: dict[Venue, _VenueFence] = {
            venue: _new_fence(generation) for venue, generation in self._expected_seed.items()
        }
        self._emitted_keys: set[str] = set()
        self._last_fill: dict[tuple[str, Venue], Decimal] = {}
        self._fill_piece_hashes: dict[tuple[str, Venue], set[str]] = {}
        self._fill_piece_qty: dict[tuple[str, Venue], Decimal] = {}

    def register(self, intent: TradeIntent, plans: Sequence[LegPlan]) -> None:
        if not isinstance(intent, TradeIntent):
            raise AdapterError("invalid_registration")
        if not isinstance(plans, (list, tuple)) or len(plans) != 2:
            raise AdapterError("invalid_registration", intent_id=intent.intent_id)
        if self._intents and intent.intent_id not in self._intents:
            raise AdapterError("too_many_intents", intent_id=intent.intent_id)
        venues: set[Venue] = set()
        instruments: set[tuple[Venue, str]] = set()
        bound: list[_BoundLeg] = []
        for plan in plans:
            if not isinstance(plan, LegPlan):
                raise AdapterError("invalid_registration", intent_id=intent.intent_id)
            if plan.intent_id != intent.intent_id:
                raise AdapterError("intent_mismatch", intent_id=intent.intent_id)
            expected = derive_client_id(
                intent.intent_id, plan.venue, reduce_only=plan.reduce_only
            )
            if plan.client_id != expected:
                raise AdapterError("client_id_mismatch", intent_id=intent.intent_id)
            if plan.venue in venues:
                raise AdapterError("duplicate_binding", intent_id=intent.intent_id)
            key = (plan.venue, plan.instrument)
            if key in instruments:
                raise AdapterError("duplicate_binding", intent_id=intent.intent_id)
            existing = self._legs_by_instrument.get(key)
            if existing is not None and existing.intent.intent_id != intent.intent_id:
                raise AdapterError("duplicate_binding", intent_id=intent.intent_id)
            venues.add(plan.venue)
            instruments.add(key)
            bound.append(_BoundLeg(intent=intent, plan=plan))
        if venues != {Venue.BYBIT, Venue.OKX}:
            raise AdapterError("invalid_registration", intent_id=intent.intent_id)
        self._clear_intent(intent.intent_id)
        self._intents[intent.intent_id] = intent
        self._seq[intent.intent_id] = _IntentSeq(
            last_sequence=self._seq_seed.get(intent.intent_id, 0),
            last_monotonic_ns=self._mono_seed,
        )
        for item in bound:
            self._legs_by_client[item.plan.client_id] = item
            self._legs_by_instrument[(item.plan.venue, item.plan.instrument)] = item

    def expected_generation(self, venue: Venue) -> int:
        return self._fences[_coerce_venue(venue)].expected

    def is_blocked(self, venue: Venue) -> bool:
        return self._fences[_coerce_venue(venue)].blocked

    def last_sequence(self, intent_id: str) -> int:
        rec = self._seq.get(intent_id)
        if rec is None:
            return self._seq_seed.get(intent_id, 0)
        return rec.last_sequence

    def adapt(
        self,
        payload: Mapping[str, Any],
        *,
        venue: Venue,
        source: str,
        generation: int,
        receive_mono_ns: int,
        expected_client_id: Optional[str] = None,
        snapshot_complete: bool = False,
    ) -> AdapterBatch:
        events: list[ExecutionEvent] = []
        issues: list[AdapterIssue] = []
        venue_e = _coerce_venue(venue)
        source_n = _normalize_source(source)
        if source_n is None:
            issues.append(
                _issue("invalid_source", venue=venue_e, source=str(source), generation=generation)
            )
            return _batch(events, issues)
        if not isinstance(payload, Mapping) or isinstance(payload, (str, bytes)):
            issues.append(
                _issue("malformed_frame", venue=venue_e, source=source_n, generation=generation)
            )
            return _batch(events, issues)
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            issues.append(
                _issue("malformed_frame", venue=venue_e, source=source_n, generation=generation)
            )
            return _batch(events, issues)
        if (
            not isinstance(receive_mono_ns, int)
            or isinstance(receive_mono_ns, bool)
            or receive_mono_ns < 0
        ):
            issues.append(
                _issue("malformed_frame", venue=venue_e, source=source_n, generation=generation)
            )
            return _batch(events, issues)
        if expected_client_id is not None and (
            not isinstance(expected_client_id, str) or not expected_client_id
        ):
            issues.append(
                _issue(
                    "unknown_correlation",
                    venue=venue_e,
                    source=source_n,
                    generation=generation,
                )
            )
            return _batch(events, issues)
        if not isinstance(snapshot_complete, bool):
            issues.append(
                _issue("malformed_frame", venue=venue_e, source=source_n, generation=generation)
            )
            return _batch(events, issues)

        fence = self._fences[venue_e]
        if generation < fence.expected:
            issues.append(
                _issue("stale_generation", venue=venue_e, source=source_n, generation=generation)
            )
            return _batch(events, issues)
        if generation > fence.expected:
            self._emit_mismatch(venue_e, generation, receive_mono_ns, events)
            fence = self._fences[venue_e]
        if fence.blocked and source_n in _ORDINARY_SOURCES:
            issues.append(
                _issue(
                    "blocked_until_reseed",
                    venue=venue_e,
                    source=source_n,
                    generation=generation,
                )
            )
            return _batch(events, issues)

        if source_n == "trade_ack":
            self._adapt_ack(
                payload,
                venue=venue_e,
                source=source_n,
                generation=generation,
                receive_mono_ns=receive_mono_ns,
                expected_client_id=expected_client_id,
                events=events,
                issues=issues,
            )
        elif source_n in {"order", "execution"}:
            self._adapt_order_execution(
                payload,
                venue=venue_e,
                source=source_n,
                generation=generation,
                receive_mono_ns=receive_mono_ns,
                events=events,
                issues=issues,
            )
        elif source_n == "position":
            self._adapt_positions(
                payload,
                venue=venue_e,
                source=source_n,
                generation=generation,
                receive_mono_ns=receive_mono_ns,
                snapshot_complete=False,
                is_rest=False,
                events=events,
                issues=issues,
            )
        elif source_n == "rest_positions":
            self._adapt_rest_positions(
                payload,
                venue=venue_e,
                generation=generation,
                receive_mono_ns=receive_mono_ns,
                snapshot_complete=snapshot_complete,
                events=events,
                issues=issues,
            )
        else:
            self._adapt_rest_open_orders(
                payload,
                venue=venue_e,
                generation=generation,
                receive_mono_ns=receive_mono_ns,
                snapshot_complete=snapshot_complete,
                events=events,
                issues=issues,
            )
        if source_n in _REST_SOURCES:
            self._maybe_emit_rest_recon(
                venue_e, generation, receive_mono_ns, events, issues
            )
        return _batch(events, issues)

    def _clear_intent(self, intent_id: str) -> None:
        drop_clients = [
            client_id
            for client_id, bound in self._legs_by_client.items()
            if bound.intent.intent_id == intent_id
        ]
        for client_id in drop_clients:
            del self._legs_by_client[client_id]
        drop_inst = [
            key
            for key, bound in self._legs_by_instrument.items()
            if bound.intent.intent_id == intent_id
        ]
        for key in drop_inst:
            del self._legs_by_instrument[key]
        self._intents.pop(intent_id, None)
        self._seq.pop(intent_id, None)
        for key in list(self._last_fill):
            if key[0] == intent_id:
                del self._last_fill[key]
        for key in list(self._fill_piece_hashes):
            if key[0] == intent_id:
                del self._fill_piece_hashes[key]
        for key in list(self._fill_piece_qty):
            if key[0] == intent_id:
                del self._fill_piece_qty[key]
        self._emitted_keys = {key for key in self._emitted_keys if not key.startswith(f"{intent_id}|")}

    def _emit_mismatch(
        self,
        venue: Venue,
        generation: int,
        receive_mono_ns: int,
        events: list[ExecutionEvent],
    ) -> None:
        fence = self._fences[venue]
        fence.expected = generation
        fence.blocked = True
        self._reset_rest_cycle(fence)
        for intent in self._intents.values():
            if not any(bound.plan.venue is venue and bound.intent.intent_id == intent.intent_id for bound in self._legs_by_client.values()):
                continue
            token = (intent.intent_id, generation)
            if token in fence.mismatch_emitted:
                continue
            bound = self._bound_for_venue(intent.intent_id, venue)
            event = self._make_event(
                intent=intent,
                event_type=ExecutionEventType.STREAM_GENERATION_MISMATCH,
                venue=venue,
                leg_id=None if bound is None else bound.plan.leg_id,
                receive_mono_ns=receive_mono_ns,
                payload={"stream_generation": generation},
                dedupe_key=f"{intent.intent_id}|mismatch|{venue.value}|{generation}",
            )
            if event is not None:
                events.append(event)
                fence.mismatch_emitted.add(token)

    def _reset_rest_cycle(self, fence: _VenueFence) -> None:
        self._reset_rest_positions_half(fence)
        self._reset_rest_orders_half(fence)

    def _reset_rest_positions_half(self, fence: _VenueFence) -> None:
        fence.rest_positions_ok = False
        fence.rest_positions_dirty = False
        fence.observed_position_legs = set()

    def _reset_rest_orders_half(self, fence: _VenueFence) -> None:
        fence.rest_orders_ok = False
        fence.rest_orders_dirty = False
        fence.observed_order_legs = set()

    def _invalidate_rest_positions(self, venue: Venue) -> None:
        fence = self._fences[venue]
        fence.rest_positions_ok = False
        fence.rest_positions_dirty = True
        fence.observed_position_legs = set()
        fence.rest_orders_ok = False

    def _invalidate_rest_orders(self, venue: Venue) -> None:
        fence = self._fences[venue]
        fence.rest_orders_ok = False
        fence.rest_orders_dirty = True
        fence.observed_order_legs = set()
        fence.rest_positions_ok = False

    def _break_rest_pair(self, fence: _VenueFence) -> None:
        fence.rest_positions_ok = False
        fence.rest_orders_ok = False

    def _bound_for_venue(self, intent_id: str, venue: Venue) -> Optional[_BoundLeg]:
        for bound in self._legs_by_client.values():
            if bound.intent.intent_id == intent_id and bound.plan.venue is venue:
                return bound
        return None

    def _lookup_client(self, client_id: Optional[str]) -> Optional[_BoundLeg]:
        if not client_id:
            return None
        return self._legs_by_client.get(client_id)

    def _lookup_instrument(self, venue: Venue, instrument: Optional[str]) -> Optional[_BoundLeg]:
        if not instrument:
            return None
        return self._legs_by_instrument.get((venue, instrument))

    def _resolve_bound(
        self,
        *,
        venue: Venue,
        source: str,
        generation: int,
        client_id: Optional[str],
        instrument: Optional[str],
        issues: list[AdapterIssue],
    ) -> Optional[_BoundLeg]:
        by_client = self._lookup_client(client_id)
        by_inst = self._lookup_instrument(venue, instrument)
        if by_client is not None and by_client.plan.venue is not venue:
            issues.append(
                _issue(
                    "conflicting_identity",
                    venue=venue,
                    source=source,
                    generation=generation,
                )
            )
            return None
        if by_client is not None and by_inst is not None and by_client is not by_inst:
            issues.append(
                _issue(
                    "conflicting_identity",
                    venue=venue,
                    source=source,
                    generation=generation,
                    intent_id=by_client.intent.intent_id,
                    leg_id=by_client.plan.leg_id,
                )
            )
            return None
        if by_client is not None:
            if instrument and instrument != by_client.plan.instrument:
                issues.append(
                    _issue(
                        "conflicting_identity",
                        venue=venue,
                        source=source,
                        generation=generation,
                        intent_id=by_client.intent.intent_id,
                        leg_id=by_client.plan.leg_id,
                    )
                )
                return None
            return by_client
        if by_inst is not None and client_id:
            issues.append(
                _issue(
                    "unknown_correlation",
                    venue=venue,
                    source=source,
                    generation=generation,
                    intent_id=by_inst.intent.intent_id,
                    leg_id=by_inst.plan.leg_id,
                )
            )
            return None
        if by_inst is not None:
            return by_inst
        if client_id or instrument:
            issues.append(
                _issue(
                    "unknown_correlation",
                    venue=venue,
                    source=source,
                    generation=generation,
                )
            )
        return None

    def _adapt_ack(
        self,
        payload: Mapping[str, Any],
        *,
        venue: Venue,
        source: str,
        generation: int,
        receive_mono_ns: int,
        expected_client_id: Optional[str],
        events: list[ExecutionEvent],
        issues: list[AdapterIssue],
    ) -> None:
        if venue is Venue.BYBIT:
            client_id = _text(payload.get("reqId"))
            if client_id is None:
                issues.append(
                    _issue(
                        "unknown_correlation",
                        venue=venue,
                        source=source,
                        generation=generation,
                    )
                )
                return
            accepted = _bybit_ack_accepted(payload)
        else:
            client_id = _text(payload.get("id"))
            event_name = _text(payload.get("event")) or ""
            if event_name == "error" and client_id is None:
                if expected_client_id is None or expected_client_id not in self._legs_by_client:
                    issues.append(
                        _issue(
                            "id_less_ack_unbound",
                            venue=venue,
                            source=source,
                            generation=generation,
                        )
                    )
                    return
                client_id = expected_client_id
                accepted = False
            else:
                if client_id is None:
                    issues.append(
                        _issue(
                            "unknown_correlation",
                            venue=venue,
                            source=source,
                            generation=generation,
                        )
                    )
                    return
                accepted = _okx_ack_accepted(payload)
        bound = self._lookup_client(client_id)
        if bound is None or bound.plan.venue is not venue:
            issues.append(
                _issue(
                    "unknown_correlation",
                    venue=venue,
                    source=source,
                    generation=generation,
                )
            )
            return
        event_type = (
            ExecutionEventType.ACK_ACCEPTED if accepted else ExecutionEventType.ACK_REJECTED
        )
        payload_out: dict[str, Any] = {}
        if not accepted:
            payload_out["reason_code"] = "venue_rejected"
        event = self._make_event(
            intent=bound.intent,
            event_type=event_type,
            venue=venue,
            leg_id=bound.plan.leg_id,
            receive_mono_ns=receive_mono_ns,
            payload=payload_out,
            dedupe_key=f"{bound.intent.intent_id}|ack|{venue.value}|{event_type.value}",
        )
        if event is not None:
            events.append(event)

    def _adapt_order_execution(
        self,
        payload: Mapping[str, Any],
        *,
        venue: Venue,
        source: str,
        generation: int,
        receive_mono_ns: int,
        events: list[ExecutionEvent],
        issues: list[AdapterIssue],
    ) -> None:
        rows, row_error = _rows(payload, venue, source)
        if row_error is not None:
            issues.append(
                _issue(row_error, venue=venue, source=source, generation=generation)
            )
            return
        for row in rows:
            if not isinstance(row, Mapping):
                issues.append(
                    _issue("malformed_frame", venue=venue, source=source, generation=generation)
                )
                continue
            client_id = _client_id_from_row(row, venue)
            instrument = _instrument_from_row(row, venue, payload)
            bound = self._resolve_bound(
                venue=venue,
                source=source,
                generation=generation,
                client_id=client_id,
                instrument=instrument,
                issues=issues,
            )
            if bound is None:
                known = self._lookup_instrument(venue, instrument)
                if known is not None:
                    event = self._make_event(
                        intent=known.intent,
                        event_type=ExecutionEventType.UNKNOWN_CORRELATION,
                        venue=venue,
                        leg_id=known.plan.leg_id,
                        receive_mono_ns=receive_mono_ns,
                        payload={"reason_code": "unknown_correlation"},
                        dedupe_key=(
                            f"{known.intent.intent_id}|unk|{venue.value}|"
                            f"{known.plan.leg_id}|{generation}"
                        ),
                    )
                    if event is not None:
                        events.append(event)
                continue
            if client_id is None:
                event = self._make_event(
                    intent=bound.intent,
                    event_type=ExecutionEventType.UNKNOWN_CORRELATION,
                    venue=venue,
                    leg_id=bound.plan.leg_id,
                    receive_mono_ns=receive_mono_ns,
                    payload={"reason_code": "unknown_correlation"},
                    dedupe_key=(
                        f"{bound.intent.intent_id}|unk|{venue.value}|"
                        f"{bound.plan.leg_id}|{generation}"
                    ),
                )
                if event is not None:
                    events.append(event)
                continue
            qty, qty_issue, piece_hash = _row_fill_quantity(row, venue, source)
            if qty_issue == "missing_quantity" and piece_hash is None and qty is None:
                status = _order_status(row, venue, source)
                if status in _cancel_states(venue) or status in _reject_states(venue):
                    self._emit_cancel_or_reject(
                        bound,
                        venue,
                        source,
                        generation,
                        receive_mono_ns,
                        status,
                        events,
                    )
                    continue
                issues.append(
                    _issue(
                        "missing_quantity",
                        venue=venue,
                        source=source,
                        generation=generation,
                        intent_id=bound.intent.intent_id,
                        leg_id=bound.plan.leg_id,
                    )
                )
                continue
            if qty_issue is not None and qty is None:
                issues.append(
                    _issue(
                        qty_issue,
                        venue=venue,
                        source=source,
                        generation=generation,
                        intent_id=bound.intent.intent_id,
                        leg_id=bound.plan.leg_id,
                    )
                )
                continue
            if qty is None and piece_hash is not None:
                piece_qty, piece_err = _incremental_piece(row, venue)
                if piece_err is not None or piece_qty is None:
                    issues.append(
                        _issue(
                            piece_err or "missing_quantity",
                            venue=venue,
                            source=source,
                            generation=generation,
                            intent_id=bound.intent.intent_id,
                            leg_id=bound.plan.leg_id,
                        )
                    )
                    continue
                qty = self._accumulate_piece(bound, venue, piece_hash, piece_qty)
            if qty is None:
                issues.append(
                    _issue(
                        "missing_quantity",
                        venue=venue,
                        source=source,
                        generation=generation,
                        intent_id=bound.intent.intent_id,
                        leg_id=bound.plan.leg_id,
                    )
                )
                continue
            self._emit_fill(
                bound,
                venue,
                source,
                generation,
                receive_mono_ns,
                qty,
                row,
                events,
                issues,
            )
            status = _order_status(row, venue, source)
            if status in _cancel_states(venue) or status in _reject_states(venue):
                self._emit_cancel_or_reject(
                    bound,
                    venue,
                    source,
                    generation,
                    receive_mono_ns,
                    status,
                    events,
                )

    def _accumulate_piece(
        self,
        bound: _BoundLeg,
        venue: Venue,
        piece_hash: str,
        piece_qty: Decimal,
    ) -> Optional[Decimal]:
        key = (bound.intent.intent_id, venue)
        seen = self._fill_piece_hashes.setdefault(key, set())
        if piece_hash in seen:
            return self._fill_piece_qty.get(key, Decimal("0"))
        seen.add(piece_hash)
        current = self._fill_piece_qty.get(key, Decimal("0")) + piece_qty
        self._fill_piece_qty[key] = current
        last = self._last_fill.get(key, Decimal("0"))
        return current if current > last else current

    def _emit_fill(
        self,
        bound: _BoundLeg,
        venue: Venue,
        source: str,
        generation: int,
        receive_mono_ns: int,
        qty: Decimal,
        row: Mapping[str, Any],
        events: list[ExecutionEvent],
        issues: list[AdapterIssue],
    ) -> None:
        key = (bound.intent.intent_id, venue)
        last = self._last_fill.get(key)
        if last is not None and qty < last:
            issues.append(
                _issue(
                    "stale_quantity",
                    venue=venue,
                    source=source,
                    generation=generation,
                    intent_id=bound.intent.intent_id,
                    leg_id=bound.plan.leg_id,
                )
            )
            return
        if last is not None and qty == last:
            return
        if qty <= 0:
            issues.append(
                _issue(
                    "malformed_quantity",
                    venue=venue,
                    source=source,
                    generation=generation,
                    intent_id=bound.intent.intent_id,
                    leg_id=bound.plan.leg_id,
                )
            )
            return
        terminal = _is_terminal_filled(row, venue, source)
        if terminal or qty >= bound.plan.quantity:
            event_type = ExecutionEventType.FILL
        else:
            event_type = ExecutionEventType.PARTIAL_FILL
        event = self._make_event(
            intent=bound.intent,
            event_type=event_type,
            venue=venue,
            leg_id=bound.plan.leg_id,
            receive_mono_ns=receive_mono_ns,
            payload={"quantity": decimal_to_canonical(qty)},
            dedupe_key=(
                f"{bound.intent.intent_id}|fill|{venue.value}|"
                f"{decimal_to_canonical(qty)}"
            ),
        )
        if event is not None:
            events.append(event)
            self._last_fill[key] = qty

    def _emit_cancel_or_reject(
        self,
        bound: _BoundLeg,
        venue: Venue,
        source: str,
        generation: int,
        receive_mono_ns: int,
        status: str,
        events: list[ExecutionEvent],
    ) -> None:
        del source, generation
        if status in _reject_states(venue):
            event_type = ExecutionEventType.ACK_REJECTED
            payload: dict[str, Any] = {"reason_code": "venue_rejected"}
            dedupe = f"{bound.intent.intent_id}|ack|{venue.value}|{event_type.value}"
        else:
            event_type = ExecutionEventType.CANCEL_ACK
            payload = {}
            dedupe = f"{bound.intent.intent_id}|cancel|{venue.value}"
        event = self._make_event(
            intent=bound.intent,
            event_type=event_type,
            venue=venue,
            leg_id=bound.plan.leg_id,
            receive_mono_ns=receive_mono_ns,
            payload=payload,
            dedupe_key=dedupe,
        )
        if event is not None:
            events.append(event)

    def _adapt_positions(
        self,
        payload: Mapping[str, Any],
        *,
        venue: Venue,
        source: str,
        generation: int,
        receive_mono_ns: int,
        snapshot_complete: bool,
        is_rest: bool,
        events: list[ExecutionEvent],
        issues: list[AdapterIssue],
    ) -> None:
        rows, row_error = _rows(payload, venue, source)
        if row_error is not None:
            issues.append(
                _issue(row_error, venue=venue, source=source, generation=generation)
            )
            if is_rest:
                self._invalidate_rest_positions(venue)
            return
        if is_rest:
            rest_error = _complete_rest_rows_invalid(rows, venue, payload)
            if rest_error is not None:
                issues.append(
                    _issue(rest_error, venue=venue, source=source, generation=generation)
                )
                self._invalidate_rest_positions(venue)
                return
        grouped: dict[tuple[Venue, str], list[Mapping[str, Any]]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                issues.append(
                    _issue("malformed_frame", venue=venue, source=source, generation=generation)
                )
                if is_rest:
                    self._invalidate_rest_positions(venue)
                    return
                continue
            instrument = _instrument_from_row(row, venue, payload)
            if instrument is None:
                if is_rest:
                    issues.append(
                        _issue(
                            "malformed_frame",
                            venue=venue,
                            source=source,
                            generation=generation,
                        )
                    )
                    self._invalidate_rest_positions(venue)
                    return
                continue
            grouped.setdefault((venue, instrument), []).append(row)
        seen_registered: set[str] = set()
        for key, group in grouped.items():
            bound = self._legs_by_instrument.get(key)
            if bound is None:
                continue
            seen_registered.add(bound.plan.leg_id)
            qty = _project_position(group, bound.plan, venue)
            if qty is None:
                issues.append(
                    _issue(
                        "conflicting_position",
                        venue=venue,
                        source=source,
                        generation=generation,
                        intent_id=bound.intent.intent_id,
                        leg_id=bound.plan.leg_id,
                    )
                )
                if is_rest:
                    self._fences[venue].rest_positions_dirty = True
                continue
            event = self._make_event(
                intent=bound.intent,
                event_type=ExecutionEventType.POSITION_OBSERVED,
                venue=venue,
                leg_id=bound.plan.leg_id,
                receive_mono_ns=receive_mono_ns,
                payload={"quantity": decimal_to_canonical(qty)},
                dedupe_key=(
                    f"{bound.intent.intent_id}|pos|{venue.value}|"
                    f"{generation}|{decimal_to_canonical(qty)}"
                ),
            )
            if event is not None:
                events.append(event)
            # Validated explicit REST qty is accounted even when a same-generation
            # same-qty replay suppresses a second POSITION_OBSERVED. Conflicting
            # or malformed rows never reach this point.
            if is_rest:
                self._fences[venue].observed_position_legs.add(bound.plan.leg_id)
        if is_rest and snapshot_complete and not self._fences[venue].rest_positions_dirty:
            for bound in self._registered_on_venue(venue):
                if bound.plan.leg_id in seen_registered:
                    continue
                event = self._make_event(
                    intent=bound.intent,
                    event_type=ExecutionEventType.POSITION_OBSERVED,
                    venue=venue,
                    leg_id=bound.plan.leg_id,
                    receive_mono_ns=receive_mono_ns,
                    payload={"quantity": "0"},
                    dedupe_key=f"{bound.intent.intent_id}|pos|{venue.value}|{generation}|0",
                )
                if event is not None:
                    events.append(event)
                self._fences[venue].observed_position_legs.add(bound.plan.leg_id)

    def _adapt_rest_positions(
        self,
        payload: Mapping[str, Any],
        *,
        venue: Venue,
        generation: int,
        receive_mono_ns: int,
        snapshot_complete: bool,
        events: list[ExecutionEvent],
        issues: list[AdapterIssue],
    ) -> None:
        fence = self._fences[venue]
        self._reset_rest_positions_half(fence)
        if generation != fence.expected:
            self._break_rest_pair(fence)
            issues.append(
                _issue(
                    "stale_generation",
                    venue=venue,
                    source="rest_positions",
                    generation=generation,
                )
            )
            return
        status = _rest_status(payload, venue)
        if status != "ok":
            self._break_rest_pair(fence)
            issues.append(
                _issue(status, venue=venue, source="rest_positions", generation=generation)
            )
            return
        if _rest_has_more(payload, venue) or not snapshot_complete:
            self._break_rest_pair(fence)
            reason = "paginated_snapshot" if _rest_has_more(payload, venue) else "incomplete_snapshot"
            issues.append(
                _issue(reason, venue=venue, source="rest_positions", generation=generation)
            )
            return
        self._adapt_positions(
            payload,
            venue=venue,
            source="rest_positions",
            generation=generation,
            receive_mono_ns=receive_mono_ns,
            snapshot_complete=True,
            is_rest=True,
            events=events,
            issues=issues,
        )
        if fence.rest_positions_dirty:
            self._break_rest_pair(fence)
        else:
            fence.rest_positions_ok = True

    def _adapt_rest_open_orders(
        self,
        payload: Mapping[str, Any],
        *,
        venue: Venue,
        generation: int,
        receive_mono_ns: int,
        snapshot_complete: bool,
        events: list[ExecutionEvent],
        issues: list[AdapterIssue],
    ) -> None:
        fence = self._fences[venue]
        self._reset_rest_orders_half(fence)
        if generation != fence.expected:
            self._break_rest_pair(fence)
            issues.append(
                _issue(
                    "stale_generation",
                    venue=venue,
                    source="rest_open_orders",
                    generation=generation,
                )
            )
            return
        status = _rest_status(payload, venue)
        if status != "ok":
            self._break_rest_pair(fence)
            issues.append(
                _issue(status, venue=venue, source="rest_open_orders", generation=generation)
            )
            return
        if _rest_has_more(payload, venue) or not snapshot_complete:
            self._break_rest_pair(fence)
            reason = "paginated_snapshot" if _rest_has_more(payload, venue) else "incomplete_snapshot"
            issues.append(
                _issue(reason, venue=venue, source="rest_open_orders", generation=generation)
            )
            return
        rows, row_error = _rows(payload, venue, "rest_open_orders")
        if row_error is not None:
            self._break_rest_pair(fence)
            issues.append(
                _issue(row_error, venue=venue, source="rest_open_orders", generation=generation)
            )
            return
        rest_error = _complete_rest_rows_invalid(rows, venue, payload)
        if rest_error is not None:
            self._break_rest_pair(fence)
            issues.append(
                _issue(
                    rest_error,
                    venue=venue,
                    source="rest_open_orders",
                    generation=generation,
                )
            )
            fence.rest_orders_dirty = True
            return
        by_inst: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                issues.append(
                    _issue(
                        "malformed_frame",
                        venue=venue,
                        source="rest_open_orders",
                        generation=generation,
                    )
                )
                self._invalidate_rest_orders(venue)
                return
            instrument = _instrument_from_row(row, venue, payload)
            if instrument is None:
                issues.append(
                    _issue(
                        "malformed_frame",
                        venue=venue,
                        source="rest_open_orders",
                        generation=generation,
                    )
                )
                self._invalidate_rest_orders(venue)
                return
            by_inst.setdefault(instrument, []).append(row)
        for bound in self._registered_on_venue(venue):
            group = by_inst.get(bound.plan.instrument, [])
            unknown = False
            for row in group:
                client_id = _client_id_from_row(row, venue)
                matched = self._lookup_client(client_id)
                if (
                    matched is None
                    or matched.plan.leg_id != bound.plan.leg_id
                    or matched.plan.venue is not venue
                ):
                    unknown = True
                    issues.append(
                        _issue(
                            "unknown_correlation",
                            venue=venue,
                            source="rest_open_orders",
                            generation=generation,
                            intent_id=bound.intent.intent_id,
                            leg_id=bound.plan.leg_id,
                        )
                    )
                    event = self._make_event(
                        intent=bound.intent,
                        event_type=ExecutionEventType.UNKNOWN_CORRELATION,
                        venue=venue,
                        leg_id=bound.plan.leg_id,
                        receive_mono_ns=receive_mono_ns,
                        payload={"reason_code": "unknown_correlation"},
                        dedupe_key=(
                            f"{bound.intent.intent_id}|unk|{venue.value}|"
                            f"{bound.plan.leg_id}|{generation}"
                        ),
                    )
                    if event is not None:
                        events.append(event)
            if unknown:
                fence.rest_orders_dirty = True
            event = self._make_event(
                intent=bound.intent,
                event_type=ExecutionEventType.OPEN_ORDERS_OBSERVED,
                venue=venue,
                leg_id=bound.plan.leg_id,
                receive_mono_ns=receive_mono_ns,
                payload={"open_order_count": len(group)},
                dedupe_key=(
                    f"{bound.intent.intent_id}|oo|{venue.value}|{generation}|{len(group)}"
                ),
            )
            if event is not None:
                events.append(event)
            fence.observed_order_legs.add(bound.plan.leg_id)
        fence.rest_orders_ok = True

    def _registered_on_venue(self, venue: Venue) -> list[_BoundLeg]:
        out: list[_BoundLeg] = []
        seen: set[str] = set()
        for bound in self._legs_by_client.values():
            if bound.plan.venue is venue and bound.plan.leg_id not in seen:
                out.append(bound)
                seen.add(bound.plan.leg_id)
        return out

    def _maybe_emit_rest_recon(
        self,
        venue: Venue,
        generation: int,
        receive_mono_ns: int,
        events: list[ExecutionEvent],
        issues: list[AdapterIssue],
    ) -> None:
        del issues
        fence = self._fences[venue]
        if not fence.rest_positions_ok or not fence.rest_orders_ok:
            return
        registered = self._registered_on_venue(venue)
        if not registered:
            return
        needed = {bound.plan.leg_id for bound in registered}
        if fence.observed_position_legs != needed or fence.observed_order_legs != needed:
            return
        matched = not (fence.rest_positions_dirty or fence.rest_orders_dirty)
        for bound in registered:
            event = self._make_event(
                intent=bound.intent,
                event_type=ExecutionEventType.RECONCILIATION,
                venue=venue,
                leg_id=bound.plan.leg_id,
                receive_mono_ns=receive_mono_ns,
                payload={"matched": matched},
                dedupe_key=(
                    f"{bound.intent.intent_id}|recon|{venue.value}|"
                    f"{generation}|{int(matched)}"
                ),
            )
            if event is not None:
                events.append(event)
        if matched:
            fence.blocked = False

    def _make_event(
        self,
        *,
        intent: TradeIntent,
        event_type: ExecutionEventType,
        venue: Optional[Venue],
        leg_id: Optional[str],
        receive_mono_ns: int,
        payload: Mapping[str, Any],
        dedupe_key: str,
    ) -> Optional[ExecutionEvent]:
        if dedupe_key in self._emitted_keys:
            return None
        rec = self._seq.get(intent.intent_id)
        if rec is None:
            rec = _IntentSeq(last_sequence=self._seq_seed.get(intent.intent_id, 0), last_monotonic_ns=self._mono_seed)
            self._seq[intent.intent_id] = rec
        sequence = rec.last_sequence + 1
        monotonic_ns = receive_mono_ns
        if monotonic_ns < rec.last_monotonic_ns:
            monotonic_ns = rec.last_monotonic_ns
        event_id = _event_id(
            intent.intent_id,
            event_type.value,
            None if venue is None else venue.value,
            leg_id,
            sequence,
            dedupe_key,
        )
        try:
            event = ExecutionEvent(
                schema_version=CONTRACT_SCHEMA_VERSION,
                event_id=event_id,
                event_type=event_type,
                intent_id=intent.intent_id,
                run_id=intent.run_id,
                sequence=sequence,
                monotonic_ns=monotonic_ns,
                venue=venue,
                leg_id=leg_id,
                payload=payload,
            )
        except ContractValidationError:
            return None
        self._emitted_keys.add(dedupe_key)
        rec.last_sequence = sequence
        rec.last_monotonic_ns = monotonic_ns
        return event


def _new_fence(expected: int) -> _VenueFence:
    return _VenueFence(
        expected=expected,
        blocked=False,
        mismatch_emitted=set(),
        rest_positions_ok=False,
        rest_orders_ok=False,
        rest_positions_dirty=False,
        rest_orders_dirty=False,
        observed_position_legs=set(),
        observed_order_legs=set(),
    )


def _batch(events: list[ExecutionEvent], issues: list[AdapterIssue]) -> AdapterBatch:
    return AdapterBatch(
        schema_version=SCHEMA_VERSION,
        events=tuple(events),
        issues=tuple(issues),
    )


def _issue(
    reason_code: str,
    *,
    venue: Optional[Venue] = None,
    source: Optional[str] = None,
    generation: Optional[int] = None,
    intent_id: Optional[str] = None,
    leg_id: Optional[str] = None,
) -> AdapterIssue:
    if reason_code not in _ISSUE_REASON_CODES:
        reason_code = "malformed_frame"
    return AdapterIssue(
        schema_version=SCHEMA_VERSION,
        reason_code=reason_code,
        venue=venue,
        source=source,
        generation=generation,
        intent_id=intent_id,
        leg_id=leg_id,
    )


def _coerce_venue(venue: object) -> Venue:
    if isinstance(venue, Venue):
        return venue
    try:
        return Venue(str(venue))
    except ValueError as exc:
        raise AdapterError("invalid_registration") from exc


def _normalize_source(source: object) -> Optional[str]:
    if isinstance(source, AdapterSource):
        return source.value
    if not isinstance(source, str):
        return None
    return _SOURCE_ALIASES.get(source.strip().lower())


def _text(value: object) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, Decimal)):
        text = str(value).strip()
        return text or None
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _bybit_ack_accepted(payload: Mapping[str, Any]) -> bool:
    ret = payload.get("retCode")
    if ret is not None and ret not in (0, "0"):
        return False
    if ret in (0, "0"):
        return True
    return payload.get("success") is True


def _okx_ack_accepted(payload: Mapping[str, Any]) -> bool:
    top_ok = str(payload.get("code", "")) == "0"
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows:
        return top_ok
    accepted = top_ok
    for row in rows:
        if not isinstance(row, Mapping):
            return False
        if "sCode" in row and str(row.get("sCode")) != "0":
            return False
    return accepted


def _complete_rest_rows_invalid(
    rows: Sequence[Any],
    venue: Venue,
    payload: Mapping[str, Any],
) -> Optional[str]:
    for row in rows:
        if not isinstance(row, Mapping) or isinstance(row, (str, bytes)):
            return "malformed_frame"
        if _instrument_from_row(row, venue, payload) is None:
            return "malformed_frame"
    return None


def _rows(
    payload: Mapping[str, Any], venue: Venue, source: str
) -> tuple[list[Any], Optional[str]]:
    if source in _REST_SOURCES and venue is Venue.BYBIT:
        result = payload.get("result")
        if result is None:
            return [], None
        if not isinstance(result, Mapping):
            return [], "malformed_frame"
        rows = result.get("list")
        if rows is None:
            return [], None
        if not isinstance(rows, list):
            return [], "malformed_frame"
        return rows, None
    rows = payload.get("data")
    if rows is None and source in _REST_SOURCES:
        return [], None
    if rows is None:
        return [], None
    if not isinstance(rows, list):
        return [], "malformed_frame"
    return rows, None


def _rest_status(payload: Mapping[str, Any], venue: Venue) -> str:
    if venue is Venue.BYBIT:
        if "retCode" not in payload:
            return "malformed_frame"
        if payload.get("retCode") not in (0, "0"):
            return "snapshot_failed"
        result = payload.get("result")
        if result is None:
            return "malformed_frame"
        if not isinstance(result, Mapping):
            return "malformed_frame"
        if "list" in result and not isinstance(result.get("list"), list):
            return "malformed_frame"
        return "ok"
    if "code" not in payload:
        return "malformed_frame"
    if str(payload.get("code")) != "0":
        return "snapshot_failed"
    if "data" in payload and not isinstance(payload.get("data"), list):
        return "malformed_frame"
    return "ok"


def _rest_has_more(payload: Mapping[str, Any], venue: Venue) -> bool:
    if venue is Venue.BYBIT:
        result = payload.get("result")
        if not isinstance(result, Mapping):
            return False
        cursor = result.get("nextPageCursor")
        return isinstance(cursor, str) and bool(cursor.strip())
    return False


def _client_id_from_row(row: Mapping[str, Any], venue: Venue) -> Optional[str]:
    if venue is Venue.BYBIT:
        return _text(row.get("orderLinkId"))
    return _text(row.get("clOrdId"))


def _instrument_from_row(
    row: Mapping[str, Any], venue: Venue, payload: Mapping[str, Any]
) -> Optional[str]:
    if venue is Venue.BYBIT:
        return _text(row.get("symbol"))
    inst = _text(row.get("instId"))
    if inst is not None:
        return inst
    arg = payload.get("arg")
    if isinstance(arg, Mapping):
        return _text(arg.get("instId"))
    return None


def _order_status(row: Mapping[str, Any], venue: Venue, source: str) -> str:
    if venue is Venue.BYBIT:
        raw = row.get("orderStatus")
        if raw is None and source == "execution":
            raw = row.get("execType")
        return str(raw or "").strip().lower()
    raw = row.get("state")
    if raw is None:
        return ""
    return str(raw).strip().lower()


def _cancel_states(venue: Venue) -> frozenset[str]:
    return _BYBIT_CANCELLED if venue is Venue.BYBIT else _OKX_CANCELLED


def _reject_states(venue: Venue) -> frozenset[str]:
    return _BYBIT_REJECTED if venue is Venue.BYBIT else _OKX_REJECTED


def _is_terminal_filled(row: Mapping[str, Any], venue: Venue, source: str) -> bool:
    status = _order_status(row, venue, source)
    if venue is Venue.BYBIT:
        return status in _BYBIT_TERMINAL_FILLED
    return status in _OKX_TERMINAL_FILLED


def _parse_qty(value: object, *, field: str) -> tuple[Optional[Decimal], Optional[str]]:
    del field
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, "malformed_quantity"
    if isinstance(value, float):
        return None, "float_rejected"
    try:
        parsed = canonical_decimal(value, field="quantity", allow_zero=True)
    except ContractValidationError as exc:
        text = str(exc)
        if "must be >= 0" in text:
            return None, "negative_quantity"
        return None, "malformed_quantity"
    return parsed, None


def _row_fill_quantity(
    row: Mapping[str, Any], venue: Venue, source: str
) -> tuple[Optional[Decimal], Optional[str], Optional[str]]:
    if venue is Venue.BYBIT:
        raw = row.get("cumExecQty")
        if raw is None:
            raw = row.get("cumFilledQty")
        qty, err = _parse_qty(raw, field="cumExecQty")
        if err is not None:
            return None, err, None
        if qty is not None:
            return qty, None, None
        piece_id = _text(row.get("execId"))
        if source == "execution" and piece_id is not None:
            return None, None, _hash_source_id(venue, piece_id)
        return None, "missing_quantity", None
    raw = row.get("accFillSz")
    qty, err = _parse_qty(raw, field="accFillSz")
    if err is not None:
        return None, err, None
    if qty is not None:
        return qty, None, None
    piece_id = _text(row.get("tradeId")) or _text(row.get("billId")) or _text(row.get("fillId"))
    if source == "execution" and piece_id is not None:
        return None, None, _hash_source_id(venue, piece_id)
    return None, "missing_quantity", None


def _incremental_piece(
    row: Mapping[str, Any], venue: Venue
) -> tuple[Optional[Decimal], Optional[str]]:
    raw = row.get("execQty") if venue is Venue.BYBIT else row.get("fillSz")
    qty, err = _parse_qty(raw, field="piece")
    if err is not None:
        return None, err
    if qty is None:
        return None, "missing_quantity"
    if qty <= 0:
        return None, "malformed_quantity"
    return qty, None


def _hash_source_id(venue: Venue, source_id: str) -> str:
    digest = hashlib.sha256(f"{venue.value}:{source_id}".encode("utf-8")).hexdigest()
    return digest


def _project_position(
    rows: Sequence[Mapping[str, Any]], plan: LegPlan, venue: Venue
) -> Optional[Decimal]:
    sides: dict[str, Decimal] = {}
    saw_explicit = False
    for row in rows:
        qty, side, err = _position_row(row, venue)
        if err is not None:
            return None
        if qty is None:
            continue
        saw_explicit = True
        sides[side] = sides.get(side, Decimal("0")) + qty
    if not saw_explicit:
        return None
    nonzero = {side: qty for side, qty in sides.items() if side != "flat" and qty > 0}
    if len(nonzero) > 1:
        return None
    wanted = "long" if plan.side == "buy" else "short"
    if not nonzero:
        return Decimal("0")
    side, qty = next(iter(nonzero.items()))
    if side != wanted:
        return None
    return qty


def _position_row(
    row: Mapping[str, Any], venue: Venue
) -> tuple[Optional[Decimal], str, Optional[str]]:
    if venue is Venue.BYBIT:
        qty, err = _parse_qty(row.get("size"), field="size")
        if err is not None:
            return None, "flat", err
        if qty is None:
            return None, "flat", None
        idx = str(row.get("positionIdx", "0"))
        side_raw = str(row.get("side") or "").strip().lower()
        if idx == "1":
            side = "long"
        elif idx == "2":
            side = "short"
        elif side_raw in _LONG_SIDES:
            side = "long"
        elif side_raw in _SHORT_SIDES:
            side = "short"
        else:
            side = "flat"
        if qty == 0:
            return Decimal("0"), "flat", None
        return qty, side, None
    raw = row.get("pos")
    if isinstance(raw, bool):
        return None, "flat", "malformed_quantity"
    if isinstance(raw, float):
        return None, "flat", "float_rejected"
    if raw is None:
        return None, "flat", None
    if isinstance(raw, int) and raw < 0:
        qty, err = _parse_qty(-raw, field="pos")
        if err is not None or qty is None:
            return None, "flat", err or "malformed_quantity"
        return qty, "short", None
    if isinstance(raw, str) and raw.strip().startswith("-"):
        abs_raw = raw.strip()[1:]
        qty, err = _parse_qty(abs_raw, field="pos")
        if err is not None or qty is None:
            return None, "flat", err or "malformed_quantity"
        return qty, "short", None
    qty, err = _parse_qty(raw, field="pos")
    if err is not None:
        return None, "flat", err
    if qty is None:
        return None, "flat", None
    pos_side = str(row.get("posSide") or "net").strip().lower()
    if qty == 0:
        return Decimal("0"), "flat", None
    if pos_side in _SHORT_SIDES:
        return qty, "short", None
    return qty, "long", None


def _event_id(
    intent_id: str,
    event_type: str,
    venue: Optional[str],
    leg_id: Optional[str],
    sequence: int,
    dedupe_key: str,
) -> str:
    digest = hashlib.sha256(
        f"{intent_id}|{event_type}|{venue or '-'}|{leg_id or '-'}|{sequence}|{dedupe_key}".encode(
            "utf-8"
        )
    ).hexdigest()
    return "e" + digest[:31]


def _norm_key(name: object) -> str:
    return str(name).strip().lower().replace("-", "_")


def _assert_public(node: object, *, path: str = "$") -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            nk = _norm_key(key)
            if nk in _FORBIDDEN_PUBLIC_KEYS:
                raise AdapterError("invalid_registration")
            _assert_public(value, path=f"{path}.{nk}")
        return
    if isinstance(node, (list, tuple)):
        for i, item in enumerate(node):
            _assert_public(item, path=f"{path}[{i}]")


__all__ = [
    "SCHEMA_VERSION",
    "AdapterBatch",
    "AdapterError",
    "AdapterIssue",
    "AdapterSource",
    "PrivateEventAdapter",
]
