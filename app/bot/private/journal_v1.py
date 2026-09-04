"""Append-only ``bbot.private.journal.v1`` writer + validator.

No order send/cancel/amend, no private WS, no network. Spec:
``docs/b-private-journal-contract.md``.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Optional, Sequence

from app.bot.private.paths import (
    _is_under_denied,
    events_jsonl_path,
    resolve_data_root,
)

SCHEMA_VERSION = "bbot.private.journal.v1"

VENUES = frozenset({"bybit", "okx"})
ENVIRONMENTS = frozenset({"testnet", "demo", "live"})
OUTCOMES = frozenset({"success", "failure", "pending", "observed"})

EVENT_TYPES = frozenset(
    {
        "auth",
        "account_read",
        "operator_approval",
        "pre_send_gate",
        "order_prepared",
        "request_sent",
        "ack_received",
        "terminal_update",
        "cancel_requested",
        "cancel_ack",
        "reject",
        "dual_leg_abort",
        "reconciliation",
        "latency_summary",
    }
)

OUTCOMES_BY_TYPE: Mapping[str, frozenset[str]] = {
    "auth": frozenset({"success", "failure"}),
    "account_read": frozenset({"success", "failure"}),
    "operator_approval": frozenset({"success", "observed"}),
    "pre_send_gate": frozenset({"observed"}),
    "order_prepared": frozenset({"pending"}),
    "request_sent": frozenset({"pending", "failure"}),
    "ack_received": frozenset({"success", "failure"}),
    "terminal_update": frozenset({"observed"}),
    "cancel_requested": frozenset({"pending", "failure"}),
    "cancel_ack": frozenset({"success", "failure"}),
    "reject": frozenset({"failure"}),
    "dual_leg_abort": frozenset({"observed"}),
    "reconciliation": frozenset({"success", "failure", "observed"}),
    "latency_summary": frozenset({"observed"}),
}

COMMON_REQUIRED = (
    "schema_version",
    "event_id",
    "event_type",
    "event_date",
    "event_ts_utc",
    "event_monotonic_ns",
    "run_id",
    "operation_id",
    "event_seq",
    "venue",
    "environment",
    "outcome",
)

EXTRA_FIELDS_BY_TYPE: Mapping[str, frozenset[str]] = {
    "auth": frozenset({"auth_method", "credential_presence", "error_code"}),
    "account_read": frozenset({"account_scope", "request_kind", "error_code"}),
    "operator_approval": frozenset(
        {
            "approval_action",
            "approval_token_fingerprint",
            "approval_scope",
            "approval_record_id",
            "approval_expires_at_utc",
            "approval_grant_event_id",
            "consumed_for_operation_id",
        }
    ),
    "pre_send_gate": frozenset({"gate_kind", "gate_decision"}),
    "order_prepared": frozenset(
        {
            "dual_leg_id",
            "leg_id",
            "instrument_class",
            "symbol_alias",
            "side",
            "order_kind",
            "quantity_bucket",
            "notional_bucket",
            "reduce_only",
            "post_only",
            "ttl_bucket",
            "request_fingerprint",
        }
    ),
    "request_sent": frozenset(
        {
            "dual_leg_id",
            "leg_id",
            "request_kind",
            "request_fingerprint",
            "transport_attempt",
            "send_monotonic_ns",
            "transport",
            "reconnect_generation",
            "subscription_readiness",
            "error_code",
        }
    ),
    "ack_received": frozenset(
        {
            "dual_leg_id",
            "leg_id",
            "request_kind",
            "request_fingerprint",
            "ack_state",
            "receive_monotonic_ns",
            "transport",
            "reconnect_generation",
            "subscription_readiness",
            "error_code",
        }
    ),
    "terminal_update": frozenset(
        {
            "dual_leg_id",
            "leg_id",
            "terminal_state",
            "request_fingerprint",
            "receive_monotonic_ns",
            "exchange_event_ts_utc",
            "clock_offset_evidence",
            "observation_source",
            "reconnect_generation",
            "sequence_state",
        }
    ),
    "cancel_requested": frozenset(
        {
            "dual_leg_id",
            "leg_id",
            "request_fingerprint",
            "cancel_reason",
            "send_monotonic_ns",
            "error_code",
            "transport",
            "reconnect_generation",
        }
    ),
    "cancel_ack": frozenset(
        {
            "dual_leg_id",
            "leg_id",
            "cancel_state",
            "request_fingerprint",
            "receive_monotonic_ns",
            "error_code",
            "transport",
            "reconnect_generation",
        }
    ),
    "reject": frozenset(
        {
            "dual_leg_id",
            "leg_id",
            "request_kind",
            "request_fingerprint",
            "reject_stage",
            "error_code",
        }
    ),
    "dual_leg_abort": frozenset(
        {
            "dual_leg_id",
            "leg_id",
            "peer_leg_id",
            "abort_reason",
            "request_fingerprint",
        }
    ),
    "reconciliation": frozenset(
        {
            "dual_leg_id",
            "leg_id",
            "reconciliation_scope",
            "reconciliation_state",
            "mismatch_fields",
            "transport",
            "observation_source",
            "reconnect_generation",
            "sequence_state",
            "subscription_readiness",
            "error_code",
        }
    ),
    "latency_summary": frozenset(
        {
            "dual_leg_id",
            "leg_id",
            "latency_intervals_ms",
            "clock_offset_evidence",
            "latency_basis",
            "sample_count",
        }
    ),
}

# Required extras (when event is written) beyond common — soft requirements for builders.
AUTH_METHODS = frozenset({"hmac", "api_key_signature"})
ACCOUNT_SCOPES = frozenset({"wallet", "balance", "positions"})
REQUEST_KINDS = frozenset({"account_read", "place", "cancel", "ws_subscribe"})
INSTRUMENT_CLASSES = frozenset({"spot", "linear_perpetual", "inverse_perpetual"})
SIDES = frozenset({"buy", "sell"})
ORDER_KINDS = frozenset({"market", "limit"})
ACK_STATES = frozenset({"accepted", "received"})
TERMINAL_STATES = frozenset({"filled", "cancelled", "expired"})
CANCEL_STATES = frozenset({"accepted", "cancelled"})
REJECT_STAGES = frozenset({"auth", "prepare", "send", "ack", "cancel"})
CANCEL_REASONS = frozenset(
    {"operator_request", "timeout_guard", "dual_leg_guard", "post_only_ttl_expired"}
)
ABORT_REASONS = frozenset(
    {"peer_rejected", "peer_timeout", "peer_terminal_before_send", "safety_guard"}
)
RECON_SCOPES = frozenset(
    {
        "order_state",
        "request_ack",
        "dual_leg_state",
        "post_dispatch_ambiguity",
        "post_only_ttl_recovery",
        "private_stream_reseed",
    }
)
RECON_STATES = frozenset({"matched", "mismatch", "inconclusive"})
MISMATCH_FIELDS = frozenset({"state", "timing", "fingerprint", "leg_link"})
APPROVAL_ACTIONS = frozenset({"granted", "consumed"})
APPROVAL_SCOPES = frozenset({"live_order_send"})
TTL_BUCKETS = frozenset({"short", "medium", "long"})
GATE_KINDS = frozenset({"rest", "price"})
GATE_DECISIONS = frozenset({"blocked"})
APPROVAL_FP_RE = re.compile(r"^[0-9a-f]{64}$")
LATENCY_BASIS = frozenset({"monotonic_local", "offset_adjusted_observed"})
CLOCK_OFFSET_METHODS = frozenset({"ntp_offset", "venue_time_probe"})
TRANSPORTS = frozenset({"ws_trade", "rest"})
OBSERVATION_SOURCES = frozenset({"private_ws", "rest_reconcile"})
SEQUENCE_STATES = frozenset({"healthy", "gap", "reseed_required"})
SUBSCRIPTION_READINESS = frozenset({"not_ready", "ready"})

LATENCY_INTERVAL_NAMES = frozenset(
    {
        "local_prepare",
        "request_ack_rtt",
        "local_response_processing",
        "ack_terminal_receive",
        "exchange_to_client_observed",
    }
)

# RTT must never be labeled or aliased as one-way path latency.
_ONE_WAY_FORBIDDEN_TOKENS = frozenset(
    {
        "one_way",
        "oneway",
        "one-way",
        "one way",
        "path_latency",
        "to_exchange_latency",
        "latency_to_exchange",
    }
)

ERROR_CODES = frozenset(
    {
        "auth_failed",
        "auth_unavailable",
        "account_read_failed",
        "invalid_request",
        "signature_error",
        "network_error",
        "timeout",
        "transport_error",
        "rate_limited",
        "venue_rejected",
        "order_rejected",
        "cancel_rejected",
        "reconciliation_mismatch",
        "dual_leg_aborted",
        "internal_error",
        "unknown",
    }
)

REDACTION_DENYLIST = frozenset(
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
        "request_body",
        "response_body",
        "headers",
        "canonical_request",
        "operator_phrase",
        "operator_message",
        "operator_id",
        "operator_user_id",
        "operator_uuid",
        "operator_handle",
        "operator_name",
        "operator_email",
        "approver_id",
        "approver_user_id",
        "approver_uuid",
        "approver_handle",
        "approver_name",
        "approver_email",
        "approval_token",
        "approval_phrase",
        "approval_message",
        "account_id",
        "uid",
        "member_id",
        "wallet_address",
        "exchange_order_id",
        "order_id",
        "client_order_id",
        "clordid",
        "balance",
        "available_balance",
        "equity",
        "margin",
        "position",
        "account_value",
        "price",
        "qty",
        "quantity",
        "notional",
        "fee",
        "fill_price",
        "fill_qty",
    }
)

# §4 enum literals may equal a denylist token (e.g. account_scope=balance).
# Keys remain denied; these tokens are allowed only as explicit enum values.
_ALLOWED_ENUM_VALUE_TOKENS = (
    OUTCOMES
    | VENUES
    | ENVIRONMENTS
    | EVENT_TYPES
    | AUTH_METHODS
    | ACCOUNT_SCOPES
    | REQUEST_KINDS
    | INSTRUMENT_CLASSES
    | SIDES
    | ORDER_KINDS
    | ACK_STATES
    | TERMINAL_STATES
    | CANCEL_STATES
    | REJECT_STAGES
    | CANCEL_REASONS
    | ABORT_REASONS
    | RECON_SCOPES
    | RECON_STATES
    | MISMATCH_FIELDS
    | APPROVAL_ACTIONS
    | APPROVAL_SCOPES
    | TTL_BUCKETS
    | GATE_KINDS
    | GATE_DECISIONS
    | LATENCY_BASIS
    | CLOCK_OFFSET_METHODS
    | LATENCY_INTERVAL_NAMES
    | ERROR_CODES
    | TRANSPORTS
    | OBSERVATION_SOURCES
    | SEQUENCE_STATES
    | SUBSCRIPTION_READINESS
    | frozenset({"hmac", "api_key_signature"})
)

# Current writer form (exactly one key).
_CREDENTIAL_PRESENCE_KEYS_CURRENT = frozenset({"credentials_configured"})
# Legacy append-only form (exactly these three keys).
_CREDENTIAL_PRESENCE_KEYS_LEGACY = frozenset(
    {"api_key_present", "api_secret_present", "passphrase_present"}
)
APPROVAL_LOCK_BASENAME = ".approval.lock"

_RFC3339_Z_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_URL_HINT_RE = re.compile(r"(?i)\bhttps?://|\bwss?://")


class JournalValidationError(ValueError):
    """Fail-closed journal contract violation."""


def new_opaque_id(prefix: str = "id") -> str:
    """Generated correlation id: ``prefix_`` + UUIDv4 hex (32 lowercase)."""
    clean = re.sub(r"[^a-z0-9_]", "", str(prefix).strip().lower()) or "id"
    if not clean[0].isalpha():
        clean = f"id_{clean}"
    return f"{clean}_{uuid.uuid4().hex}"


# Correlation / opaque ID fields that must not smuggle raw secrets.
_OPAQUE_ID_FIELDS = frozenset(
    {
        "event_id",
        "run_id",
        "operation_id",
        "leg_id",
        "dual_leg_id",
        "peer_leg_id",
        "request_fingerprint",
        "approval_record_id",
        "approval_grant_event_id",
        "consumed_for_operation_id",
        "approval_token_fingerprint",
    }
)

# Generated opaque IDs: prefix_ + UUIDv4 hex (version nibble 4, RFC4122 variant).
_GENERATED_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}_([0-9a-f]{32})$")
# Hyphenated UUIDv4.
_UUID_V4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
# Fixed-length lowercase hex fingerprints (request fp_ + 32 hex, or 64-hex HMAC).
_REQUEST_FP_RE = re.compile(r"^fp_[0-9a-f]{32}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
# Tight venue order-id: Bybit-style all-digit 8–32, or OKX-style uppercase alnum 16–32.
_VENUE_ORDER_ID_RE = re.compile(r"^(?:[0-9]{8,32}|[0-9A-Z]{16,32})$")
_OPAQUE_MAX_LEN = 96
_SECRET_SMUGGLE_RE = re.compile(
    r"(?i)(BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|-----BEGIN|"
    r"\beyJ[A-Za-z0-9_-]{20,}\.|"  # JWT-like
    r"\b(sk-|AKIA|ASIA)[A-Za-z0-9_-]{8,}|"
    r"api[_-]?secret|api[_-]?key\s*=|"
    r"password\s*=|secret[_-]?key|private[_-]?key)"
)


def _is_uuid4_hex(hex32: str) -> bool:
    if len(hex32) != 32 or not re.fullmatch(r"[0-9a-f]{32}", hex32):
        return False
    # UUID hex layout: version at nibble index 12, variant at index 16.
    return hex32[12] == "4" and hex32[16] in "89ab"


def assert_opaque_id_safe(field: str, value: Any) -> None:
    """Allow only UUIDv4-generated IDs, fixed hex fingerprints, or venue order ids."""
    if not isinstance(value, str):
        raise JournalValidationError(f"{field} must be string opaque id")
    if len(value) < 8 or len(value) > _OPAQUE_MAX_LEN:
        raise JournalValidationError(f"{field} opaque id length out of bounds")
    if any(ch.isspace() for ch in value):
        raise JournalValidationError(f"{field} opaque id must not contain whitespace")
    if any(ch in value for ch in "{}$\\`\n\r\t"):
        raise JournalValidationError(f"{field} opaque id charset rejected")
    if _SECRET_SMUGGLE_RE.search(value):
        raise JournalValidationError(f"{field} looks like smuggled secret material")
    if len(value) >= 40 and ("+" in value or "/" in value or value.endswith("=")):
        raise JournalValidationError(f"{field} rejects base64-shaped secret material")

    if field == "approval_token_fingerprint":
        if not APPROVAL_FP_RE.fullmatch(value):
            raise JournalValidationError(
                "approval_token_fingerprint must be 64-char lowercase hex"
            )
        return

    if field == "request_fingerprint" or field.endswith("fingerprint"):
        if _REQUEST_FP_RE.fullmatch(value) or _HEX64_RE.fullmatch(value):
            return
        raise JournalValidationError(
            f"{field} must be fp_+32hex or 64-char lowercase hex fingerprint"
        )

    # Generated prefix_ + UUIDv4 hex.
    m = _GENERATED_ID_RE.fullmatch(value)
    if m and _is_uuid4_hex(m.group(1)):
        return
    # Raw hyphenated UUIDv4.
    if _UUID_V4_RE.fullmatch(value):
        return
    # Tight venue order-id format only (no arbitrary alphanumerics).
    if _VENUE_ORDER_ID_RE.fullmatch(value):
        return
    raise JournalValidationError(f"{field} opaque id charset/type rejected")


def validate_opaque_ids_in_event(event: Mapping[str, Any]) -> None:
    for field in _OPAQUE_ID_FIELDS:
        if field in event and event[field] is not None:
            assert_opaque_id_safe(field, event[field])


def utc_now_rfc3339() -> str:
    # Millisecond precision, always Z.
    now = datetime.now(timezone.utc)
    ms = now.microsecond // 1000
    return now.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms:03d}Z"


def event_date_from_ts_utc(event_ts_utc: str) -> str:
    if not _RFC3339_Z_RE.match(event_ts_utc):
        raise JournalValidationError("event_ts_utc must be RFC3339 UTC with Z")
    return event_ts_utc[:10]


def _normalize_key_name(name: str) -> str:
    return str(name).strip().lower().replace("-", "_")


def assert_no_redaction_violations(obj: Any, *, path: str = "$") -> None:
    """Reject denylisted keys or values (case-insensitive; ``-`` ≡ ``_``)."""
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            nk = _normalize_key_name(k)
            if nk in REDACTION_DENYLIST:
                raise JournalValidationError(f"redaction denylist key at {path}.{nk}")
            if isinstance(v, str):
                nv = _normalize_key_name(v)
                if nv in REDACTION_DENYLIST and nv not in _ALLOWED_ENUM_VALUE_TOKENS:
                    raise JournalValidationError(
                        f"redaction denylist value at {path}.{nk}"
                    )
                if _looks_like_forbidden_payload(v):
                    raise JournalValidationError(
                        f"forbidden payload-shaped value at {path}.{nk}"
                    )
            assert_no_redaction_violations(v, path=f"{path}.{nk}")
        return
    if isinstance(obj, list):
        for i, item in enumerate(obj):
            assert_no_redaction_violations(item, path=f"{path}[{i}]")
        return
    if isinstance(obj, str):
        nv = _normalize_key_name(obj)
        if nv in REDACTION_DENYLIST and nv not in _ALLOWED_ENUM_VALUE_TOKENS:
            raise JournalValidationError(f"redaction denylist string at {path}")
        if _looks_like_forbidden_payload(obj):
            raise JournalValidationError(f"forbidden payload-shaped string at {path}")


def _looks_like_forbidden_payload(value: str) -> bool:
    if _URL_HINT_RE.search(value) and ("?" in value or "@" in value or "api" in value.lower()):
        return True
    # Block obvious secret-source path markers without requiring full path echo.
    lowered = value.lower()
    if "bbot-private" in lowered and lowered.endswith(".env"):
        return True
    if "/etc/spread/" in lowered:
        return True
    return False


def validate_clock_offset_evidence(evidence: Any) -> dict[str, Any]:
    if not isinstance(evidence, Mapping):
        raise JournalValidationError("clock_offset_evidence must be object")
    keys = set(evidence.keys())
    required = {"method", "measured_at_utc", "offset_ms"}
    if keys != required:
        raise JournalValidationError(
            "clock_offset_evidence must have exactly method, measured_at_utc, offset_ms"
        )
    method = evidence["method"]
    if method not in CLOCK_OFFSET_METHODS:
        raise JournalValidationError("invalid clock_offset_evidence.method")
    measured = evidence["measured_at_utc"]
    if not isinstance(measured, str) or not _RFC3339_Z_RE.match(measured):
        raise JournalValidationError("clock_offset_evidence.measured_at_utc invalid")
    offset = evidence["offset_ms"]
    if not isinstance(offset, (int, float)) or isinstance(offset, bool):
        raise JournalValidationError("clock_offset_evidence.offset_ms must be number")
    return {
        "method": method,
        "measured_at_utc": measured,
        "offset_ms": float(offset),
    }


def _ms_from_rfc3339(ts: str) -> float:
    if not _RFC3339_Z_RE.match(ts):
        raise JournalValidationError(f"invalid RFC3339 UTC: {ts!r}")
    if "." in ts:
        head, frac_z = ts.split(".", 1)
        frac = frac_z[:-1]  # drop Z
        frac = (frac + "000000")[:6]
        dt = datetime.strptime(head, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.timestamp() * 1000.0 + int(frac) / 1000.0
    dt = datetime.strptime(ts[:-1], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000.0


def assert_rtt_not_named_one_way(intervals: Mapping[str, Any], *, latency_basis: str) -> None:
    """Fail if RTT (or any interval) is labeled as one-way path latency."""
    for name in intervals:
        n = _normalize_key_name(name)
        for tok in _ONE_WAY_FORBIDDEN_TOKENS:
            if tok in n:
                raise JournalValidationError(
                    "RTT/observed latency must not be named as one-way path latency"
                )
        if n == "request_ack_rtt" and "one" in n and "way" in n:
            raise JournalValidationError("request_ack_rtt must remain RTT, not one-way")
    if latency_basis not in LATENCY_BASIS:
        raise JournalValidationError("invalid latency_basis")
    # Explicit: monotonic RTT is never marketed as one-way exchange latency.
    if "request_ack_rtt" in intervals and latency_basis not in LATENCY_BASIS:
        raise JournalValidationError("invalid latency_basis for RTT")


def derive_latency_intervals_from_op_events(
    events: Sequence[Mapping[str, Any]],
) -> dict[str, float]:
    """Derive allowlisted §5 intervals from one operation's journal events.

    Prefers monotonic anchors when present; falls back to ``event_ts_utc`` wall
    deltas only for ``request_ack_rtt`` / ``ack_terminal_receive``. Never invents
    ``exchange_to_client_observed`` or non-allowlisted cancel-RTT labels.
    """
    by_type: dict[str, list[Mapping[str, Any]]] = {}
    for ev in events:
        by_type.setdefault(str(ev.get("event_type")), []).append(ev)

    prepared = by_type.get("order_prepared", [None])[-1]
    sent = by_type.get("request_sent", [None])[-1]
    ack = by_type.get("ack_received", [None])[-1]
    terminal = by_type.get("terminal_update", [None])[-1]

    prep_mono = (
        int(prepared["event_monotonic_ns"])
        if prepared and "event_monotonic_ns" in prepared
        else None
    )
    send_mono = (
        int(sent["send_monotonic_ns"])
        if sent and "send_monotonic_ns" in sent
        else None
    )
    ack_recv = (
        int(ack["receive_monotonic_ns"])
        if ack and "receive_monotonic_ns" in ack
        else None
    )
    ack_evt = (
        int(ack["event_monotonic_ns"])
        if ack and "event_monotonic_ns" in ack
        else None
    )
    term_recv = (
        int(terminal["receive_monotonic_ns"])
        if terminal and "receive_monotonic_ns" in terminal
        else None
    )
    term_evt = (
        int(terminal["event_monotonic_ns"])
        if terminal and "event_monotonic_ns" in terminal
        else None
    )

    intervals = compute_latency_intervals_ms(
        order_prepared_monotonic_ns=prep_mono,
        request_sent_send_monotonic_ns=send_mono,
        ack_receive_monotonic_ns=ack_recv,
        ack_event_monotonic_ns=ack_evt,
        terminal_receive_monotonic_ns=term_recv,
        terminal_event_monotonic_ns=term_evt,
        terminal_event_ts_utc=str(terminal["event_ts_utc"]) if terminal else None,
    )

    # Wall-clock fallbacks when monotonic place/ack/terminal anchors are absent
    # (append-only repair from public timestamps). Cancel RTT has no allowlisted
    # interval name — never invent one; only place/ack/terminal labels.
    if "request_ack_rtt" not in intervals and sent and ack:
        try:
            start = _ms_from_rfc3339(str(sent["event_ts_utc"]))
            end = _ms_from_rfc3339(str(ack["event_ts_utc"]))
            if end >= start:
                intervals["request_ack_rtt"] = float(end - start)
        except (JournalValidationError, KeyError, TypeError, ValueError):
            pass
    if "ack_terminal_receive" not in intervals and ack and terminal:
        try:
            start = _ms_from_rfc3339(str(ack["event_ts_utc"]))
            end = _ms_from_rfc3339(str(terminal["event_ts_utc"]))
            if end >= start:
                intervals["ack_terminal_receive"] = float(end - start)
        except (JournalValidationError, KeyError, TypeError, ValueError):
            pass
    if (
        "local_prepare" not in intervals
        and prepared
        and sent
        and "event_ts_utc" in prepared
        and "event_ts_utc" in sent
    ):
        try:
            start = _ms_from_rfc3339(str(prepared["event_ts_utc"]))
            end = _ms_from_rfc3339(str(sent["event_ts_utc"]))
            if end >= start:
                intervals["local_prepare"] = float(end - start)
        except (JournalValidationError, KeyError, TypeError, ValueError):
            pass

    assert_rtt_not_named_one_way(intervals, latency_basis="monotonic_local")
    return intervals


def derive_cancel_rtt_ms_for_report(
    events: Sequence[Mapping[str, Any]],
) -> Optional[float]:
    """Public cancel RTT for operator reports only — not a journal interval name."""
    cancel_req = None
    cancel_ack = None
    for ev in events:
        et = str(ev.get("event_type"))
        if et == "cancel_requested":
            cancel_req = ev
        elif et == "cancel_ack":
            cancel_ack = ev
    if cancel_req is None or cancel_ack is None:
        return None
    send_ns = cancel_req.get("send_monotonic_ns")
    recv_ns = cancel_ack.get("receive_monotonic_ns")
    if isinstance(send_ns, int) and isinstance(recv_ns, int) and recv_ns >= send_ns:
        return (recv_ns - send_ns) / 1_000_000.0
    try:
        start = _ms_from_rfc3339(str(cancel_req["event_ts_utc"]))
        end = _ms_from_rfc3339(str(cancel_ack["event_ts_utc"]))
    except (JournalValidationError, KeyError, TypeError, ValueError):
        return None
    if end < start:
        return None
    return float(end - start)


def compute_latency_intervals_ms(
    *,
    order_prepared_monotonic_ns: Optional[int] = None,
    request_sent_send_monotonic_ns: Optional[int] = None,
    ack_receive_monotonic_ns: Optional[int] = None,
    ack_event_monotonic_ns: Optional[int] = None,
    terminal_receive_monotonic_ns: Optional[int] = None,
    terminal_event_monotonic_ns: Optional[int] = None,
    terminal_event_ts_utc: Optional[str] = None,
    exchange_event_ts_utc: Optional[str] = None,
    clock_offset_evidence: Optional[Mapping[str, Any]] = None,
) -> dict[str, float]:
    """Compute allowed §5 intervals. Never aliases RTT as one-way latency."""
    out: dict[str, float] = {}

    def _nonneg_ms(start_ns: int, end_ns: int, name: str) -> float:
        if end_ns < start_ns:
            raise JournalValidationError(f"{name}: end before start")
        return (end_ns - start_ns) / 1_000_000.0

    if (
        order_prepared_monotonic_ns is not None
        and request_sent_send_monotonic_ns is not None
    ):
        out["local_prepare"] = _nonneg_ms(
            order_prepared_monotonic_ns,
            request_sent_send_monotonic_ns,
            "local_prepare",
        )

    if (
        request_sent_send_monotonic_ns is not None
        and ack_receive_monotonic_ns is not None
    ):
        # Round-trip / observed response time — NOT one-way path latency.
        out["request_ack_rtt"] = _nonneg_ms(
            request_sent_send_monotonic_ns,
            ack_receive_monotonic_ns,
            "request_ack_rtt",
        )

    if ack_receive_monotonic_ns is not None and ack_event_monotonic_ns is not None:
        out["local_response_processing"] = _nonneg_ms(
            ack_receive_monotonic_ns,
            ack_event_monotonic_ns,
            "local_response_processing",
        )

    if (
        ack_receive_monotonic_ns is not None
        and terminal_receive_monotonic_ns is not None
    ):
        out["ack_terminal_receive"] = _nonneg_ms(
            ack_receive_monotonic_ns,
            terminal_receive_monotonic_ns,
            "ack_terminal_receive",
        )

    want_x2c = exchange_event_ts_utc is not None or clock_offset_evidence is not None
    if want_x2c:
        if exchange_event_ts_utc is None or clock_offset_evidence is None:
            raise JournalValidationError(
                "exchange_to_client_observed requires exchange_event_ts_utc "
                "and clock_offset_evidence"
            )
        if (
            terminal_receive_monotonic_ns is None
            or terminal_event_monotonic_ns is None
            or terminal_event_ts_utc is None
        ):
            raise JournalValidationError(
                "exchange_to_client_observed requires terminal receive/event anchors"
            )
        evidence = validate_clock_offset_evidence(clock_offset_evidence)
        # offset_ms := local_utc_ms - venue_utc_ms at probe time.
        exchange_ms = _ms_from_rfc3339(exchange_event_ts_utc)
        adjusted_local_ms = exchange_ms + float(evidence["offset_ms"])
        terminal_wall_ms = _ms_from_rfc3339(terminal_event_ts_utc)
        delta_ns = int(terminal_event_monotonic_ns) - int(terminal_receive_monotonic_ns)
        receive_wall_ms = terminal_wall_ms - (delta_ns / 1_000_000.0)
        value = receive_wall_ms - adjusted_local_ms
        if value < 0:
            raise JournalValidationError("exchange_to_client_observed would be negative")
        out["exchange_to_client_observed"] = float(value)

    assert_rtt_not_named_one_way(
        out,
        latency_basis=(
            "offset_adjusted_observed"
            if "exchange_to_client_observed" in out
            else "monotonic_local"
        ),
    )
    for k, v in out.items():
        if k not in LATENCY_INTERVAL_NAMES:
            raise JournalValidationError(f"unknown latency interval {k}")
        if not isinstance(v, (int, float)) or float(v) < 0:
            raise JournalValidationError(f"latency {k} must be >= 0")
    return out


def _validate_credential_presence(obj: Any) -> None:
    """Accept exactly one of two strict forms (legacy triplet or current single).

    Writers must emit current ``{"credentials_configured": bool}``. Validators
    also accept historical
    ``{"api_key_present","api_secret_present","passphrase_present"}`` all bool.
    Hybrid/extra/missing keys fail closed; no rewrite of historical rows.
    """
    if not isinstance(obj, Mapping):
        raise JournalValidationError("credential_presence must be object")
    keys = set(obj.keys())
    if keys == _CREDENTIAL_PRESENCE_KEYS_CURRENT:
        form = "current"
    elif keys == _CREDENTIAL_PRESENCE_KEYS_LEGACY:
        form = "legacy"
    else:
        raise JournalValidationError(
            "credential_presence must be exactly legacy "
            "{api_key_present,api_secret_present,passphrase_present} "
            "or current {credentials_configured}"
        )
    for k, v in obj.items():
        if not isinstance(v, bool):
            raise JournalValidationError(f"credential_presence.{k} must be bool")
    _ = form  # form distinguished only by key set above


# Historical venue/client order-id field names (normalized). Presence blocks
# legacy_pre_send_no_dispatch recognition (condition 4).
_LEGACY_PRE_SEND_ORDER_ID_KEYS = frozenset(
    {
        "order_id",
        "client_order_id",
        "exchange_order_id",
        "order_link_id",
        "orderlinkid",
        "cl_ord_id",
        "clordid",
        "ord_id",
        "ordid",
        "orig_cl_ord_id",
        "origclordid",
        "exchangeordid",
        "bybit_order_id",
        "okx_ord_id",
    }
)


def _event_has_order_id_fields(event: Mapping[str, Any]) -> bool:
    """True if any venue/client order-id field is present (any value)."""
    for key in event.keys():
        nk = _normalize_key_name(str(key))
        if nk in _LEGACY_PRE_SEND_ORDER_ID_KEYS:
            return True
    return False


def _is_exact_legacy_pre_send_reject(event: Mapping[str, Any]) -> bool:
    return (
        event.get("event_type") == "reject"
        and event.get("outcome") == "failure"
        and event.get("reject_stage") == "auth"
        and event.get("error_code") == "invalid_request"
    )


def _is_exact_legacy_pre_send_recon(event: Mapping[str, Any]) -> bool:
    return (
        event.get("event_type") == "reconciliation"
        and event.get("outcome") == "observed"
        and event.get("reconciliation_scope") == "order_state"
        and event.get("reconciliation_state") == "inconclusive"
    )


def find_legacy_pre_send_no_dispatch_ops(
    events: Sequence[Mapping[str, Any]],
) -> frozenset[str]:
    """Return operation_ids matching the exact §4.3 legacy_pre_send_no_dispatch pair.

    Recognition is exact and fail-closed: no variation, no rewrite. Requires
    contiguous reject→reconciliation as the only two events for that
    ``operation_id``, with no order-id fields and no later lifecycle types.
    """
    by_op: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for idx, event in enumerate(events):
        op = event.get("operation_id")
        if op is None:
            continue
        by_op.setdefault(str(op), []).append((idx, event))

    matched: set[str] = set()
    for op_id, items in by_op.items():
        if len(items) != 2:
            continue
        (i1, e1), (i2, e2) = items
        # Must be immediately consecutive in the global stream.
        if i2 != i1 + 1:
            continue
        if not _is_exact_legacy_pre_send_reject(e1):
            continue
        if not _is_exact_legacy_pre_send_recon(e2):
            continue
        if _event_has_order_id_fields(e1) or _event_has_order_id_fields(e2):
            continue
        matched.add(op_id)
    return frozenset(matched)


def materialize_legacy_pre_send_semantics(
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """In-memory only: replace exact legacy pairs with one ``pre_send_gate(blocked)``.

    Never mutates or rewrites durable JSONL. Unmatched events are shallow-copied.
    """
    legacy_ops = find_legacy_pre_send_no_dispatch_ops(events)
    out: list[dict[str, Any]] = []
    skip_recon = False
    for event in events:
        if skip_recon:
            skip_recon = False
            continue
        op_id = str(event.get("operation_id") or "")
        if op_id in legacy_ops and _is_exact_legacy_pre_send_reject(event):
            semantic: dict[str, Any] = {
                "schema_version": event.get("schema_version", SCHEMA_VERSION),
                "event_id": event["event_id"],
                "event_type": "pre_send_gate",
                "event_date": event["event_date"],
                "event_ts_utc": event["event_ts_utc"],
                "event_monotonic_ns": event["event_monotonic_ns"],
                "run_id": event["run_id"],
                "operation_id": event["operation_id"],
                "event_seq": event["event_seq"],
                "venue": event["venue"],
                "environment": event["environment"],
                "outcome": "observed",
                "gate_kind": "price",
                "gate_decision": "blocked",
            }
            out.append(semantic)
            skip_recon = True
            continue
        out.append(dict(event))
    return out


def validate_event_shape(
    event: Mapping[str, Any],
    *,
    require_opaque_ids: bool = True,
) -> None:
    """Validate one event object against the contract (no file chronology)."""
    if not isinstance(event, Mapping):
        raise JournalValidationError("event must be object")
    assert_no_redaction_violations(event)

    for key in COMMON_REQUIRED:
        if key not in event or event[key] is None:
            raise JournalValidationError(f"missing required field {key}")

    # Strict opaque IDs for all new/normal events. Legacy pre-send pair rows are
    # recognized by the stream validator before this gate is applied.
    if require_opaque_ids:
        validate_opaque_ids_in_event(event)

    if event["schema_version"] != SCHEMA_VERSION:
        raise JournalValidationError("invalid schema_version")

    event_type = event["event_type"]
    if event_type not in EVENT_TYPES:
        raise JournalValidationError(f"unknown event_type {event_type!r}")

    allowed_keys = set(COMMON_REQUIRED) | set(EXTRA_FIELDS_BY_TYPE[event_type])
    unknown = set(event.keys()) - allowed_keys
    if unknown:
        raise JournalValidationError(f"unknown fields for {event_type}: {sorted(unknown)}")

    outcome = event["outcome"]
    if outcome not in OUTCOMES_BY_TYPE[event_type]:
        raise JournalValidationError(
            f"outcome {outcome!r} not allowed for {event_type}"
        )

    if event["venue"] not in VENUES:
        raise JournalValidationError("invalid venue")
    if event["environment"] not in ENVIRONMENTS:
        raise JournalValidationError("invalid environment")

    for id_field in ("event_id", "run_id", "operation_id"):
        val = event[id_field]
        if not isinstance(val, str) or not val.strip():
            raise JournalValidationError(f"{id_field} must be non-empty string")

    ts = event["event_ts_utc"]
    if not isinstance(ts, str) or not _RFC3339_Z_RE.match(ts):
        raise JournalValidationError("event_ts_utc must be RFC3339 UTC with Z")
    date = event["event_date"]
    if not isinstance(date, str) or not _DATE_RE.match(date):
        raise JournalValidationError("event_date must be YYYY-MM-DD")
    if date != event_date_from_ts_utc(ts):
        raise JournalValidationError("event_date must match UTC date of event_ts_utc")

    mono = event["event_monotonic_ns"]
    if not isinstance(mono, int) or isinstance(mono, bool) or mono < 0:
        raise JournalValidationError("event_monotonic_ns must be int >= 0")
    seq = event["event_seq"]
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise JournalValidationError("event_seq must be int >= 1")

    # error_code vs outcome
    if outcome == "failure":
        if "error_code" not in event:
            raise JournalValidationError("error_code required when outcome=failure")
        if event["error_code"] not in ERROR_CODES:
            raise JournalValidationError("error_code not in allowlist")
    elif outcome == "success":
        if "error_code" in event:
            raise JournalValidationError("error_code forbidden when outcome=success")
    else:
        if "error_code" in event:
            raise JournalValidationError(
                "error_code forbidden when outcome is pending/observed"
            )

    # Type-specific enums / shapes — continue with existing branches below.
    # (Body of type-specific validation remains after this splice point.)
    _validate_event_type_fields(event, event_type)


def _validate_event_type_fields(event: Mapping[str, Any], event_type: str) -> None:
    if event_type == "auth":
        if event.get("auth_method") not in AUTH_METHODS:
            raise JournalValidationError("invalid auth_method")
        _validate_credential_presence(event.get("credential_presence"))
    elif event_type == "account_read":
        if event.get("account_scope") not in ACCOUNT_SCOPES:
            raise JournalValidationError("invalid account_scope")
        if event.get("request_kind") != "account_read":
            raise JournalValidationError("account_read.request_kind must be account_read")
    elif event_type == "operator_approval":
        _require_fields(
            event,
            (
                "approval_action",
                "approval_token_fingerprint",
                "approval_scope",
                "approval_record_id",
            ),
        )
        action = event["approval_action"]
        if action not in APPROVAL_ACTIONS:
            raise JournalValidationError("invalid approval_action")
        if event["approval_scope"] not in APPROVAL_SCOPES:
            raise JournalValidationError("invalid approval_scope")
        fp = event["approval_token_fingerprint"]
        if not isinstance(fp, str) or not APPROVAL_FP_RE.fullmatch(fp):
            raise JournalValidationError(
                "approval_token_fingerprint must be 64-char lowercase hex"
            )
        if not isinstance(event["approval_record_id"], str) or not event["approval_record_id"]:
            raise JournalValidationError("approval_record_id must be non-empty string")
        if action == "granted":
            if event.get("outcome") != "success":
                raise JournalValidationError("granted approval outcome must be success")
            if "approval_expires_at_utc" not in event:
                raise JournalValidationError("granted requires approval_expires_at_utc")
            if not isinstance(event["approval_expires_at_utc"], str) or not _RFC3339_Z_RE.match(
                event["approval_expires_at_utc"]
            ):
                raise JournalValidationError("invalid approval_expires_at_utc")
            if "approval_grant_event_id" in event or "consumed_for_operation_id" in event:
                raise JournalValidationError("granted must not include consume fields")
        else:
            if event.get("outcome") != "observed":
                raise JournalValidationError("consumed approval outcome must be observed")
            _require_fields(event, ("approval_grant_event_id", "consumed_for_operation_id"))
            if "approval_expires_at_utc" in event:
                raise JournalValidationError("consumed must not include approval_expires_at_utc")
            if str(event["operation_id"]) != str(event["consumed_for_operation_id"]):
                raise JournalValidationError(
                    "consumed operation_id must equal consumed_for_operation_id"
                )
            if not isinstance(event["approval_grant_event_id"], str) or not event[
                "approval_grant_event_id"
            ]:
                raise JournalValidationError("approval_grant_event_id must be non-empty string")
    elif event_type == "pre_send_gate":
        _require_fields(event, ("gate_kind", "gate_decision"))
        if event["gate_kind"] not in GATE_KINDS:
            raise JournalValidationError("invalid gate_kind")
        if event["gate_decision"] not in GATE_DECISIONS:
            raise JournalValidationError("invalid gate_decision")
        if event.get("outcome") != "observed":
            raise JournalValidationError("pre_send_gate outcome must be observed")
    elif event_type == "order_prepared":
        for req in (
            "dual_leg_id",
            "leg_id",
            "instrument_class",
            "symbol_alias",
            "side",
            "order_kind",
            "quantity_bucket",
            "notional_bucket",
            "reduce_only",
            "post_only",
            "request_fingerprint",
        ):
            if req not in event:
                raise JournalValidationError(f"order_prepared missing {req}")
        if event["instrument_class"] not in INSTRUMENT_CLASSES:
            raise JournalValidationError("invalid instrument_class")
        if event["side"] not in SIDES:
            raise JournalValidationError("invalid side")
        if event["order_kind"] not in ORDER_KINDS:
            raise JournalValidationError("invalid order_kind")
        if not isinstance(event["reduce_only"], bool):
            raise JournalValidationError("reduce_only must be bool")
        if not isinstance(event["post_only"], bool):
            raise JournalValidationError("post_only must be bool")
        if event["post_only"]:
            if event.get("ttl_bucket") not in TTL_BUCKETS:
                raise JournalValidationError("post_only requires ttl_bucket")
        elif "ttl_bucket" in event:
            raise JournalValidationError("ttl_bucket forbidden when post_only=false")
        if not isinstance(event["quantity_bucket"], str) or not event["quantity_bucket"]:
            raise JournalValidationError("quantity_bucket must be non-empty string")
        if not isinstance(event["notional_bucket"], str) or not event["notional_bucket"]:
            raise JournalValidationError("notional_bucket must be non-empty string")
        # Buckets are labels — reject numeric-looking exact trade sizes.
        for b in (event["quantity_bucket"], event["notional_bucket"]):
            if re.fullmatch(r"[-+]?\d+(\.\d+)?", b):
                raise JournalValidationError("bucket must not be a raw numeric size")
    elif event_type == "request_sent":
        if event.get("request_kind") not in REQUEST_KINDS:
            raise JournalValidationError("invalid request_kind")
        ta = event.get("transport_attempt")
        if not isinstance(ta, int) or isinstance(ta, bool) or ta < 1:
            raise JournalValidationError("transport_attempt must be int >= 1")
        if "send_monotonic_ns" in event:
            _check_mono_field(event, "send_monotonic_ns", lte_event=True)
        rk = event["request_kind"]
        if rk == "ws_subscribe":
            for forbidden in ("dual_leg_id", "leg_id", "request_fingerprint"):
                if forbidden in event:
                    raise JournalValidationError(
                        f"ws_subscribe forbids {forbidden}"
                    )
            _require_fields(
                event,
                (
                    "request_kind",
                    "transport_attempt",
                    "transport",
                    "reconnect_generation",
                    "subscription_readiness",
                ),
            )
            if event["transport"] != "ws_trade":
                raise JournalValidationError("ws_subscribe transport must be ws_trade")
            if event["subscription_readiness"] != "not_ready":
                raise JournalValidationError(
                    "ws_subscribe request_sent readiness must be not_ready"
                )
            _validate_reconnect_generation(event["reconnect_generation"])
        else:
            _require_fields(
                event,
                ("leg_id", "request_kind", "request_fingerprint", "transport_attempt"),
            )
            if "transport" in event:
                if event["transport"] not in TRANSPORTS:
                    raise JournalValidationError("invalid transport")
                if rk in {"place", "cancel"} and event["transport"] not in TRANSPORTS:
                    raise JournalValidationError("invalid transport for place/cancel")
            if "reconnect_generation" in event:
                # Pure REST order lifecycle forbids reconnect_generation.
                if event.get("transport") == "rest" or "transport" not in event:
                    raise JournalValidationError(
                        "reconnect_generation forbidden for pure REST order lifecycle"
                    )
                _validate_reconnect_generation(event["reconnect_generation"])
            if "subscription_readiness" in event:
                raise JournalValidationError(
                    "subscription_readiness only for ws_subscribe / private-stream recon"
                )
    elif event_type == "ack_received":
        if event.get("ack_state") not in ACK_STATES:
            raise JournalValidationError("invalid ack_state")
        if event.get("request_kind") not in REQUEST_KINDS:
            raise JournalValidationError("invalid request_kind")
        rk = event["request_kind"]
        if rk == "ws_subscribe":
            for forbidden in ("dual_leg_id", "leg_id", "request_fingerprint"):
                if forbidden in event:
                    raise JournalValidationError(
                        f"ws_subscribe forbids {forbidden}"
                    )
            _require_fields(
                event,
                (
                    "request_kind",
                    "ack_state",
                    "receive_monotonic_ns",
                    "transport",
                    "reconnect_generation",
                    "subscription_readiness",
                ),
            )
            if event["transport"] != "ws_trade":
                raise JournalValidationError("ws_subscribe transport must be ws_trade")
            if event["outcome"] == "success" and event["subscription_readiness"] != "ready":
                raise JournalValidationError(
                    "successful ws_subscribe ack requires subscription_readiness=ready"
                )
            if event["outcome"] == "success" and event["ack_state"] != "received":
                raise JournalValidationError(
                    "ws_subscribe ack_state must be received"
                )
            _validate_reconnect_generation(event["reconnect_generation"])
            _check_mono_field(event, "receive_monotonic_ns", lte_event=True)
        else:
            _require_fields(
                event,
                (
                    "leg_id",
                    "request_kind",
                    "request_fingerprint",
                    "ack_state",
                    "receive_monotonic_ns",
                ),
            )
            _check_mono_field(event, "receive_monotonic_ns", lte_event=True)
            if "transport" in event:
                if event["transport"] not in TRANSPORTS:
                    raise JournalValidationError("invalid transport")
            if "reconnect_generation" in event:
                if event.get("transport") == "rest" or "transport" not in event:
                    raise JournalValidationError(
                        "reconnect_generation forbidden for pure REST order lifecycle"
                    )
                _validate_reconnect_generation(event["reconnect_generation"])
            if "subscription_readiness" in event:
                raise JournalValidationError(
                    "subscription_readiness only for ws_subscribe / private-stream recon"
                )
    elif event_type == "terminal_update":
        _require_fields(
            event,
            ("leg_id", "terminal_state", "request_fingerprint", "receive_monotonic_ns"),
        )
        if event["terminal_state"] not in TERMINAL_STATES:
            raise JournalValidationError("invalid terminal_state")
        _check_mono_field(event, "receive_monotonic_ns", lte_event=True)
        has_ex = "exchange_event_ts_utc" in event
        has_ev = "clock_offset_evidence" in event
        if has_ex or has_ev:
            if not (has_ex and has_ev):
                raise JournalValidationError(
                    "exchange_event_ts_utc requires clock_offset_evidence and vice versa"
                )
            if not isinstance(event["exchange_event_ts_utc"], str) or not _RFC3339_Z_RE.match(
                event["exchange_event_ts_utc"]
            ):
                raise JournalValidationError("invalid exchange_event_ts_utc")
            validate_clock_offset_evidence(event["clock_offset_evidence"])
        if "observation_source" in event:
            if event["observation_source"] not in OBSERVATION_SOURCES:
                raise JournalValidationError("invalid observation_source")
            if event["observation_source"] == "private_ws":
                if event.get("sequence_state") != "healthy":
                    raise JournalValidationError(
                        "private_ws terminal_update requires sequence_state=healthy"
                    )
                if "reconnect_generation" not in event:
                    raise JournalValidationError(
                        "private_ws terminal_update requires reconnect_generation"
                    )
                _validate_reconnect_generation(event["reconnect_generation"])
            elif event["observation_source"] == "rest_reconcile":
                if "sequence_state" in event and event["sequence_state"] not in SEQUENCE_STATES:
                    raise JournalValidationError("invalid sequence_state")
        elif "sequence_state" in event or "reconnect_generation" in event:
            # Legacy rows omit observation_source; WS fields without it are invalid.
            raise JournalValidationError(
                "sequence_state/reconnect_generation require observation_source"
            )
        if "sequence_state" in event and event["sequence_state"] not in SEQUENCE_STATES:
            raise JournalValidationError("invalid sequence_state")
        if "reconnect_generation" in event and "observation_source" in event:
            _validate_reconnect_generation(event["reconnect_generation"])
    elif event_type == "cancel_requested":
        _require_fields(event, ("leg_id", "request_fingerprint", "cancel_reason"))
        if event["cancel_reason"] not in CANCEL_REASONS:
            raise JournalValidationError("invalid cancel_reason")
        if "send_monotonic_ns" in event:
            _check_mono_field(event, "send_monotonic_ns", lte_event=True)
        if "transport" in event and event["transport"] not in TRANSPORTS:
            raise JournalValidationError("invalid transport")
        if "reconnect_generation" in event:
            _validate_reconnect_generation(event["reconnect_generation"])
    elif event_type == "cancel_ack":
        _require_fields(
            event,
            ("leg_id", "cancel_state", "request_fingerprint", "receive_monotonic_ns"),
        )
        if event["cancel_state"] not in CANCEL_STATES:
            raise JournalValidationError("invalid cancel_state")
        _check_mono_field(event, "receive_monotonic_ns", lte_event=True)
        if "transport" in event and event["transport"] not in TRANSPORTS:
            raise JournalValidationError("invalid transport")
        if "reconnect_generation" in event:
            _validate_reconnect_generation(event["reconnect_generation"])
    elif event_type == "reject":
        _require_fields(event, ("reject_stage", "error_code"))
        if event["reject_stage"] not in REJECT_STAGES:
            raise JournalValidationError("invalid reject_stage")
    elif event_type == "dual_leg_abort":
        _require_fields(
            event,
            ("dual_leg_id", "leg_id", "peer_leg_id", "abort_reason", "request_fingerprint"),
        )
        if event["abort_reason"] not in ABORT_REASONS:
            raise JournalValidationError("invalid abort_reason")
        if event["leg_id"] == event["peer_leg_id"]:
            raise JournalValidationError("peer_leg_id must differ from leg_id")
    elif event_type == "reconciliation":
        _require_fields(
            event,
            ("reconciliation_scope", "reconciliation_state"),
        )
        if event["reconciliation_scope"] not in RECON_SCOPES:
            raise JournalValidationError("invalid reconciliation_scope")
        if event["reconciliation_state"] not in RECON_STATES:
            raise JournalValidationError("invalid reconciliation_state")
        if event["reconciliation_state"] == "mismatch":
            if event.get("outcome") != "failure":
                raise JournalValidationError("mismatch reconciliation outcome must be failure")
            if event.get("error_code") != "reconciliation_mismatch":
                raise JournalValidationError(
                    "mismatch requires error_code=reconciliation_mismatch"
                )
        if "mismatch_fields" in event:
            mf = event["mismatch_fields"]
            if not isinstance(mf, list):
                raise JournalValidationError("mismatch_fields must be array")
            for item in mf:
                if item not in MISMATCH_FIELDS:
                    raise JournalValidationError("invalid mismatch_fields entry")
        scope = event["reconciliation_scope"]
        if scope == "private_stream_reseed":
            _require_fields(
                event,
                (
                    "observation_source",
                    "reconnect_generation",
                    "sequence_state",
                    "subscription_readiness",
                ),
            )
            if event["observation_source"] not in OBSERVATION_SOURCES:
                raise JournalValidationError("invalid observation_source")
            if event["sequence_state"] not in SEQUENCE_STATES:
                raise JournalValidationError("invalid sequence_state")
            if event["subscription_readiness"] not in SUBSCRIPTION_READINESS:
                raise JournalValidationError("invalid subscription_readiness")
            _validate_reconnect_generation(event["reconnect_generation"])
            src = event["observation_source"]
            if src == "private_ws":
                if "transport" in event:
                    raise JournalValidationError(
                        "transport forbidden on private_ws gap/reconnect observation"
                    )
                if event["sequence_state"] not in {"gap", "reseed_required"}:
                    raise JournalValidationError(
                        "private_ws stream recon requires gap or reseed_required"
                    )
                if event["subscription_readiness"] != "not_ready":
                    raise JournalValidationError(
                        "private_ws stream recon readiness must be not_ready"
                    )
            elif src == "rest_reconcile":
                if event.get("transport") != "rest":
                    raise JournalValidationError(
                        "rest_reconcile private_stream_reseed requires transport=rest"
                    )
                if event["reconciliation_state"] == "matched":
                    if event["sequence_state"] != "healthy":
                        raise JournalValidationError(
                            "matched rest reseed requires sequence_state=healthy"
                        )
                    if event["subscription_readiness"] != "ready":
                        raise JournalValidationError(
                            "matched rest reseed requires subscription_readiness=ready"
                        )
        else:
            # Legacy-compatible: WS fields optional; validate when present.
            if "observation_source" in event:
                if event["observation_source"] not in OBSERVATION_SOURCES:
                    raise JournalValidationError("invalid observation_source")
            if "transport" in event and event["transport"] not in TRANSPORTS:
                raise JournalValidationError("invalid transport")
            if "sequence_state" in event and event["sequence_state"] not in SEQUENCE_STATES:
                raise JournalValidationError("invalid sequence_state")
            if (
                "subscription_readiness" in event
                and event["subscription_readiness"] not in SUBSCRIPTION_READINESS
            ):
                raise JournalValidationError("invalid subscription_readiness")
            if "reconnect_generation" in event:
                _validate_reconnect_generation(event["reconnect_generation"])
    elif event_type == "latency_summary":
        _require_fields(event, ("latency_intervals_ms", "latency_basis", "sample_count"))
        basis = event["latency_basis"]
        if basis not in LATENCY_BASIS:
            raise JournalValidationError("invalid latency_basis")
        sc = event["sample_count"]
        if not isinstance(sc, int) or isinstance(sc, bool) or sc < 1:
            raise JournalValidationError("sample_count must be int >= 1")
        intervals = event["latency_intervals_ms"]
        if not isinstance(intervals, Mapping) or not intervals:
            raise JournalValidationError("latency_intervals_ms must be non-empty object")
        for name, val in intervals.items():
            if name not in LATENCY_INTERVAL_NAMES:
                raise JournalValidationError(f"invalid latency interval name {name}")
            if not isinstance(val, (int, float)) or isinstance(val, bool) or float(val) < 0:
                raise JournalValidationError(f"latency {name} must be number >= 0")
        assert_rtt_not_named_one_way(intervals, latency_basis=basis)
        if "exchange_to_client_observed" in intervals:
            if "clock_offset_evidence" not in event:
                raise JournalValidationError(
                    "exchange_to_client_observed requires clock_offset_evidence"
                )
            validate_clock_offset_evidence(event["clock_offset_evidence"])
            if basis != "offset_adjusted_observed":
                raise JournalValidationError(
                    "exchange_to_client_observed requires latency_basis="
                    "offset_adjusted_observed"
                )
        elif "clock_offset_evidence" in event:
            validate_clock_offset_evidence(event["clock_offset_evidence"])


def _require_fields(event: Mapping[str, Any], names: Sequence[str]) -> None:
    for n in names:
        if n not in event or event[n] is None:
            raise JournalValidationError(f"missing field {n}")


def _validate_reconnect_generation(val: Any) -> None:
    if not isinstance(val, int) or isinstance(val, bool) or val < 0:
        raise JournalValidationError("reconnect_generation must be int >= 0")


def _check_mono_field(
    event: Mapping[str, Any], name: str, *, lte_event: bool
) -> None:
    val = event[name]
    if not isinstance(val, int) or isinstance(val, bool) or val < 0:
        raise JournalValidationError(f"{name} must be int >= 0")
    if lte_event and val > int(event["event_monotonic_ns"]):
        raise JournalValidationError(f"{name} must be <= event_monotonic_ns")


@dataclass
class _OpState:
    types: list[str] = field(default_factory=list)
    leg_id: Optional[str] = None
    dual_leg_id: Optional[str] = None
    request_fingerprint: Optional[str] = None
    saw_ack: bool = False
    saw_terminal: bool = False
    saw_approval_consumed: bool = False
    saw_final_dispatch_recon: bool = False
    saw_pre_send_gate: bool = False
    dual_leg_legs: set[str] = field(default_factory=set)


def _validate_lifecycle_step(state: _OpState, event: Mapping[str, Any]) -> None:
    et = str(event["event_type"])
    if et in {"auth", "account_read"}:
        state.types.append(et)
        return
    if et == "operator_approval":
        if event.get("approval_action") == "consumed":
            state.saw_approval_consumed = True
        state.types.append(et)
        return

    if et == "pre_send_gate":
        # Non-order block before prepare/dispatch. May follow consumed approval.
        if "order_prepared" in state.types or "request_sent" in state.types:
            raise JournalValidationError(
                "pre_send_gate must precede order_prepared and request_sent"
            )
        if state.saw_pre_send_gate:
            raise JournalValidationError("duplicate pre_send_gate for operation_id")
        state.saw_pre_send_gate = True
        state.types.append(et)
        return

    if et in {
        "order_prepared",
        "request_sent",
        "ack_received",
        "terminal_update",
        "cancel_requested",
        "cancel_ack",
        "reject",
        "dual_leg_abort",
        "reconciliation",
        "latency_summary",
    }:
        leg = event.get("leg_id")
        dual = event.get("dual_leg_id")
        fp = event.get("request_fingerprint")
        if et == "order_prepared":
            if state.saw_pre_send_gate:
                raise JournalValidationError(
                    "order_prepared forbidden after pre_send_gate on same operation_id"
                )
            # Live send path requires durable consumed approval before prepare.
            if str(event.get("environment")) == "live" and not state.saw_approval_consumed:
                raise JournalValidationError(
                    "live order_prepared requires prior consumed operator_approval"
                )
            if state.types and state.types[-1] not in {
                "auth",
                "account_read",
                "operator_approval",
            }:
                if "order_prepared" in state.types:
                    raise JournalValidationError("duplicate order_prepared for operation_id")
            state.leg_id = str(leg)
            state.dual_leg_id = str(dual) if dual is not None else None
            state.request_fingerprint = str(fp)
            if state.dual_leg_id:
                state.dual_leg_legs.add(str(leg))
            state.types.append(et)
            return

        if et == "request_sent":
            rk = event.get("request_kind")
            if rk == "ws_subscribe":
                if "auth" not in state.types:
                    raise JournalValidationError(
                        "ws_subscribe requires prior auth on same operation_id"
                    )
                if event.get("outcome") == "pending" and "order_prepared" in state.types:
                    raise JournalValidationError(
                        "ws_subscribe must not share order lifecycle operation_id"
                    )
                state.types.append(et)
                return
            if "order_prepared" not in state.types:
                raise JournalValidationError("request_sent requires prior order_prepared")
            _check_corr(state, leg, dual, fp)
            state.types.append(et)
            return

        if et == "ack_received":
            if "request_sent" not in state.types:
                raise JournalValidationError("ack_received requires prior request_sent")
            rk = event.get("request_kind")
            if rk == "ws_subscribe":
                state.saw_ack = True
                state.types.append(et)
                return
            _check_corr(state, leg, dual, fp)
            state.saw_ack = True
            state.types.append(et)
            return

        if et == "cancel_requested":
            if not state.saw_ack:
                raise JournalValidationError("cancel_requested requires prior ack_received")
            _check_corr(state, leg, dual, fp)
            state.types.append(et)
            return

        if et == "cancel_ack":
            if "cancel_requested" not in state.types:
                raise JournalValidationError("cancel_ack requires prior cancel_requested")
            _check_corr(state, leg, dual, fp)
            state.types.append(et)
            return

        if et == "terminal_update":
            if not state.saw_ack:
                raise JournalValidationError("terminal_update requires prior ack_received")
            if state.saw_terminal:
                raise JournalValidationError(
                    "repeat terminal_update forbidden; use reconciliation"
                )
            _check_corr(state, leg, dual, fp)
            # Private WS terminal must follow a trade (place/cancel) ACK, not only subscribe.
            if event.get("observation_source") == "private_ws":
                if not any(
                    e == "ack_received" for e in state.types
                ):
                    raise JournalValidationError(
                        "private_ws terminal_update requires prior ack_received"
                    )
            state.saw_terminal = True
            state.types.append(et)
            return

        if et == "reject":
            stage = event.get("reject_stage")
            # Do not pretend auth failure for pre-dispatch rest/price/preflight.
            # Use event_type=auth for auth outcomes, or pre_send_gate for gates.
            if stage == "auth" and "request_sent" not in state.types:
                raise JournalValidationError(
                    "reject auth requires prior request_sent; "
                    "use auth or pre_send_gate before dispatch"
                )
            if stage == "prepare" and "order_prepared" not in state.types:
                raise JournalValidationError("reject prepare without order_prepared")
            if stage == "send" and "request_sent" not in state.types:
                raise JournalValidationError("reject send without request_sent")
            if stage == "ack" and not state.saw_ack:
                raise JournalValidationError("reject ack without ack_received")
            if stage == "cancel" and "cancel_requested" not in state.types:
                raise JournalValidationError("reject cancel without cancel_requested")
            if leg is not None or dual is not None or fp is not None:
                if state.leg_id is not None:
                    _check_corr(state, leg, dual, fp)
            state.types.append(et)
            return

        if et == "dual_leg_abort":
            if dual is None:
                raise JournalValidationError("dual_leg_abort requires dual_leg_id")
            if state.dual_leg_id is not None and str(dual) != state.dual_leg_id:
                raise JournalValidationError("dual_leg_id correlation mismatch")
            if not state.types:
                raise JournalValidationError(
                    "dual_leg_abort cannot precede observations that caused it"
                )
            peer = str(event["peer_leg_id"])
            if str(leg) == peer:
                raise JournalValidationError("peer_leg_id must differ from leg_id")
            state.dual_leg_legs.add(str(leg))
            state.dual_leg_legs.add(peer)
            if len(state.dual_leg_legs) < 2:
                raise JournalValidationError(
                    "dual_leg_id must reference two distinct leg_ids"
                )
            state.types.append(et)
            return

        if et == "reconciliation":
            if not state.types:
                raise JournalValidationError("reconciliation requires prior events")
            # Gate-blocked ops never started dispatch — no reconciliation.
            if state.saw_pre_send_gate and "request_sent" not in state.types:
                raise JournalValidationError(
                    "reconciliation forbidden after pre_send_gate without request_sent"
                )
            scope = event.get("reconciliation_scope")
            # Scopes that describe order/dispatch state require transport dispatch.
            if scope in {
                "order_state",
                "request_ack",
                "post_dispatch_ambiguity",
                "post_only_ttl_recovery",
            }:
                if "request_sent" not in state.types:
                    raise JournalValidationError(
                        f"reconciliation({scope}) requires prior request_sent"
                    )
            if scope == "private_stream_reseed":
                # May follow auth / ws_subscribe on the stream operation.
                if "auth" not in state.types and "request_sent" not in state.types:
                    raise JournalValidationError(
                        "private_stream_reseed requires prior auth or request_sent"
                    )
            if leg is not None and state.leg_id is not None and str(leg) != state.leg_id:
                raise JournalValidationError("leg_id correlation mismatch")
            if event.get("reconciliation_scope") == "post_dispatch_ambiguity":
                if event.get("reconciliation_state") in {"matched", "mismatch"}:
                    state.saw_final_dispatch_recon = True
            state.types.append(et)
            return

        if et == "latency_summary":
            # Must follow events it summarizes — at least prepare+sent+ack for RTT.
            if "ack_received" not in state.types and "request_sent" not in state.types:
                if "order_prepared" not in state.types and "account_read" not in state.types:
                    raise JournalValidationError(
                        "latency_summary requires prior described events"
                    )
            intervals = event.get("latency_intervals_ms") or {}
            if "request_ack_rtt" in intervals and "ack_received" not in state.types:
                raise JournalValidationError(
                    "request_ack_rtt requires prior ack_received in operation"
                )
            if "local_prepare" in intervals and "request_sent" not in state.types:
                raise JournalValidationError(
                    "local_prepare requires prior request_sent in operation"
                )
            if (
                "exchange_to_client_observed" in intervals
                and "terminal_update" not in state.types
            ):
                raise JournalValidationError(
                    "exchange_to_client_observed requires prior terminal_update"
                )
            if leg is not None and state.leg_id is not None and str(leg) != state.leg_id:
                raise JournalValidationError("leg_id correlation mismatch")
            state.types.append(et)
            return

def _check_corr(
    state: _OpState,
    leg: Any,
    dual: Any,
    fp: Any,
) -> None:
    if state.leg_id is not None and leg is not None and str(leg) != state.leg_id:
        raise JournalValidationError("leg_id must remain constant for operation_id")
    if state.dual_leg_id is not None and dual is not None and str(dual) != state.dual_leg_id:
        raise JournalValidationError("dual_leg_id must remain constant for operation_id")
    if (
        state.request_fingerprint is not None
        and fp is not None
        and str(fp) != state.request_fingerprint
    ):
        raise JournalValidationError(
            "request_fingerprint must remain constant for operation_id"
        )


def validate_event_stream(
    events: Sequence[Mapping[str, Any]],
    *,
    expected_partition_date: Optional[str] = None,
) -> None:
    """Validate a chronological list of events for one or more runs."""
    seen_event_ids: set[str] = set()
    # run_id -> last seq, last mono, last utc, seen seqs
    run_meta: dict[str, dict[str, Any]] = {}
    ops: dict[str, _OpState] = {}
    dual_legs: dict[str, set[str]] = {}
    approval_grants: dict[str, dict[str, Any]] = {}  # record_id -> grant event
    approval_consumed: set[str] = set()  # record_ids
    approval_fps_granted: set[str] = set()
    # (run_id, venue, environment) → private WS stream gate state.
    stream_gates: dict[tuple[str, str, str], dict[str, Any]] = {}

    # Recognize exact historical pairs before opaque-ID / lifecycle gates.
    legacy_pre_send_ops = find_legacy_pre_send_no_dispatch_ops(events)

    for event in events:
        op_id = str(event["operation_id"])
        is_legacy_pair_row = op_id in legacy_pre_send_ops and (
            _is_exact_legacy_pre_send_reject(event)
            or _is_exact_legacy_pre_send_recon(event)
        )
        # Legacy pair rows skip opaque-ID gate; all other events remain strict.
        validate_event_shape(event, require_opaque_ids=not is_legacy_pair_row)
        if expected_partition_date is not None and event["event_date"] != expected_partition_date:
            raise JournalValidationError(
                "event_date must match journal directory partition"
            )

        eid = str(event["event_id"])
        if eid in seen_event_ids:
            raise JournalValidationError("duplicate event_id")
        seen_event_ids.add(eid)

        run_id = str(event["run_id"])
        meta = run_meta.setdefault(
            run_id,
            {"last_seq": 0, "last_mono": -1, "last_ts": None, "seqs": set()},
        )
        seq = int(event["event_seq"])
        mono = int(event["event_monotonic_ns"])
        ts = str(event["event_ts_utc"])
        if seq in meta["seqs"]:
            raise JournalValidationError("duplicate event_seq within run_id")
        if seq <= meta["last_seq"]:
            raise JournalValidationError("event_seq must strictly increase within run_id")
        if mono <= meta["last_mono"]:
            raise JournalValidationError(
                "event_monotonic_ns must strictly increase within run_id"
            )
        if meta["last_ts"] is not None and ts < meta["last_ts"]:
            raise JournalValidationError("event_ts_utc must be non-decreasing within run_id")
        meta["seqs"].add(seq)
        meta["last_seq"] = seq
        meta["last_mono"] = mono
        meta["last_ts"] = ts

        _validate_private_ws_stream_gate(event, stream_gates)

        state = ops.setdefault(op_id, _OpState())
        if op_id in legacy_pre_send_ops and _is_exact_legacy_pre_send_reject(event):
            # Semantic supersession: treat as pre_send_gate(blocked) in memory only.
            _validate_lifecycle_step(
                state,
                {
                    "event_type": "pre_send_gate",
                    "outcome": "observed",
                    "gate_kind": "price",
                    "gate_decision": "blocked",
                },
            )
        elif op_id in legacy_pre_send_ops and _is_exact_legacy_pre_send_recon(event):
            # Paired recon is absorbed by the semantic pre_send_gate above.
            if not state.saw_pre_send_gate:
                raise JournalValidationError(
                    "legacy_pre_send_no_dispatch recon without prior semantic gate"
                )
        else:
            _validate_lifecycle_step(state, event)

        if event["event_type"] == "operator_approval":
            _validate_approval_stream_step(
                event,
                approval_grants=approval_grants,
                approval_consumed=approval_consumed,
                approval_fps_granted=approval_fps_granted,
            )

        if event.get("dual_leg_id") and event.get("leg_id"):
            dual_legs.setdefault(str(event["dual_leg_id"]), set()).add(str(event["leg_id"]))
        if event["event_type"] == "dual_leg_abort":
            dual_legs.setdefault(str(event["dual_leg_id"]), set()).add(str(event["leg_id"]))
            dual_legs.setdefault(str(event["dual_leg_id"]), set()).add(
                str(event["peer_leg_id"])
            )
            if len(dual_legs[str(event["dual_leg_id"])]) < 2:
                raise JournalValidationError(
                    "dual_leg_id must reference two distinct leg_ids"
                )


def _stream_gate_key(event: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(event["run_id"]), str(event["venue"]), str(event["environment"]))


def _validate_private_ws_stream_gate(
    event: Mapping[str, Any],
    stream_gates: dict[tuple[str, str, str], dict[str, Any]],
) -> None:
    """Enforce reconnect_generation monotonicity and place/cancel block until REST reseed."""
    key = _stream_gate_key(event)
    gate = stream_gates.setdefault(
        key,
        {
            "last_gen": None,
            "sends_blocked": False,
            "saw_ws": False,
        },
    )
    et = str(event["event_type"])
    gen = event.get("reconnect_generation")
    rk = event.get("request_kind")

    if gen is not None:
        if not isinstance(gen, int) or isinstance(gen, bool) or gen < 0:
            raise JournalValidationError("reconnect_generation must be int >= 0")
        last = gate["last_gen"]
        if last is not None and gen < last:
            raise JournalValidationError(
                "reconnect_generation must not decrease within run/venue/environment"
            )
        if et in {"request_sent", "ack_received"} and rk == "ws_subscribe":
            if last is not None and gen > last + 1:
                raise JournalValidationError(
                    "new private WS login generation must increase by exactly one"
                )
            if last is not None and gen == last + 1:
                # New generation after reconnect — block until REST reseed.
                gate["sends_blocked"] = True
            elif last is None:
                # First subscription generation — block until REST reseed.
                gate["sends_blocked"] = True
            gate["last_gen"] = gen
            gate["saw_ws"] = True
        elif last is None or gen >= last:
            gate["last_gen"] = gen
            gate["saw_ws"] = True

    if et == "reconciliation" and event.get("reconciliation_scope") == "private_stream_reseed":
        src = event.get("observation_source")
        seq_state = event.get("sequence_state")
        if src == "private_ws" and seq_state in {"gap", "reseed_required"}:
            gate["sends_blocked"] = True
        elif (
            src == "rest_reconcile"
            and event.get("reconciliation_state") == "matched"
            and seq_state == "healthy"
            and event.get("subscription_readiness") == "ready"
            and event.get("outcome") == "success"
        ):
            gate["sends_blocked"] = False
        elif src == "rest_reconcile" and event.get("reconciliation_state") != "matched":
            gate["sends_blocked"] = True

    if et == "request_sent" and rk in {"place", "cancel"}:
        if gate["sends_blocked"]:
            raise JournalValidationError(
                "place/cancel blocked until private_stream_reseed rest matched"
            )


def _validate_approval_stream_step(
    event: Mapping[str, Any],
    *,
    approval_grants: dict[str, dict[str, Any]],
    approval_consumed: set[str],
    approval_fps_granted: set[str],
) -> None:
    record_id = str(event["approval_record_id"])
    fp = str(event["approval_token_fingerprint"])
    action = str(event["approval_action"])
    if action == "granted":
        if record_id in approval_grants:
            raise JournalValidationError("duplicate approval_record_id grant")
        if fp in approval_fps_granted:
            raise JournalValidationError("duplicate approval_token_fingerprint grant")
        approval_grants[record_id] = dict(event)
        approval_fps_granted.add(fp)
        return
    # consumed
    grant = approval_grants.get(record_id)
    if grant is None:
        raise JournalValidationError("consumed without prior granted approval_record_id")
    if str(grant["approval_token_fingerprint"]) != fp:
        raise JournalValidationError("consumed fingerprint mismatch vs grant")
    if str(grant["approval_scope"]) != str(event["approval_scope"]):
        raise JournalValidationError("consumed scope mismatch vs grant")
    if str(event["approval_grant_event_id"]) != str(grant["event_id"]):
        raise JournalValidationError("approval_grant_event_id must reference grant event_id")
    if record_id in approval_consumed:
        raise JournalValidationError("approval_record_id already consumed")
    # Expiry vs consume wall clock.
    expires = str(grant["approval_expires_at_utc"])
    if str(event["event_ts_utc"]) > expires:
        raise JournalValidationError("approval consumed after approval_expires_at_utc")
    approval_consumed.add(record_id)


def read_events_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read events.jsonl; reject truncated final line fail-closed."""
    if not path.is_file():
        return []
    raw = path.read_bytes()
    if not raw:
        return []
    if not raw.endswith(b"\n"):
        raise JournalValidationError("truncated final journal line (missing trailing newline)")
    text = raw.decode("utf-8")
    events: list[dict[str, Any]] = []
    for i, line in enumerate(text.split("\n")):
        if line == "":
            continue
        if not line.strip():
            raise JournalValidationError(f"blank journal line at index {i}")
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise JournalValidationError(f"invalid JSONL at line {i}") from exc
        if not isinstance(obj, dict):
            raise JournalValidationError(f"journal line {i} is not an object")
        events.append(obj)
    return events


