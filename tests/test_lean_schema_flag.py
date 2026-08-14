"""Lean schema flag (Option B) and bar normalize for production writer."""

from __future__ import annotations

import importlib
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pyarrow.parquet as pq

from app.schema.lean_event import LEAN_BAR_5M_BODY_COLS, LEAN_TICK_BODY_COLS
from app.schema.spread_event import (
    SPREAD_EVENT_BODY_COLS,
    lean_schema_enabled,
    tick_schema_mode,
)
from app.storage.mount_state import MountFailureState
from app.storage.spool import DurableSpool
from app.storage.writer import (
    ParquetPublisher,
    collect_bars_enabled,
    normalize_records,
)


def lean_tick_record(base_coin: str = "BTC") -> dict[str, object]:
    return {
        "event_local_ts_ms": 1_700_000_000_000,
        "base_coin": base_coin,
        "trigger": "okx",
        "calc_local_ts_ms": 1_700_000_000_100,
        "okx_local_recv_ts_ms": 1_700_000_000_000,
        "okx_ts_ms": 1_699_999_999_990,
        "bybit_local_recv_ts_ms": 1_700_000_000_050,
        "bybit_ts_ms": 1_699_999_999_995,
        "okx_bid_price": 100.0,
        "okx_bid_size": 1.0,
        "okx_ask_price": 101.0,
        "okx_ask_size": 2.0,
        "bybit_bid_price": 102.0,
        "bybit_bid_size": 3.0,
        "bybit_ask_price": 103.0,
        "bybit_ask_size": 4.0,
    }


def v1_tick_record(base_coin: str = "BTC") -> dict[str, object]:
    return {
        "base_coin": base_coin,
        "trigger": "okx",
        "calc_local_ts_ms": 1_700_000_000_100,
        "okx_local_recv_ts_ms": 1_700_000_000_000,
        "bybit_local_recv_ts_ms": 1_700_000_000_050,
        "okx_latency_ms": 10,
        "bybit_latency_ms": 20,
        "spread_long": 1.0,
        "spread_short": -1.0,
        "okx_ts_ms": 1_699_999_999_990,
        "bybit_ts_ms": 1_699_999_999_995,
        "okx_bid_price": 100.0,
        "okx_bid_size": 1.0,
        "okx_ask_price": 101.0,
        "okx_ask_size": 2.0,
        "bybit_bid_price": 102.0,
        "bybit_bid_size": 3.0,
        "bybit_ask_price": 103.0,
        "bybit_ask_size": 4.0,
    }


