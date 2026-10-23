"""Local checks for the HL v2 sharded canary contour.

Does not prove VPS or remote durability. Does not touch /data/live.
"""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pyarrow.parquet as pq

from app.hl_v2.l2book import (
    channels_from_env,
    l2book_subscribe_payload,
    parse_l2book_message,
)
from app.hl_v2.paths import HlV2PathError, assert_hl_v2_storage_path, resolve_parquet_root
from app.hl_v2.sharding import shard_count_from_env, split_round_robin
from app.hl_v2.staff import QuoteBook, build_staff_record, quote_state_for_pair
from app.hl_v2.universe import perp_names_from_meta, select_pairs
from app.schema.hl_v2_event import HL_V2_BODY_COLS, hl_v2_body_is_exact
from app.storage.mount_state import MountFailureState
from app.storage.spool import DurableSpool
from app.storage.writer import ParquetPublisher, normalize_hl_v2_records


REPO = Path(__file__).resolve().parents[1]
UNIT = REPO / "deploy" / "systemd" / "spread-collector-hl-v2.service"


def _complete_state() -> dict:
    state = quote_state_for_pair("BTC-USDT-SWAP", "BTCUSDT")
    for exchange, ts in (("okx", 1000), ("bybit", 1001), ("hl", 1002)):
        leg = state[exchange]
        leg["bid_price"] = 100.0 + (0.1 if exchange == "okx" else 0.0)
        leg["bid_size"] = 1.0
        leg["ask_price"] = 101.0
        leg["ask_size"] = 2.0
        leg["ts_exchange"] = float(ts)
        leg["local_recv_ts_ms"] = float(ts + 5)
    return state


class ShardingTests(unittest.TestCase):
    def test_default_socket_count_is_one(self) -> None:
        self.assertEqual(shard_count_from_env(None), 1)
        self.assertEqual(shard_count_from_env(""), 1)

    def test_round_robin_split(self) -> None:
        shards = split_round_robin(["A", "B", "C", "D", "E"], 2)
        self.assertEqual(shards[0], ("A", "C", "E"))
        self.assertEqual(shards[1], ("B", "D"))

    def test_one_shard_keeps_order(self) -> None:
        shards = split_round_robin(["A", "B", "C"], 1)
        self.assertEqual(shards, [("A", "B", "C")])

    def test_socket_count_bounds(self) -> None:
        with self.assertRaises(ValueError):
            shard_count_from_env("0")
        with self.assertRaises(ValueError):
            shard_count_from_env("10")


class L2BookParseTests(unittest.TestCase):
    def test_top_of_book_from_levels(self) -> None:
        message = {
            "channel": "l2Book",
            "data": {
                "coin": "BTC",
                "time": 1_700_000_000_010,
                "levels": [
                    [{"px": "84437.0", "sz": "7.4", "n": 3}],
                    [{"px": "84438.0", "sz": "1.3", "n": 2}],
                ],
            },
        }
        parsed = parse_l2book_message(message)
        self.assertEqual(parsed.kind, "l2Book")
        self.assertEqual(parsed.coin, "BTC")
        self.assertEqual(parsed.ts_ms, 1_700_000_000_010)
        self.assertEqual(parsed.bid_price, 84437.0)
        self.assertEqual(parsed.ask_size, 1.3)

    def test_subscribe_payload_is_l2book_only(self) -> None:
        self.assertEqual(
            l2book_subscribe_payload("ETH"),
            {"method": "subscribe", "subscription": {"type": "l2Book", "coin": "ETH"}},
        )

    def test_channels_refuse_trades(self) -> None:
        self.assertEqual(channels_from_env(None), ("l2Book",))
        with self.assertRaises(ValueError):
            channels_from_env("l2Book,trades")


class PathGuardTests(unittest.TestCase):
    def test_refuses_data_live(self) -> None:
        with self.assertRaises(HlV2PathError):
            assert_hl_v2_storage_path(Path("/data/live"), role="parquet root", source="test")

    def test_allows_live_hl_v2(self) -> None:
        path = assert_hl_v2_storage_path(
            Path("/data/live_hl_v2"),
            role="parquet root",
            source="test",
        )
        self.assertEqual(path, Path("/data/live_hl_v2"))

    def test_env_spread_parquet_root_guarded(self) -> None:
        with mock.patch.dict("os.environ", {"SPREAD_PARQUET_ROOT": "/data/live"}, clear=False):
            with self.assertRaises(HlV2PathError):
                resolve_parquet_root()


class UniverseTests(unittest.TestCase):
    def test_exact_intersect_no_alias(self) -> None:
        pairs = [
            {"base_coin": "BTC", "okx_symbol": "BTC-USDT-SWAP", "bybit_symbol": "BTCUSDT"},
            {"base_coin": "1000PEPE", "okx_symbol": "X", "bybit_symbol": "Y"},
        ]
        selection = select_pairs(pairs, ["BTC", "kPEPE"])
        self.assertEqual([p.base_coin for p in selection.matched], ["BTC"])
        self.assertEqual(selection.unmatched_coins, ("1000PEPE",))

    def test_meta_names(self) -> None:
        names = perp_names_from_meta({"universe": [{"name": "SOL"}, {"name": "SOL"}]})
        self.assertEqual(names, ["SOL"])