def validate_events_file(path: Path, *, partition_date: Optional[str] = None) -> list[dict[str, Any]]:
    if _is_under_denied(path):
        raise JournalValidationError(f"refusing to validate denied path: {path}")
    # Also refuse stub journal tree explicitly.
    text = str(path.resolve()) if path.exists() else str(path)
    if text == "/data/bbot/journal" or text.startswith("/data/bbot/journal/"):
        raise JournalValidationError("private journal must not use stub journal tree")
    date = partition_date
    if date is None:
        # Prefer directory event_date=YYYY-MM-DD
        parent = path.parent.name
        if parent.startswith("event_date="):
            date = parent.split("=", 1)[1]
    events = read_events_jsonl(path)
    validate_event_stream(events, expected_partition_date=date)
    return events


def journal_lock_path(data_root: Path, event_date: str) -> Path:
    """Deprecated partition lock path — retained only to reject as layout violation."""
    if not _DATE_RE.match(event_date):
        raise JournalValidationError("invalid event_date for lock")
    lock_dir = data_root / "journal"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir / f".writer.lock.event_date={event_date}"


def approval_lock_path(data_root: Path) -> Path:
    """Global exclusive approval lock: journal/.approval.lock only."""
    if _is_under_denied(data_root):
        raise JournalValidationError("refusing journal lock under denied path")
    lock_dir = data_root / "journal"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir / APPROVAL_LOCK_BASENAME


