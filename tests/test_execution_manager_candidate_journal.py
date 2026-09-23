"""EV2-12B2b: durable, idempotent staging is not K=1 publication."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.bot.execution.durable_projection import inspect_durable_manager_candidate
from app.bot.execution.manager_candidate_journal import (
    ManagerCandidateJournalError,
    stage_manager_candidate,
)
from app.bot.execution.state_machine import apply_events, initial_spread_state
from app.bot.execution.wal import ExecutionWal
from app.bot.theta_trade_manager import (
    SCHEMA_VERSION,
    ThetaTradeConfig,
    ThetaTradeJournalWriter,
    ThetaTradeManager,
    ThetaTradeRecoveryError,
    replay_theta_trade_history,
)
from tests.test_execution_state_machine import RUN_ID, Clock, _happy_open_events


def _candidate(root: Path):
    wal = ExecutionWal(
        root / "wal.v2" / "wal.jsonl", run_id=RUN_ID,
        max_queue=32, reserved_tail=4, max_durable_lag=32,
    )
    wal.replay()
    events = _happy_open_events(Clock())
    state = apply_events(initial_spread_state(run_id=RUN_ID), events)
    wal.enqueue_batch(events)
    wal.drain_all()
    return inspect_durable_manager_candidate(
        engine_state=state, replay=wal.replay(), health=wal.health(),
        committed_trade_id=None,
    )


class ManagerCandidateJournalTests(unittest.TestCase):
    def test_stage_once_replay_and_retry_without_k1_publication(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            candidate = _candidate(root)
            first = stage_manager_candidate(
                data_root=root, candidate=candidate, policy_id="gear22",
                recorded_at_ms=1_700_000_000_000,
            )
            self.assertTrue(first.newly_staged)
            self.assertTrue(first.venue_reconciliation_required)
            pending_replay = replay_theta_trade_history(root)
            self.assertIsNone(pending_replay.position)
            self.assertEqual(pending_replay.pending_ev2_candidates, 1)
            manager = ThetaTradeManager(
                data_root=root, config=ThetaTradeConfig(policy_id="gear22"),
            )
            self.assertTrue(manager.recovery_blocked)
            with self.assertRaisesRegex(ThetaTradeRecoveryError, "trade_history_unhealthy"):
                manager._assert_recovery_writable()

            restarted_candidate = _candidate_from_replay(root)
            again = stage_manager_candidate(
                data_root=root, candidate=restarted_candidate, policy_id="gear22",
                recorded_at_ms=1_700_000_001_000,
            )
            self.assertFalse(again.newly_staged)
            self.assertEqual(again.candidate_id, first.candidate_id)
            replay = replay_theta_trade_history(root)
            self.assertEqual(replay.rows_seen, 1)
            self.assertEqual(replay.lifecycle_rows, 0)
            self.assertEqual(replay.pending_ev2_candidates, 1)
            self.assertIsNone(replay.position)

    def test_same_wal_sequence_with_different_proof_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            candidate = _candidate(root)
            stage_manager_candidate(
                data_root=root, candidate=candidate, policy_id="gear22",
                recorded_at_ms=1_700_000_000_000,
            )
            conflicting = replace(candidate, wal_record_hash="0" * 64)
            with self.assertRaisesRegex(ManagerCandidateJournalError, "wal_candidate_conflict"):
                stage_manager_candidate(
                    data_root=root, candidate=conflicting, policy_id="gear22",
                    recorded_at_ms=1_700_000_001_000,
                )
            self.assertEqual(replay_theta_trade_history(root).rows_seen, 1)

    def test_second_candidate_cannot_bypass_pending_k1_latch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            candidate = _candidate(root)
            stage_manager_candidate(
                data_root=root, candidate=candidate, policy_id="gear22",
                recorded_at_ms=1_700_000_000_000,
            )
            later = replace(candidate, wal_seq=candidate.wal_seq + 1)
            with self.assertRaisesRegex(
                ManagerCandidateJournalError, "manager_candidate_already_pending"
            ):
                stage_manager_candidate(
                    data_root=root, candidate=later, policy_id="gear22",
                    recorded_at_ms=1_700_000_001_000,
                )

    def test_ambiguous_append_replays_existing_stage_without_duplication(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            candidate = _candidate(root)
            real_append = ThetaTradeJournalWriter.append_rows

            def append_then_raise(writer, rows):
                real_append(writer, rows)
                raise OSError("injected crash after fsync")

            with patch.object(ThetaTradeJournalWriter, "append_rows", append_then_raise):
                with self.assertRaisesRegex(ManagerCandidateJournalError, "candidate_append_ambiguous"):
                    stage_manager_candidate(
                        data_root=root, candidate=candidate, policy_id="gear22",
                        recorded_at_ms=1_700_000_000_000,
                    )
            recovered = stage_manager_candidate(
                data_root=root, candidate=_candidate_from_replay(root),
                policy_id="gear22", recorded_at_ms=1_700_000_001_000,
            )
            self.assertFalse(recovered.newly_staged)
            self.assertEqual(replay_theta_trade_history(root).rows_seen, 1)
            self.assertIsNone(replay_theta_trade_history(root).position)

    def test_new_open_candidate_refuses_occupied_manager_slot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            candidate = _candidate(root)
            ThetaTradeJournalWriter(root).append_rows([{
                "schema_version": SCHEMA_VERSION,
                "trade_id": "other-trade",
                "base_coin": "BTC", "side": "long", "event": "open",
                "would_send": True, "send": False, "live_send": False,
                "signal_ts_ms": 1_700_000_000_000,
                "fill_ts_ms": 1_700_000_000_070,
                "spread_fill": 0.2, "notional_usdt": 20.0,
                "policy_id": "gear22",
            }])
            with self.assertRaisesRegex(ManagerCandidateJournalError, "manager_slot_not_flat"):
                stage_manager_candidate(
                    data_root=root, candidate=candidate, policy_id="gear22",
                    recorded_at_ms=1_700_000_001_000,
                )


def _candidate_from_replay(root: Path):
    wal = ExecutionWal(
        root / "wal.v2" / "wal.jsonl", run_id=RUN_ID,
        max_queue=32, reserved_tail=4, max_durable_lag=32,
    )
    replay = wal.replay()
    return inspect_durable_manager_candidate(
        engine_state=replay.state, replay=replay, health=wal.health(),
        committed_trade_id=None,
    )


if __name__ == "__main__":
    unittest.main()
