"""EV2-05 WAL exporter and pure Sentry mapper tests. No SDK or network."""

from __future__ import annotations

import inspect
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.bot.execution.contracts import (
    SCHEMA_VERSION,
    ExecutionEvent,
    ExecutionEventType,
    Venue,
    derive_client_id,
)
from app.bot.execution.exporters import (
    InMemoryExporter,
    MetricsSnapshot,
    SentryEnvelope,
    WalExportCursor,
    map_lifecycle_to_sentry_envelope,
    metrics_snapshot,
)
from app.bot.execution.wal import (
    GENESIS_HASH,
    ExecutionWal,
    WalIntegrityError,
    encode_wal_record,
)

INTENT_ID = "95790b7c-8b21-4e82-bae2-1832ddc5bee1"
CLOSE_INTENT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
RUN_ID = "run_aa11bb22cc33dd44ee55ff6677889900"


class Clock:
    def __init__(self) -> None:
        self.seq = 0
        self.mono = 1000

    def next(self) -> tuple[int, int]:
        self.seq += 1
        self.mono += 1
        return self.seq, self.mono


def _event(
    clock: Clock,
    event_type: ExecutionEventType,
    *,
    intent_id: str = INTENT_ID,
    venue: Venue | None = None,
    leg_id: str | None = None,
    payload: dict[str, object] | None = None,
) -> ExecutionEvent:
    seq, mono = clock.next()
    return ExecutionEvent(
        schema_version=SCHEMA_VERSION,
        event_id=f"evt_{mono:032d}",
        event_type=event_type,
        intent_id=intent_id,
        run_id=RUN_ID,
        sequence=seq,
        monotonic_ns=mono,
        venue=venue,
        leg_id=leg_id,
        payload=payload or {},
    )


def _open_intent(clock: Clock) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.INTENT_ACCEPTED,
        payload={
            "action": "open",
            "coin": "BTC",
            "spread_direction": "long",
            "lot_tolerance": "0",
        },
    )


def _close_intent(clock: Clock) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.INTENT_ACCEPTED,
        intent_id=CLOSE_INTENT_ID,
        payload={
            "action": "close",
            "coin": "BTC",
            "spread_direction": "long",
            "lot_tolerance": "0",
        },
    )


def _flatness(clock: Clock) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.FLATNESS_PROVEN,
        intent_id=CLOSE_INTENT_ID,
        payload={"positions_flat": True, "open_orders_flat": True},
    )


def _ack(clock: Clock) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.ACK_ACCEPTED,
        venue=Venue.OKX,
        leg_id="leg_okx",
    )


def _fill(clock: Clock) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.FILL,
        venue=Venue.OKX,
        leg_id="leg_okx",
        payload={"quantity": "1"},
    )


def _record(event: ExecutionEvent, wal_seq: int, prev_hash: str = GENESIS_HASH):
    record, _line = encode_wal_record(
        event, wal_seq=wal_seq, run_id=RUN_ID, prev_hash=prev_hash
    )
    return record