class GlobalApprovalLock:
    """Exclusive inter-process lock spanning all journal partitions."""

    def __init__(self, data_root: Path) -> None:
        self._path = approval_lock_path(data_root)
        self._fh: Optional[Any] = None

    def __enter__(self) -> "GlobalApprovalLock":
        import fcntl

        self._fh = self._path.open("a+", encoding="utf-8")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        import fcntl

        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None


# Backward-compatible alias (must not be used for new partition locks).
JournalPartitionLock = GlobalApprovalLock


def iter_journal_event_files(data_root: Path) -> list[Path]:
    root = data_root / "journal"
    if not root.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and child.name.startswith("event_date="):
            path = child / "events.jsonl"
            if path.is_file():
                out.append(path)
    return out


def assert_journal_layout(data_root: Path) -> None:
    """Only event_date=*/events.jsonl and journal/.approval.lock are allowed."""
    root = data_root / "journal"
    if not root.exists():
        return
    if not root.is_dir():
        raise JournalValidationError("journal path must be a directory")
    for child in root.iterdir():
        name = child.name
        if name == APPROVAL_LOCK_BASENAME:
            if not child.is_file():
                raise JournalValidationError(".approval.lock must be a file")
            continue
        if child.is_dir() and name.startswith("event_date="):
            date = name.split("=", 1)[1]
            if not _DATE_RE.match(date):
                raise JournalValidationError(f"invalid journal partition dir {name}")
            for nested in child.iterdir():
                if nested.name != "events.jsonl":
                    raise JournalValidationError(
                        f"forbidden journal artifact under {name}: {nested.name}"
                    )
                if not nested.is_file():
                    raise JournalValidationError("events.jsonl must be a file")
            continue
        # Any other lock/sidecar/state file is forbidden.
        raise JournalValidationError(f"forbidden journal artifact: {name}")


