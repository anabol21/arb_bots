"""WS gap JSONL contract, journal pairing, and path root."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.schema.ws_gap import (
    WS_GAP_REQUIRED_FIELDS,
    WsGapSchemaError,
    encode_gap_record,
    gaps_jsonl_path,
    utc_event_date,
    validate_gap_record,
)
from app.storage.paths import resolve_gaps_root
from app.utils.ws_gap_journal import WsGapJournal


class WsGapSchemaTests(unittest.TestCase):
    def test_encode_roundtrip_and_path(self) -> None:
        t_down = 1_704_067_200_000  # 2024-01-01T00:00:00Z
        record = encode_gap_record(
            base_coin="BTC",
            exchange="bybit",
            channel="orderbook.1",
            t_down_ms=t_down,
            t_up_ms=t_down + 2_000,
            close_code=1006,
        )
        for name in WS_GAP_REQUIRED_FIELDS:
            self.assertIn(name, record)
        self.assertEqual(record["t_up_ms"] - record["t_down_ms"], 2000)
        self.assertEqual(utc_event_date(record["t_down_ms"]), "2024-01-01")
        path = gaps_jsonl_path("/data/gaps", "2025-08-23")
        self.assertEqual(path, Path("/data/gaps/event_date=2025-08-23/gaps.jsonl"))
        self.assertNotIn("live", path.parts)
        self.assertNotIn("bbot", path.parts)

    def test_rejects_missing_and_inverted_interval(self) -> None:
        with self.assertRaises(WsGapSchemaError):
            validate_gap_record({"base_coin": "BTC"})
        with self.assertRaises(WsGapSchemaError):
            encode_gap_record(
                base_coin="BTC",
                exchange="okx",
                channel="books5",
                t_down_ms=2_000,
                t_up_ms=1_000,
                close_code=None,
            )


class GapsRootTests(unittest.TestCase):
    def test_default_and_env(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SPREAD_GAPS_ROOT", None)
            self.assertEqual(resolve_gaps_root(), Path("/data/gaps"))
        with mock.patch.dict(os.environ, {"SPREAD_GAPS_ROOT": "/tmp/gaps-test"}):
            self.assertEqual(resolve_gaps_root(), Path("/tmp/gaps-test"))


class WsGapJournalTests(unittest.TestCase):
    def test_first_subscribe_writes_nothing(self) -> None:
        root = Path(tempfile.mkdtemp())
        journal = WsGapJournal(root, clock_ms=lambda: 1_000)
        self.assertIsNone(
            journal.note_subscribe_ok(
                exchange="okx", channel="books5", base_coin="ETH"
            )
        )
        self.assertEqual(list(root.rglob("*.jsonl")), [])

    def test_disconnect_then_subscribe_writes_seconds_interval(self) -> None:
        root = Path(tempfile.mkdtemp())
        clock = {"ms": 1_724_371_260_000}

        def now() -> int:
            return clock["ms"]

        journal = WsGapJournal(root, clock_ms=now)
        journal.note_disconnect(
            exchange="bybit",
            channel="orderbook.1",
            base_coin="BTC",
            close_code=1006,
        )
        clock["ms"] += 2_400
        record = journal.note_subscribe_ok(
            exchange="bybit", channel="orderbook.1", base_coin="BTC"
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["t_up_ms"] - record["t_down_ms"], 2400)
        path = gaps_jsonl_path(root, utc_event_date(record["t_down_ms"]))
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        loaded = json.loads(lines[0])
        self.assertEqual(loaded["base_coin"], "BTC")
        self.assertEqual(loaded["close_code"], 1006)

    def test_retry_disconnect_keeps_first_t_down(self) -> None:
        root = Path(tempfile.mkdtemp())
        journal = WsGapJournal(root, clock_ms=lambda: 5_000)
        journal.note_disconnect(
            exchange="okx",
            channel="books5",
            base_coin="SOL",
            close_code=1006,
            t_down_ms=1_000,
        )
        journal.note_disconnect(
            exchange="okx",
            channel="books5",
            base_coin="SOL",
            close_code=1011,
            t_down_ms=4_000,
        )
        record = journal.note_subscribe_ok(
            exchange="okx",
            channel="books5",
            base_coin="SOL",
            t_up_ms=5_000,
        )
        assert record is not None
        self.assertEqual(record["t_down_ms"], 1_000)
        self.assertEqual(record["close_code"], 1011)


if __name__ == "__main__":
    unittest.main()
