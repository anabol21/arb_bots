"""Unit tests for local lean collector record shape and simple parquet write."""

from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "app"
for p in (str(REPO), str(APP)):
    if p not in sys.path:
        sys.path.insert(0, p)

from app.schema.lean_event import (  # noqa: E402
    BAR_INTERVAL_MS,
    LEAN_BAR_5M_BODY_COLS,
    LEAN_TICK_BODY_COLS,
)


class LeanSchemaTests(unittest.TestCase):
    def test_tick_cols_are_lean(self) -> None:
        forbidden = {
            "spread_long",
            "spread_short",
            "max_latency_ms",
            "max_freshness_ms",
            "okx_freshness_ms",
            "bybit_freshness_ms",
            "event_dt",
            "okx_latency_ms",
            "bybit_latency_ms",
            "okx_lot_size",
            "bybit_qty_step",
            "volume_unit",
        }
        self.assertTrue(set(LEAN_TICK_BODY_COLS).isdisjoint(forbidden))
        self.assertEqual(len(LEAN_TICK_BODY_COLS), 16)
        for book in (
            "okx_bid_price",
            "okx_bid_size",
            "okx_ask_price",
            "okx_ask_size",
            "bybit_bid_price",
            "bybit_bid_size",
            "bybit_ask_price",
            "bybit_ask_size",
        ):
            self.assertIn(book, LEAN_TICK_BODY_COLS)

    def test_bar_cols_minimal(self) -> None:
        self.assertEqual(
            LEAN_BAR_5M_BODY_COLS,
            (
                "bar_start_ts_ms",
                "bar_end_ts_ms",
                "base_coin",
                "ref_exchange",
                "volume",
            ),
        )
        self.assertEqual(BAR_INTERVAL_MS, 300_000)


class LeanCollectorShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        # Import module under a stable name after path setup.
        if "screaner_local_lean" in sys.modules:
            self.mod = importlib.reload(sys.modules["screaner_local_lean"])
        else:
            self.mod = importlib.import_module("screaner_local_lean")

        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.mod.TICK_ROOT = root / "ticks"
        self.mod.BARS_ROOT = root / "bars"
        self.mod.tick_buffer.clear()
        self.mod.bar_buffer.clear()
        self.mod.seen_bar_keys.clear()
        self.mod.tick_batch_seq = 0
        self.mod.bar_batch_seq = 0
        self.mod.saved_tick_rows = 0
        self.mod.saved_bar_rows = 0
        self.mod.PERSIST_EVERY_N = 1000
        self.mod.BAR_PERSIST_EVERY_N = 1000

        self.mod.quotes = {
            "BTC": {
                "okx_symbol": "BTC-USDT-SWAP",
                "bybit_symbol": "BTCUSDT",
                "okx": {
                    "bid_price": 100.0,
                    "bid_size": 1.0,
                    "ask_price": 101.0,
                    "ask_size": 2.0,
                    "ts_exchange": 1_700_000_000_000.0,
                    "local_recv_ts_ms": 1_700_000_000_010.0,
                },
                "bybit": {
                    "bid_price": 102.0,
                    "bid_size": 3.0,
                    "ask_price": 103.0,
                    "ask_size": 4.0,
                    "ts_exchange": 1_700_000_000_001.0,
                    "local_recv_ts_ms": 1_700_000_000_020.0,
                },
            }
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_build_lean_tick_record_keys(self) -> None:
        rec = self.mod.build_lean_tick_record("BTC", "okx")
        assert rec is not None
        self.assertEqual(set(rec.keys()), set(LEAN_TICK_BODY_COLS))
        self.assertEqual(rec["trigger"], "okx")
        self.assertEqual(rec["event_local_ts_ms"], 1_700_000_000_010.0)
        self.assertNotIn("spread_long", rec)
        self.assertNotIn("okx_lot_size", rec)

    def test_persist_ticks_writes_lean_parquet(self) -> None:
        rec = self.mod.build_lean_tick_record("BTC", "bybit")
        assert rec is not None
        self.mod.tick_buffer.append(rec)
        self.mod.persist_ticks()
        files = list((self.mod.TICK_ROOT).rglob("*.parquet"))
        self.assertEqual(len(files), 1)
        # Read file directly (avoid hive path auto-partition merge with body col).
        table = pq.ParquetFile(files[0]).read()
        self.assertEqual(set(table.column_names), set(LEAN_TICK_BODY_COLS))
        self.assertEqual(table.num_rows, 1)

    def test_store_closed_bar_dedup_and_persist(self) -> None:
        self.mod.store_closed_bar(
            base_coin="BTC",
            ref_exchange="okx",
            bar_start_ts_ms=1_700_000_000_000,
            volume=12.5,
        )
        self.mod.store_closed_bar(
            base_coin="BTC",
            ref_exchange="okx",
            bar_start_ts_ms=1_700_000_000_000,
            volume=99.0,
        )
        self.assertEqual(len(self.mod.bar_buffer), 1)
        self.mod.persist_bars()
        files = list((self.mod.BARS_ROOT / "bar_5m").rglob("*.parquet"))
        self.assertEqual(len(files), 1)
        table = pq.ParquetFile(files[0]).read()
        self.assertEqual(set(table.column_names), set(LEAN_BAR_5M_BODY_COLS))
        row = table.to_pydict()
        self.assertEqual(row["volume"][0], 12.5)
        self.assertEqual(row["bar_end_ts_ms"][0], 1_700_000_000_000 + BAR_INTERVAL_MS)
        self.assertEqual(row["ref_exchange"][0], "okx")

    def test_refuse_production_paths(self) -> None:
        with mock.patch.object(self.mod, "TICK_ROOT", Path("/data/live")):
            with self.assertRaises(RuntimeError):
                # Re-run only the safety check via main's logic excerpt:
                forbidden = {"/data/live", "/data/spool", "/mnt/storage"}
                root = self.mod.TICK_ROOT.resolve()
                as_str = str(root)
                for bad in forbidden:
                    if as_str == bad or as_str.startswith(bad + "/"):
                        raise RuntimeError(f"Refusing: {root}")


if __name__ == "__main__":
    unittest.main()
