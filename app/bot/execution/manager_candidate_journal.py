"""Stage a durable EV2 proof in the existing K=1 journal, without publication.

This module is intentionally not wired to the live runtime. A staged row is
pending venue reconciliation, never an OPEN/CLOSE lifecycle row or order grant.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app.bot.execution.durable_projection import DurableManagerCandidate
from app.bot.theta_trade_manager import (
    SCHEMA_VERSION,
    ThetaTradeJournalWriter,
    ThetaTradeRecoveryError,
    replay_theta_trade_history,
)


_EVENT = "ev2_candidate"
_STATUS = "pending_venue_reconciliation"
_EVIDENCE_KEYS = (
    "ev2_trade_id",
    "ev2_close_intent_id",
    "ev2_coin",
    "ev2_side",
    "ev2_legs",
    "ev2_wal_run_id",
    "ev2_wal_seq",
    "ev2_wal_record_hash",
)


class ManagerCandidateJournalError(RuntimeError):
    """The K=1 handoff is ambiguous or not durably stageable."""


@dataclass(frozen=True)
class CandidateStageResult:
    candidate_id: str
    newly_staged: bool
    venue_reconciliation_required: bool = True


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _payload(candidate: DurableManagerCandidate, *, policy_id: str) -> dict[str, Any]:
    if not isinstance(candidate, DurableManagerCandidate):
        raise ManagerCandidateJournalError("invalid_durable_candidate")
    if candidate.venue_reconciliation_required is not True:
        raise ManagerCandidateJournalError("venue_reconciliation_required")
    projection = candidate.projection
    if projection.publication not in {"open", "close"} or not projection.trade_id:
        raise ManagerCandidateJournalError("invalid_publication")
    if not policy_id or not isinstance(policy_id, str):
        raise ManagerCandidateJournalError("invalid_policy_id")
    return {
        "event": _EVENT,
        "candidate_status": _STATUS,
        "policy_id": policy_id,
        "publication": projection.publication,
        **candidate.journal_evidence(),
    }


def _staged_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        payload = {
            "event": row["event"],
            "candidate_status": row["candidate_status"],
            "policy_id": row["policy_id"],
            "publication": row["publication"],
            **{key: row[key] for key in _EVIDENCE_KEYS},
        }
    except KeyError as exc:
        raise ManagerCandidateJournalError("malformed_staged_candidate") from exc
    if (
        row.get("schema_version") != SCHEMA_VERSION
        or row.get("lifecycle_committed") is not False
        or row.get("live_send") is not False
        or row.get("send") is not False
        or row.get("would_send") is not False
        or payload["candidate_status"] != _STATUS
        or payload["publication"] not in {"open", "close"}
        or row.get("candidate_id") != _digest(payload)
    ):
        raise ManagerCandidateJournalError("malformed_staged_candidate")
    return payload


def _existing_candidates(data_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((data_root / "theta_trades").glob("event_date=*/trades.jsonl")):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ManagerCandidateJournalError("candidate_history_read_failed") from exc
        if raw and not raw.endswith("\n"):
            raise ManagerCandidateJournalError("truncated_candidate_history")
        for line in raw.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ManagerCandidateJournalError("invalid_candidate_history") from exc
            if isinstance(row, Mapping) and row.get("event") == _EVENT:
                _staged_payload(row)
                rows.append(dict(row))
    return rows


def stage_manager_candidate(
    *,
    data_root: Path,
    candidate: DurableManagerCandidate,
    policy_id: str,
    recorded_at_ms: int,
) -> CandidateStageResult:
    """Fsync a pending proof once; never change the K=1 slot or send orders.

    Requires a single-writer data root. Future runtime wiring must hold the
    ownership lease and reconcile signed venue quantities before lifecycle
    publication. After an ambiguous append error, reconstruct this call from
    fresh WAL and journal replay; do not infer a flat exchange position.
    """

    root = Path(data_root)
    payload = _payload(candidate, policy_id=policy_id)
    if isinstance(recorded_at_ms, bool) or not isinstance(recorded_at_ms, int) or recorded_at_ms <= 0:
        raise ManagerCandidateJournalError("invalid_recorded_at_ms")
    try:
        replay = replay_theta_trade_history(root, expected_policy_id=policy_id)
    except ThetaTradeRecoveryError as exc:
        raise ManagerCandidateJournalError("manager_replay_failed") from exc
    candidate_id = _digest(payload)
    key = (payload["ev2_wal_run_id"], payload["ev2_wal_seq"])
    for row in _existing_candidates(root):
        existing = _staged_payload(row)
        if (existing["ev2_wal_run_id"], existing["ev2_wal_seq"]) == key:
            if existing != payload:
                raise ManagerCandidateJournalError("wal_candidate_conflict")
            return CandidateStageResult(candidate_id, newly_staged=False)

    if replay.pending_ev2_candidates:
        raise ManagerCandidateJournalError("manager_candidate_already_pending")

    held = replay.position
    if payload["publication"] == "open":
        if held is not None:
            raise ManagerCandidateJournalError("manager_slot_not_flat")
    elif held is None or held.trade_id != payload["ev2_trade_id"]:
        raise ManagerCandidateJournalError("manager_trade_id_mismatch")

    row = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "computed_at_ms": recorded_at_ms,
        "lifecycle_committed": False,
        "live_send": False,
        "send": False,
        "would_send": False,
        **payload,
    }
    try:
        ThetaTradeJournalWriter(root).append_rows([row])
    except Exception as exc:  # noqa: BLE001 - fsync outcome is ambiguous
        raise ManagerCandidateJournalError("candidate_append_ambiguous") from exc
    return CandidateStageResult(candidate_id, newly_staged=True)
