"""EV2-05 WAL v2, replay and in-memory projector tests. No VPS or live I/O."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

from app.bot.execution.contracts import (
    SCHEMA_VERSION,
    ExecutionEvent,
    ExecutionEventType,
    SpreadStatus,
    Venue,
    derive_client_id,
)
from app.bot.execution.exporters import InMemoryExporter
from app.bot.execution.state_machine import (
    apply_events,
    initial_spread_state,
    is_proven_flat,
    opens_allowed,
)
from app.bot.execution.wal import (
    CRASH_AFTER_FLUSH_BEFORE_FSYNC,
    CRASH_AFTER_FSYNC_BEFORE_ACK,
    CRASH_AFTER_WRITE_BEFORE_FLUSH,
    CRASH_BEFORE_WRITE,
    CRASH_TORN_WRITE,
    GENESIS_HASH,
    SCHEMA_VERSION as WAL_SCHEMA_VERSION,
    ExecutionWal,
    InMemoryProjector,
    ReplayResult,
    WalAppendAck,
    WalDurableAck,
    WalError,
    WalIntegrityError,
    WalRecord,
    decode_wal_line,
    encode_wal_record,
    require_wal_record,
)

INTENT_ID = "95790b7c-8b21-4e82-bae2-1832ddc5bee1"
OTHER_INTENT_ID = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
CLOSE_INTENT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
RUN_ID = "run_aa11bb22cc33dd44ee55ff6677889900"
OTHER_RUN = "run_bb22cc33dd44ee55ff6677889900aa11"
OKX_LEG = "leg_okx"
BYBIT_LEG = "leg_bybit"
QTY = "1"
FORBIDDEN_MARKERS = (
    "api_key",
    "api_secret",
    "passphrase",
    "RAW-ORDER-ID-99",
    "acct-secret-1",
    "signature",
)


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
    run_id: str = RUN_ID,
    venue: Optional[Venue] = None,
    leg_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> ExecutionEvent:
    seq, mono = clock.next()
    return ExecutionEvent(
        schema_version=SCHEMA_VERSION,
        event_id=f"evt_{mono:032d}",
        event_type=event_type,
        intent_id=intent_id,
        run_id=run_id,
        sequence=seq,
        monotonic_ns=mono,
        venue=venue,
        leg_id=leg_id,
        payload=payload or {},
    )


def _arm(clock: Clock, *, intent_id: str = INTENT_ID) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.INTENT_ACCEPTED,
        intent_id=intent_id,
        payload={
            "action": "open",
            "coin": "BTC",
            "spread_direction": "long",
            "lot_tolerance": "0",
        },
    )


def _sent(
    clock: Clock,
    venue: Venue,
    leg_id: str,
    *,
    intent_id: str = INTENT_ID,
    reduce_only: bool = False,
    quantity: str = QTY,
) -> ExecutionEvent:
    side = "buy" if venue is Venue.OKX else "sell"
    if reduce_only:
        side = "sell" if venue is Venue.OKX else "buy"
    instrument = "BTC-USDT-SWAP" if venue is Venue.OKX else "BTCUSDT"
    return _event(
        clock,
        ExecutionEventType.REQUEST_SENT,
        intent_id=intent_id,
        venue=venue,
        leg_id=leg_id,
        payload={
            "quantity": quantity,
            "reduce_only": reduce_only,
            "instrument": instrument,
            "side": side,
            "client_id": derive_client_id(intent_id, venue, reduce_only=reduce_only),
        },
    )


def _ack(
    clock: Clock,
    venue: Venue,
    leg_id: str,
    *,
    ok: bool = True,
    intent_id: str = INTENT_ID,
) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.ACK_ACCEPTED if ok else ExecutionEventType.ACK_REJECTED,
        intent_id=intent_id,
        venue=venue,
        leg_id=leg_id,
        payload={} if ok else {"reason_code": "venue_rejected"},
    )


def _fill(
    clock: Clock,
    venue: Venue,
    leg_id: str,
    *,
    quantity: str = QTY,
    partial: bool = False,
    intent_id: str = INTENT_ID,
) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.PARTIAL_FILL if partial else ExecutionEventType.FILL,
        intent_id=intent_id,
        venue=venue,
        leg_id=leg_id,
        payload={"quantity": quantity},
    )


def _pos(
    clock: Clock,
    venue: Venue,
    leg_id: str,
    quantity: str,
    *,
    intent_id: str = INTENT_ID,
) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.POSITION_OBSERVED,
        intent_id=intent_id,
        venue=venue,
        leg_id=leg_id,
        payload={"quantity": quantity},
    )


def _orders(
    clock: Clock,
    venue: Venue,
    leg_id: str,
    count: int,
    *,
    intent_id: str = INTENT_ID,
) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.OPEN_ORDERS_OBSERVED,
        intent_id=intent_id,
        venue=venue,
        leg_id=leg_id,
        payload={"open_order_count": count},
    )


def _recon(
    clock: Clock,
    *,
    matched: bool = True,
    intent_id: str = INTENT_ID,
    venue: Optional[Venue] = None,
    leg_id: Optional[str] = None,
) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.RECONCILIATION,
        intent_id=intent_id,
        venue=venue,
        leg_id=leg_id,
        payload={"matched": matched},
    )


def _mismatch(
    clock: Clock,
    *,
    intent_id: str = INTENT_ID,
    generation: int = 2,
) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.STREAM_GENERATION_MISMATCH,
        intent_id=intent_id,
        payload={
            "stream_generation": generation,
            "reason_code": "stream_generation_mismatch",
        },
    )


def _close_arm(clock: Clock, *, intent_id: str = CLOSE_INTENT_ID) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.INTENT_ACCEPTED,
        intent_id=intent_id,
        payload={
            "action": "close",
            "coin": "BTC",
            "spread_direction": "long",
            "lot_tolerance": "0",
        },
    )


def _flatness(clock: Clock, *, intent_id: str = CLOSE_INTENT_ID) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.FLATNESS_PROVEN,
        intent_id=intent_id,
        payload={"positions_flat": True, "open_orders_flat": True},
    )


def _happy_open(clock: Clock) -> list[ExecutionEvent]:
    return [
        _arm(clock),
        _sent(clock, Venue.OKX, OKX_LEG),
        _sent(clock, Venue.BYBIT, BYBIT_LEG),
        _ack(clock, Venue.OKX, OKX_LEG),
        _ack(clock, Venue.BYBIT, BYBIT_LEG),
        _fill(clock, Venue.OKX, OKX_LEG, quantity="0.4", partial=True),
        _fill(clock, Venue.OKX, OKX_LEG, quantity="1"),
        _fill(clock, Venue.BYBIT, BYBIT_LEG),
    ]


def _close_events(clock: Clock) -> list[ExecutionEvent]:
    cid = CLOSE_INTENT_ID
    return [
        _close_arm(clock, intent_id=cid),
        _sent(clock, Venue.OKX, OKX_LEG, intent_id=cid, reduce_only=True),
        _sent(clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid, reduce_only=True),
        _ack(clock, Venue.OKX, OKX_LEG, intent_id=cid),
        _ack(clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
        _fill(clock, Venue.OKX, OKX_LEG, intent_id=cid),
        _fill(clock, Venue.BYBIT, BYBIT_LEG, intent_id=cid),
        _pos(clock, Venue.OKX, OKX_LEG, "0", intent_id=cid),
        _pos(clock, Venue.BYBIT, BYBIT_LEG, "0", intent_id=cid),
        _orders(clock, Venue.OKX, OKX_LEG, 0, intent_id=cid),
        _orders(clock, Venue.BYBIT, BYBIT_LEG, 0, intent_id=cid),
        _flatness(clock, intent_id=cid),
    ]


class CrashAt:
    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.seen: list[str] = []

    def __call__(self, stage: str) -> None:
        self.seen.append(stage)
        if stage == self.stage:
            raise RuntimeError("injected_crash")


def _wal(path: Path, **overrides: object) -> ExecutionWal:
    payload = dict(
        run_id=RUN_ID,
        max_queue=8,
        reserved_tail=2,
        max_durable_lag=8,
    )
    payload.update(overrides)
    return ExecutionWal(path, **payload)  # type: ignore[arg-type]


def _drain_lifecycle(path: Path, events: list[ExecutionEvent], **overrides: object) -> ExecutionWal:
    wal = _wal(path, **overrides)
    wal.replay()
    for event in events:
        ack = wal.enqueue(event)
        assert ack.accepted and ack.durable is False
        durable = wal.drain_once()
        assert durable is not None and durable.durable
    return wal


def _mark_both_venues(
    wal: ExecutionWal,
    clock: Clock,
    *,
    intent_id: str = INTENT_ID,
    venue_wide: bool = False,
) -> tuple[str, str]:
    tokens: list[str] = []
    pairs = (
        ((Venue.OKX, None), (Venue.BYBIT, None))
        if venue_wide
        else ((Venue.OKX, OKX_LEG), (Venue.BYBIT, BYBIT_LEG))
    )
    for venue, leg_id in pairs:
        ack = wal.enqueue(_recon(clock, venue=venue, leg_id=leg_id, intent_id=intent_id))
        assert ack.accepted and ack.wal_seq is not None
        durable = wal.drain_once()
        assert durable is not None and durable.reconciliation_token
        tokens.append(durable.reconciliation_token)
    wal.mark_venue_reconciled(tokens[0])
    wal.mark_venue_reconciled(tokens[1])
    return tokens[0], tokens[1]


class WalPathAndConstructionTests(unittest.TestCase):
    def test_rejects_invalid_and_denied_paths(self) -> None:
        for bad in (
            "/tmp/events.jsonl",
            "/tmp/wal.jsonl",
            "/tmp/wal.v2/events.jsonl",
            "/data/live/wal.v2/wal.jsonl",
            "/data/bars/wal.v2/wal.jsonl",
            "/data/bbot/journal/wal.v2/wal.jsonl",
        ):
            with self.assertRaises(WalError):
                _wal(Path(bad))

    def test_rejects_invalid_queue_config(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            with self.assertRaises(WalError):
                _wal(path, max_queue=0)
            with self.assertRaises(WalError):
                _wal(path, reserved_tail=8)
            with self.assertRaises(WalError):
                _wal(path, reserved_tail=-1)
            with self.assertRaises(WalError):
                _wal(path, max_durable_lag=-1)

    def test_construction_creates_no_file_or_thread(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            before = threading.active_count()
            env_gets: list[object] = []
            real_get = os.environ.get

            def tracked_get(*args: object, **kwargs: object) -> object:
                env_gets.append(args)
                return real_get(*args, **kwargs)

            with patch.object(os.environ, "get", tracked_get):
                wal = _wal(path)
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())
            self.assertEqual(threading.active_count(), before)
            self.assertFalse(env_gets)
            self.assertTrue(wal.health().blocks_opens)
            self.assertFalse(wal.health().venue_reconciliation_complete)

    def test_import_has_no_network_or_sentry_surface(self) -> None:
        import app.bot.execution.wal as wal_mod

        source = inspect.getsource(wal_mod)
        self.assertNotIn("sentry_setup", source)
        self.assertNotIn("sentry_sdk", source)
        self.assertNotIn("psycopg", source)
        self.assertNotIn("socket", source)
        self.assertNotIn("threading", source)
        self.assertNotIn("os.environ", source)


class EnqueueDurabilityTests(unittest.TestCase):
    def test_enqueue_ack_is_not_durable_and_creates_no_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            wal = _wal(path)
            wal.replay()
            clock = Clock()
            event = _fill(clock, Venue.OKX, OKX_LEG, quantity="0.4", partial=True)
            fsync_calls = []
            opened: list[str] = []
            real_open = os.open

            def guarded_open(name: str, flags: int, *args: object, **kwargs: object) -> int:
                opened.append(str(name))
                return real_open(name, flags, *args, **kwargs)

            projector = InMemoryProjector()
            exporter = InMemoryExporter()
            with patch("os.fsync", side_effect=lambda fd: fsync_calls.append(fd)):
                with patch("os.open", guarded_open):
                    ack = wal.enqueue(event)
            self.assertIsInstance(ack, WalAppendAck)
            self.assertTrue(ack.accepted)
            self.assertFalse(ack.durable)
            self.assertEqual(ack.wal_seq, 1)
            self.assertFalse(path.exists())
            self.assertFalse(fsync_calls)
            self.assertFalse(opened)
            self.assertEqual(projector.watermark, 0)
            self.assertEqual(exporter.cursor.last_wal_seq, 0)
            self.assertEqual(wal.health().durable_wal_seq, 0)
            durable = wal.drain_once()
            self.assertIsInstance(durable, WalDurableAck)
            self.assertTrue(durable.durable)
            self.assertEqual(durable.wal_seq, 1)
            self.assertTrue(path.exists())
            raw_line = path.read_bytes().split(b"\n")[0]
            restored = decode_wal_line(
                raw_line,
                expected_run_id=RUN_ID,
                expected_seq=1,
                expected_prev=GENESIS_HASH,
            )
            self.assertEqual(restored.event.payload["quantity"], "0.4")
            self.assertEqual(restored.event.event_type, ExecutionEventType.PARTIAL_FILL)

    def test_partial_and_final_quantity_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            clock = Clock()
            events = _happy_open(clock)
            _drain_lifecycle(path, events)
            replayed = _wal(path).replay()
            quantities = [
                record.event.payload.get("quantity")
                for record in replayed.records
                if record.event.event_type
                in {ExecutionEventType.PARTIAL_FILL, ExecutionEventType.FILL}
            ]
            self.assertEqual(quantities, ["0.4", "1", "1"])
            okx = next(leg for leg in replayed.state.legs if leg.leg_id == OKX_LEG)
            self.assertEqual(str(okx.filled_quantity), "1")
            self.assertEqual(replayed.state.status, SpreadStatus.OPEN)


class BackpressureTests(unittest.TestCase):
    def test_reserved_tail_and_absolute_full_nack(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            wal = _wal(path, max_queue=4, reserved_tail=2)
            wal.replay()
            clock = Clock()
            first = wal.enqueue(_recon(clock))
            second = wal.enqueue(
                _event(clock, ExecutionEventType.PAUSE, payload={"pause": True, "reason_code": "pause"})
            )
            self.assertTrue(first.accepted)
            self.assertTrue(second.accepted)
            self.assertEqual(second.wal_seq, 2)
            nack = wal.enqueue(_arm(clock))
            self.assertFalse(nack.accepted)
            self.assertFalse(nack.durable)
            self.assertIsNone(nack.wal_seq)
            self.assertEqual(nack.reason_code, "reserved_tail")
            third = wal.enqueue(_recon(clock, intent_id=INTENT_ID))
            self.assertTrue(third.accepted)
            self.assertEqual(third.wal_seq, 3)
            fourth = wal.enqueue(
                _event(clock, ExecutionEventType.FAULT, payload={"reason_code": "fault"})
            )
            self.assertTrue(fourth.accepted)
            self.assertEqual(fourth.wal_seq, 4)
            filled = wal.health()
            self.assertTrue(filled.hard_full)
            self.assertTrue(filled.blocks_opens)
            full = wal.enqueue(_recon(clock))
            self.assertFalse(full.accepted)
            self.assertIsNone(full.wal_seq)
            self.assertEqual(full.reason_code, "hard_unhealthy")
            self.assertTrue(wal.health().hard_full)
            self.assertTrue(wal.health().blocks_opens)
            later = wal.enqueue(
                _event(clock, ExecutionEventType.PAUSE, payload={"pause": True, "reason_code": "pause"})
            )
            self.assertFalse(later.accepted)
            self.assertIsNone(later.wal_seq)
            self.assertEqual(later.reason_code, "hard_unhealthy")

    def test_nack_does_not_consume_seq_and_lifecycle_drains_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            wal = _wal(path, max_queue=6, reserved_tail=3)
            wal.replay()
            clock = Clock()
            accepted = [
                wal.enqueue(_arm(clock)),
                wal.enqueue(_sent(clock, Venue.OKX, OKX_LEG)),
                wal.enqueue(_sent(clock, Venue.BYBIT, BYBIT_LEG)),
            ]
            nack = wal.enqueue(_arm(Clock()))
            self.assertFalse(nack.accepted)
            self.assertIsNone(nack.wal_seq)
            more = [
                wal.enqueue(_ack(clock, Venue.OKX, OKX_LEG)),
                wal.enqueue(_ack(clock, Venue.BYBIT, BYBIT_LEG)),
                wal.enqueue(_fill(clock, Venue.OKX, OKX_LEG, quantity="0.4", partial=True)),
            ]
            self.assertTrue(all(item.accepted for item in accepted + more))
            self.assertEqual([item.wal_seq for item in accepted + more], [1, 2, 3, 4, 5, 6])
            acks = wal.drain_all()
            self.assertEqual([item.wal_seq for item in acks], [1, 2, 3, 4, 5, 6])
            types = [record.event.event_type for record in _wal(path).replay().records]
            self.assertEqual(
                types,
                [
                    ExecutionEventType.INTENT_ACCEPTED,
                    ExecutionEventType.REQUEST_SENT,
                    ExecutionEventType.REQUEST_SENT,
                    ExecutionEventType.ACK_ACCEPTED,
                    ExecutionEventType.ACK_ACCEPTED,
                    ExecutionEventType.PARTIAL_FILL,
                ],
            )


class WriterFailureTests(unittest.TestCase):
    def test_fsync_failure_latches_unhealthy_and_is_not_durable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            clock = Clock()

            def boom(_fd: int) -> None:
                raise OSError("fsync_failed")

            wal = _wal(path, fsync_fn=boom)
            wal.replay()
            wal.enqueue(_arm(clock))
            with self.assertRaises(WalError):
                wal.drain_once()
            health = wal.health()
            self.assertTrue(health.writer_unhealthy)
            self.assertTrue(health.blocks_opens)
            self.assertEqual(health.durable_wal_seq, 0)
            self.assertEqual(health.queue_depth, 1)
            with self.assertRaises(WalError):
                wal.drain_once()

    def test_flush_failure_latches_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            clock = Clock()
            wal = _wal(path)
            wal.replay()
            wal.enqueue(_arm(clock))
            real_open = Path.open

            def exploding_open(self: Path, *args: object, **kwargs: object) -> object:
                handle = real_open(self, *args, **kwargs)
                if self.name == "wal.jsonl" and "b" in str(args[0] if args else kwargs.get("mode", "")):
                    handle.flush = lambda: (_ for _ in ()).throw(OSError("flush_failed"))  # type: ignore[method-assign]
                return handle

            with patch.object(Path, "open", exploding_open):
                with self.assertRaises(WalError):
                    wal.drain_once()
            self.assertTrue(wal.health().writer_unhealthy)
            self.assertEqual(wal.health().durable_wal_seq, 0)


class CrashAndCorruptionTests(unittest.TestCase):
    def test_torn_tail_replays_prefix_and_is_removed_before_append(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            clock = Clock()
            first = _arm(clock)
            _drain_lifecycle(path, [first])
            with path.open("ab") as handle:
                handle.write(b'{"schema_version":"bbot.execution.wal.v2","wal_seq":2')
            restarted = _wal(path)
            result = restarted.replay()
            self.assertTrue(result.torn_tail)
            self.assertEqual(len(result.records), 1)
            self.assertEqual(result.records[0].event.event_id, first.event_id)
            second = _recon(clock)
            ack = restarted.enqueue(second)
            self.assertTrue(ack.accepted)
            durable = restarted.drain_once()
            assert durable is not None
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.endswith("\n"))
            self.assertNotIn('{"schema_version":"bbot.execution.wal.v2","wal_seq":2', text)
            self.assertTrue((path.parent / "wal.jsonl.torn").exists())
            replayed = _wal(path).replay()
            self.assertFalse(replayed.torn_tail)
            self.assertEqual(len(replayed.records), 2)
            self.assertEqual(replayed.records[1].prev_hash, replayed.records[0].record_hash)

    def test_middle_corruption_classes_fail_closed(self) -> None:
        cases = (
            "blank",
            "bad_json",
            "exact_key",
            "bad_hash",
            "chain_break",
            "sequence_gap",
            "run_mismatch",
        )
        for kind in cases:
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as raw:
                    path = Path(raw) / "wal.v2" / "wal.jsonl"
                    clock = Clock()
                    _drain_lifecycle(path, [_arm(clock), _sent(clock, Venue.OKX, OKX_LEG)])
                    lines = path.read_text(encoding="utf-8").splitlines()
                    parsed = json.loads(lines[1])
                    if kind == "blank":
                        lines.insert(1, "")
                    elif kind == "bad_json":
                        lines.insert(1, "{not-json")
                    elif kind == "exact_key":
                        parsed["extra"] = "nope"
                        lines[1] = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
                    elif kind == "bad_hash":
                        parsed["record_hash"] = "a" * 64
                        lines[1] = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
                    elif kind == "chain_break":
                        parsed["prev_hash"] = "b" * 64
                        body = {k: parsed[k] for k in parsed if k != "record_hash"}
                        parsed["record_hash"] = hashlib.sha256(
                            json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
                        ).hexdigest()
                        lines[1] = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
                    elif kind == "sequence_gap":
                        parsed["wal_seq"] = 9
                        body = {k: parsed[k] for k in parsed if k != "record_hash"}
                        parsed["record_hash"] = hashlib.sha256(
                            json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
                        ).hexdigest()
                        lines[1] = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
                    else:
                        parsed["run_id"] = OTHER_RUN
                        parsed["event"]["run_id"] = OTHER_RUN
                        body = {k: parsed[k] for k in parsed if k != "record_hash"}
                        parsed["record_hash"] = hashlib.sha256(
                            json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
                        ).hexdigest()
                        lines[1] = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
                    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    wal = _wal(path)
                    with self.assertRaises(WalIntegrityError):
                        wal.replay()
                    self.assertTrue(wal.health().integrity_unhealthy)
                    self.assertTrue(wal.health().blocks_opens)

    def test_run_mismatch_against_nonempty_wal_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            _drain_lifecycle(path, [_arm(Clock())])
            other = ExecutionWal(
                path,
                run_id=OTHER_RUN,
                max_queue=8,
                reserved_tail=2,
                max_durable_lag=8,
            )
            with self.assertRaises(WalIntegrityError):
                other.replay()

    def test_crash_hooks_expose_expected_prefix(self) -> None:
        stages = (
            CRASH_BEFORE_WRITE,
            CRASH_AFTER_WRITE_BEFORE_FLUSH,
            CRASH_AFTER_FLUSH_BEFORE_FSYNC,
            CRASH_TORN_WRITE,
            CRASH_AFTER_FSYNC_BEFORE_ACK,
        )
        for stage in stages:
            with self.subTest(stage=stage):
                with tempfile.TemporaryDirectory() as raw:
                    path = Path(raw) / "wal.v2" / "wal.jsonl"
                    clock = Clock()
                    first = _arm(clock)
                    _drain_lifecycle(path, [first])
                    hook = CrashAt(stage)
                    wal = _wal(path, crash_hook=hook)
                    wal.replay()
                    wal.enqueue(_recon(clock))
                    with self.assertRaises(RuntimeError):
                        wal.drain_once()
                    self.assertIn(stage, hook.seen)
                    self.assertTrue(wal.health().writer_unhealthy)
                    restarted = _wal(path)
                    result = restarted.replay()
                    if stage == CRASH_BEFORE_WRITE:
                        self.assertEqual(len(result.records), 1)
                        self.assertFalse(result.torn_tail)
                    if stage == CRASH_TORN_WRITE:
                        self.assertEqual(len(result.records), 1)
                        self.assertTrue(result.torn_tail)
                        self.assertFalse(path.read_bytes().endswith(b"\n"))
                    if stage == CRASH_AFTER_FSYNC_BEFORE_ACK:
                        self.assertEqual(len(result.records), 2)
                        self.assertFalse(result.torn_tail)
                    self.assertFalse(result.opens_allowed)


class ReplayGateTests(unittest.TestCase):
    def test_replayed_open_flat_and_empty_block_live_opens(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            empty_path = Path(raw) / "empty" / "wal.v2" / "wal.jsonl"
            open_path = Path(raw) / "open" / "wal.v2" / "wal.jsonl"
            flat_path = Path(raw) / "flat" / "wal.v2" / "wal.jsonl"
            empty = _wal(empty_path).replay()
            self.assertEqual(empty.records, ())
            self.assertFalse(empty.opens_allowed)
            self.assertTrue(empty.requires_venue_reconciliation)
            self.assertEqual(empty.state.status, SpreadStatus.IDLE)
            clock = Clock()
            _drain_lifecycle(open_path, _happy_open(clock))
            opened = _wal(open_path).replay()
            self.assertEqual(opened.state.status, SpreadStatus.OPEN)
            self.assertFalse(opens_allowed(opened.state))
            self.assertFalse(opened.opens_allowed)
            self.assertTrue(opened.requires_venue_reconciliation)
            close_clock = Clock()
            close_clock.seq = 0
            close_clock.mono = 2000
            _drain_lifecycle(flat_path, _happy_open(Clock()) + _close_events(close_clock))
            flat = _wal(flat_path).replay()
            self.assertEqual(flat.state.status, SpreadStatus.FLAT)
            self.assertTrue(is_proven_flat(flat.state))
            self.assertFalse(flat.opens_allowed)
            self.assertTrue(flat.requires_venue_reconciliation)
            self.assertIsInstance(flat, ReplayResult)

    def test_only_current_process_durable_recon_token_clears_restart_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            clock = Clock()
            _drain_lifecycle(path, _happy_open(clock))
            restarted = _wal(path)
            replayed = restarted.replay()
            self.assertTrue(replayed.requires_venue_reconciliation)
            self.assertTrue(restarted.health().blocks_opens)
            with self.assertRaises(WalError):
                restarted.mark_venue_reconciled("forged-token")
            historical = replayed.records[0]
            with self.assertRaises(WalError):
                restarted.mark_venue_reconciled(
                    f"forged:{historical.wal_seq}:{historical.record_hash}"
                )
            venue_less = restarted.enqueue(_recon(clock))
            self.assertTrue(venue_less.accepted)
            venue_less_durable = restarted.drain_once()
            assert venue_less_durable is not None
            self.assertIsNone(venue_less_durable.reconciliation_token)
            with self.assertRaises(WalError):
                restarted.mark_venue_reconciled("unused")
            unmatched = restarted.enqueue(
                _recon(clock, venue=Venue.OKX, leg_id=OKX_LEG, matched=False)
            )
            self.assertTrue(unmatched.accepted)
            unmatched_durable = restarted.drain_once()
            assert unmatched_durable is not None
            self.assertIsNone(unmatched_durable.reconciliation_token)
            okx_ack = restarted.enqueue(_recon(clock, venue=Venue.OKX, leg_id=OKX_LEG))
            self.assertTrue(okx_ack.accepted)
            okx_durable = restarted.drain_once()
            assert okx_durable is not None
            self.assertIsNotNone(okx_durable.reconciliation_token)
            restarted.mark_venue_reconciled(okx_durable.reconciliation_token)
            one_venue = restarted.health()
            self.assertFalse(one_venue.venue_reconciliation_complete)
            self.assertTrue(one_venue.blocks_opens)
            bybit_ack = restarted.enqueue(_recon(clock, venue=Venue.BYBIT, leg_id=BYBIT_LEG))
            self.assertTrue(bybit_ack.accepted)
            bybit_durable = restarted.drain_once()
            assert bybit_durable is not None
            self.assertIsNotNone(bybit_durable.reconciliation_token)
            self.assertNotEqual(okx_durable.reconciliation_token, bybit_durable.reconciliation_token)
            restarted.mark_venue_reconciled(bybit_durable.reconciliation_token)
            healthy = restarted.health()
            self.assertTrue(healthy.venue_reconciliation_complete)
            self.assertFalse(healthy.writer_unhealthy)
            self.assertFalse(healthy.blocks_opens)

    def test_unhealthy_and_lag_still_dominate_after_mark(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            clock = Clock()
            wal = _wal(path, max_queue=8, reserved_tail=2, max_durable_lag=0)
            wal.replay()
            _mark_both_venues(wal, clock, venue_wide=True)
            self.assertFalse(wal.health().blocks_opens)
            wal.enqueue(_event(clock, ExecutionEventType.PAUSE, payload={"pause": True, "reason_code": "pause"}))
            self.assertGreater(wal.health().durable_lag, 0)
            self.assertTrue(wal.health().blocks_opens)

    def test_lifecycle_replay_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            clock = Clock()
            events = [
                *_happy_open(clock),
                _recon(clock, venue=Venue.OKX, leg_id=OKX_LEG),
                _recon(clock, venue=Venue.BYBIT, leg_id=BYBIT_LEG),
            ]
            close_clock = Clock()
            close_clock.seq = 0
            close_clock.mono = clock.mono + 1000
            events.extend(_close_events(close_clock))
            _drain_lifecycle(path, events)
            first = _wal(path).replay()
            second = _wal(path).replay()
            self.assertEqual(first.state.to_public_dict(), second.state.to_public_dict())
            self.assertEqual(
                [item.record_hash for item in first.records],
                [item.record_hash for item in second.records],
            )
            expected = apply_events(initial_spread_state(run_id=RUN_ID), events)
            self.assertEqual(first.state.to_public_dict(), expected.to_public_dict())
            self.assertEqual(first.state.status, SpreadStatus.FLAT)


class ProjectorTests(unittest.TestCase):
    def test_apply_reapply_gap_and_conflict(self) -> None:
        clock = Clock()
        first, _ = encode_wal_record(
            _arm(clock), wal_seq=1, run_id=RUN_ID, prev_hash=GENESIS_HASH
        )
        second, _ = encode_wal_record(
            _recon(clock), wal_seq=2, run_id=RUN_ID, prev_hash=first.record_hash
        )
        third, _ = encode_wal_record(
            _event(clock, ExecutionEventType.PAUSE, payload={"pause": True, "reason_code": "pause"}),
            wal_seq=3,
            run_id=RUN_ID,
            prev_hash=second.record_hash,
        )
        projector = InMemoryProjector()
        ack1 = projector.apply(first)
        self.assertTrue(ack1.applied)
        self.assertEqual(ack1.watermark, 1)
        ack1b = projector.apply(first)
        self.assertFalse(ack1b.applied)
        self.assertTrue(ack1b.duplicate)
        self.assertEqual(ack1b.watermark, 1)
        with self.assertRaises(WalIntegrityError):
            projector.apply(third)
        ack2 = projector.apply(second)
        self.assertTrue(ack2.applied)
        conflict, _ = encode_wal_record(
            _event(clock, ExecutionEventType.FAULT, payload={"reason_code": "fault"}),
            wal_seq=2,
            run_id=RUN_ID,
            prev_hash=first.record_hash,
        )
        require_wal_record(second)
        require_wal_record(conflict)
        self.assertEqual((second.run_id, second.wal_seq), (conflict.run_id, conflict.wal_seq))
        self.assertNotEqual(second.record_hash, conflict.record_hash)
        with self.assertRaises(WalIntegrityError):
            projector.apply(conflict)
        self.assertIn("watermark", projector.projection())
        blob = json.dumps(dict(projector.projection()))
        for marker in FORBIDDEN_MARKERS:
            self.assertNotIn(marker, blob)

    def test_projector_failure_does_not_change_wal_durability(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            wal = _wal(path)
            wal.replay()
            clock = Clock()
            wal.enqueue(_arm(clock))
            durable = wal.drain_once()
            assert durable is not None
            projector = InMemoryProjector()
            gap, _ = encode_wal_record(
                _recon(clock), wal_seq=3, run_id=RUN_ID, prev_hash=GENESIS_HASH
            )
            require_wal_record(gap)
            with self.assertRaises(WalIntegrityError):
                projector.apply(gap)
            self.assertEqual(wal.health().durable_wal_seq, 1)
            self.assertFalse(wal.health().writer_unhealthy)
            self.assertTrue(path.exists())


class RedactionAndIsolationTests(unittest.TestCase):
    def test_public_errors_and_reprs_redact_forbidden_input(self) -> None:
        err = WalError("bad", api_key="acct-secret-1", order_id="RAW-ORDER-ID-99")
        text = repr(err) + str(err) + json.dumps(err.to_public_dict())
        for marker in FORBIDDEN_MARKERS:
            self.assertNotIn(marker, text)
        clock = Clock()
        record, _ = encode_wal_record(
            _arm(clock), wal_seq=1, run_id=RUN_ID, prev_hash=GENESIS_HASH
        )
        public = json.dumps(record.to_public_dict())
        self.assertIn(WAL_SCHEMA_VERSION, public)
        self.assertNotIn("RAW-ORDER-ID-99", public)
        self.assertEqual(record.prev_hash, GENESIS_HASH)

    def test_enqueue_run_mismatch_fails_closed_without_seq(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            wal = _wal(path)
            wal.replay()
            clock = Clock()
            with self.assertRaises(WalError):
                wal.enqueue(_arm(clock).__class__(  # type: ignore[misc]
                    schema_version=SCHEMA_VERSION,
                    event_id="evt_00000000000000000000000000000099",
                    event_type=ExecutionEventType.PAUSE,
                    intent_id=INTENT_ID,
                    run_id=OTHER_RUN,
                    sequence=1,
                    monotonic_ns=1,
                    venue=None,
                    leg_id=None,
                    payload={"pause": True, "reason_code": "pause"},
                ))
            self.assertEqual(wal.health().next_wal_seq, 1)
            self.assertEqual(wal.health().queue_depth, 0)

    def test_durable_lag_blocks_opens_until_drain(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            wal = _wal(path, max_durable_lag=0)
            wal.replay()
            clock = Clock()
            first = wal.enqueue(_recon(clock, venue=Venue.OKX))
            self.assertTrue(first.accepted)
            self.assertTrue(wal.health().blocks_opens)
            first_durable = wal.drain_once()
            assert first_durable is not None
            second = wal.enqueue(_recon(clock, venue=Venue.BYBIT))
            self.assertTrue(second.accepted)
            self.assertTrue(wal.health().blocks_opens)
            second_durable = wal.drain_once()
            assert second_durable is not None
            wal.mark_venue_reconciled(first_durable.reconciliation_token)
            wal.mark_venue_reconciled(second_durable.reconciliation_token)
            self.assertEqual(second.wal_seq, second_durable.wal_seq)
            self.assertFalse(wal.health().blocks_opens)


class ReplayPreconditionTests(unittest.TestCase):
    def test_enqueue_before_replay_nacks_first_boot_without_seq_or_fs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            wal = _wal(path)
            clock = Clock()
            opened: list[str] = []
            exists_calls: list[str] = []
            real_open = os.open
            real_exists = Path.exists

            def guarded_open(name: str, flags: int, *args: object, **kwargs: object) -> int:
                opened.append(str(name))
                return real_open(name, flags, *args, **kwargs)

            def tracked_exists(self: Path) -> bool:
                exists_calls.append(str(self))
                return real_exists(self)

            with patch("os.open", guarded_open), patch.object(Path, "exists", tracked_exists):
                with patch("os.fsync", side_effect=lambda _fd: (_ for _ in ()).throw(AssertionError("fsync"))):
                    ack = wal.enqueue(_arm(clock))
            self.assertFalse(ack.accepted)
            self.assertFalse(ack.durable)
            self.assertIsNone(ack.wal_seq)
            self.assertEqual(ack.reason_code, "replay_required")
            self.assertEqual(wal.health().next_wal_seq, 1)
            self.assertEqual(wal.health().queue_depth, 0)
            self.assertFalse(path.exists())
            self.assertFalse(opened)
            self.assertFalse(exists_calls)
            wal.replay()
            accepted = wal.enqueue(_arm(Clock()))
            self.assertTrue(accepted.accepted)
            self.assertEqual(accepted.wal_seq, 1)

    def test_enqueue_before_replay_nacks_restart_without_unusable_seq(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            clock = Clock()
            first = _arm(clock)
            _drain_lifecycle(path, [first])
            before = path.read_bytes()
            restarted = _wal(path)
            opened: list[str] = []
            real_open = os.open

            def guarded_open(name: str, flags: int, *args: object, **kwargs: object) -> int:
                opened.append(str(name))
                return real_open(name, flags, *args, **kwargs)

            with patch("os.open", guarded_open):
                nack = restarted.enqueue(_recon(clock))
            self.assertFalse(nack.accepted)
            self.assertIsNone(nack.wal_seq)
            self.assertEqual(nack.reason_code, "replay_required")
            self.assertEqual(restarted.health().next_wal_seq, 1)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(opened)
            replayed = restarted.replay()
            self.assertEqual(len(replayed.records), 1)
            accepted = restarted.enqueue(_recon(clock))
            self.assertTrue(accepted.accepted)
            self.assertEqual(accepted.wal_seq, 2)


class ReservedTailAndHardFullHealthTests(unittest.TestCase):
    def test_reserved_tail_watermark_blocks_opens_without_hard_full(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            wal = _wal(path, max_queue=4, reserved_tail=2, max_durable_lag=8)
            wal.replay()
            clock = Clock()
            _mark_both_venues(wal, clock, venue_wide=True)
            self.assertFalse(wal.health().blocks_opens)
            self.assertFalse(wal.health().hard_full)
            first = wal.enqueue(
                _event(clock, ExecutionEventType.PAUSE, payload={"pause": True, "reason_code": "pause"})
            )
            second = wal.enqueue(
                _event(clock, ExecutionEventType.FAULT, payload={"reason_code": "fault"})
            )
            self.assertTrue(first.accepted)
            self.assertTrue(second.accepted)
            health = wal.health()
            self.assertEqual(health.queue_depth, 2)
            self.assertTrue(health.blocks_opens)
            self.assertFalse(health.hard_full)
            self.assertFalse(health.writer_unhealthy)
            self.assertTrue(health.venue_reconciliation_complete)

    def test_absolute_full_latches_without_further_enqueue_and_drain_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            wal = _wal(path, max_queue=3, reserved_tail=1, max_durable_lag=8)
            wal.replay()
            clock = Clock()
            _mark_both_venues(wal, clock, venue_wide=True)
            self.assertFalse(wal.health().hard_full)
            for _ in range(3):
                ack = wal.enqueue(
                    _event(clock, ExecutionEventType.PAUSE, payload={"pause": True, "reason_code": "pause"})
                )
                self.assertTrue(ack.accepted)
            health = wal.health()
            self.assertEqual(health.queue_depth, 3)
            self.assertTrue(health.hard_full)
            self.assertTrue(health.blocks_opens)
            acks = wal.drain_all()
            self.assertEqual([item.wal_seq for item in acks], [3, 4, 5])
            self.assertEqual(wal.health().queue_depth, 0)
            self.assertTrue(wal.health().hard_full)
            self.assertTrue(wal.health().blocks_opens)
            later = wal.enqueue(
                _event(clock, ExecutionEventType.FAULT, payload={"reason_code": "fault"})
            )
            self.assertFalse(later.accepted)
            self.assertIsNone(later.wal_seq)
            self.assertEqual(later.reason_code, "hard_unhealthy")


class VenueReconciliationTokenTests(unittest.TestCase):
    def test_first_boot_requires_distinct_bybit_and_okx_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            wal = _wal(path)
            wal.replay()
            clock = Clock()
            venue_less = wal.enqueue(_recon(clock))
            self.assertTrue(venue_less.accepted)
            self.assertIsNone(wal.drain_once().reconciliation_token)  # type: ignore[union-attr]
            unmatched_wide = wal.enqueue(_recon(clock, venue=Venue.OKX, matched=False))
            self.assertTrue(unmatched_wide.accepted)
            self.assertIsNone(wal.drain_once().reconciliation_token)  # type: ignore[union-attr]
            okx = wal.enqueue(_recon(clock, venue=Venue.OKX))
            okx_durable = wal.drain_once()
            assert okx.accepted and okx_durable is not None
            self.assertIsNotNone(okx_durable.reconciliation_token)
            wal.mark_venue_reconciled(okx_durable.reconciliation_token)
            self.assertFalse(wal.health().venue_reconciliation_complete)
            self.assertTrue(wal.health().blocks_opens)
            second_okx = wal.enqueue(_recon(clock, venue=Venue.OKX))
            second_okx_durable = wal.drain_once()
            assert second_okx_durable is not None
            wal.mark_venue_reconciled(second_okx_durable.reconciliation_token)
            self.assertFalse(wal.health().venue_reconciliation_complete)
            bybit = wal.enqueue(_recon(clock, venue=Venue.BYBIT))
            bybit_durable = wal.drain_once()
            assert bybit_durable is not None
            self.assertNotEqual(okx_durable.reconciliation_token, bybit_durable.reconciliation_token)
            wal.mark_venue_reconciled(bybit_durable.reconciliation_token)
            self.assertTrue(wal.health().venue_reconciliation_complete)
            self.assertFalse(wal.health().blocks_opens)
            same = wal.replay()
            self.assertEqual(same.state.status, SpreadStatus.IDLE)
            self.assertTrue(same.integrity_ok)
            self.assertFalse(same.opens_allowed)
            restarted = _wal(path).replay()
            self.assertEqual(restarted.state.status, SpreadStatus.IDLE)
            self.assertEqual(len(restarted.records), 5)
            self.assertTrue(restarted.integrity_ok)

    def test_same_intent_first_boot_venue_wide_pair_replays(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            wal = _wal(path)
            empty = wal.replay()
            self.assertEqual(empty.state.status, SpreadStatus.IDLE)
            self.assertEqual(empty.records, ())
            clock = Clock()
            okx_tok, bybit_tok = _mark_both_venues(wal, clock, venue_wide=True)
            self.assertNotEqual(okx_tok, bybit_tok)
            self.assertTrue(wal.health().venue_reconciliation_complete)
            self.assertFalse(wal.health().blocks_opens)
            replayed = wal.replay()
            self.assertEqual(replayed.state.status, SpreadStatus.IDLE)
            self.assertFalse(replayed.state.recovery_required)
            self.assertTrue(replayed.state.stream_generation_ok)
            self.assertTrue(replayed.requires_venue_reconciliation)
            self.assertFalse(replayed.opens_allowed)
            self.assertEqual(len(replayed.records), 2)
            self.assertTrue(all(item.event.leg_id is None for item in replayed.records))
            self.assertEqual(
                {item.event.intent_id for item in replayed.records}, {INTENT_ID}
            )
            restarted = _wal(path)
            fresh = restarted.replay()
            self.assertEqual(fresh.state.status, SpreadStatus.IDLE)
            self.assertTrue(fresh.integrity_ok)
            self.assertTrue(restarted.health().blocks_opens)
            self.assertFalse(restarted.health().venue_reconciliation_complete)

    def test_cross_intent_first_boot_tokens_cannot_combine(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            wal = _wal(path)
            wal.replay()
            clock = Clock()
            okx = wal.enqueue(_recon(clock, venue=Venue.OKX, intent_id=INTENT_ID))
            okx_durable = wal.drain_once()
            bybit = wal.enqueue(_recon(clock, venue=Venue.BYBIT, intent_id=OTHER_INTENT_ID))
            bybit_durable = wal.drain_once()
            assert okx.accepted and bybit.accepted
            assert okx_durable is not None and bybit_durable is not None
            self.assertIsNotNone(okx_durable.reconciliation_token)
            self.assertIsNotNone(bybit_durable.reconciliation_token)
            wal.mark_venue_reconciled(okx_durable.reconciliation_token)
            self.assertFalse(wal.health().venue_reconciliation_complete)
            self.assertTrue(wal.health().blocks_opens)
            with self.assertRaises(WalError) as raised:
                wal.mark_venue_reconciled(bybit_durable.reconciliation_token)
            self.assertEqual(raised.exception.code, "intent_mismatch")
            self.assertFalse(wal.health().venue_reconciliation_complete)
            self.assertTrue(wal.health().blocks_opens)
            same_intent = wal.enqueue(_recon(clock, venue=Venue.BYBIT, intent_id=INTENT_ID))
            same_durable = wal.drain_once()
            assert same_intent.accepted and same_durable is not None
            wal.mark_venue_reconciled(same_durable.reconciliation_token)
            self.assertTrue(wal.health().venue_reconciliation_complete)
            self.assertFalse(wal.health().blocks_opens)

    def test_cross_intent_active_tokens_cannot_combine(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            clock = Clock()
            wal = _drain_lifecycle(path, _happy_open(clock))
            okx = wal.enqueue(
                _recon(clock, venue=Venue.OKX, leg_id=OKX_LEG, intent_id=INTENT_ID)
            )
            okx_durable = wal.drain_once()
            bybit = wal.enqueue(
                _recon(clock, venue=Venue.BYBIT, leg_id=BYBIT_LEG, intent_id=OTHER_INTENT_ID)
            )
            bybit_durable = wal.drain_once()
            assert okx.accepted and bybit.accepted
            assert okx_durable is not None and bybit_durable is not None
            wal.mark_venue_reconciled(okx_durable.reconciliation_token)
            self.assertFalse(wal.health().venue_reconciliation_complete)
            with self.assertRaises(WalError) as raised:
                wal.mark_venue_reconciled(bybit_durable.reconciliation_token)
            self.assertEqual(raised.exception.code, "intent_mismatch")
            self.assertFalse(wal.health().venue_reconciliation_complete)
            self.assertTrue(wal.health().blocks_opens)
            self.assertFalse(wal.health().integrity_unhealthy)
            same = wal.enqueue(
                _recon(clock, venue=Venue.BYBIT, leg_id=BYBIT_LEG, intent_id=INTENT_ID)
            )
            same_durable = wal.drain_once()
            assert same.accepted and same_durable is not None
            wal.mark_venue_reconciled(same_durable.reconciliation_token)
            self.assertTrue(wal.health().venue_reconciliation_complete)

    def test_stream_generation_mismatch_invalidates_recon_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            wal = _wal(path)
            wal.replay()
            clock = Clock()
            okx = wal.enqueue(_recon(clock, venue=Venue.OKX))
            okx_durable = wal.drain_once()
            bybit = wal.enqueue(_recon(clock, venue=Venue.BYBIT))
            bybit_durable = wal.drain_once()
            assert okx_durable is not None and bybit_durable is not None
            pre_okx = okx_durable.reconciliation_token
            pre_bybit = bybit_durable.reconciliation_token
            wal.mark_venue_reconciled(pre_okx)
            mismatch = wal.enqueue(_mismatch(clock))
            self.assertTrue(mismatch.accepted)
            mismatch_durable = wal.drain_once()
            assert mismatch_durable is not None
            self.assertIsNone(mismatch_durable.reconciliation_token)
            self.assertFalse(wal.health().venue_reconciliation_complete)
            self.assertTrue(wal.health().blocks_opens)
            with self.assertRaises(WalError):
                wal.mark_venue_reconciled(pre_okx)
            with self.assertRaises(WalError):
                wal.mark_venue_reconciled(pre_bybit)
            self.assertFalse(wal.health().venue_reconciliation_complete)
            self.assertTrue(wal.health().blocks_opens)
            fresh_okx = wal.enqueue(_recon(clock, venue=Venue.OKX))
            fresh_okx_durable = wal.drain_once()
            fresh_bybit = wal.enqueue(_recon(clock, venue=Venue.BYBIT))
            fresh_bybit_durable = wal.drain_once()
            assert fresh_okx_durable is not None and fresh_bybit_durable is not None
            self.assertNotEqual(fresh_okx_durable.reconciliation_token, pre_okx)
            wal.mark_venue_reconciled(fresh_okx_durable.reconciliation_token)
            self.assertFalse(wal.health().venue_reconciliation_complete)
            wal.mark_venue_reconciled(fresh_bybit_durable.reconciliation_token)
            self.assertTrue(wal.health().venue_reconciliation_complete)
            self.assertFalse(wal.health().blocks_opens)
            replayed = wal.replay()
            self.assertTrue(replayed.integrity_ok)
            self.assertEqual(replayed.state.status, SpreadStatus.IDLE)
            self.assertTrue(replayed.requires_venue_reconciliation)

    def test_mismatch_after_completed_pair_blocks_opens_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            clock = Clock()
            wal = _drain_lifecycle(path, _happy_open(clock))
            okx_tok, bybit_tok = _mark_both_venues(wal, clock)
            self.assertTrue(wal.health().venue_reconciliation_complete)
            self.assertFalse(wal.health().blocks_opens)
            mismatch = wal.enqueue(_mismatch(clock))
            self.assertTrue(mismatch.accepted)
            durable = wal.drain_once()
            assert durable is not None
            health = wal.health()
            self.assertFalse(health.venue_reconciliation_complete)
            self.assertTrue(health.blocks_opens)
            self.assertFalse(health.writer_unhealthy)
            self.assertFalse(health.integrity_unhealthy)
            with self.assertRaises(WalError):
                wal.mark_venue_reconciled(okx_tok)
            with self.assertRaises(WalError):
                wal.mark_venue_reconciled(bybit_tok)
            self.assertTrue(wal.health().blocks_opens)
            fresh_okx, fresh_bybit = _mark_both_venues(wal, clock)
            self.assertNotEqual(fresh_okx, okx_tok)
            self.assertNotEqual(fresh_bybit, bybit_tok)
            self.assertTrue(wal.health().venue_reconciliation_complete)
            self.assertFalse(wal.health().blocks_opens)

    def test_historical_and_unqualified_tokens_cannot_clear_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            clock = Clock()
            _drain_lifecycle(path, _happy_open(clock))
            first = _wal(path)
            first.replay()
            okx_tok, bybit_tok = _mark_both_venues(first, clock)
            self.assertTrue(first.health().venue_reconciliation_complete)
            restarted = _wal(path)
            replayed = restarted.replay()
            self.assertTrue(restarted.health().blocks_opens)
            self.assertFalse(restarted.health().venue_reconciliation_complete)
            with self.assertRaises(WalError):
                restarted.mark_venue_reconciled(okx_tok)
            with self.assertRaises(WalError):
                restarted.mark_venue_reconciled(bybit_tok)
            historical = next(
                item
                for item in replayed.records
                if item.event.event_type is ExecutionEventType.RECONCILIATION
            )
            with self.assertRaises(WalError):
                restarted.mark_venue_reconciled(
                    f"{historical.wal_seq}:{historical.record_hash}"
                )

    def test_begin_restart_advances_epoch_and_rejects_prior_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wal.v2" / "wal.jsonl"
            clock = Clock()
            wal = _wal(path)
            wal.replay()
            prior_okx, prior_bybit = _mark_both_venues(wal, clock, venue_wide=True)
            self.assertTrue(wal.health().venue_reconciliation_complete)
            self.assertFalse(wal.health().blocks_opens)
            wal.begin_restart()
            self.assertFalse(wal.health().venue_reconciliation_complete)
            self.assertTrue(wal.health().blocks_opens)
            with self.assertRaises(WalError):
                wal.mark_venue_reconciled(prior_okx)
            with self.assertRaises(WalError):
                wal.mark_venue_reconciled(prior_bybit)
            fresh_okx = wal.enqueue(_recon(clock, venue=Venue.OKX))
            fresh_okx_durable = wal.drain_once()
            assert fresh_okx.accepted and fresh_okx_durable is not None
            accepted = wal.mark_venue_reconciled(fresh_okx_durable.reconciliation_token)
            self.assertEqual(accepted, Venue.OKX)
            self.assertFalse(wal.health().venue_reconciliation_complete)
            self.assertTrue(wal.health().blocks_opens)
            fresh_bybit = wal.enqueue(_recon(clock, venue=Venue.BYBIT))
            fresh_bybit_durable = wal.drain_once()
            assert fresh_bybit.accepted and fresh_bybit_durable is not None
            self.assertEqual(
                wal.mark_venue_reconciled(fresh_bybit_durable.reconciliation_token),
                Venue.BYBIT,
            )
            self.assertTrue(wal.health().venue_reconciliation_complete)
            self.assertFalse(wal.health().blocks_opens)
            self.assertNotEqual(fresh_okx_durable.reconciliation_token, prior_okx)
            self.assertNotEqual(fresh_bybit_durable.reconciliation_token, prior_bybit)


class WalRecordValidationTests(unittest.TestCase):
    def test_public_record_rejects_schema_type_run_and_hash_errors(self) -> None:
        clock = Clock()
        event = _arm(clock)
        valid, _ = encode_wal_record(event, wal_seq=1, run_id=RUN_ID, prev_hash=GENESIS_HASH)
        require_wal_record(valid)
        self.assertEqual(set(valid.to_public_dict()), set(valid.to_public_dict().keys()))
        with self.assertRaises(WalIntegrityError):
            WalRecord(
                schema_version="bbot.private.journal.v1",
                wal_seq=1,
                run_id=RUN_ID,
                prev_hash=GENESIS_HASH,
                event=event,
                event_content_hash=event.content_hash(),
                event_sequence_hash=event.sequence_hash(),
                record_hash=valid.record_hash,
            )
        with self.assertRaises(WalIntegrityError):
            WalRecord(
                schema_version=WAL_SCHEMA_VERSION,
                wal_seq=True,  # type: ignore[arg-type]
                run_id=RUN_ID,
                prev_hash=GENESIS_HASH,
                event=event,
                event_content_hash=event.content_hash(),
                event_sequence_hash=event.sequence_hash(),
                record_hash=valid.record_hash,
            )
        with self.assertRaises(WalIntegrityError):
            WalRecord(
                schema_version=WAL_SCHEMA_VERSION,
                wal_seq=1,
                run_id=OTHER_RUN,
                prev_hash=GENESIS_HASH,
                event=event,
                event_content_hash=event.content_hash(),
                event_sequence_hash=event.sequence_hash(),
                record_hash=valid.record_hash,
            )
        with self.assertRaises(WalIntegrityError):
            WalRecord(
                schema_version=WAL_SCHEMA_VERSION,
                wal_seq=1,
                run_id=RUN_ID,
                prev_hash=GENESIS_HASH,
                event=event,
                event_content_hash="a" * 64,
                event_sequence_hash=event.sequence_hash(),
                record_hash=valid.record_hash,
            )
        with self.assertRaises(WalIntegrityError):
            WalRecord(
                schema_version=WAL_SCHEMA_VERSION,
                wal_seq=1,
                run_id=RUN_ID,
                prev_hash=GENESIS_HASH,
                event=event,
                event_content_hash=event.content_hash(),
                event_sequence_hash=event.sequence_hash(),
                record_hash="c" * 64,
            )

    def test_projector_and_exporter_conflict_uses_independently_valid_records(self) -> None:
        clock = Clock()
        first_event = _arm(clock)
        second_event = _recon(clock, venue=Venue.OKX, leg_id=OKX_LEG)
        left, _ = encode_wal_record(
            first_event, wal_seq=1, run_id=RUN_ID, prev_hash=GENESIS_HASH
        )
        right, _ = encode_wal_record(
            second_event, wal_seq=1, run_id=RUN_ID, prev_hash=GENESIS_HASH
        )
        require_wal_record(left)
        require_wal_record(right)
        self.assertEqual((left.run_id, left.wal_seq), (right.run_id, right.wal_seq))
        self.assertNotEqual(left.record_hash, right.record_hash)
        projector = InMemoryProjector()
        self.assertTrue(projector.apply(left).applied)
        with self.assertRaises(WalIntegrityError):
            projector.apply(right)
        exporter = InMemoryExporter()
        exporter.export(left)
        with self.assertRaises(WalIntegrityError):
            exporter.export(right)


if __name__ == "__main__":
    unittest.main()