def scan_all_journal_events(data_root: Path) -> list[dict[str, Any]]:
    """Offline chronological scan of all canonical events.jsonl partitions."""
    assert_journal_layout(data_root)
    events: list[dict[str, Any]] = []
    for path in iter_journal_event_files(data_root):
        part = path.parent.name.split("=", 1)[1]
        for ev in read_events_jsonl(path):
            if str(ev.get("event_date")) != part:
                raise JournalValidationError(
                    "event_date must match journal directory partition"
                )
            events.append(ev)
    events.sort(
        key=lambda e: (
            str(e.get("event_ts_utc") or ""),
            str(e.get("run_id") or ""),
            int(e.get("event_seq") or 0),
        )
    )
    return events


def validate_journal_tree(data_root: Path) -> list[dict[str, Any]]:
    events = scan_all_journal_events(data_root)
    if events:
        validate_event_stream(events, expected_partition_date=None)
    return events


def count_consumed_approvals_for_operation(
    data_root: Path, operation_id: str
) -> int:
    """Count consumed operator_approval rows for one operation_id (approval scan only)."""
    op = str(operation_id)
    n = 0
    for ev in scan_operator_approvals(data_root):
        if (
            ev.get("approval_action") == "consumed"
            and str(ev.get("operation_id")) == op
        ):
            n += 1
    return n


