"""Canonical journal-backed one-time operator approval (grant/consume).

Truth is only ``operator_approval`` events in ``events.jsonl``. No sidecar
``approval_consumed.jsonl``. Raw tokens and user phrases are never stored.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from app.bot.private.journal_v1 import (
    GlobalApprovalLock,
    PrivateJournalWriter,
    event_date_from_ts_utc,
    new_opaque_id,
    scan_operator_approvals,
    utc_now_rfc3339,
)
from app.bot.private.order_plan import OrderPlan, OrderPlanError, parse_rfc3339_utc

APPROVAL_DOMAIN = b"bbot.private.approval.v1"
APPROVAL_SCOPE = "live_order_send"


class ApprovalError(ValueError):
    """Approval issue / consume failure."""


@dataclass(frozen=True)
class ApprovalToken:
    """In-memory approval handle. Never journal raw token material."""

    approval_record_id: str
    approval_token_fingerprint: str
    grant_event_id: str
    expires_at_utc: str
    plan_fingerprint: str
    # Private fields — never serialized into journal / public views.
    _plan_binding: str
    _token_nonce: str

    def public_dict(self) -> dict[str, object]:
        return {
            "approval_record_id": self.approval_record_id,
            "approval_token_fingerprint": self.approval_token_fingerprint,
            "grant_event_id": self.grant_event_id,
            "expires_at_utc": self.expires_at_utc,
            "plan_fingerprint": self.plan_fingerprint,
            "authorization_source": "orchestrator_provided",
            "chat_verified": False,
        }


def _token_fingerprint(hmac_key: bytes, nonce: str) -> str:
    digest = hmac.new(
        hmac_key, APPROVAL_DOMAIN + b"|" + nonce.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return digest


def _plan_binding(hmac_key: bytes, plan: OrderPlan) -> str:
    return hmac.new(hmac_key, plan.canonical_bytes(), hashlib.sha256).hexdigest()


def _index_approvals(events: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    grants: dict[str, dict[str, Any]] = {}
    consumed: set[str] = set()
    for ev in events:
        action = ev.get("approval_action")
        rid = str(ev.get("approval_record_id") or "")
        if action == "granted":
            grants[rid] = ev
        elif action == "consumed":
            consumed.add(rid)
    return grants, consumed


class ApprovalVault:
    """Issues/consumes plan-bound approvals via exclusive journal lock.

    Warm dual-leg hot path: grant/consume indexes load from durable journal at
    most once per vault, then update in memory under the approval lock so each
    leg does not rescan ``events.jsonl``.
    """

    def __init__(
        self,
        *,
        journal: PrivateJournalWriter,
        hmac_key: bytes | None = None,
        venue: str = "bybit",
        environment: str = "live",
    ) -> None:
        self._journal = journal
        self._key = hmac_key if hmac_key is not None else secrets.token_bytes(32)
        self._venue = venue
        self._environment = environment
        self._index_loaded = False
        self._grants: dict[str, dict[str, Any]] = {}
        self._consumed: set[str] = set()
        self._fps_granted: set[str] = set()

    @property
    def data_root(self) -> Path:
        return self._journal.data_root

    def _ensure_index_locked(self) -> None:
        """Caller must hold approval lock. Full scan at most once per vault."""
        if self._index_loaded:
            return
        events = scan_operator_approvals(self.data_root)
        grants, consumed = _index_approvals(events)
        self._grants = dict(grants)
        self._consumed = set(consumed)
        self._fps_granted = {
            str(g.get("approval_token_fingerprint")) for g in grants.values()
        }
        self._index_loaded = True

    def prefetch_index(self) -> None:
        """Optional warm-up: load approval index before the dual-leg critical path."""
        with self._journal.approval_lock():
            self._ensure_index_locked()

    def issue(
        self,
        plan: OrderPlan,
        *,
        now: Optional[datetime] = None,
    ) -> ApprovalToken:
        """Append granted under exclusive lock; returns in-memory token handle."""
        expires_at = plan.expires_at_utc
        parse_rfc3339_utc(expires_at)
        nonce = secrets.token_hex(32)
        fingerprint = _token_fingerprint(self._key, nonce)
        record_id = new_opaque_id("approval_record")
        grant_op = new_opaque_id("op_approval")
        ts = utc_now_rfc3339() if now is None else _rfc3339(now)
        event_date = event_date_from_ts_utc(ts)
        binding = _plan_binding(self._key, plan)

        with self._journal.approval_lock() as lock:
            self._ensure_index_locked()
            if fingerprint in self._fps_granted:
                raise ApprovalError("approval fingerprint collision")
            if record_id in self._grants:
                raise ApprovalError("approval_record_id collision")
            granted = self._journal.append_under_approval_lock(
                lock,
                {
                    "event_type": "operator_approval",
                    "operation_id": grant_op,
                    "venue": self._venue if not plan.venue.startswith("okx") else "okx",
                    "environment": self._environment,
                    "outcome": "success",
                    "event_ts_utc": ts,
                    "event_date": event_date,
                    "approval_action": "granted",
                    "approval_token_fingerprint": fingerprint,
                    "approval_scope": APPROVAL_SCOPE,
                    "approval_record_id": record_id,
                    "approval_expires_at_utc": expires_at,
                },
            )
            self._grants[record_id] = dict(granted)
            self._fps_granted.add(fingerprint)
        return ApprovalToken(
            approval_record_id=record_id,
            approval_token_fingerprint=fingerprint,
            grant_event_id=str(granted["event_id"]),
            expires_at_utc=expires_at,
            plan_fingerprint=plan.request_fingerprint,
            _plan_binding=binding,
            _token_nonce=nonce,
        )

    def consume(
        self,
        plan: OrderPlan,
        token: ApprovalToken,
        *,
        now: Optional[datetime] = None,
        now_mono_ns: Optional[int] = None,
    ) -> dict[str, Any]:
        """Durable check+consume under exclusive lock. Restart scans journal only."""
        _ = now_mono_ns  # expiry uses plan mono separately at sender
        if plan.is_expired(now_utc=now, now_mono_ns=now_mono_ns):
            raise ApprovalError("approval token expired")
        expected_fp = _token_fingerprint(self._key, token._token_nonce)
        if not hmac.compare_digest(expected_fp, token.approval_token_fingerprint):
            raise ApprovalError("approval fingerprint mismatch")
        if not hmac.compare_digest(token._plan_binding, _plan_binding(self._key, plan)):
            raise ApprovalError("approval token does not match plan")
        if token.plan_fingerprint != plan.request_fingerprint:
            raise ApprovalError("approval fingerprint mismatch")
        if token.expires_at_utc != plan.expires_at_utc:
            raise ApprovalError("approval expiry mismatch")

        ts = utc_now_rfc3339() if now is None else _rfc3339(now)
        event_date = event_date_from_ts_utc(ts)
        consumed_for = plan.order_attempt_id

        with self._journal.approval_lock() as lock:
            self._ensure_index_locked()
            if token.approval_record_id in self._consumed:
                raise ApprovalError("approval token already consumed")
            grant = self._grants.get(token.approval_record_id)
            if grant is None:
                raise ApprovalError("unknown approval grant")
            if str(grant["approval_token_fingerprint"]) != token.approval_token_fingerprint:
                raise ApprovalError("approval fingerprint mismatch")
            if str(grant["event_id"]) != token.grant_event_id:
                raise ApprovalError("approval grant event mismatch")
            if str(grant.get("approval_scope")) != APPROVAL_SCOPE:
                raise ApprovalError("approval scope mismatch")
            expires = str(grant["approval_expires_at_utc"])
            if ts > expires:
                raise ApprovalError("approval token expired")
            if now is not None and _rfc3339(now) > expires:
                raise ApprovalError("approval token expired")

            consumed_ev = self._journal.append_under_approval_lock(
                lock,
                {
                    "event_type": "operator_approval",
                    "operation_id": consumed_for,
                    "venue": "okx" if plan.venue.startswith("okx") else "bybit",
                    "environment": self._environment,
                    "outcome": "observed",
                    "event_ts_utc": ts,
                    "event_date": event_date,
                    "approval_action": "consumed",
                    "approval_token_fingerprint": token.approval_token_fingerprint,
                    "approval_scope": APPROVAL_SCOPE,
                    "approval_record_id": token.approval_record_id,
                    "approval_grant_event_id": token.grant_event_id,
                    "consumed_for_operation_id": consumed_for,
                },
            )
            self._consumed.add(token.approval_record_id)
            return consumed_ev


def assert_plan_unmutated(original: OrderPlan, candidate: OrderPlan) -> None:
    if original.canonical_bytes() != candidate.canonical_bytes():
        raise OrderPlanError("order plan mutation detected")


def _rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    ms = dt.microsecond // 1000
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms:03d}Z"
