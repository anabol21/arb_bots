"""Fail-closed tick gates: generation + skew/age."""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.utils.tick_validity import TickValidityGate, book_l1_complete, skew_age_thresholds
from validation.check_tick_coverage import analyze_logs, parse_since


def _book(ts: float, recv: float) -> dict[str, object]:
    return {
        "bid_price": 100.0,
        "ask_price": 101.0,
        "ts_exchange": ts,
        "local_recv_ts_ms": recv,
    }


class ThresholdTests(unittest.TestCase):
    def test_defaults(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SPREAD_TICK_SKEW_MAX_MS", None)
            os.environ.pop("SPREAD_TICK_AGE_MAX_MS", None)
            self.assertEqual(skew_age_thresholds(), (2000, 2000))


class GenerationTests(unittest.TestCase):
    def test_reconnect_one_leg_does_not_write_until_other_updates(self) -> None:
        gate = TickValidityGate(skew_max_ms=2000, age_max_ms=2000)
        gate.note_subscribe_ok("BTC", "books5")
        gate.note_subscribe_ok("BTC", "orderbook.1")
        gate.note_book_update("BTC", "okx", complete_l1=True)
        gate.note_book_update("BTC", "bybit", complete_l1=True)
        okx = _book(1_000_000.0, 1_000_010.0)
        bybit = _book(1_000_005.0, 1_000_012.0)
        self.assertIsNone(gate.evaluate("BTC", okx, bybit, 1_000_020.0))

        gate.note_disconnect("BTC", "bybit")
        gate.note_subscribe_ok("BTC", "orderbook.1")
        gate.note_book_update("BTC", "bybit", complete_l1=True)
        # OKX still on previous generation.
        self.assertEqual(gate.evaluate("BTC", okx, bybit, 1_000_020.0), "generation")
        self.assertEqual(gate.counters["ticks_suppressed_generation"], 1)

        gate.note_book_update("BTC", "okx", complete_l1=True)
        self.assertIsNone(gate.evaluate("BTC", okx, bybit, 1_000_020.0))

    def test_candle_subscribe_does_not_bump_generation(self) -> None:
        gate = TickValidityGate()
        gate.note_subscribe_ok("ETH", "books5")
        self.assertEqual(gate.coin_generation["ETH"], 1)
        gate.note_subscribe_ok("ETH", "candle5m")
        self.assertEqual(gate.coin_generation["ETH"], 1)

    def test_partial_l1_does_not_mark_fresh(self) -> None:
        gate = TickValidityGate()
        gate.note_subscribe_ok("SOL", "books5")
        gate.note_book_update("SOL", "okx", complete_l1=False)
        self.assertNotIn(("SOL", "okx"), gate.leg_generation)


class StaleTests(unittest.TestCase):
    def test_skew_above_2s_suppressed(self) -> None:
        gate = TickValidityGate(skew_max_ms=2000, age_max_ms=10_000)
        gate.note_book_update("BTC", "okx", complete_l1=True)
        gate.note_book_update("BTC", "bybit", complete_l1=True)
        okx = _book(1_000_000.0, 1_002_000.0)
        bybit = _book(1_003_000.0, 1_003_010.0)
        self.assertEqual(gate.evaluate("BTC", okx, bybit, 1_003_020.0), "skew")
        self.assertEqual(gate.counters["ticks_suppressed_stale"], 1)

    def test_age_above_2s_suppressed(self) -> None:
        gate = TickValidityGate(skew_max_ms=10_000, age_max_ms=2000)
        gate.note_book_update("BTC", "okx", complete_l1=True)
        gate.note_book_update("BTC", "bybit", complete_l1=True)
        okx = _book(1_000_000.0, 1_000_000.0)
        bybit = _book(1_000_100.0, 1_000_100.0)
        self.assertEqual(gate.evaluate("BTC", okx, bybit, 1_003_000.0), "age")

    def test_book_l1_complete(self) -> None:
        self.assertFalse(book_l1_complete({"bid_price": 1.0}))
        self.assertTrue(book_l1_complete(_book(1.0, 2.0)))


class CalcStoreGateTests(unittest.TestCase):
    def test_reconnect_bybit_does_not_write_until_okx_fresh(self) -> None:
        root = Path(tempfile.mkdtemp())
        env = {
            "SPREAD_PARQUET_ROOT": str(root / "live"),
            "SPREAD_RUNTIME_LOG": str(root / "runtime.log"),
            "SPREAD_FAILED_BATCHES_LOG": str(root / "failed.log"),
            "SPREAD_SPOOL_ROOT": str(root / "spool"),
            "SPREAD_LEAN_SCHEMA": "1",
            "SPREAD_TICK_SKEW_MAX_MS": "2000",
            "SPREAD_TICK_AGE_MAX_MS": "2000",
        }
        with mock.patch.dict(os.environ, env):
            sys.modules.pop("app.screaner_b_o", None)
            sys.modules.pop("screaner_b_o", None)
            runtime = importlib.import_module("app.screaner_b_o")
            try:
                runtime.opportunities_buffer.clear()
                runtime.tick_validity.coin_generation.clear()
                runtime.tick_validity.leg_generation.clear()
                runtime.tick_validity.counters.update(
                    {
                        "ticks_suppressed_stale": 0,
                        "ticks_suppressed_generation": 0,
                        "ticks_accepted": 0,
                    }
                )
                coin = next(iter(runtime.quotes))
                now = time.time() * 1000
                runtime.quotes[coin]["okx"].update(_book(now, now + 10))
                runtime.quotes[coin]["bybit"].update(_book(now + 5, now + 12))
                runtime.tick_validity.note_subscribe_ok(coin, "books5")
                runtime.tick_validity.note_subscribe_ok(coin, "orderbook.1")
                runtime.tick_validity.note_book_update(coin, "okx", complete_l1=True)
                runtime.tick_validity.note_book_update(coin, "bybit", complete_l1=True)
                with mock.patch.object(runtime.time, "time", return_value=now / 1000 + 0.02):
                    runtime.calc_and_store_spread(coin, "bybit")
                self.assertEqual(len(runtime.opportunities_buffer), 1)

                runtime.tick_validity.note_disconnect(coin, "bybit")
                runtime.tick_validity.note_subscribe_ok(coin, "orderbook.1")
                runtime.tick_validity.note_book_update(coin, "bybit", complete_l1=True)
                with mock.patch.object(runtime.time, "time", return_value=now / 1000 + 0.03):
                    runtime.calc_and_store_spread(coin, "bybit")
                self.assertEqual(len(runtime.opportunities_buffer), 1)
                self.assertGreater(
                    runtime.tick_validity.counters["ticks_suppressed_generation"], 0
                )

                runtime.tick_validity.note_book_update(coin, "okx", complete_l1=True)
                with mock.patch.object(runtime.time, "time", return_value=now / 1000 + 0.04):
                    runtime.calc_and_store_spread(coin, "okx")
                self.assertEqual(len(runtime.opportunities_buffer), 2)
            finally:
                runtime.opportunities_buffer.clear()
                for logger in (runtime.runtime_logger, runtime.failed_batches_logger):
                    for handler in list(logger.handlers):
                        logger.removeHandler(handler)


class CoverageScriptTests(unittest.TestCase):
    def test_disconnect_marks_incomplete_window(self) -> None:
        path = Path(tempfile.mkdtemp()) / "runtime.log"
        path.write_text(
            "26-08-15 15:01:00 - WARNING - ws_disconnect | exchange=bybit | "
            "channel=orderbook.1 | coin=BTC | close_code=1006 | reason_class=abrupt\n"
            "26-08-15 15:01:30 - INFO - heartbeat | ticks_suppressed_stale=3 | "
            "ticks_suppressed_generation=8 | ticks_accepted=10\n",
            encoding="utf-8",
        )
        report = analyze_logs([path], parse_since("2026-08-15T15:00:00Z"))
        self.assertEqual(report["disconnects"], 1)
        self.assertEqual(report["incomplete_from_log"], 1)
        self.assertEqual(report["incomplete_windows"][0]["base_coin"], "BTC")


if __name__ == "__main__":
    unittest.main()
