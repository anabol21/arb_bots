"""EV2-12C2a: signed read-only probe remains non-publishing."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.bot.execution.contracts import ExecutionEventType, Venue
from app.bot.execution.durable_projection import inspect_durable_manager_candidate
from app.bot.execution.signed_quantity_probe import (
    SignedQuantityProbeError,
    probe_signed_candidate_quantities,
)
from app.bot.execution.wal import ExecutionWal
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.venue import endpoints_for_venue
from tests.test_execution_manager_candidate_journal import _candidate
from tests.test_execution_state_machine import (
    BYBIT_LEG,
    CLOSE_INTENT_ID,
    INTENT_ID,
    OKX_LEG,
    RUN_ID,
    Clock,
    _ack,
    _close_arm,
    _event,
    _fill,
    _happy_open_events,
    _orders,
    _pos,
    _sent,
)
from tests.test_execution_venue_quantity_compare import _POOL, _responses


_NOW_MS = 1_700_000_000_000


class _SignedReplies:
    def __init__(self) -> None:
        self.responses = _responses()
        self.responses["bybit_positions"]["time"] = _NOW_MS
        self.responses["bybit_open_orders"]["time"] = _NOW_MS
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url, headers, *, timeout_sec):
        self.calls.append((url, dict(headers)))
        if timeout_sec <= 0:
            raise AssertionError("timeout must be positive")
        if "/v5/position/list" in url:
            return self.responses["bybit_positions"]
        if "/v5/order/realtime" in url:
            return self.responses["bybit_open_orders"]
        if "/api/v5/account/positions" in url:
            return self.responses["okx_positions"]
        if "/api/v5/trade/orders-pending" in url:
            return self.responses["okx_open_orders"]
        raise AssertionError("unexpected endpoint")


class SignedQuantityProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.candidate = _candidate(root)
        wal = ExecutionWal(
            root / "wal.v2" / "wal.jsonl", run_id=RUN_ID,
            max_queue=32, reserved_tail=4, max_durable_lag=32,
        )
        self.replay = wal.replay()
        self.health = wal.health()
        self.bybit = LiveCredentials(api_key="bybit-key", api_secret="bybit-secret")
        self.okx = LiveCredentials(
            api_key="okx-key", api_secret="okx-secret", passphrase="okx-pass",
        )
        self.fingerprints = {
            Venue.BYBIT: hashlib.sha256(self.bybit.api_key.encode()).hexdigest(),
            Venue.OKX: hashlib.sha256(self.okx.api_key.encode()).hexdigest(),
        }

    def probe(self, replies, **overrides):
        args = {
            "candidate": self.candidate,
            "replay": self.replay,
            "health": self.health,
            "bybit_credentials": self.bybit,
            "okx_credentials": self.okx,
            "pool_symbols": _POOL,
            "expected_key_fingerprints": self.fingerprints,
            "http_get_json": replies,
            "clock_ms": iter((_NOW_MS, _NOW_MS + 100)).__next__,
        }
        args.update(overrides)
        return probe_signed_candidate_quantities(**args)

    def test_four_signed_allowlisted_reads_still_cannot_publish(self) -> None:
        replies = _SignedReplies()
        result = self.probe(replies)
        self.assertTrue(result.comparison.matched)
        self.assertFalse(result.comparison.requires_signed_fresh_source)
        self.assertFalse(result.publication_ready)
        self.assertTrue(result.account_ownership_required)
        self.assertTrue(result.private_reseed_required)
        self.assertTrue(result.snapshot_consistency_required)
        self.assertTrue(result.okx_pagination_required)
        self.assertEqual(result.elapsed_ms, 100)
        self.assertEqual(len(replies.calls), 4)
        self.assertTrue(all("/order/create" not in url for url, _ in replies.calls))
        self.assertTrue(all(
            ("X-BAPI-SIGN" in headers or "OK-ACCESS-SIGN" in headers)
            for _, headers in replies.calls
        ))

    def test_stale_bybit_response_or_slow_window_fails_closed(self) -> None:
        replies = _SignedReplies()
        replies.responses["bybit_positions"]["time"] = _NOW_MS - 10_000
        with self.assertRaisesRegex(SignedQuantityProbeError, "bybit_server_time_stale"):
            self.probe(replies)
        replies = _SignedReplies()
        with self.assertRaisesRegex(SignedQuantityProbeError, "snapshot_window_invalid"):
            self.probe(
                replies, clock_ms=iter((_NOW_MS, _NOW_MS + 6000)).__next__,
            )

    def test_wal_or_key_mismatch_stops_before_any_rest_read(self) -> None:
        replies = _SignedReplies()
        forged = replace(self.candidate, wal_record_hash="0" * 64)
        with self.assertRaisesRegex(SignedQuantityProbeError, "candidate_wal_mismatch"):
            self.probe(replies, candidate=forged)
        self.assertEqual(replies.calls, [])
        with self.assertRaisesRegex(SignedQuantityProbeError, "key_fingerprint_mismatch"):
            self.probe(replies, expected_key_fingerprints={
                Venue.BYBIT: "0" * 64, Venue.OKX: self.fingerprints[Venue.OKX],
            })
        self.assertEqual(replies.calls, [])
        with self.assertRaisesRegex(SignedQuantityProbeError, "live_read_endpoints_required"):
            self.probe(replies, endpoints=endpoints_for_venue("testnet"))
        self.assertEqual(replies.calls, [])

    def test_unread_page_or_qty_mismatch_never_produces_approval(self) -> None:
        replies = _SignedReplies()
        replies.responses["bybit_positions"]["result"]["nextPageCursor"] = "next"
        with self.assertRaisesRegex(SignedQuantityProbeError, "quantity_snapshot_invalid"):
            self.probe(replies)
        replies = _SignedReplies()
        replies.responses["okx_positions"]["data"][0]["pos"] = "0.5"
        result = self.probe(replies)
        self.assertFalse(result.comparison.matched)
        self.assertFalse(result.publication_ready)

    def test_durable_close_plan_reads_both_flat_without_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wal = ExecutionWal(
                root / "wal.v2" / "wal.jsonl", run_id=RUN_ID,
                max_queue=32, reserved_tail=4, max_durable_lag=32,
            )
            wal.replay()
            clock = Clock()
            wal.enqueue_batch(_happy_open_events(clock))
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
            wal.enqueue_batch(close_events)
            wal.drain_all()
            replay = wal.replay()
            candidate = inspect_durable_manager_candidate(
                engine_state=replay.state, replay=replay, health=wal.health(),
                committed_trade_id=INTENT_ID,
            )
            replies = _SignedReplies()
            replies.responses["bybit_positions"]["result"]["list"] = []
            replies.responses["okx_positions"]["data"] = []
            result = self.probe(
                replies, candidate=candidate, replay=replay, health=wal.health(),
            )
            self.assertTrue(result.comparison.matched)
            self.assertFalse(result.publication_ready)


if __name__ == "__main__":
    unittest.main()
