"""Immutable execution v2 contracts. Stdlib only. No I/O."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional

SCHEMA_VERSION = "bbot.execution.v2"

OKX_CLIENT_ID_MAX_LEN = 32
BYBIT_CLIENT_ID_MAX_LEN = 36
CLIENT_ID_MIN_HEX = 12

_OKX_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9]{1,32}$")
_BYBIT_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,36}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HEX_RE = re.compile(r"^[0-9a-f]+$")
# H is an exchange-listed member of the frozen Gear 2.2 thirty-coin universe.
_COIN_RE = re.compile(r"^(?:H|[A-Z0-9]{2,16})$")
_INSTRUMENT_RE = re.compile(r"^[A-Z0-9][A-Z0-9._:-]{1,63}$")

_FORBIDDEN_KEYS = frozenset(
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

_PAYLOAD_ALLOWED_KEYS = frozenset(
    {
        "action",
        "ack_status",
        "audit_mode",
        "canary_stage",
        "client_id",
        "coin",
        "confirmed_unfilled",
        "filled_quantity",
        "halt",
        "instrument",
        "lot_tolerance",
        "matched",
        "notional_usdt",
        "open_order_count",
        "open_orders_flat",
        "pause",
        "planned_quantity",
        "policy_version",
        "position_quantity",
        "positions_flat",
        "prewrite_passed",
        "quantity",
        "reason_code",
        "reduce_only",
        "risk_policy_revision",
        "side",
        "signal_snapshot_ref",
        "spread_direction",
        "stream_generation",
        "working",
    }
)

_ACK_STATUSES = frozenset({"none", "accepted", "rejected", "timeout"})
_SIDES = frozenset({"buy", "sell"})
_REASON_CODES = frozenset(
    {
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
    }
)


class ContractValidationError(ValueError):
    """Fail-closed contract/schema violation. No raw venue payloads."""


class IntentAction(str, Enum):
    OPEN = "open"
    CLOSE = "close"


class SpreadDirection(str, Enum):
    LONG = "long"
    SHORT = "short"


class Venue(str, Enum):
    OKX = "okx"
    BYBIT = "bybit"


class LegStatus(str, Enum):
    NEW = "NEW"
    SENT = "SENT"
    ACK_ACCEPTED = "ACK_ACCEPTED"
    ACK_REJECTED = "ACK_REJECTED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"
    CANCELLED = "CANCELLED"


class SpreadStatus(str, Enum):
    IDLE = "IDLE"
    ARMED = "ARMED"
    DISPATCHING = "DISPATCHING"
    EXPOSURE_UNKNOWN = "EXPOSURE_UNKNOWN"
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    FLAT = "FLAT"
    RECOVERING = "RECOVERING"
    HALTED = "HALTED"


class ExecutionEventType(str, Enum):
    INTENT_ACCEPTED = "intent_accepted"
    INTENT_REJECTED = "intent_rejected"
    REQUEST_SENT = "request_sent"
    ACK_ACCEPTED = "ack_accepted"
    ACK_REJECTED = "ack_rejected"
    ACK_TIMEOUT = "ack_timeout"
    PARTIAL_FILL = "partial_fill"
    FILL = "fill"
    CANCEL_REQUESTED = "cancel_requested"
    CANCEL_ACK = "cancel_ack"
    POSITION_OBSERVED = "position_observed"
    OPEN_ORDERS_OBSERVED = "open_orders_observed"
    RECONCILIATION = "reconciliation"
    FLATNESS_PROVEN = "flatness_proven"
    PAUSE = "pause"
    FAULT = "fault"
    STREAM_GENERATION_MISMATCH = "stream_generation_mismatch"
    UNKNOWN_CORRELATION = "unknown_correlation"


def _norm_key(name: object) -> str:
    return str(name).strip().lower().replace("-", "_")


def _assert_no_forbidden(node: object, *, path: str = "$") -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            nk = _norm_key(key)
            if nk in _FORBIDDEN_KEYS:
                raise ContractValidationError(f"forbidden field {nk} at {path}")
            _assert_no_forbidden(value, path=f"{path}.{nk}")
        return
    if isinstance(node, (list, tuple)):
        for i, item in enumerate(node):
            _assert_no_forbidden(item, path=f"{path}[{i}]")


def _require_mapping(raw: object, *, label: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or isinstance(raw, (str, bytes)):
        raise ContractValidationError(f"{label} must be an object")
    data = dict(raw)
    _assert_no_forbidden(data, path=label)
    return data


def _require_exact_keys(data: Mapping[str, Any], required: set[str], *, label: str) -> None:
    got = set(data.keys())
    missing = required - got
    extra = got - required
    if missing:
        raise ContractValidationError(
            f"{label} missing fields: {', '.join(sorted(missing))}"
        )
    if extra:
        raise ContractValidationError(
            f"{label} unknown fields: {', '.join(sorted(extra))}"
        )


def _require_str(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractValidationError(f"{field} must be a non-empty string")
    if not _ID_RE.fullmatch(value) and field not in {
        "instrument",
        "coin",
        "signal_snapshot_ref",
        "canary_stage",
        "policy_version",
        "risk_policy_revision",
        "client_id",
        "reason_code",
        "side",
    }:
        raise ContractValidationError(f"{field} is not a venue-safe opaque id")
    return value


def _require_enum(value: object, enum_cls: type[Enum], *, field: str) -> Any:
    if isinstance(value, enum_cls):
        return value
    if not isinstance(value, str):
        raise ContractValidationError(f"{field} must be a string enum")
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise ContractValidationError(f"{field} has unknown value") from exc


def canonical_decimal(value: object, *, field: str, allow_zero: bool = True) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ContractValidationError(f"{field} must be a canonical decimal string")
    if isinstance(value, int):
        text = format(value, "d")
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise ContractValidationError(f"{field} must be finite")
        text = format(value.normalize(), "f")
    elif isinstance(value, str):
        text = value
    else:
        raise ContractValidationError(f"{field} must be a canonical decimal string")
    if not text or "e" in text.lower() or text.strip() != text:
        raise ContractValidationError(f"{field} is not a canonical decimal string")
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ContractValidationError(f"{field} is not a canonical decimal string") from exc
    if not parsed.is_finite():
        raise ContractValidationError(f"{field} must be finite")
    if parsed < 0:
        raise ContractValidationError(f"{field} must be >= 0")
    if parsed == 0 and not allow_zero:
        raise ContractValidationError(f"{field} must be > 0")
    return parsed.normalize()


def decimal_to_canonical(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ContractValidationError("decimal must be finite")
    return format(value.normalize(), "f")


def _require_int(value: object, *, field: str, min_value: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractValidationError(f"{field} must be an integer")
    if value < min_value:
        raise ContractValidationError(f"{field} must be >= {min_value}")
    return value


def _require_bool(value: object, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise ContractValidationError(f"{field} must be a boolean")
    return value


def _require_schema(value: object) -> str:
    if value != SCHEMA_VERSION:
        raise ContractValidationError("unsupported schema_version")
    return SCHEMA_VERSION


def _intent_digest(intent_id: str) -> str:
    """Collision-resistant hex tail from the full intent_id, before venue truncation."""
    return hashlib.sha256(intent_id.encode("utf-8")).hexdigest()


def derive_client_id(
    intent_id: str,
    venue: Venue,
    *,
    reduce_only: bool = False,
) -> str:
    """Venue-safe client id from intent_id. Never a venue order id.

    The hex tail is SHA-256 of the full ``intent_id`` (not stripped UUID hex).
    Truncation to venue caps happens only after that digest, so OKX's 32-char
    limit cannot collide two UUIDs that differ only in the dropped nibble.

    Shape matches the 2026-09-08 canary prefixes: OKX ``clOrdId`` alphanumeric
    <=32 (``o`` / ``fo`` + hex); Bybit ``orderLinkId`` <=36 (``b`` / ``fb`` +
    the same digest truncated to the venue cap). Exact legacy hex content is
    not required; this layer is not live.
    """
    if not isinstance(intent_id, str) or not intent_id:
        raise ContractValidationError("intent_id must be a non-empty string")
    venue_e = venue if isinstance(venue, Venue) else _require_enum(venue, Venue, field="venue")
    digest = _intent_digest(intent_id)
    if len(digest) < CLIENT_ID_MIN_HEX:
        raise ContractValidationError("intent digest too short for client id")
    if reduce_only:
        prefix = "fo" if venue_e is Venue.OKX else "fb"
    else:
        prefix = "o" if venue_e is Venue.OKX else "b"
    max_len = OKX_CLIENT_ID_MAX_LEN if venue_e is Venue.OKX else BYBIT_CLIENT_ID_MAX_LEN
    out = prefix + digest[: max_len - len(prefix)]
    validate_client_id(out, venue_e)
    return out


def validate_client_id(client_id: str, venue: Venue) -> str:
    if not isinstance(client_id, str) or not client_id:
        raise ContractValidationError("client_id must be a non-empty string")
    if venue is Venue.OKX:
        if not _OKX_CLIENT_ID_RE.fullmatch(client_id):
            raise ContractValidationError("OKX client_id must be alphanumeric <=32")
        if len(client_id) > OKX_CLIENT_ID_MAX_LEN:
            raise ContractValidationError("OKX client_id exceeds 32")
    elif venue is Venue.BYBIT:
        if not _BYBIT_CLIENT_ID_RE.fullmatch(client_id):
            raise ContractValidationError("Bybit client_id charset/length invalid")
        if len(client_id) > BYBIT_CLIENT_ID_MAX_LEN:
            raise ContractValidationError("Bybit client_id exceeds 36")
    else:
        raise ContractValidationError("unknown venue")
    return client_id


def _freeze_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = _require_mapping(payload, label="payload")
    extra = set(data) - _PAYLOAD_ALLOWED_KEYS
    if extra:
        raise ContractValidationError(
            f"payload unknown fields: {', '.join(sorted(extra))}"
        )
    frozen: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, Mapping) or isinstance(value, (list, tuple, bytes)):
            raise ContractValidationError(f"payload.{key} must be a scalar")
        if isinstance(value, float):
            raise ContractValidationError(f"payload.{key} must not be a float")
        if key in {
            "quantity",
            "filled_quantity",
            "planned_quantity",
            "position_quantity",
            "lot_tolerance",
            "notional_usdt",
        }:
            frozen[key] = decimal_to_canonical(
                canonical_decimal(
                    value,
                    field=f"payload.{key}",
                    allow_zero=key != "notional_usdt" and key != "planned_quantity",
                )
            )
        elif key == "reason_code":
            if value not in _REASON_CODES:
                raise ContractValidationError("payload.reason_code not allowlisted")
            frozen[key] = value
        elif key == "side":
            if value not in _SIDES:
                raise ContractValidationError("payload.side must be buy or sell")
            frozen[key] = value
        elif key == "ack_status":
            if value not in _ACK_STATUSES:
                raise ContractValidationError("payload.ack_status invalid")
            frozen[key] = value
        elif key in {
            "reduce_only",
            "pause",
            "halt",
            "matched",
            "positions_flat",
            "open_orders_flat",
            "working",
            "confirmed_unfilled",
            "prewrite_passed",
        }:
            frozen[key] = _require_bool(value, field=f"payload.{key}")
        elif key in {"open_order_count", "stream_generation"}:
            frozen[key] = _require_int(value, field=f"payload.{key}", min_value=0)
        elif key == "action":
            frozen[key] = _require_enum(value, IntentAction, field="payload.action").value
        elif key == "audit_mode":
            if value != "no_order_prewrite":
                raise ContractValidationError("payload.audit_mode invalid")
            frozen[key] = value
        elif key == "spread_direction":
            frozen[key] = _require_enum(
                value, SpreadDirection, field="payload.spread_direction"
            ).value
        elif key == "coin":
            if not isinstance(value, str) or not _COIN_RE.fullmatch(value):
                raise ContractValidationError("payload.coin invalid")
            frozen[key] = value
        elif key == "instrument":
            if not isinstance(value, str) or not _INSTRUMENT_RE.fullmatch(value):
                raise ContractValidationError("payload.instrument invalid")
            frozen[key] = value
        elif key == "client_id":
            if not isinstance(value, str) or not value:
                raise ContractValidationError("payload.client_id invalid")
            frozen[key] = value
        else:
            if not isinstance(value, str) or not value:
                raise ContractValidationError(f"payload.{key} must be a non-empty string")
            frozen[key] = value
    return MappingProxyType(frozen)


def _payload_public(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {k: payload[k] for k in sorted(payload)}


@dataclass(frozen=True)
class TradeIntent:
    schema_version: str
    intent_id: str
    run_id: str
    policy_version: str
    action: IntentAction
    spread_direction: SpreadDirection
    coin: str
    notional_usdt: Decimal
    signal_mono_ns: int
    signal_wall_ns: int
    expiry_mono_ns: int
    signal_snapshot_ref: str
    canary_stage: str
    risk_policy_revision: str

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        object.__setattr__(self, "intent_id", _require_str(self.intent_id, field="intent_id"))
        object.__setattr__(self, "run_id", _require_str(self.run_id, field="run_id"))
        object.__setattr__(
            self, "policy_version", _require_str(self.policy_version, field="policy_version")
        )
        object.__setattr__(
            self,
            "action",
            _require_enum(self.action, IntentAction, field="action"),
        )
        object.__setattr__(
            self,
            "spread_direction",
            _require_enum(self.spread_direction, SpreadDirection, field="spread_direction"),
        )
        if not isinstance(self.coin, str) or not _COIN_RE.fullmatch(self.coin):
            raise ContractValidationError("coin invalid")
        object.__setattr__(
            self,
            "notional_usdt",
            canonical_decimal(self.notional_usdt, field="notional_usdt", allow_zero=False),
        )
        object.__setattr__(
            self, "signal_mono_ns", _require_int(self.signal_mono_ns, field="signal_mono_ns")
        )
        object.__setattr__(
            self, "signal_wall_ns", _require_int(self.signal_wall_ns, field="signal_wall_ns")
        )
        object.__setattr__(
            self, "expiry_mono_ns", _require_int(self.expiry_mono_ns, field="expiry_mono_ns")
        )
        if self.expiry_mono_ns <= self.signal_mono_ns:
            raise ContractValidationError("expiry_mono_ns must be after signal_mono_ns")
        object.__setattr__(
            self,
            "signal_snapshot_ref",
            _require_str(self.signal_snapshot_ref, field="signal_snapshot_ref"),
        )
        object.__setattr__(
            self, "canary_stage", _require_str(self.canary_stage, field="canary_stage")
        )
        object.__setattr__(
            self,
            "risk_policy_revision",
            _require_str(self.risk_policy_revision, field="risk_policy_revision"),
        )

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": self.schema_version,
            "intent_id": self.intent_id,
            "run_id": self.run_id,
            "policy_version": self.policy_version,
            "action": self.action.value,
            "spread_direction": self.spread_direction.value,
            "coin": self.coin,
            "notional_usdt": decimal_to_canonical(self.notional_usdt),
            "signal_mono_ns": self.signal_mono_ns,
            "signal_wall_ns": self.signal_wall_ns,
            "expiry_mono_ns": self.expiry_mono_ns,
            "signal_snapshot_ref": self.signal_snapshot_ref,
            "canary_stage": self.canary_stage,
            "risk_policy_revision": self.risk_policy_revision,
        }
        _assert_no_forbidden(out)
        return out

    @classmethod
    def from_public_dict(cls, raw: Mapping[str, Any]) -> "TradeIntent":
        data = _require_mapping(raw, label="TradeIntent")
        _require_exact_keys(data, set(cls.__dataclass_fields__), label="TradeIntent")
        return cls(
            schema_version=_require_schema(data["schema_version"]),
            intent_id=data["intent_id"],
            run_id=data["run_id"],
            policy_version=data["policy_version"],
            action=data["action"],
            spread_direction=data["spread_direction"],
            coin=data["coin"],
            notional_usdt=data["notional_usdt"],
            signal_mono_ns=data["signal_mono_ns"],
            signal_wall_ns=data["signal_wall_ns"],
            expiry_mono_ns=data["expiry_mono_ns"],
            signal_snapshot_ref=data["signal_snapshot_ref"],
            canary_stage=data["canary_stage"],
            risk_policy_revision=data["risk_policy_revision"],
        )


@dataclass(frozen=True)
class LegPlan:
    schema_version: str
    intent_id: str
    leg_id: str
    venue: Venue
    instrument: str
    side: str
    quantity: Decimal
    reduce_only: bool
    client_id: str
    lot_tolerance: Decimal

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        object.__setattr__(self, "intent_id", _require_str(self.intent_id, field="intent_id"))
        object.__setattr__(self, "leg_id", _require_str(self.leg_id, field="leg_id"))
        object.__setattr__(self, "venue", _require_enum(self.venue, Venue, field="venue"))
        if not isinstance(self.instrument, str) or not _INSTRUMENT_RE.fullmatch(self.instrument):
            raise ContractValidationError("instrument invalid")
        if self.side not in _SIDES:
            raise ContractValidationError("side must be buy or sell")
        object.__setattr__(
            self,
            "quantity",
            canonical_decimal(self.quantity, field="quantity", allow_zero=False),
        )
        object.__setattr__(
            self, "reduce_only", _require_bool(self.reduce_only, field="reduce_only")
        )
        expected = derive_client_id(
            self.intent_id, self.venue, reduce_only=self.reduce_only
        )
        client_id = validate_client_id(str(self.client_id), self.venue)
        if client_id != expected:
            raise ContractValidationError("client_id must be derived from intent_id")
        object.__setattr__(self, "client_id", client_id)
        object.__setattr__(
            self,
            "lot_tolerance",
            canonical_decimal(self.lot_tolerance, field="lot_tolerance"),
        )

    @classmethod
    def build(
        cls,
        *,
        intent_id: str,
        leg_id: str,
        venue: Venue,
        instrument: str,
        side: str,
        quantity: Decimal,
        reduce_only: bool = False,
        lot_tolerance: Decimal = Decimal("0"),
    ) -> "LegPlan":
        venue_e = venue if isinstance(venue, Venue) else _require_enum(venue, Venue, field="venue")
        return cls(
            schema_version=SCHEMA_VERSION,
            intent_id=intent_id,
            leg_id=leg_id,
            venue=venue_e,
            instrument=instrument,
            side=side,
            quantity=quantity,
            reduce_only=reduce_only,
            client_id=derive_client_id(intent_id, venue_e, reduce_only=reduce_only),
            lot_tolerance=lot_tolerance,
        )

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": self.schema_version,
            "intent_id": self.intent_id,
            "leg_id": self.leg_id,
            "venue": self.venue.value,
            "instrument": self.instrument,
            "side": self.side,
            "quantity": decimal_to_canonical(self.quantity),
            "reduce_only": self.reduce_only,
            "client_id": self.client_id,
            "lot_tolerance": decimal_to_canonical(self.lot_tolerance),
        }
        _assert_no_forbidden(out)
        return out

    @classmethod
    def from_public_dict(cls, raw: Mapping[str, Any]) -> "LegPlan":
        data = _require_mapping(raw, label="LegPlan")
        _require_exact_keys(data, set(cls.__dataclass_fields__), label="LegPlan")
        return cls(
            schema_version=_require_schema(data["schema_version"]),
            intent_id=data["intent_id"],
            leg_id=data["leg_id"],
            venue=data["venue"],
            instrument=data["instrument"],
            side=data["side"],
            quantity=data["quantity"],
            reduce_only=data["reduce_only"],
            client_id=data["client_id"],
            lot_tolerance=data["lot_tolerance"],
        )


@dataclass(frozen=True)
class ExecutionEvent:
    schema_version: str
    event_id: str
    event_type: ExecutionEventType
    intent_id: str
    run_id: str
    sequence: int
    monotonic_ns: int
    venue: Optional[Venue]
    leg_id: Optional[str]
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        object.__setattr__(self, "event_id", _require_str(self.event_id, field="event_id"))
        object.__setattr__(
            self,
            "event_type",
            _require_enum(self.event_type, ExecutionEventType, field="event_type"),
        )
        object.__setattr__(self, "intent_id", _require_str(self.intent_id, field="intent_id"))
        object.__setattr__(self, "run_id", _require_str(self.run_id, field="run_id"))
        object.__setattr__(
            self, "sequence", _require_int(self.sequence, field="sequence", min_value=1)
        )
        object.__setattr__(
            self, "monotonic_ns", _require_int(self.monotonic_ns, field="monotonic_ns")
        )
        venue = self.venue
        if venue is not None:
            venue = _require_enum(venue, Venue, field="venue")
        object.__setattr__(self, "venue", venue)
        leg_id = self.leg_id
        if leg_id is not None:
            leg_id = _require_str(leg_id, field="leg_id")
        object.__setattr__(self, "leg_id", leg_id)
        object.__setattr__(self, "payload", _freeze_payload(self.payload))
        _validate_event_shape(self)

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "intent_id": self.intent_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "monotonic_ns": self.monotonic_ns,
            "venue": None if self.venue is None else self.venue.value,
            "leg_id": self.leg_id,
            "payload": _payload_public(self.payload),
        }
        _assert_no_forbidden(out)
        return out

    @classmethod
    def from_public_dict(cls, raw: Mapping[str, Any]) -> "ExecutionEvent":
        data = _require_mapping(raw, label="ExecutionEvent")
        _require_exact_keys(data, set(cls.__dataclass_fields__), label="ExecutionEvent")
        venue = data["venue"]
        return cls(
            schema_version=_require_schema(data["schema_version"]),
            event_id=data["event_id"],
            event_type=data["event_type"],
            intent_id=data["intent_id"],
            run_id=data["run_id"],
            sequence=data["sequence"],
            monotonic_ns=data["monotonic_ns"],
            venue=venue,
            leg_id=data["leg_id"],
            payload=data["payload"],
        )

    def content_hash(self) -> str:
        raw = json.dumps(
            self.to_public_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def sequence_hash(self) -> str:
        body = self.to_public_dict()
        body.pop("event_id", None)
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_event_shape(event: ExecutionEvent) -> None:
    et = event.event_type
    audit_fields = {"audit_mode", "prewrite_passed"}
    if audit_fields.intersection(event.payload) and (
        et is not ExecutionEventType.INTENT_REJECTED
        or not audit_fields.issubset(event.payload)
        or event.payload.get("action") != IntentAction.OPEN.value
        or event.payload.get("reason_code") != "intent_rejected"
    ):
        raise ContractValidationError("no-order audit marker invalid")
    needs_leg = et in {
        ExecutionEventType.REQUEST_SENT,
        ExecutionEventType.ACK_ACCEPTED,
        ExecutionEventType.ACK_REJECTED,
        ExecutionEventType.ACK_TIMEOUT,
        ExecutionEventType.PARTIAL_FILL,
        ExecutionEventType.FILL,
        ExecutionEventType.CANCEL_REQUESTED,
        ExecutionEventType.CANCEL_ACK,
        ExecutionEventType.POSITION_OBSERVED,
        ExecutionEventType.OPEN_ORDERS_OBSERVED,
        ExecutionEventType.UNKNOWN_CORRELATION,
    }
    if needs_leg and (event.leg_id is None or event.venue is None):
        raise ContractValidationError(f"{et.value} requires venue and leg_id")
    if et is ExecutionEventType.REQUEST_SENT:
        for key in ("quantity", "reduce_only", "instrument", "side"):
            if key not in event.payload:
                raise ContractValidationError(f"request_sent payload missing {key}")
    if et in {ExecutionEventType.PARTIAL_FILL, ExecutionEventType.FILL}:
        if "quantity" not in event.payload:
            raise ContractValidationError(f"{et.value} payload missing quantity")
    if et is ExecutionEventType.POSITION_OBSERVED and "quantity" not in event.payload:
        raise ContractValidationError("position_observed payload missing quantity")
    if (
        et is ExecutionEventType.OPEN_ORDERS_OBSERVED
        and "open_order_count" not in event.payload
    ):
        raise ContractValidationError("open_orders_observed payload missing open_order_count")
    if et is ExecutionEventType.INTENT_ACCEPTED:
        for key in ("action", "coin", "spread_direction", "lot_tolerance"):
            if key not in event.payload:
                raise ContractValidationError(f"intent_accepted payload missing {key}")
    if et is ExecutionEventType.FLATNESS_PROVEN:
        for key in ("positions_flat", "open_orders_flat"):
            if key not in event.payload:
                raise ContractValidationError(f"flatness_proven payload missing {key}")
    if et is ExecutionEventType.STREAM_GENERATION_MISMATCH:
        if "stream_generation" not in event.payload:
            raise ContractValidationError("stream_generation_mismatch missing generation")


@dataclass(frozen=True)
class LegState:
    schema_version: str
    leg_id: str
    venue: Venue
    status: LegStatus
    client_id: str
    planned_quantity: Decimal
    filled_quantity: Decimal
    ack_status: str
    reduce_only: bool
    position_quantity: Decimal
    open_order_count: int
    position_observed: bool
    open_orders_observed: bool
    stream_generation: int
    confirmed_unfilled: bool

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        object.__setattr__(self, "leg_id", _require_str(self.leg_id, field="leg_id"))
        object.__setattr__(self, "venue", _require_enum(self.venue, Venue, field="venue"))
        object.__setattr__(
            self, "status", _require_enum(self.status, LegStatus, field="status")
        )
        object.__setattr__(
            self, "client_id", validate_client_id(str(self.client_id), self.venue)
        )
        object.__setattr__(
            self,
            "planned_quantity",
            canonical_decimal(self.planned_quantity, field="planned_quantity"),
        )
        object.__setattr__(
            self,
            "filled_quantity",
            canonical_decimal(self.filled_quantity, field="filled_quantity"),
        )
        if self.ack_status not in _ACK_STATUSES:
            raise ContractValidationError("ack_status invalid")
        object.__setattr__(
            self, "reduce_only", _require_bool(self.reduce_only, field="reduce_only")
        )
        object.__setattr__(
            self,
            "position_quantity",
            canonical_decimal(self.position_quantity, field="position_quantity"),
        )
        object.__setattr__(
            self,
            "open_order_count",
            _require_int(self.open_order_count, field="open_order_count"),
        )
        object.__setattr__(
            self,
            "position_observed",
            _require_bool(self.position_observed, field="position_observed"),
        )
        object.__setattr__(
            self,
            "open_orders_observed",
            _require_bool(self.open_orders_observed, field="open_orders_observed"),
        )
        object.__setattr__(
            self,
            "stream_generation",
            _require_int(self.stream_generation, field="stream_generation"),
        )
        object.__setattr__(
            self,
            "confirmed_unfilled",
            _require_bool(self.confirmed_unfilled, field="confirmed_unfilled"),
        )

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": self.schema_version,
            "leg_id": self.leg_id,
            "venue": self.venue.value,
            "status": self.status.value,
            "client_id": self.client_id,
            "planned_quantity": decimal_to_canonical(self.planned_quantity),
            "filled_quantity": decimal_to_canonical(self.filled_quantity),
            "ack_status": self.ack_status,
            "reduce_only": self.reduce_only,
            "position_quantity": decimal_to_canonical(self.position_quantity),
            "open_order_count": self.open_order_count,
            "position_observed": self.position_observed,
            "open_orders_observed": self.open_orders_observed,
            "stream_generation": self.stream_generation,
            "confirmed_unfilled": self.confirmed_unfilled,
        }
        _assert_no_forbidden(out)
        return out

    @classmethod
    def from_public_dict(cls, raw: Mapping[str, Any]) -> "LegState":
        data = _require_mapping(raw, label="LegState")
        _require_exact_keys(data, set(cls.__dataclass_fields__), label="LegState")
        return cls(
            schema_version=_require_schema(data["schema_version"]),
            leg_id=data["leg_id"],
            venue=data["venue"],
            status=data["status"],
            client_id=data["client_id"],
            planned_quantity=data["planned_quantity"],
            filled_quantity=data["filled_quantity"],
            ack_status=data["ack_status"],
            reduce_only=data["reduce_only"],
            position_quantity=data["position_quantity"],
            open_order_count=data["open_order_count"],
            position_observed=data["position_observed"],
            open_orders_observed=data["open_orders_observed"],
            stream_generation=data["stream_generation"],
            confirmed_unfilled=data["confirmed_unfilled"],
        )


def timeout_blocks_open_proof(leg: LegState, tolerance: Decimal) -> bool:
    """True when ack timeout is still unresolved for OPEN proof.

    A timeout may remain as historical request evidence only after matched
    reconciliation has put the non-reduce leg in FILLED with an authoritative
    matching positive position. Unresolved timeout cannot prove OPEN.
    """
    if not isinstance(leg, LegState) or leg.ack_status != "timeout":
        return False
    if leg.reduce_only or leg.status is not LegStatus.FILLED:
        return True
    if not leg.position_observed or leg.position_quantity <= 0:
        return True
    if observed_position_contradicts_fill(leg, tolerance):
        return True
    return abs(leg.position_quantity - leg.planned_quantity) > tolerance


def timeout_blocks_flat_proof(leg: LegState) -> bool:
    """True when ack timeout is still unresolved for FLAT proof.

    A timeout may remain as historical request evidence only after matched
    reconciliation has put the leg in FILLED (filled_quantity>0) or
    CANCELLED (filled_quantity==0) with a fresh zero position and zero
    open-order snapshot. Unresolved timeout cannot prove FLAT.
    """
    if not isinstance(leg, LegState) or leg.ack_status != "timeout":
        return False
    if not (
        leg.position_observed
        and leg.position_quantity == 0
        and leg.open_orders_observed
        and leg.open_order_count == 0
    ):
        return True
    if leg.status is LegStatus.FILLED:
        return leg.filled_quantity <= 0
    if leg.status is LegStatus.CANCELLED:
        return leg.filled_quantity != 0
    return True


def ack_timeout_is_unresolved(leg: LegState, tolerance: Decimal) -> bool:
    """True when ack timeout still blocks exposure, OPEN, and FLAT proof."""
    if not isinstance(leg, LegState) or leg.ack_status != "timeout":
        return False
    return timeout_blocks_open_proof(leg, tolerance) and timeout_blocks_flat_proof(leg)


def flat_evidence_is_complete(
    *,
    legs: tuple[LegState, ...],
    positions_flat: bool,
    open_orders_flat: bool,
) -> bool:
    """True only when exactly two legs are observed flat with zero exposure."""
    if len(legs) != 2:
        return False
    if not positions_flat or not open_orders_flat:
        return False
    for leg in legs:
        if not isinstance(leg, LegState):
            return False
        if not leg.position_observed or not leg.open_orders_observed:
            return False
        if leg.position_quantity != 0 or leg.open_order_count != 0:
            return False
        if leg.status in {LegStatus.PARTIAL, LegStatus.UNKNOWN, LegStatus.RECONCILING}:
            return False
        if timeout_blocks_flat_proof(leg):
            return False
    return True


def idle_state_is_coherent(
    *,
    intent_id: Optional[str],
    open_intent_id: Optional[str],
    direction: Optional[SpreadDirection],
    coin: Optional[str],
    legs: tuple[LegState, ...],
) -> bool:
    """IDLE may latch pause/recovery, but must not carry live intent or exposure."""
    if legs:
        return False
    if intent_id is not None or open_intent_id is not None:
        return False
    if direction is not None or coin is not None:
        return False
    return True


def armed_state_is_coherent(
    *,
    intent_id: Optional[str],
    open_intent_id: Optional[str],
    direction: Optional[SpreadDirection],
    coin: Optional[str],
    legs: tuple[LegState, ...],
) -> bool:
    """ARMED is an accepted open intent before any live leg evidence."""
    if intent_id is None or open_intent_id != intent_id:
        return False
    if direction is None or coin is None:
        return False
    return not legs


def observed_working_open_orders(leg: LegState) -> bool:
    """True when a non-reduce leg has a freshly observed live order."""
    if not isinstance(leg, LegState) or leg.reduce_only:
        return False
    return bool(leg.open_orders_observed and leg.open_order_count > 0)


def terminal_fill_is_incomplete(leg: LegState, tolerance: Decimal) -> bool:
    """True when a claimed terminal fill is short of plan minus lot tolerance."""
    if not isinstance(leg, LegState) or leg.planned_quantity <= 0:
        return False
    if leg.status is not LegStatus.FILLED:
        return False
    if leg.filled_quantity <= 0:
        return False
    return leg.filled_quantity < (leg.planned_quantity - tolerance)


OPEN_PROOF_LEG_STATUSES = frozenset(
    {LegStatus.SENT, LegStatus.ACK_ACCEPTED, LegStatus.FILLED}
)


def observed_position_contradicts_fill(leg: LegState, tolerance: Decimal) -> bool:
    """True when an observed position, including zero, disagrees with a fill.

    Reduce-only close fills are expected to leave a zero position, so they are
    not treated as OPEN-proof contradictions.
    """
    if not isinstance(leg, LegState):
        return True
    if leg.reduce_only:
        return False
    if not leg.position_observed or leg.filled_quantity <= 0:
        return False
    return abs(leg.filled_quantity - leg.position_quantity) > tolerance


def leg_quantity_exceeds_plan(leg: LegState, tolerance: Decimal) -> bool:
    """True when fill or observed position is above planned quantity + tolerance."""
    if not isinstance(leg, LegState) or leg.planned_quantity <= 0:
        return False
    limit = leg.planned_quantity + tolerance
    if leg.filled_quantity > limit:
        return True
    return bool(leg.position_observed and leg.position_quantity > limit)


def leg_open_qty(leg: LegState, tolerance: Decimal) -> Optional[Decimal]:
    """Fill and/or observed position quantity that may prove an open leg.

    An observed position is authoritative even when zero: fill plus a
    disagreeing observed quantity cannot prove OPEN.
    """
    if observed_position_contradicts_fill(leg, tolerance):
        return None
    fill_qty = (
        leg.filled_quantity
        if leg.status is LegStatus.FILLED and leg.filled_quantity > 0
        else None
    )
    pos_qty = (
        leg.position_quantity
        if leg.position_observed and leg.position_quantity > 0
        else None
    )
    if fill_qty is not None and pos_qty is not None:
        if abs(fill_qty - pos_qty) > tolerance:
            return None
        return fill_qty
    if fill_qty is not None:
        return fill_qty
    if pos_qty is not None:
        return pos_qty
    return None


def open_evidence_is_complete(
    *,
    intent_id: Optional[str],
    open_intent_id: Optional[str],
    stream_generation_ok: bool,
    lot_tolerance: Decimal,
    legs: tuple[LegState, ...],
) -> bool:
    """True only when both legs have non-contradictory fill/position OPEN proof.

    ACK-only, ACK_REJECTED, CANCELLED, UNKNOWN, RECONCILING, PARTIAL and an
    unresolved ack timeout cannot prove OPEN. SENT/ACK_ACCEPTED/FILLED may,
    when fill and/or observed position matches the planned quantity within
    ``lot_tolerance``. A timeout ack may remain only after the leg is
    explicitly reconciled to FILLED with matching positive position proof.
    """
    if open_intent_id is None or intent_id is None:
        return False
    if intent_id != open_intent_id:
        return False
    if not stream_generation_ok:
        return False
    if len(legs) != 2:
        return False
    if any(leg.planned_quantity <= 0 or leg.reduce_only for leg in legs):
        return False
    if any(leg.status not in OPEN_PROOF_LEG_STATUSES for leg in legs):
        return False
    if any(timeout_blocks_open_proof(leg, lot_tolerance) for leg in legs):
        return False
    if any(observed_working_open_orders(leg) for leg in legs):
        return False
    if any(observed_position_contradicts_fill(leg, lot_tolerance) for leg in legs):
        return False
    if any(leg_quantity_exceeds_plan(leg, lot_tolerance) for leg in legs):
        return False
    if any(terminal_fill_is_incomplete(leg, lot_tolerance) for leg in legs):
        return False
    qtys: list[Decimal] = []
    for leg in legs:
        qty = leg_open_qty(leg, lot_tolerance)
        if qty is None or qty <= 0:
            return False
        if abs(qty - leg.planned_quantity) > lot_tolerance:
            return False
        qtys.append(qty)
    return abs(qtys[0] - qtys[1]) <= lot_tolerance


def _freeze_id_set(raw: object, *, label: str) -> frozenset[str]:
    if isinstance(raw, frozenset):
        items = raw
    elif isinstance(raw, (list, set, tuple)):
        items = raw
    else:
        raise ContractValidationError(f"{label} must be a list")
    frozen: list[str] = []
    for item in items:
        frozen.append(_require_str(item, field=label))
    return frozenset(frozen)


def _freeze_hex_map(
    raw: object,
    *,
    label: str,
    digit_keys: bool,
) -> Mapping[str, str]:
    data = _require_mapping(raw, label=label)
    frozen: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not key:
            raise ContractValidationError(f"{label} keys must be non-empty strings")
        if digit_keys:
            if not key.isdigit():
                raise ContractValidationError(f"{label} keys must be digit strings")
        elif not _ID_RE.fullmatch(key):
            raise ContractValidationError(f"{label} keys must be venue-safe opaque ids")
        if not isinstance(value, str) or not _HEX_RE.fullmatch(value):
            raise ContractValidationError(f"{label} values must be hex")
        frozen[key] = value
    return MappingProxyType(frozen)


@dataclass(frozen=True)
class SpreadState:
    schema_version: str
    run_id: str
    intent_id: Optional[str]
    open_intent_id: Optional[str]
    status: SpreadStatus
    direction: Optional[SpreadDirection]
    coin: Optional[str]
    lot_tolerance: Decimal
    legs: tuple[LegState, ...]
    last_sequence: int
    last_monotonic_ns: int
    applied_event_ids: frozenset[str]
    accepted_intent_ids: frozenset[str]
    sequence_hashes: Mapping[str, str]
    event_hashes: Mapping[str, str]
    recovery_required: bool
    pause_latched: bool
    stream_generation_ok: bool
    positions_flat: bool
    open_orders_flat: bool
    halt_reason: Optional[str]

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        object.__setattr__(self, "run_id", _require_str(self.run_id, field="run_id"))
        if self.intent_id is not None:
            object.__setattr__(
                self, "intent_id", _require_str(self.intent_id, field="intent_id")
            )
        if self.open_intent_id is not None:
            object.__setattr__(
                self,
                "open_intent_id",
                _require_str(self.open_intent_id, field="open_intent_id"),
            )
        object.__setattr__(
            self, "status", _require_enum(self.status, SpreadStatus, field="status")
        )
        if self.direction is not None:
            object.__setattr__(
                self,
                "direction",
                _require_enum(self.direction, SpreadDirection, field="direction"),
            )
        if self.coin is not None and (
            not isinstance(self.coin, str) or not _COIN_RE.fullmatch(self.coin)
        ):
            raise ContractValidationError("coin invalid")
        object.__setattr__(
            self,
            "lot_tolerance",
            canonical_decimal(self.lot_tolerance, field="lot_tolerance"),
        )
        if not isinstance(self.legs, tuple):
            raise ContractValidationError("legs must be a tuple")
        if len(self.legs) > 2:
            raise ContractValidationError("K_live=1 allows at most two legs")
        seen_venues: set[Venue] = set()
        frozen_legs: list[LegState] = []
        for leg in self.legs:
            if not isinstance(leg, LegState):
                raise ContractValidationError("legs must contain LegState")
            if leg.venue in seen_venues:
                raise ContractValidationError("duplicate venue leg")
            seen_venues.add(leg.venue)
            source_id = self.intent_id if leg.reduce_only else self.open_intent_id
            if source_id is None:
                raise ContractValidationError("leg client_id requires intent id")
            expected = derive_client_id(
                source_id, leg.venue, reduce_only=leg.reduce_only
            )
            if leg.client_id != expected:
                raise ContractValidationError("client_id must be derived from intent_id")
            frozen_legs.append(leg)
        object.__setattr__(self, "legs", tuple(frozen_legs))
        object.__setattr__(
            self, "last_sequence", _require_int(self.last_sequence, field="last_sequence")
        )
        object.__setattr__(
            self,
            "last_monotonic_ns",
            _require_int(self.last_monotonic_ns, field="last_monotonic_ns"),
        )
        ids = self.applied_event_ids
        if not isinstance(ids, frozenset):
            ids = frozenset(str(x) for x in ids)
        object.__setattr__(self, "applied_event_ids", ids)
        object.__setattr__(
            self,
            "accepted_intent_ids",
            _freeze_id_set(self.accepted_intent_ids, label="accepted_intent_ids"),
        )
        if self.intent_id is not None and self.intent_id not in self.accepted_intent_ids:
            raise ContractValidationError("intent_id must be an accepted intent")
        if (
            self.open_intent_id is not None
            and self.open_intent_id not in self.accepted_intent_ids
        ):
            raise ContractValidationError("open_intent_id must be an accepted intent")
        object.__setattr__(
            self,
            "sequence_hashes",
            _freeze_hex_map(self.sequence_hashes, label="sequence_hashes", digit_keys=True),
        )
        object.__setattr__(
            self,
            "event_hashes",
            _freeze_hex_map(self.event_hashes, label="event_hashes", digit_keys=False),
        )
        if frozenset(self.event_hashes) != self.applied_event_ids:
            raise ContractValidationError("event_hashes keys must match applied_event_ids")
        object.__setattr__(
            self,
            "recovery_required",
            _require_bool(self.recovery_required, field="recovery_required"),
        )
        object.__setattr__(
            self, "pause_latched", _require_bool(self.pause_latched, field="pause_latched")
        )
        object.__setattr__(
            self,
            "stream_generation_ok",
            _require_bool(self.stream_generation_ok, field="stream_generation_ok"),
        )
        object.__setattr__(
            self, "positions_flat", _require_bool(self.positions_flat, field="positions_flat")
        )
        object.__setattr__(
            self,
            "open_orders_flat",
            _require_bool(self.open_orders_flat, field="open_orders_flat"),
        )
        if self.halt_reason is not None:
            if self.halt_reason not in _REASON_CODES:
                raise ContractValidationError("halt_reason not allowlisted")
        if self.status is SpreadStatus.IDLE and not idle_state_is_coherent(
            intent_id=self.intent_id,
            open_intent_id=self.open_intent_id,
            direction=self.direction,
            coin=self.coin,
            legs=self.legs,
        ):
            raise ContractValidationError("IDLE must not carry live intent or exposure")
        if self.status is SpreadStatus.ARMED and not armed_state_is_coherent(
            intent_id=self.intent_id,
            open_intent_id=self.open_intent_id,
            direction=self.direction,
            coin=self.coin,
            legs=self.legs,
        ):
            raise ContractValidationError("ARMED requires accepted open intent without legs")
        if self.status is SpreadStatus.FLAT and not flat_evidence_is_complete(
            legs=self.legs,
            positions_flat=self.positions_flat,
            open_orders_flat=self.open_orders_flat,
        ):
            raise ContractValidationError("FLAT requires two reconciled observed legs")
        if self.status is SpreadStatus.OPEN:
            if self.intent_id != self.open_intent_id:
                raise ContractValidationError("OPEN requires intent_id == open_intent_id")
            if not open_evidence_is_complete(
                intent_id=self.intent_id,
                open_intent_id=self.open_intent_id,
                stream_generation_ok=self.stream_generation_ok,
                lot_tolerance=self.lot_tolerance,
                legs=self.legs,
            ):
                raise ContractValidationError(
                    "OPEN requires two-leg fill or position proof"
                )

    def leg_by_id(self, leg_id: str) -> Optional[LegState]:
        for leg in self.legs:
            if leg.leg_id == leg_id:
                return leg
        return None

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "intent_id": self.intent_id,
            "open_intent_id": self.open_intent_id,
            "status": self.status.value,
            "direction": None if self.direction is None else self.direction.value,
            "coin": self.coin,
            "lot_tolerance": decimal_to_canonical(self.lot_tolerance),
            "legs": [leg.to_public_dict() for leg in self.legs],
            "last_sequence": self.last_sequence,
            "last_monotonic_ns": self.last_monotonic_ns,
            "applied_event_ids": sorted(self.applied_event_ids),
            "accepted_intent_ids": sorted(self.accepted_intent_ids),
            "sequence_hashes": {k: self.sequence_hashes[k] for k in sorted(self.sequence_hashes)},
            "event_hashes": {k: self.event_hashes[k] for k in sorted(self.event_hashes)},
            "recovery_required": self.recovery_required,
            "pause_latched": self.pause_latched,
            "stream_generation_ok": self.stream_generation_ok,
            "positions_flat": self.positions_flat,
            "open_orders_flat": self.open_orders_flat,
            "halt_reason": self.halt_reason,
        }
        _assert_no_forbidden(out)
        return out

    @classmethod
    def from_public_dict(cls, raw: Mapping[str, Any]) -> "SpreadState":
        data = _require_mapping(raw, label="SpreadState")
        required = set(cls.__dataclass_fields__)
        _require_exact_keys(data, required, label="SpreadState")
        if not isinstance(data["legs"], list):
            raise ContractValidationError("legs must be a list")
        if not isinstance(data["applied_event_ids"], list):
            raise ContractValidationError("applied_event_ids must be a list")
        if not isinstance(data["accepted_intent_ids"], list):
            raise ContractValidationError("accepted_intent_ids must be a list")
        return cls(
            schema_version=_require_schema(data["schema_version"]),
            run_id=data["run_id"],
            intent_id=data["intent_id"],
            open_intent_id=data["open_intent_id"],
            status=data["status"],
            direction=data["direction"],
            coin=data["coin"],
            lot_tolerance=data["lot_tolerance"],
            legs=tuple(LegState.from_public_dict(item) for item in data["legs"]),
            last_sequence=data["last_sequence"],
            last_monotonic_ns=data["last_monotonic_ns"],
            applied_event_ids=frozenset(str(x) for x in data["applied_event_ids"]),
            accepted_intent_ids=frozenset(str(x) for x in data["accepted_intent_ids"]),
            sequence_hashes=data["sequence_hashes"],
            event_hashes=data["event_hashes"],
            recovery_required=data["recovery_required"],
            pause_latched=data["pause_latched"],
            stream_generation_ok=data["stream_generation_ok"],
            positions_flat=data["positions_flat"],
            open_orders_flat=data["open_orders_flat"],
            halt_reason=data["halt_reason"],
        )
