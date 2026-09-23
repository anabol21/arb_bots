"""Read-only WAL durability fence for a proposed EV2 → K=1 lifecycle.

This module never drains the WAL, writes trade history, mutates a slot, or
authorizes an order. Venue reconciliation is still required after replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.bot.execution.contracts import SpreadState
from app.bot.execution.manager_projection import (
    ManagerExposureProjection,
    project_manager_exposure,
)
from app.bot.execution.wal import ReplayResult, WalHealth


class DurableProjectionError(ValueError):
    """No exact fsynced WAL proof for the proposed manager lifecycle."""


@dataclass(frozen=True)
class DurableManagerCandidate:
    projection: ManagerExposureProjection
    run_id: str
    wal_seq: int
    wal_record_hash: str
    venue_reconciliation_required: bool = True

    def journal_evidence(self) -> dict[str, object]:
        """Candidate fields only; caller must still reconcile and fsync."""

        return {
            **self.projection.journal_evidence(),
            "ev2_wal_run_id": self.run_id,
            "ev2_wal_seq": self.wal_seq,
            "ev2_wal_record_hash": self.wal_record_hash,
        }


def inspect_durable_manager_candidate(
    *,
    engine_state: SpreadState,
    replay: ReplayResult,
    health: WalHealth,
    committed_trade_id: Optional[str],
) -> DurableManagerCandidate:
    """Require byte-replayed WAL state to equal the current engine state.

    An accepted queue entry is insufficient: all events contributing to the
    projected state must have crossed the WAL fsync boundary, with no pending
    tail. ``ReplayResult`` must come from the same WAL as ``WalHealth``.
    This is a candidate, *not* a production publication approval.
    """

    if not isinstance(engine_state, SpreadState):
        raise DurableProjectionError("invalid_engine_state")
    if not isinstance(replay, ReplayResult) or not isinstance(health, WalHealth):
        raise DurableProjectionError("invalid_wal_proof")
    if not isinstance(replay.state, SpreadState):
        raise DurableProjectionError("invalid_replayed_state")
    if (
        not replay.integrity_ok
        or replay.torn_tail
        or health.torn_tail
        or health.writer_unhealthy
        or health.integrity_unhealthy
        or health.hard_full
    ):
        raise DurableProjectionError("wal_unhealthy")
    if health.queue_depth != 0 or health.durable_lag != 0:
        raise DurableProjectionError("wal_not_durable")
    if (
        not replay.records
        or replay.durable_watermark != health.durable_wal_seq
        or replay.records[-1].wal_seq != replay.durable_watermark
    ):
        raise DurableProjectionError("wal_watermark_mismatch")
    if replay.state.to_public_dict() != engine_state.to_public_dict():
        raise DurableProjectionError("durable_state_mismatch")
    last = replay.records[-1]
    if last.run_id != engine_state.run_id:
        raise DurableProjectionError("wal_run_mismatch")
    projection = project_manager_exposure(
        engine_state, committed_trade_id=committed_trade_id
    )
    if projection.publication == "none":
        raise DurableProjectionError("no_proven_lifecycle")
    return DurableManagerCandidate(
        projection=projection,
        run_id=last.run_id,
        wal_seq=last.wal_seq,
        wal_record_hash=last.record_hash,
    )