def assert_live_order_prepare_ready(data_root: Path, operation_id: str) -> None:
    """Require exactly one prior consumed approval before live order_prepared.

    Hot-path invariant only — does **not** re-validate the entire journal tree
    (that belongs to offline ``validate_journal_tree``). Full-tree validation on
    every prepare was the dominant ``local_prepare`` cost on warm dual-leg sends.
    """
    if count_consumed_approvals_for_operation(data_root, operation_id) != 1:
        raise JournalValidationError(
            "live order_prepared requires exactly one prior consumed operator_approval"
        )


def find_nonterminal_request_ops(
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Ops with request_sent lacking reject/terminal/final recon.

    Unacked place dispatches stay blocking (post_dispatch_ambiguity).
    Acked **market** ops without terminal_update also stay blocking — W5
    must not skip them (crash after ack, before fill/cancel).
    Acked post_only/limit ops are handled by lease TTL reconstruction.
    """
    by_op: dict[str, list[Mapping[str, Any]]] = {}
    for ev in events:
        by_op.setdefault(str(ev["operation_id"]), []).append(ev)
    out: list[dict[str, Any]] = []
    for op_id, ops in by_op.items():
        types = [str(e["event_type"]) for e in ops]
        if "request_sent" not in types:
            continue
        if "reject" in types or "terminal_update" in types:
            continue
        failed_ack = any(
            e.get("event_type") == "ack_received" and e.get("outcome") == "failure"
            for e in ops
        )
        if failed_ack:
            # Venue/transport rejected — no working order. Not a fill-unknown market.
            continue
        finals = [
            e
            for e in ops
            if e.get("event_type") == "reconciliation"
            and e.get("reconciliation_scope") == "post_dispatch_ambiguity"
            and e.get("reconciliation_state") in {"matched", "mismatch"}
        ]
        if finals:
            continue
        prepared = next(
            (e for e in ops if e.get("event_type") == "order_prepared"), None
        )
        acked = "ack_received" in types
        if acked:
            # Post-only/limit: TTL lease path owns recovery; do not list here.
            if prepared is None:
                continue
            if bool(prepared.get("post_only")):
                continue
            if str(prepared.get("order_kind") or "limit") != "market":
                continue
        # Already has inconclusive recon — still nonterminal/blocking.
        sent = next(e for e in ops if e.get("event_type") == "request_sent")
        has_inconclusive = any(
            e.get("event_type") == "reconciliation"
            and e.get("reconciliation_scope") == "post_dispatch_ambiguity"
            and e.get("reconciliation_state") == "inconclusive"
            for e in ops
        )
        out.append(
            {
                "operation_id": op_id,
                "request_sent": dict(sent),
                "needs_recon_append": not has_inconclusive,
                "blocking": True,
            }
        )
    return out


def scan_operator_approvals(data_root: Path) -> list[dict[str, Any]]:
    """Restart-safe scan of canonical operator_approval events only."""
    events: list[dict[str, Any]] = []
    for path in iter_journal_event_files(data_root):
        for ev in read_events_jsonl(path):
            if ev.get("event_type") == "operator_approval":
                events.append(ev)
    return events


class PrivateJournalWriter:
    """Append-only writer for ``bbot.private.journal.v1`` under private data root."""

    def __init__(
        self,
        data_root: Optional[Path] = None,
        *,
        run_id: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        root = data_root if data_root is not None else resolve_data_root(
            dict(env) if env is not None else None
        )
        if _is_under_denied(root):
            raise JournalValidationError(f"refusing private journal under denied path: {root}")
        stub = Path("/data/bbot/journal")
        try:
            if root.resolve() == stub.resolve() or str(root.resolve()).startswith(
                str(stub.resolve()) + os.sep
            ):
                raise JournalValidationError("refusing stub journal path for private writer")
        except OSError:
            if str(root) == "/data/bbot/journal" or str(root).startswith("/data/bbot/journal/"):
                raise JournalValidationError("refusing stub journal path for private writer")
        self.data_root = root
        self.run_id = run_id or new_opaque_id("run")
        self._seq = 0
        self._last_mono = -1
        self._last_ts: Optional[str] = None
        self._op_states: dict[str, _OpState] = {}
        self._seen_event_ids: set[str] = set()
        self._dual_legs: dict[str, set[str]] = {}
        self._held_lock: Optional[GlobalApprovalLock] = None
        self._write_lock = threading.Lock()
        # Hot-path indexes: load durable journal at most once per writer, then
        # maintain incrementally so dual-leg prepare does not rescan/revalidate.
        self._disk_index_loaded = False
        self._events_by_op: dict[str, list[dict[str, Any]]] = {}
        self._flat_events: list[dict[str, Any]] = []
        self._approval_consumed_by_op: dict[str, int] = {}

    def _next_mono(self) -> int:
        # time.monotonic_ns is process-monotonic; ensure strict increase.
        mono = time.monotonic_ns()
        if mono <= self._last_mono:
            mono = self._last_mono + 1
        return mono

    def approval_lock(self) -> GlobalApprovalLock:
        return GlobalApprovalLock(self.data_root)

    def partition_lock(self, event_date: str) -> GlobalApprovalLock:
        # Compatibility shim: always global approval lock (ignores partition).
        _ = event_date
        return self.approval_lock()

    def append(self, partial: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and append one event. Fills seq/id/timestamps when omitted."""
        with self._write_lock:
            return self._append_unlocked(partial)

    def _note_approval_index(self, event: Mapping[str, Any]) -> None:
        if event.get("event_type") != "operator_approval":
            return
        if event.get("approval_action") != "consumed":
            return
        op = str(event.get("operation_id") or "")
        if not op:
            return
        self._approval_consumed_by_op[op] = self._approval_consumed_by_op.get(op, 0) + 1

    def _ensure_disk_index_unlocked(self) -> None:
        """Load durable events once; subsequent appends update indexes in memory."""
        if self._disk_index_loaded:
            return
        assert_journal_layout(self.data_root)
        events = scan_all_journal_events(self.data_root)
        self._flat_events = list(events)
        self._events_by_op = {}
        self._approval_consumed_by_op = {}
        for ev in events:
            op = str(ev.get("operation_id") or "")
            if op:
                self._events_by_op.setdefault(op, []).append(ev)
            self._note_approval_index(ev)
        self._disk_index_loaded = True

    def _assert_live_prepare_ready_unlocked(self, operation_id: str) -> None:
        self._ensure_disk_index_unlocked()
        if self._approval_consumed_by_op.get(str(operation_id), 0) != 1:
            raise JournalValidationError(
                "live order_prepared requires exactly one prior consumed operator_approval"
            )

    def _hydrate_op_state_unlocked(self, op_id: str) -> None:
        if op_id in self._op_states:
            return
        self._ensure_disk_index_unlocked()
        self._op_states[op_id] = _OpState()
        priors = list(self._events_by_op.get(op_id, []))
        if not priors:
            return
        legacy_ops = find_legacy_pre_send_no_dispatch_ops(self._flat_events)
        for prior in priors:
            if op_id in legacy_ops and _is_exact_legacy_pre_send_reject(prior):
                _validate_lifecycle_step(
                    self._op_states[op_id],
                    {
                        "event_type": "pre_send_gate",
                        "outcome": "observed",
                        "gate_kind": "price",
                        "gate_decision": "blocked",
                    },
                )
            elif op_id in legacy_ops and _is_exact_legacy_pre_send_recon(prior):
                continue
            else:
                _validate_lifecycle_step(self._op_states[op_id], prior)

    def _append_unlocked(self, partial: Mapping[str, Any]) -> dict[str, Any]:
        assert_journal_layout(self.data_root)
        event = dict(partial)
        event.setdefault("schema_version", SCHEMA_VERSION)
        event.setdefault("run_id", self.run_id)
        event.setdefault("event_id", new_opaque_id("evt"))
        if "event_ts_utc" not in event:
            event["event_ts_utc"] = utc_now_rfc3339()
        event.setdefault("event_date", event_date_from_ts_utc(str(event["event_ts_utc"])))
        if "event_monotonic_ns" not in event:
            event["event_monotonic_ns"] = self._next_mono()
        else:
            incoming_mono = int(event["event_monotonic_ns"])
            if incoming_mono <= self._last_mono:
                # Concurrent senders may stamp mono before acquiring the write
                # lock; keep append order legal without rewriting send_monotonic_ns.
                event["event_monotonic_ns"] = self._last_mono + 1
        self._seq += 1
        if "event_seq" in event and int(event["event_seq"]) != self._seq:
            raise JournalValidationError("event_seq must be assigned by writer sequentially")
        event["event_seq"] = self._seq

        # Chronology vs prior appends in this writer instance.
        mono = int(event["event_monotonic_ns"])
        ts = str(event["event_ts_utc"])
        if self._last_ts is not None and ts < self._last_ts:
            event["event_ts_utc"] = self._last_ts
            event["event_date"] = event_date_from_ts_utc(str(event["event_ts_utc"]))
            ts = str(event["event_ts_utc"])
        if mono <= self._last_mono:
            raise JournalValidationError("event_monotonic_ns must strictly increase")
        if self._last_ts is not None and ts < self._last_ts:
            raise JournalValidationError("event_ts_utc must be non-decreasing")
        if event["event_id"] in self._seen_event_ids:
            raise JournalValidationError("duplicate event_id")

        if (
            event.get("event_type") == "order_prepared"
            and str(event.get("environment")) == "live"
        ):
            self._assert_live_prepare_ready_unlocked(str(event["operation_id"]))

        validate_event_shape(event)
        op_id = str(event["operation_id"])
        self._hydrate_op_state_unlocked(op_id)
        state = self._op_states[op_id]
        _validate_lifecycle_step(state, event)

        if event.get("dual_leg_id") and event.get("leg_id"):
            legs = self._dual_legs.setdefault(str(event["dual_leg_id"]), set())
            legs.add(str(event["leg_id"]))
            if event["event_type"] == "dual_leg_abort":
                legs.add(str(event["peer_leg_id"]))
                if len(legs) < 2:
                    raise JournalValidationError(
                        "dual_leg_id must have two distinct leg_ids"
                    )

        path = events_jsonl_path(self.data_root, str(event["event_date"]))
        if _is_under_denied(path):
            raise JournalValidationError(f"refusing append under denied path: {path}")

        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        # Defense: serialized line must not contain denylist tokens as keys.
        assert_no_redaction_violations(event)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

        self._last_mono = mono
        self._last_ts = ts
        self._seen_event_ids.add(str(event["event_id"]))
        # Keep hot-path indexes coherent with durable append.
        self._ensure_disk_index_unlocked()
        self._events_by_op.setdefault(op_id, []).append(event)
        self._flat_events.append(event)
        self._note_approval_index(event)
        return event

    def append_under_approval_lock(
        self,
        lock: GlobalApprovalLock,
        partial: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Append while holding global approval lock."""
        if lock._fh is None:  # noqa: SLF001
            raise JournalValidationError("approval lock not held")
        return self.append(partial)

    def append_under_partition_lock(
        self,
        lock: GlobalApprovalLock,
        partial: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self.append_under_approval_lock(lock, partial)

    def append_auth(
        self,
        *,
        venue: str,
        environment: str,
        outcome: str,
        auth_method: str = "hmac",
        credential_presence: Mapping[str, bool] | None = None,
        credentials_configured: bool | None = None,
        operation_id: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> dict[str, Any]:
        """Append auth using current credential_presence form only."""
        if credential_presence is not None and credentials_configured is not None:
            raise JournalValidationError(
                "pass either credential_presence or credentials_configured, not both"
            )
        if credentials_configured is not None:
            presence = {"credentials_configured": bool(credentials_configured)}
        elif credential_presence is not None:
            # Normalize legacy caller dicts to current writer form.
            if set(credential_presence.keys()) == _CREDENTIAL_PRESENCE_KEYS_LEGACY:
                configured = all(bool(credential_presence[k]) for k in _CREDENTIAL_PRESENCE_KEYS_LEGACY)
                presence = {"credentials_configured": configured}
            else:
                presence = dict(credential_presence)
        else:
            raise JournalValidationError("credential_presence required")
        # Writer always emits current form.
        if set(presence.keys()) != _CREDENTIAL_PRESENCE_KEYS_CURRENT:
            raise JournalValidationError(
                "new auth writer must use {credentials_configured: bool}"
            )
        body: dict[str, Any] = {
            "event_type": "auth",
            "operation_id": operation_id or new_opaque_id("op_auth"),
            "venue": venue,
            "environment": environment,
            "outcome": outcome,
            "auth_method": auth_method,
            "credential_presence": presence,
        }
        if error_code is not None:
            body["error_code"] = error_code
        return self.append(body)

    def append_pre_send_gate(
        self,
        *,
        venue: str,
        environment: str,
        gate_kind: str,
        operation_id: str,
        gate_decision: str = "blocked",
    ) -> dict[str, Any]:
        """Canonical rest/price/preflight block before order_prepared/dispatch."""
        return self.append(
            {
                "event_type": "pre_send_gate",
                "operation_id": operation_id,
                "venue": venue,
                "environment": environment,
                "outcome": "observed",
                "gate_kind": gate_kind,
                "gate_decision": gate_decision,
            }
        )

    def append_account_read(
        self,
        *,
        venue: str,
        environment: str,
        outcome: str,
        account_scope: str = "balance",
        operation_id: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "event_type": "account_read",
            "operation_id": operation_id or new_opaque_id("op_acct"),
            "venue": venue,
            "environment": environment,
            "outcome": outcome,
            "account_scope": account_scope,
            "request_kind": "account_read",
        }
        if error_code is not None:
            body["error_code"] = error_code
        return self.append(body)

    def append_post_only_ttl_matched_followup(
        self,
        *,
        venue: str,
        environment: str,
        operation_id: str,
        dual_leg_id: Optional[str] = None,
        leg_id: Optional[str] = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """After observed terminal following post-only TTL cancel: recon + latency.

        Always appends ``reconciliation(post_only_ttl_recovery, matched)`` then a
        redacted ``latency_summary`` with allowlisted interval labels derived from
        prior events for this ``operation_id``. Does not invent cancel-RTT names.
        """
        recon_body: dict[str, Any] = {
            "event_type": "reconciliation",
            "operation_id": operation_id,
            "venue": venue,
            "environment": environment,
            "outcome": "observed",
            "reconciliation_scope": "post_only_ttl_recovery",
            "reconciliation_state": "matched",
        }
        if dual_leg_id is not None:
            recon_body["dual_leg_id"] = dual_leg_id
        if leg_id is not None:
            recon_body["leg_id"] = leg_id
        recon = self.append(recon_body)

        op_events = [
            e
            for e in scan_all_journal_events(self.data_root)
            if str(e.get("operation_id")) == str(operation_id)
        ]
        intervals = derive_latency_intervals_from_op_events(op_events)
        if not intervals:
            raise JournalValidationError(
                "post_only_ttl_recovery matched requires derivable latency intervals"
            )
        summary_partial = self.build_latency_summary_event(
            venue=venue,
            environment=environment,
            operation_id=operation_id,
            intervals_ms=intervals,
            latency_basis="monotonic_local",
            sample_count=1,
            dual_leg_id=dual_leg_id,
            leg_id=leg_id,
        )
        summary = self.append(summary_partial)
        return recon, summary

    def build_latency_summary_event(
        self,
        *,
        venue: str,
        environment: str,
        operation_id: str,
        intervals_ms: Mapping[str, float],
        latency_basis: str,
        sample_count: int = 1,
        dual_leg_id: Optional[str] = None,
        leg_id: Optional[str] = None,
        clock_offset_evidence: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Build (do not append) a latency_summary payload; RTT stays RTT."""
        assert_rtt_not_named_one_way(intervals_ms, latency_basis=latency_basis)
        body: dict[str, Any] = {
            "event_type": "latency_summary",
            "operation_id": operation_id,
            "venue": venue,
            "environment": environment,
            "outcome": "observed",
            "latency_intervals_ms": {k: float(v) for k, v in intervals_ms.items()},
            "latency_basis": latency_basis,
            "sample_count": sample_count,
        }
        if dual_leg_id is not None:
            body["dual_leg_id"] = dual_leg_id
        if leg_id is not None:
            body["leg_id"] = leg_id
        if clock_offset_evidence is not None:
            body["clock_offset_evidence"] = dict(
                validate_clock_offset_evidence(clock_offset_evidence)
            )
        if "exchange_to_client_observed" in intervals_ms and clock_offset_evidence is None:
            raise JournalValidationError(
                "exchange_to_client_observed requires clock_offset_evidence"
            )
        return body


def assert_no_order_surface() -> None:
    """R2 invariant: this module must not expose order/network send helpers."""
    forbidden = {
        "place_order",
        "cancel_order",
        "amend_order",
        "send_order",
        "create_order",
        "ws_connect",
        "ws_subscribe",
        "private_ws_login",
    }
    present = forbidden.intersection(globals())
    if present:
        raise RuntimeError(f"order/ws surface must not exist: {sorted(present)}")


def map_probe_outcome_to_error_code(outcome: str) -> str:
    """Map harness normalized probe outcome → journal error_code allowlist."""
    mapping = {
        "auth_rejected": "auth_failed",
        "auth_forbidden": "auth_failed",
        "network_error": "network_error",
        "malformed_response": "invalid_request",
        "unknown_error": "unknown",
        "account_read_failed": "account_read_failed",
    }
    return mapping.get(outcome, "account_read_failed")
