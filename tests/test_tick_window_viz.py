"""Hermetic tests for the 5-minute tick-window helper (no VPS, no backup)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from research.tick_window_viz.delay import message_delay_ms
from research.tick_window_viz.fetch import derive_spreads
from research.tick_window_viz.plot import build_tick_window_figure
from research.tick_window_viz.window import (
    FIVE_MIN_MS,
    compacted_filename,
    compacted_names_covering,
    is_five_min_aligned,
    normalize_coin,
    parse_window_start,
    window_bounds_ms,
)


class WindowHelpersTest(unittest.TestCase):
    def test_normalize_coin(self) -> None:
        self.assertEqual(normalize_coin(" btc "), "BTC")
        with self.assertRaises(ValueError):
            normalize_coin("  ")

    def test_parse_naive_is_utc(self) -> None:
        dt = parse_window_start("2026-09-01T00:00:00")
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertEqual(dt, datetime(2026, 9, 1, tzinfo=timezone.utc))

    def test_parse_zulu(self) -> None:
        dt = parse_window_start("2026-09-01T00:05:00Z")
        self.assertEqual(dt, datetime(2026, 9, 1, 0, 5, tzinfo=timezone.utc))

    def test_window_bounds_exclusive_end(self) -> None:
        start_ms, end_ms = window_bounds_ms("2026-09-01T00:00:00Z", minutes=5)
        self.assertEqual(end_ms - start_ms, FIVE_MIN_MS)
        self.assertTrue(is_five_min_aligned("2026-09-01T00:00:00Z"))
        self.assertFalse(is_five_min_aligned("2026-09-01T00:02:00Z"))

    def test_compacted_filename_and_cover(self) -> None:
        start_ms, end_ms = window_bounds_ms("2026-09-01T00:00:00Z")
        self.assertEqual(
            compacted_filename(start_ms, end_ms),
            "spread_20260901T000000Z_20260901T000500Z.parquet",
        )
        self.assertEqual(
            compacted_names_covering(start_ms, end_ms),
            ["spread_20260901T000000Z_20260901T000500Z.parquet"],
        )

    def test_cover_non_aligned_spans_two_files(self) -> None:
        start_ms, end_ms = window_bounds_ms("2026-09-01T00:02:00Z", minutes=5)
        names = compacted_names_covering(start_ms, end_ms)
        self.assertEqual(
            names,
            [
                "spread_20260901T000000Z_20260901T000500Z.parquet",
                "spread_20260901T000500Z_20260901T001000Z.parquet",
            ],
        )


class MessageDelayTest(unittest.TestCase):
    def test_local_minus_exchange(self) -> None:
        self.assertEqual(message_delay_ms(1_000, 970), 30.0)
        self.assertEqual(message_delay_ms(5_000, 4_988), 12.0)

    def test_missing_or_nonfinite_is_none(self) -> None:
        self.assertIsNone(message_delay_ms(None, 100))
        self.assertIsNone(message_delay_ms(100, None))
        self.assertIsNone(message_delay_ms(float("nan"), 100))
        self.assertIsNone(message_delay_ms(100, float("inf")))


class DeriveSpreadsTest(unittest.TestCase):
    def test_long_short_formulas_and_hole_skip(self) -> None:
        raw = pd.DataFrame(
            {
                "event_local_ts_ms": [1_000, 2_000, 3_000],
                "base_coin": ["btc", "BTC", "BTC"],
                "okx_bid_price": [100.0, None, 100.0],
                "okx_ask_price": [101.0, 101.0, 101.0],
                "bybit_bid_price": [102.0, 102.0, 102.0],
                "bybit_ask_price": [103.0, 103.0, 103.0],
            }
        )
        df = derive_spreads(raw)
        self.assertEqual(len(df), 2)
        self.assertAlmostEqual(df.loc[0, "spread_long"], (102.0 - 101.0) / 102.0 * 100.0)
        self.assertAlmostEqual(df.loc[0, "spread_short"], (100.0 - 103.0) / 100.0 * 100.0)
        self.assertEqual(list(df["base_coin"]), ["BTC", "BTC"])

    def test_venue_latency_from_stamps(self) -> None:
        raw = pd.DataFrame(
            {
                "event_local_ts_ms": [10_000],
                "base_coin": ["BTC"],
                "trigger": ["okx"],
                "okx_bid_price": [100.0],
                "okx_ask_price": [101.0],
                "bybit_bid_price": [102.0],
                "bybit_ask_price": [103.0],
                "okx_local_recv_ts_ms": [10_000],
                "okx_ts_ms": [9_970],
                "bybit_local_recv_ts_ms": [9_990],
                "bybit_ts_ms": [9_940],
            }
        )
        df = derive_spreads(raw)
        self.assertEqual(df.loc[0, "okx_latency_ms"], 30.0)
        self.assertEqual(df.loc[0, "bybit_latency_ms"], 50.0)
        self.assertEqual(df.loc[0, "trigger"], "okx")
        self.assertEqual(
            message_delay_ms(df.loc[0, "okx_local_recv_ts_ms"], df.loc[0, "okx_ts_ms"]),
            30.0,
        )


class TickWindowFigureTest(unittest.TestCase):
    def test_two_traces_no_sma_and_gap_break(self) -> None:
        raw = pd.DataFrame(
            {
                "event_local_ts_ms": [1_000, 2_000, 10_000],
                "base_coin": ["BTC", "BTC", "BTC"],
                "event_dt": pd.to_datetime(
                    [1_000, 2_000, 10_000], unit="ms", utc=True
                ),
                "spread_long": [0.10, 0.11, 0.12],
                "spread_short": [-0.20, -0.21, -0.22],
            }
        )
        fig = build_tick_window_figure(
            raw,
            {"coin": "BTC", "n_plot": 3, "source": "test"},
            gap_break_ms=2000,
        )
        names = [tr.name for tr in fig.data]
        self.assertEqual(names[:2], ["spread_long", "spread_short"])
        self.assertIn("p50_bar5m long", names)
        self.assertIn("p50_roll_1m long", names)
        self.assertIn("p50_roll_5m long", names)
        self.assertIn("p50_bar5m short", names)
        self.assertIn("p50_roll_1m short", names)
        self.assertIn("p50_roll_5m short", names)
        self.assertEqual(len(fig.data), 8)
        self.assertTrue(all("sma" not in (n or "").lower() for n in names))
        self.assertTrue(all(tr.connectgaps is False for tr in fig.data))
        # 3 ticks + 1 None breakpoint after the 8s hole
        self.assertEqual(len(fig.data[0].x), 4)
        self.assertIsNone(fig.data[0].x[2])
        hover = fig.data[0].hovertemplate or ""
        self.assertIn("coin=", hover)
        self.assertIn("spread_long=", hover)
        self.assertIn("spread_short=", hover)
        self.assertGreaterEqual(fig.layout.width, 1400)
        self.assertGreaterEqual(fig.layout.height, 700)

    def test_l1_traces_hover_has_venue_delay(self) -> None:
        raw = pd.DataFrame(
            {
                "event_local_ts_ms": [1_000, 2_000, 10_000],
                "base_coin": ["BTC", "BTC", "BTC"],
                "trigger": ["okx", "bybit", "okx"],
                "event_dt": pd.to_datetime(
                    [1_000, 2_000, 10_000], unit="ms", utc=True
                ),
                "spread_long": [0.10, 0.11, 0.12],
                "spread_short": [-0.20, -0.21, -0.22],
                "okx_bid_price": [100.0, 100.1, 100.2],
                "okx_ask_price": [101.0, 101.1, 101.2],
                "bybit_bid_price": [102.0, 102.1, 102.2],
                "bybit_ask_price": [103.0, 103.1, 103.2],
                "okx_local_recv_ts_ms": [1_000, 2_000, 10_000],
                "okx_ts_ms": [970, 1_975, 9_960],
                "bybit_local_recv_ts_ms": [990, 1_990, 9_990],
                "bybit_ts_ms": [940, 1_940, 9_940],
            }
        )
        fig = build_tick_window_figure(
            raw,
            {"coin": "BTC", "n_plot": 3, "source": "test"},
            gap_break_ms=2000,
        )
        names = [tr.name for tr in fig.data]
        self.assertEqual(names[:2], ["spread_long", "spread_short"])
        self.assertEqual(
            names[2:8],
            [
                "p50_bar5m long",
                "p50_roll_5m long",
                "p50_roll_1m long",
                "p50_bar5m short",
                "p50_roll_5m short",
                "p50_roll_1m short",
            ],
        )
        self.assertEqual(names[8:], ["okx_bid", "okx_ask", "bybit_bid", "bybit_ask"])
        by_name = {tr.name: tr for tr in fig.data}
        okx_hover = by_name["okx_bid"].hovertemplate or ""
        self.assertIn("delay_ms=", okx_hover)
        self.assertIn("venue=", okx_hover)
        self.assertIn("side=", okx_hover)
        self.assertIn("exchange_ts=", okx_hover)
        self.assertIn("trigger=", okx_hover)
        # first OKX point: 1000 − 970 = 30; gap-break None at index 2
        okx_custom = list(by_name["okx_bid"].customdata)
        self.assertEqual(okx_custom[0][1], "okx")
        self.assertEqual(okx_custom[0][2], "bid")
        self.assertAlmostEqual(float(okx_custom[0][3]), 30.0)
        self.assertEqual(okx_custom[0][6], "okx")
        self.assertIsNone(okx_custom[2][3])
        bybit_custom = list(by_name["bybit_ask"].customdata)
        self.assertEqual(bybit_custom[0][1], "bybit")
        self.assertEqual(bybit_custom[0][2], "ask")
        self.assertAlmostEqual(float(bybit_custom[0][3]), 50.0)
        self.assertEqual(len(fig.layout.annotations), 5)
        self.assertEqual(by_name["okx_bid"].yaxis, "y2")
        self.assertEqual(by_name["okx_ask"].yaxis, "y3")
        self.assertEqual(by_name["bybit_bid"].yaxis, "y4")
        self.assertEqual(by_name["bybit_ask"].yaxis, "y5")
        self.assertGreaterEqual(fig.layout.width, 1400)
        self.assertGreaterEqual(fig.layout.height, 1400)

    def test_p50_overlays_use_locked_names_on_spread_row(self) -> None:
        # Dense 1s ticks over a full UTC 5m bar so rolling p50 has mass.
        ts = list(range(0, 300_000, 1_000))
        raw = pd.DataFrame(
            {
                "event_local_ts_ms": ts,
                "base_coin": ["SOL"] * len(ts),
                "event_dt": pd.to_datetime(ts, unit="ms", utc=True),
                "spread_long": [0.10 if t < 150_000 else 0.30 for t in ts],
                "spread_short": [-0.20 if t < 150_000 else -0.05 for t in ts],
            }
        )
        fig = build_tick_window_figure(
            raw,
            {
                "coin": "SOL",
                "n_plot": len(ts),
                "source": "test",
                "window_start_ms": 0,
                "window_end_ms": 300_000,
            },
        )
        by_name = {tr.name: tr for tr in fig.data}
        self.assertIn("p50_bar5m long", by_name)
        self.assertIn("p50_roll_1m long", by_name)
        self.assertIn("p50_roll_5m long", by_name)
        bar_y = np.asarray(by_name["p50_bar5m long"].y, dtype=float)
        roll1 = np.asarray(by_name["p50_roll_1m long"].y, dtype=float)
        self.assertTrue(np.isfinite(bar_y).any())
        self.assertTrue(np.isfinite(roll1).any())
        self.assertTrue(all("sma" not in (n or "").lower() for n in by_name))


if __name__ == "__main__":
    unittest.main()