class LeanFlagTests(unittest.TestCase):
    def test_flag_default_off(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SPREAD_LEAN_SCHEMA", None)
            os.environ.pop("SPREAD_COLLECT_BARS", None)
            self.assertFalse(lean_schema_enabled())
            self.assertEqual(tick_schema_mode(), "v1")
            self.assertFalse(collect_bars_enabled())

    def test_flag_on(self) -> None:
        with mock.patch.dict(os.environ, {"SPREAD_LEAN_SCHEMA": "1"}):
            self.assertTrue(lean_schema_enabled())
            self.assertEqual(tick_schema_mode(), "lean")


class NormalizeModeTests(unittest.TestCase):
    def test_v1_default_keeps_derive_cols(self) -> None:
        with mock.patch.dict(os.environ, {"SPREAD_LEAN_SCHEMA": "0"}):
            batch = normalize_records([v1_tick_record()])
        cols = set(batch.dataframe.columns) - {"event_date"}
        self.assertEqual(cols, set(SPREAD_EVENT_BODY_COLS))
        self.assertIn("spread_long", batch.dataframe.columns)
        self.assertIn("event_dt", batch.dataframe.columns)

    def test_lean_mode_drops_derive_cols_and_uses_int_ms(self) -> None:
        batch = normalize_records([lean_tick_record()], schema_mode="lean")
        cols = set(batch.dataframe.columns) - {"event_date"}
        self.assertEqual(cols, set(LEAN_TICK_BODY_COLS))
        forbidden = {
            "spread_long",
            "spread_short",
            "event_dt",
            "okx_freshness_ms",
            "max_latency_ms",
            "okx_latency_ms",
        }
        self.assertTrue(cols.isdisjoint(forbidden))
        ts = batch.dataframe["event_local_ts_ms"].iloc[0]
        self.assertEqual(int(ts), 1_700_000_000_000)

    def test_env_lean_selects_lean_normalize(self) -> None:
        with mock.patch.dict(os.environ, {"SPREAD_LEAN_SCHEMA": "1"}):
            batch = normalize_records([lean_tick_record()])
        cols = set(batch.dataframe.columns) - {"event_date"}
        self.assertEqual(cols, set(LEAN_TICK_BODY_COLS))

    def test_bar_normalize(self) -> None:
        rec = {
            "bar_start_ts_ms": 1_700_000_000_000,
            "bar_end_ts_ms": 1_700_000_300_000,
            "base_coin": "BTC",
            "ref_exchange": "okx",
            "volume": 12.5,
        }
        batch = normalize_records([rec], schema_mode="bar_5m")
        cols = set(batch.dataframe.columns) - {"event_date"}
        self.assertEqual(cols, set(LEAN_BAR_5M_BODY_COLS))
        self.assertEqual(len(batch.dataframe), 1)


class LeanPublisherWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.mount_state = MountFailureState()
        self.logger = logging.getLogger(f"lean-flag-{id(self)}")
        self.logger.handlers = [logging.NullHandler()]
        self.logger.propagate = False
        self.spool = DurableSpool(
            logger=self.logger,
            mount_failure_state=self.mount_state,
            root=root / "spool",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_lean_publisher_writes_lean_parquet(self) -> None:
        root = Path(self.temp_dir.name) / "live"
        publisher = ParquetPublisher(
            parquet_root=root,
            logger=self.logger,
            failed_batches_logger=self.logger,
            mount_failure_state=self.mount_state,
            spool=self.spool,
            schema_mode="lean",
        )
        publisher.start()
        self.assertTrue(publisher.enqueue_records([lean_tick_record()]))
        publisher.shutdown()
        files = list(root.rglob("*.parquet"))
        self.assertEqual(len(files), 1)
        names = set(pq.ParquetFile(files[0]).schema.names)
        self.assertEqual(names, set(LEAN_TICK_BODY_COLS))

    def test_bars_publisher_writes_bar_parquet(self) -> None:
        root = Path(self.temp_dir.name) / "bars" / "bar_5m"
        publisher = ParquetPublisher(
            parquet_root=root,
            logger=self.logger,
            failed_batches_logger=self.logger,
            mount_failure_state=self.mount_state,
            spool=self.spool,
            schema_mode="bar_5m",
            name="bars-publisher",
        )
        publisher.start()
        ok = publisher.enqueue_records(
            [
                {
                    "bar_start_ts_ms": 1_700_000_000_000,
                    "bar_end_ts_ms": 1_700_000_300_000,
                    "base_coin": "ETH",
                    "ref_exchange": "okx",
                    "volume": 9.0,
                }
            ]
        )
        self.assertTrue(ok)
        publisher.shutdown()
        files = list(root.rglob("*.parquet"))
        self.assertEqual(len(files), 1)
        names = set(pq.ParquetFile(files[0]).schema.names)
        self.assertEqual(names, set(LEAN_BAR_5M_BODY_COLS))


class ProdScreenerLeanRecordTests(unittest.TestCase):
    def test_write_spread_record_respects_flag(self) -> None:
        root = Path(tempfile.mkdtemp())
        env = {
            "SPREAD_PARQUET_ROOT": str(root / "live"),
            "SPREAD_RUNTIME_LOG": str(root / "runtime.log"),
            "SPREAD_FAILED_BATCHES_LOG": str(root / "failed.log"),
            "SPREAD_SPOOL_ROOT": str(root / "spool"),
            "SPREAD_LEAN_SCHEMA": "1",
        }
        with mock.patch.dict(os.environ, env):
            sys.modules.pop("app.screaner_b_o", None)
            # Also clear non-package alias if present.
            sys.modules.pop("screaner_b_o", None)
            runtime = importlib.import_module("app.screaner_b_o")
            try:
                runtime.opportunities_buffer.clear()
                runtime.write_spread_record(
                    base_coin="BTC",
                    trigger_exchange="okx",
                    spread_long=1.0,
                    spread_short=-1.0,
                    okx_latency_ms=10,
                    bybit_latency_ms=20,
                    calc_local_ts_ms=1_700_000_000_100,
                    okx_local_recv_ts_ms=1_700_000_000_000,
                    okx_ts_ms=1_699_999_999_990,
                    bybit_local_recv_ts_ms=1_700_000_000_050,
                    bybit_ts_ms=1_699_999_999_995,
                    okx_bid_price=100.0,
                    okx_bid_size=1.0,
                    okx_ask_price=101.0,
                    okx_ask_size=2.0,
                    bybit_bid_price=102.0,
                    bybit_bid_size=3.0,
                    bybit_ask_price=103.0,
                    bybit_ask_size=4.0,
                )
                self.assertEqual(len(runtime.opportunities_buffer), 1)
                rec = runtime.opportunities_buffer[0]
                self.assertEqual(set(rec.keys()), set(LEAN_TICK_BODY_COLS))
                self.assertNotIn("spread_long", rec)
                self.assertIsInstance(rec["event_local_ts_ms"], int)
            finally:
                runtime.opportunities_buffer.clear()
                for logger in (runtime.runtime_logger, runtime.failed_batches_logger):
                    for handler in list(logger.handlers):
                        logger.removeHandler(handler)
                        handler.close()
                sys.modules.pop("app.screaner_b_o", None)


if __name__ == "__main__":
    unittest.main()