class MapperTests(unittest.TestCase):
    def test_open_and_close_envelopes_match_theta_k1_fingerprints(self) -> None:
        clock = Clock()
        open_env = map_lifecycle_to_sentry_envelope(_open_intent(clock))
        close_env = map_lifecycle_to_sentry_envelope(
            _flatness(clock), close_coin="BTC", close_side="long"
        )
        self.assertIsInstance(open_env, SentryEnvelope)
        self.assertIsInstance(close_env, SentryEnvelope)
        assert open_env is not None
        assert close_env is not None
        self.assertEqual(open_env.event, "open")
        self.assertEqual(open_env.trade_id, INTENT_ID)
        self.assertEqual(open_env.fingerprint, ("theta_k1", INTENT_ID, "open"))
        self.assertEqual(open_env.tags["contour"], "gear22_theta_k1")
        self.assertEqual(open_env.tags["kind"], "trade")
        self.assertIn("theta_k1 trade open", open_env.message)
        self.assertIn("coin=BTC", open_env.message)
        self.assertEqual(close_env.event, "close")
        self.assertEqual(close_env.fingerprint, ("theta_k1", CLOSE_INTENT_ID, "close"))
        self.assertEqual(close_env.tags["coin"], "BTC")
        self.assertEqual(close_env.tags["side"], "long")
        self.assertIn("coin=BTC", close_env.message)
        self.assertIn("side=long", close_env.message)
        self.assertNotIn("coin=NA", close_env.message)
        self.assertNotIn("side=NA", close_env.message)
        self.assertEqual(close_env.level, "error")
        self.assertIsNone(map_lifecycle_to_sentry_envelope(_flatness(Clock())))
        self.assertIsNone(
            map_lifecycle_to_sentry_envelope(
                _flatness(Clock()), close_coin="NA", close_side="NA"
            )
        )
        self.assertIsNone(map_lifecycle_to_sentry_envelope(_ack(clock)))
        self.assertIsNone(map_lifecycle_to_sentry_envelope(_fill(clock)))
        self.assertIsNone(map_lifecycle_to_sentry_envelope(_close_intent(clock)))
        self.assertIsNone(
            map_lifecycle_to_sentry_envelope(
                _event(
                    clock,
                    ExecutionEventType.RECONCILIATION,
                    payload={"matched": True},
                )
            )
        )
        self.assertNotIn("capture", open_env.to_public_dict())
        self.assertNotIn("flush", open_env.to_public_dict())

    def test_mapper_performs_no_import_init_capture_or_flush(self) -> None:
        import ast

        import app.bot.execution.exporters as exporters_mod

        tree = ast.parse(inspect.getsource(exporters_mod))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertNotIn("sentry_sdk", imported)
        self.assertNotIn("app.bot.sentry_setup", imported)
        self.assertNotIn("psycopg", imported)
        self.assertNotIn("psycopg2", imported)
        self.assertFalse(hasattr(exporters_mod, "sentry_sdk"))
        self.assertFalse(hasattr(exporters_mod, "init_sentry"))
        self.assertFalse(hasattr(exporters_mod, "capture_trade_event"))
        clock = Clock()
        with patch.dict("sys.modules", {"sentry_sdk": None, "app.bot.sentry_setup": None}):
            envelope = map_lifecycle_to_sentry_envelope(_open_intent(clock))
        self.assertIsInstance(envelope, SentryEnvelope)


class ExporterCursorTests(unittest.TestCase):
    def test_sink_failure_is_retryable_without_cursor_advance(self) -> None:
        clock = Clock()
        first = _record(_open_intent(clock), 1)
        calls = {"n": 0}

        def flaky(envelope: SentryEnvelope) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("sink_down")

        exporter = InMemoryExporter(sink=flaky)
        with self.assertRaises(RuntimeError):
            exporter.export(first)
        self.assertEqual(exporter.cursor.last_wal_seq, 0)
        self.assertIsNone(exporter.cursor.last_record_hash)
        self.assertEqual(exporter.envelopes, ())
        retry = exporter.export(first)
        self.assertEqual(retry.last_wal_seq, 1)
        self.assertEqual(retry.last_record_hash, first.record_hash)
        self.assertEqual(len(exporter.envelopes), 1)
        self.assertEqual(calls["n"], 2)

    def test_unmapped_records_still_advance_cursor(self) -> None:
        clock = Clock()
        first = _record(_open_intent(clock), 1)
        second = _record(_close_intent(clock), 2, first.record_hash)
        third = _record(_ack(clock), 3, second.record_hash)
        fourth = _record(_fill(clock), 4, third.record_hash)
        fifth = _record(_flatness(clock), 5, fourth.record_hash)
        sinked: list[str] = []
        exporter = InMemoryExporter(sink=lambda env: sinked.append(env.event))
        exporter.export(first)
        exporter.export(second)
        exporter.export(third)
        exporter.export(fourth)
        exporter.export(fifth)
        self.assertEqual(exporter.cursor.last_wal_seq, 5)
        self.assertEqual(sinked, ["open", "close"])
        self.assertEqual([item.event for item in exporter.envelopes], ["open", "close"])
        close_env = exporter.envelopes[1]
        self.assertEqual(close_env.tags["coin"], "BTC")
        self.assertEqual(close_env.tags["side"], "long")
        self.assertNotEqual(close_env.tags["coin"], "NA")
        self.assertNotEqual(close_env.tags["side"], "NA")

    def test_idempotent_export_and_conflict_and_gap(self) -> None:
        clock = Clock()
        first = _record(_open_intent(clock), 1)
        second = _record(_ack(clock), 2, first.record_hash)
        third = _record(_fill(clock), 3, second.record_hash)
        exporter = InMemoryExporter()
        exporter.export(first)
        again = exporter.export(first)
        self.assertEqual(again.last_wal_seq, 1)
        with self.assertRaises(WalIntegrityError):
            exporter.export(third)
        exporter.export(second)
        conflict = _record(
            _event(
                clock,
                ExecutionEventType.REQUEST_SENT,
                venue=Venue.OKX,
                leg_id="leg_okx",
                payload={
                    "quantity": "1",
                    "reduce_only": False,
                    "instrument": "BTC-USDT-SWAP",
                    "side": "buy",
                    "client_id": derive_client_id(INTENT_ID, Venue.OKX),
                },
            ),
            2,
            first.record_hash,
        )
        with self.assertRaises(WalIntegrityError):
            exporter.export(conflict)

    def test_sink_failure_does_not_damage_wal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            wal = ExecutionWal(
                path,
                run_id=RUN_ID,
                max_queue=8,
                reserved_tail=2,
                max_durable_lag=8,
            )
            clock = Clock()
            wal.replay()
            wal.enqueue(_open_intent(clock))
            durable = wal.drain_once()
            assert durable is not None
            replayed = ExecutionWal(
                path,
                run_id=RUN_ID,
                max_queue=8,
                reserved_tail=2,
                max_durable_lag=8,
            ).replay()
            exporter = InMemoryExporter(sink=lambda _env: (_ for _ in ()).throw(RuntimeError("nope")))
            with self.assertRaises(RuntimeError):
                exporter.export(replayed.records[0])
            self.assertEqual(wal.health().durable_wal_seq, 1)
            self.assertFalse(wal.health().writer_unhealthy)
            self.assertTrue(path.exists())
            self.assertEqual(exporter.cursor.last_wal_seq, 0)


