"""Local checks for the Hyperliquid L1 contour.

Selection is exact name match. One published parquet is read back from a
temp directory. This does not prove VPS or remote durability.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pyarrow.parquet as pq

from app.hl.bbo import bbo_subscribe_payload, parse_bbo_message
from app.hl.buffer import HlRecordBuffer
from app.hl.paths import HlPathError, assert_hl_storage_path
from app.hl.universe import (
    HL_INFO_URL,
    fetch_perp_meta,
    perp_names_from_meta,
    select_exact_coins,
)
from app.schema.hl_l1 import HL_L1_BODY_COLS, hl_l1_body_is_exact
from app.storage.mount_state import MountFailureState
from app.storage.spool import DurableSpool
from app.storage.writer import ParquetPublisher, normalize_hl_l1_records, resolve_tick_schema_mode
from app.utils.universe_csv import load_take_yes_pairs

REPO = Path(__file__).resolve().parents[1]
UNIVERSE = REPO / "bybit_okx_universe.csv"


def _hl_record(base_coin: str = "BTC") -> dict[str, object]:
    return {
        "base_coin": base_coin,
        "event_local_ts_ms": 1_700_000_000_000,
        "hl_local_recv_ts_ms": 1_700_000_000_000,
        "hl_ts_ms": 1_700_000_000_010,
        "hl_bid_price": "100.5",
        "hl_bid_size": "1.25",
        "hl_ask_price": "100.6",
        "hl_ask_size": "2.5",
        "spread_long": 9.9,
        "okx_bid_price": 1.0,
    }


class HlUniverseTests(unittest.TestCase):
    def test_exact_match_does_not_alias_kpepe(self) -> None:
        selection = select_exact_coins(
            ["BTC", "1000PEPE", "SOL", "BTC"],
            ["BTC", "kPEPE", "SOL", "ETH"],
        )
        self.assertEqual(selection.matched, ("BTC", "SOL"))
        self.assertEqual(selection.unmatched, ("1000PEPE",))
        self.assertNotIn("kPEPE", selection.matched)
        self.assertNotIn("PEPE", selection.matched)

    def test_case_and_whitespace_are_exact(self) -> None:
        selection = select_exact_coins([" btc ", "BTC"], ["BTC"])
        self.assertEqual(selection.matched, ("BTC",))
        self.assertEqual(selection.unmatched, ("btc",))

    def test_take_yes_csv_intersects_fixture_meta(self) -> None:
        pairs = load_take_yes_pairs(UNIVERSE)
        coins = [row["base_coin"] for row in pairs]
        self.assertGreater(len(coins), 100)
        first, second = coins[0], coins[1]
        self.assertNotIn("kPEPE", coins)
        selection = select_exact_coins(coins, [first, "kPEPE"])
        self.assertIn(first, selection.matched)
        self.assertNotIn(second, selection.matched)
        self.assertIn(second, selection.unmatched)
        self.assertNotIn("kPEPE", selection.matched)
        self.assertNotIn("kPEPE", selection.unmatched)

    def test_meta_names_keep_exact_text(self) -> None:
        names = perp_names_from_meta(
            {
                "universe": [
                    {"name": "BTC"},
                    {"name": " kPEPE "},
                    {"name": "BTC"},
                    {"name": ""},
                    "skip",
                    {"szDecimals": 2},
                ]
            }
        )
        self.assertEqual(names, ["BTC", "kPEPE"])

    def test_fetch_posts_perp_meta_not_spot(self) -> None:
        class _Response:
            def __enter__(self) -> "_Response":
                return self

            def __exit__(self, *_exc: object) -> bool:
                return False

            def read(self) -> bytes:
                return json.dumps({"universe": [{"name": "SOL"}]}).encode()

        with mock.patch("app.hl.universe.urllib.request.urlopen", return_value=_Response()) as urlopen:
            payload = fetch_perp_meta()
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, HL_INFO_URL)
        self.assertEqual(json.loads(request.data.decode()), {"type": "meta"})
        self.assertEqual(perp_names_from_meta(payload), ["SOL"])


class HlBboTests(unittest.TestCase):
    def test_live_frame_shape_parses(self) -> None:
        message = (
            '{"channel":"bbo","data":{"coin":"BTC","time":1790286400697,'
            '"bbo":[{"px":"84437.0","sz":"7.41506","n":42},'
            '{"px":"84438.0","sz":"1.31971","n":9}]}}'
        )
        parsed = parse_bbo_message(message, recv_ts_ms=1_700_000_000_000)
        self.assertEqual(parsed.kind, "bbo")
        assert parsed.record is not None
        self.assertEqual(parsed.record["base_coin"], "BTC")
        self.assertEqual(parsed.record["hl_ts_ms"], 1790286400697)
        self.assertEqual(parsed.record["event_local_ts_ms"], 1_700_000_000_000)
        self.assertEqual(parsed.record["hl_local_recv_ts_ms"], 1_700_000_000_000)
        self.assertEqual(parsed.record["hl_bid_price"], "84437.0")
        self.assertEqual(parsed.record["hl_ask_size"], "1.31971")
        self.assertNotIn("spread_long", parsed.record)

    def test_null_side_and_control_frames_are_not_records(self) -> None:
        incomplete = parse_bbo_message(
            {"channel": "bbo", "data": {"coin": "BTC", "time": 10, "bbo": [None, {"px": "1", "sz": "1"}]}},
            recv_ts_ms=10,
        )
        self.assertEqual(incomplete.kind, "incomplete")
        self.assertIsNone(incomplete.record)
        ignored = parse_bbo_message(
            {"channel": "subscriptionResponse", "data": {"method": "subscribe"}},
            recv_ts_ms=10,
        )
        self.assertEqual(ignored.kind, "ignore")

    def test_subscribe_keeps_exact_coin(self) -> None:
        self.assertEqual(
            bbo_subscribe_payload("kPEPE"),
            {"method": "subscribe", "subscription": {"type": "bbo", "coin": "kPEPE"}},
        )


class HlPathTests(unittest.TestCase):
    def test_live_and_spool_roots_are_refused(self) -> None:
        for raw in ("/data/live", "/data/spool", "/data/spool-next", "/data/live/base_coin=BTC"):
            with self.assertRaises(HlPathError):
                assert_hl_storage_path(Path(raw), role="parquet root", source="test")

    def test_hl_roots_are_allowed(self) -> None:
        self.assertEqual(
            assert_hl_storage_path(Path("/data/live-hl"), role="parquet root", source="test"),
            Path("/data/live-hl"),
        )
        self.assertEqual(
            assert_hl_storage_path(Path("/data/spool-hl"), role="spool root", source="test"),
            Path("/data/spool-hl"),
        )


class HlPublishTests(unittest.TestCase):
    def test_lean_flag_does_not_select_hl_schema(self) -> None:
        with mock.patch.dict(os.environ, {"SPREAD_LEAN_SCHEMA": "1"}, clear=False):
            self.assertEqual(resolve_tick_schema_mode(), "lean")

    def test_incomplete_book_is_rejected(self) -> None:
        record = _hl_record()
        record["hl_ask_price"] = None
        batch = normalize_hl_l1_records([record])
        self.assertTrue(batch.dataframe.empty)
        self.assertEqual(len(batch.rejected), 1)
        self.assertIn("invalid_hl_ask_price", batch.rejected[0]["reasons"])

    def test_one_published_parquet_reads_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logger = logging.getLogger("hl-l1-test")
            logger.handlers = [logging.NullHandler()]
            logger.propagate = False
            mount_state = MountFailureState()
            spool = DurableSpool(
                logger=logger,
                mount_failure_state=mount_state,
                root=root / "spool-hl",
            )
            publisher = ParquetPublisher(
                parquet_root=root / "live-hl",
                logger=logger,
                failed_batches_logger=logger,
                mount_failure_state=mount_state,
                spool=spool,
                schema_mode="hl_l1",
                name="hl-l1-publisher",
            )
            publisher.start()
            self.assertTrue(publisher.enqueue_records([_hl_record()]))
            publisher.shutdown()

            finals = list((root / "live-hl").rglob("*.parquet"))
            self.assertEqual(len(finals), 1)
            final = finals[0]
            self.assertIn("base_coin=BTC", str(final))
            self.assertIn("event_date=2023-11-14", str(final))
            self.assertNotIn("/data/live/", str(final))
            table = pq.read_table(final)
            self.assertTrue(hl_l1_body_is_exact(table.column_names))
            self.assertEqual(list(table.column_names), list(HL_L1_BODY_COLS))
            self.assertEqual(table.column("base_coin").to_pylist(), ["BTC"])
            self.assertEqual(table.column("hl_bid_price").to_pylist(), [100.5])
            self.assertEqual(table.column("hl_ask_size").to_pylist(), [2.5])
            self.assertEqual(table.column("hl_ts_ms").to_pylist(), [1_700_000_000_010])
            self.assertEqual(pq.ParquetFile(final).metadata.num_rows, 1)
            snap = publisher.metrics_snapshot()
            self.assertEqual(snap["published_files_total"], 1)
            self.assertEqual(snap["published_rows_total"], 1)
            self.assertEqual(snap["failed_jobs_total"], 0)


class _FakePublisher:
    def __init__(self, *, accept: bool) -> None:
        self.accept = accept
        self.enqueued: list[list[dict[str, object]]] = []
        self.spooled: list[list[dict[str, object]]] = []

    def enqueue_records(self, records: list[dict[str, object]]) -> bool:
        if not self.accept:
            return False
        self.enqueued.append(list(records))
        return True

    def durably_spool_records(self, records: list[dict[str, object]], *, reason: str) -> bool:
        del reason
        self.spooled.append(list(records))
        return True


class HlBufferTests(unittest.TestCase):
    def test_offer_does_not_enqueue_until_flush(self) -> None:
        publisher = _FakePublisher(accept=True)
        logger = logging.getLogger("hl-buffer-test")
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        buffer = HlRecordBuffer(publisher, logger, flush_sec=60.0)
        buffer.offer(_hl_record())
        self.assertEqual(publisher.enqueued, [])
        self.assertTrue(buffer.flush_once())
        self.assertEqual(len(publisher.enqueued), 1)
        self.assertEqual(publisher.enqueued[0][0]["base_coin"], "BTC")

    def test_rejected_enqueue_does_not_drop_the_record(self) -> None:
        publisher = _FakePublisher(accept=False)
        logger = logging.getLogger("hl-buffer-reject")
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        buffer = HlRecordBuffer(publisher, logger, flush_sec=60.0, max_pending=10)
        buffer.offer(_hl_record())
        self.assertFalse(buffer.flush_once())
        self.assertEqual(buffer.pending_count(), 1)
        self.assertEqual(buffer.dropped_total, 0)
        publisher.accept = True
        self.assertEqual(buffer.drain(), "enqueued")
        self.assertEqual(len(publisher.enqueued), 1)


class HlRuntimeImportTests(unittest.TestCase):
    def test_signal_wait_can_build_a_threading_event(self) -> None:
        import app.hl.runtime as runtime

        self.assertIsInstance(runtime.threading.Event(), runtime.threading.Event)


if __name__ == "__main__":
    unittest.main()
