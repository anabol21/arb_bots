"""EV2-12B1: queued WAL events cannot publish K=1 exposure."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.bot.execution.contracts import ExecutionEventType, Venue
from app.bot.execution.durable_projection import (
    DurableProjectionError,
    inspect_durable_manager_candidate,
)
from app.bot.execution.state_machine import apply_events, initial_spread_state
from app.bot.execution.wal import ExecutionWal
from tests.test_execution_state_machine import (
    BYBIT_LEG,
    CLOSE_INTENT_ID,
    INTENT_ID,
    OKX_LEG,
    RUN_ID,
    Clock,
    _ack,
    _close_arm,
    _dispatch_open,
    _event,
    _fill,
    _happy_open_events,
    _orders,
    _pos,
    _sent,
)


def _wal(root: Path) -> ExecutionWal:
    return ExecutionWal(
        root / "wal.v2" / "wal.jsonl",
        run_id=RUN_ID,
        max_queue=32,
        reserved_tail=4,
        max_durable_lag=32,
    )


class DurableManagerCandidateTests(unittest.TestCase):
    def test_queued_open_is_not_durable_then_fsync_makes_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wal = _wal(root)
            empty_replay = wal.replay()
            events = _happy_open_events(Clock())
            state = apply_events(initial_spread_state(run_id=RUN_ID), events)
            acks = wal.enqueue_batch(events)
            self.assertTrue(all(ack.accepted and not ack.durable for ack in acks))
            with self.assertRaisesRegex(DurableProjectionError, "wal_not_durable"):
                inspect_durable_manager_candidate(
                    engine_state=state, replay=empty_replay,
                    health=wal.health(), committed_trade_id=None,
                )

            wal.drain_all()
            replay = wal.replay()
            candidate = inspect_durable_manager_candidate(
                engine_state=state, replay=replay,
                health=wal.health(), committed_trade_id=None,
            )
            self.assertEqual(candidate.projection.publication, "open")
            self.assertTrue(candidate.venue_reconciliation_required)
            self.assertEqual(candidate.wal_seq, len(events))
            self.assertEqual(candidate.journal_evidence()["ev2_wal_record_hash"], replay.records[-1].record_hash)

            # Crash after WAL fsync but before manager journal: a new process
            # sees the same candidate; it must still reconcile the venues.
            restarted = _wal(root)
            restarted_replay = restarted.replay()
            recovered = inspect_durable_manager_candidate(
                engine_state=restarted_replay.state,
                replay=restarted_replay,
                health=restarted.health(), committed_trade_id=None,
            )
            self.assertEqual(recovered.journal_evidence(), candidate.journal_evidence())
            self.assertTrue(recovered.venue_reconciliation_required)

    def test_ack_only_wal_has_no_manager_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            wal = _wal(Path(td))
            wal.replay()
            clock = Clock()
            events = [
                *_dispatch_open(clock),
                _ack(clock, Venue.OKX, OKX_LEG),
                _ack(clock, Venue.BYBIT, BYBIT_LEG),
            ]
            state = apply_events(initial_spread_state(run_id=RUN_ID), events)
            wal.enqueue_batch(events)
            wal.drain_all()
            with self.assertRaisesRegex(DurableProjectionError, "no_proven_lifecycle"):
                inspect_durable_manager_candidate(
                    engine_state=state, replay=wal.replay(),
                    health=wal.health(), committed_trade_id=None,
                )

    def test_current_state_must_match_exact_durable_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            wal = _wal(Path(td))
            wal.replay()
            events = _happy_open_events(Clock())
            wal.enqueue_batch(events)
            wal.drain_all()
            with self.assertRaisesRegex(DurableProjectionError, "durable_state_mismatch"):
                inspect_durable_manager_candidate(
                    engine_state=initial_spread_state(run_id=RUN_ID),
                    replay=wal.replay(), health=wal.health(), committed_trade_id=None,
                )

    def test_unhealthy_wal_never_proposes_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            wal = _wal(Path(td))
            wal.replay()
            events = _happy_open_events(Clock())
            state = apply_events(initial_spread_state(run_id=RUN_ID), events)
            wal.enqueue_batch(events)
            wal.drain_all()
            replay = wal.replay()
            health = wal.health()
            for bad_health in (
                replace(health, writer_unhealthy=True),
                replace(health, torn_tail=True),
                replace(health, integrity_unhealthy=True),
            ):
                with self.subTest(bad_health=bad_health):
                    with self.assertRaisesRegex(DurableProjectionError, "wal_unhealthy"):
                        inspect_durable_manager_candidate(
                            engine_state=state, replay=replay,
                            health=bad_health, committed_trade_id=None,
                        )

    def test_close_candidate_requires_fsynced_flat_proof(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            wal = _wal(Path(td))
            wal.replay()
            clock = Clock()
            open_events = _happy_open_events(clock)
            opened = apply_events(initial_spread_state(run_id=RUN_ID), open_events)
            wal.enqueue_batch(open_events)
            wal.drain_all()
            clock.seq = 0
            close_events = [
                _close_arm(clock),
                _sent(clock, Venue.OKX, OKX_LEG, intent_id=CLOSE_INTENT_ID, reduce_only=True),
                _sent(clock, Venue.BYBIT, BYBIT_LEG, intent_id=CLOSE_INTENT_ID, reduce_only=True),
                _ack(clock, Venue.OKX, OKX_LEG, intent_id=CLOSE_INTENT_ID),
                _ack(clock, Venue.BYBIT, BYBIT_LEG, intent_id=CLOSE_INTENT_ID),
                _fill(clock, Venue.OKX, OKX_LEG, intent_id=CLOSE_INTENT_ID),
                _fill(clock, Venue.BYBIT, BYBIT_LEG, intent_id=CLOSE_INTENT_ID),
                _pos(clock, Venue.OKX, OKX_LEG, "0", intent_id=CLOSE_INTENT_ID),
                _pos(clock, Venue.BYBIT, BYBIT_LEG, "0", intent_id=CLOSE_INTENT_ID),
                _orders(clock, Venue.OKX, OKX_LEG, 0, intent_id=CLOSE_INTENT_ID),
                _orders(clock, Venue.BYBIT, BYBIT_LEG, 0, intent_id=CLOSE_INTENT_ID),
                _event(
                    clock, ExecutionEventType.FLATNESS_PROVEN,
                    intent_id=CLOSE_INTENT_ID,
                    payload={"positions_flat": True, "open_orders_flat": True},
                ),
            ]
            closed = apply_events(opened, close_events)
            wal.enqueue_batch(close_events)
            with self.assertRaisesRegex(DurableProjectionError, "wal_not_durable"):
                inspect_durable_manager_candidate(
                    engine_state=closed, replay=wal.replay(),
                    health=wal.health(), committed_trade_id=INTENT_ID,
                )
            wal.drain_all()
            candidate = inspect_durable_manager_candidate(
                engine_state=closed, replay=wal.replay(),
                health=wal.health(), committed_trade_id=INTENT_ID,
            )
            self.assertEqual(candidate.projection.publication, "close")
            self.assertEqual(candidate.journal_evidence()["ev2_close_intent_id"], CLOSE_INTENT_ID)


if __name__ == "__main__":
    unittest.main()