class MetricsAndIsolationTests(unittest.TestCase):
    def test_metrics_omit_raw_event_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            wal = ExecutionWal(
                path,
                run_id=RUN_ID,
                max_queue=8,
                reserved_tail=2,
                max_durable_lag=8,
            )
            clock = Clock()
            wal.replay()
            wal.enqueue(_open_intent(clock))
            wal.enqueue(_ack(clock))
            wal.drain_once()
            exporter = InMemoryExporter()
            snap = exporter.snapshot_metrics(wal.health())
            self.assertIsInstance(snap, MetricsSnapshot)
            public = snap.to_public_dict()
            self.assertEqual(public["queue_depth"], 1)
            self.assertEqual(public["durable_lag"], 1)
            self.assertEqual(public["fsync_count"], 1)
            self.assertEqual(public["cursor_lag"], 1)
            blob = json.dumps(public)
            self.assertNotIn("intent_accepted", blob)
            self.assertNotIn(INTENT_ID, blob)
            self.assertNotIn("quantity", blob)
            other = metrics_snapshot(
                health=wal.health(),
                cursor=WalExportCursor(last_wal_seq=0, last_record_hash=None),
            )
            self.assertEqual(other.cursor_lag, 1)

    def test_import_and_construction_are_inert(self) -> None:
        import app.bot.execution.exporters as exporters_mod

        before = threading.active_count()
        env_gets: list[object] = []
        real_get = os.environ.get

        def tracked_get(*args: object, **kwargs: object) -> object:
            env_gets.append(args)
            return real_get(*args, **kwargs)

        opened: list[str] = []
        real_open = os.open

        def guarded_open(name: str, flags: int, *args: object, **kwargs: object) -> int:
            opened.append(str(name))
            return real_open(name, flags, *args, **kwargs)

        with patch.object(os.environ, "get", tracked_get):
            with patch("os.open", guarded_open):
                exporter = InMemoryExporter()
                envelope = map_lifecycle_to_sentry_envelope(_open_intent(Clock()))
        self.assertEqual(threading.active_count(), before)
        self.assertFalse(env_gets)
        self.assertFalse(opened)
        self.assertIsInstance(envelope, SentryEnvelope)
        self.assertEqual(exporter.cursor.last_wal_seq, 0)
        self.assertNotIn("socket", dir(exporters_mod))
        self.assertNotIn("urllib", dir(exporters_mod))

    def test_public_views_redact_forbidden_fields(self) -> None:
        clock = Clock()
        envelope = map_lifecycle_to_sentry_envelope(_open_intent(clock))
        assert envelope is not None
        blob = json.dumps(envelope.to_public_dict()) + repr(envelope)
        self.assertNotIn("api_key", blob)
        self.assertNotIn("signature", blob)
        self.assertNotIn("order_id", blob)