class StaffRecordTests(unittest.TestCase):
    def test_requires_all_three_legs(self) -> None:
        state = quote_state_for_pair("BTC-USDT-SWAP", "BTCUSDT")
        self.assertIsNone(
            build_staff_record(base_coin="BTC", trigger="bybit", state=state)
        )
        state = _complete_state()
        record = build_staff_record(
            base_coin="BTC",
            trigger="hl",
            state=state,
            calc_local_ts_ms=2000.0,
        )
        assert record is not None
        self.assertEqual(record["trigger"], "hl")
        self.assertEqual(record["base_coin"], "BTC")
        self.assertNotIn("spread_long", record)
        self.assertEqual(set(record), set(HL_V2_BODY_COLS))

    def test_quote_book_emits_on_third_leg(self) -> None:
        offered: list[dict] = []
        book = QuoteBook(offered.append)
        book.register("BTC", "BTC-USDT-SWAP", "BTCUSDT")
        book.update_cex(
            base_coin="BTC",
            exchange="okx",
            bid_price=1.0,
            bid_size=1.0,
            ask_price=2.0,
            ask_size=1.0,
            ts_exchange=10.0,
            local_recv_ts_ms=11.0,
        )
        self.assertEqual(offered, [])
        book.update_cex(
            base_coin="BTC",
            exchange="bybit",
            bid_price=1.1,
            bid_size=1.0,
            ask_price=2.1,
            ask_size=1.0,
            ts_exchange=12.0,
            local_recv_ts_ms=13.0,
        )
        self.assertEqual(offered, [])
        book.update_hl(
            base_coin="BTC",
            bid_price=1.05,
            bid_size=1.0,
            ask_price=2.05,
            ask_size=1.0,
            ts_exchange=14.0,
            local_recv_ts_ms=15.0,
        )
        self.assertEqual(len(offered), 1)
        self.assertEqual(offered[0]["trigger"], "hl")
        self.assertEqual(offered[0]["hl_ts_ms"], 14)


class WriterSchemaTests(unittest.TestCase):
    def test_normalize_drops_spreads_and_keeps_hl(self) -> None:
        record = build_staff_record(
            base_coin="BTC",
            trigger="okx",
            state=_complete_state(),
            calc_local_ts_ms=2000.0,
        )
        assert record is not None
        dirty = dict(record)
        dirty["spread_long"] = 9.9
        batch = normalize_hl_v2_records([dirty])
        self.assertEqual(len(batch.dataframe), 1)
        cols = [c for c in batch.dataframe.columns if c != "event_date"]
        self.assertTrue(hl_v2_body_is_exact(cols))
        self.assertNotIn("spread_long", batch.dataframe.columns)

    def test_publish_roundtrip(self) -> None:
        record = build_staff_record(
            base_coin="BTC",
            trigger="bybit",
            state=_complete_state(),
            calc_local_ts_ms=2000.0,
        )
        assert record is not None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "live_hl_v2"
            spool_root = Path(tmp) / "spool_hl_v2"
            root.mkdir()
            spool_root.mkdir()
            logger = logging.getLogger("test-hl-v2")
            logger.handlers.clear()
            logger.addHandler(logging.NullHandler())
            mount = MountFailureState()
            spool = DurableSpool(logger=logger, mount_failure_state=mount, root=spool_root)
            publisher = ParquetPublisher(
                parquet_root=root,
                logger=logger,
                failed_batches_logger=logger,
                mount_failure_state=mount,
                spool=spool,
                schema_mode="hl_v2",
                name="test-hl-v2",
            )
            publisher.start()
            self.assertTrue(publisher.enqueue_records([record]))
            publisher.shutdown()
            files = list(root.rglob("*.parquet"))
            self.assertEqual(len(files), 1)
            table = pq.read_table(files[0])
            self.assertTrue(hl_v2_body_is_exact(table.column_names))


class UnitTemplateTests(unittest.TestCase):
    def test_unit_isolates_data_live(self) -> None:
        text = UNIT.read_text(encoding="utf-8")
        self.assertIn("InaccessiblePaths=/data/live", text)
        self.assertIn("HL_SOCKET_COUNT=1", text)
        self.assertIn("HL_CHANNELS=l2Book", text)
        self.assertIn("/data/live_hl_v2", text)
        self.assertIn("python -m app.hl_v2", text)
        self.assertNotIn("WantedBy=", text)
        self.assertNotIn("SPREAD_PARQUET_ROOT=/data/live\n", text)


if __name__ == "__main__":
    unittest.main()