class CloseContextTests(unittest.TestCase):
    def test_exporter_preserves_close_intent_identity_for_flatness(self) -> None:
        clock = Clock()
        close_intent = _record(_close_intent(clock), 1)
        flat = _record(_flatness(clock), 2, close_intent.record_hash)
        exporter = InMemoryExporter()
        exporter.export(close_intent)
        self.assertEqual(exporter.envelopes, ())
        exporter.export(flat)
        self.assertEqual(len(exporter.envelopes), 1)
        envelope = exporter.envelopes[0]
        self.assertEqual(envelope.event, "close")
        self.assertEqual(envelope.tags["coin"], "BTC")
        self.assertEqual(envelope.tags["side"], "long")
        self.assertIn("coin=BTC", envelope.message)
        self.assertIn("side=long", envelope.message)
        self.assertNotIn("coin=NA", envelope.message)
        self.assertNotIn("side=NA", envelope.message)

    def test_flatness_without_close_context_does_not_emit_na_identity(self) -> None:
        clock = Clock()
        flat = _record(_flatness(clock), 1)
        exporter = InMemoryExporter()
        cursor = exporter.export(flat)
        self.assertEqual(cursor.last_wal_seq, 1)
        self.assertEqual(exporter.envelopes, ())
        self.assertIsNone(map_lifecycle_to_sentry_envelope(flat.event))
        self.assertIsNone(
            map_lifecycle_to_sentry_envelope(flat.event, close_coin="NA", close_side="long")
        )


class AdversarialIdentityTests(unittest.TestCase):
    def test_public_mapper_rejects_secret_malformed_and_unbounded_identity(self) -> None:
        clock = Clock()
        flat = _flatness(clock)
        secret_coin = "api_secret=sk-live-SUPERSECRET-99"
        secret_side = "password=venue"
        oversized = "A" * 200_000
        cases = (
            (secret_coin, secret_side),
            (secret_coin, "long"),
            ("BTC", secret_side),
            ("NA", "long"),
            ("na", "short"),
            ("BTC", "NA"),
            ("btc", "long"),
            ("BTC", "LONG"),
            ("BTC", "Long"),
            ("B", "long"),
            ("ABCDEFGHIJKLMNOPQ", "long"),
            (oversized, "long"),
            ("BTC-USDT", "long"),
            ("BTC", "buy"),
            ("BTC", ""),
            ("", "long"),
        )
        for coin, side in cases:
            envelope = map_lifecycle_to_sentry_envelope(
                flat, close_coin=coin, close_side=side
            )
            self.assertIsNone(envelope)
            self.assertNotIn(secret_coin, repr(envelope))
            self.assertNotIn(secret_side, repr(envelope))
            self.assertNotIn(oversized, repr(envelope))
        valid = map_lifecycle_to_sentry_envelope(
            flat, close_coin="BTC", close_side="long"
        )
        self.assertIsInstance(valid, SentryEnvelope)
        assert valid is not None
        public = json.dumps(valid.to_public_dict()) + repr(valid)
        self.assertIn("coin=BTC", public)
        self.assertIn("side=long", public)
        self.assertNotIn(secret_coin, public)
        self.assertNotIn(secret_side, public)
        self.assertNotIn("coin=NA", public)
        self.assertNotIn(oversized, public)

    def test_exporter_keeps_valid_close_context_and_drops_invalid_identity(self) -> None:
        clock = Clock()
        close_intent = _record(_close_intent(clock), 1)
        flat = _record(_flatness(clock), 2, close_intent.record_hash)
        exporter = InMemoryExporter()
        exporter.export(close_intent)
        exporter.export(flat)
        self.assertEqual(len(exporter.envelopes), 1)
        envelope = exporter.envelopes[0]
        blob = json.dumps(envelope.to_public_dict()) + repr(envelope)
        self.assertEqual(envelope.tags["coin"], "BTC")
        self.assertEqual(envelope.tags["side"], "long")
        self.assertIn("coin=BTC", blob)
        self.assertNotIn("api_secret", blob)
        self.assertNotIn("password=venue", blob)
        self.assertIsNone(
            map_lifecycle_to_sentry_envelope(
                flat.event,
                close_coin="api_secret=sk-live-SUPERSECRET-99",
                close_side="password=venue",
            )
        )


if __name__ == "__main__":
    unittest.main()
