"""Hermetic tests for Gear 2.2 quiet-regime visualizer (no VPS)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from research.gear22_quiet_regime_viz.candles import (
    BAR_MS,
    DEFAULT_CANDLE_BINS,
    SPREAD_LONG_COL,
    SPREAD_SHORT_COL,
    align_inspect_customdata,
    build_5m_bucket_stats,
    build_bar_inspect_payloads,
    causal_sma,
)
from research.gear22_quiet_regime_viz.cli import run_viz
from research.gear22_quiet_regime_viz.gaps import detect_gap_intervals
from research.gear22_quiet_regime_viz.load import (
    DEFAULT_SINCE_UTC,
    derive_research_series,
    load_ticks,
    parse_since_ms,
)
from research.gear22_quiet_regime_viz.metrics_ext import collect_extension_traces
from research.gear22_quiet_regime_viz.quantiles import (
    tick_hold_weights_ms,
    time_weighted_quantiles,
)

FIXTURE_TICKS = (
    Path(__file__).resolve().parents[1]
    / "research"
    / "fixtures"
    / "gear22_quiet_regime_viz"
    / "ticks"
)
SINCE = DEFAULT_SINCE_UTC
UNTIL = "2026-09-03T08:40:00Z"


class TestParseAndGaps(unittest.TestCase):
    def test_since_default_ms(self) -> None:
        self.assertEqual(parse_since_ms(SINCE), 1_788_423_660_000)

    def test_gap_detection(self) -> None:
        ts = [0, 1_000, 2_000, 50_000, 51_000]
        gaps = detect_gap_intervals(ts, gap_threshold_ms=10_000)
        self.assertEqual(gaps, [(2_000, 50_000)])

    def test_causal_sma(self) -> None:
        x = np.array([1.0, 2.0, 3.0, 4.0], dtype="float64")
        y = causal_sma(x, 3)
        self.assertTrue(np.isnan(y[0]) and np.isnan(y[1]))
        self.assertAlmostEqual(float(y[2]), 2.0)
        self.assertAlmostEqual(float(y[3]), 3.0)


class TestTimeWeightedQuantiles(unittest.TestCase):
    def test_hold_weights_and_tw_median(self) -> None:
        # Values 1 for 10ms, then 2 for 90ms → TW median should be 2.
        ts = np.asarray([0, 10], dtype="int64")
        y = np.asarray([1.0, 2.0], dtype="float64")
        w = tick_hold_weights_ms(ts, last_end_ms=100)
        self.assertEqual(list(w), [10.0, 90.0])
        q = time_weighted_quantiles(y, w)
        self.assertEqual(q["tw_p50"], 2.0)
        self.assertEqual(q["tw_p25"], 2.0)
        self.assertEqual(q["tw_p95"], 2.0)

    def test_bucket_includes_tw_columns(self) -> None:
        df = load_ticks(
            FIXTURE_TICKS,
            coins=["SOL"],
            since_ms=parse_since_ms(SINCE),
            until_ms=parse_since_ms(UNTIL),
        )
        buckets = build_5m_bucket_stats(
            df,
            value_col=SPREAD_LONG_COL,
            start_ms=parse_since_ms(SINCE),
            end_ms=parse_since_ms(UNTIL),
        )
        for col in ("tw_p25", "tw_p50", "tw_p95", "tw_p99"):
            self.assertIn(col, buckets.columns)
        nonempty = buckets.loc[buckets["tick_count"] > 0]
        self.assertFalse(nonempty.empty)
        self.assertTrue(np.isfinite(nonempty["tw_p50"]).any())


class TestLoadAndCandles(unittest.TestCase):
    def test_load_fixture_parquet(self) -> None:
        df = load_ticks(
            FIXTURE_TICKS,
            coins=["SOL", "XRP"],
            since_ms=parse_since_ms(SINCE),
            until_ms=parse_since_ms(UNTIL),
        )
        self.assertEqual(set(df["base_coin"].unique()), {"SOL", "XRP"})
        self.assertIn(SPREAD_LONG_COL, df.columns)
        self.assertIn(SPREAD_SHORT_COL, df.columns)
        self.assertIn("edge_pct", df.columns)
        self.assertTrue((df["event_local_ts_ms"] >= parse_since_ms(SINCE)).all())

    def test_policy_spread_formulas(self) -> None:
        raw = pd.DataFrame(
            {
                "event_local_ts_ms": [1],
                "base_coin": ["SOL"],
                "okx_bid_price": [100.0],
                "okx_ask_price": [101.0],
                "bybit_bid_price": [102.0],
                "bybit_ask_price": [103.0],
            }
        )
        d = derive_research_series(raw).iloc[0]
        self.assertAlmostEqual(float(d["spread_long"]), (102 - 101) / 102 * 100)
        self.assertAlmostEqual(float(d["spread_short"]), (100 - 103) / 100 * 100)

    def test_csv_loader_path(self) -> None:
        csv_path = FIXTURE_TICKS / "ticks_sample.csv"
        self.assertTrue(csv_path.is_file())
        raw = pd.read_csv(csv_path)
        derived = derive_research_series(raw)
        self.assertGreater(len(derived), 0)
        self.assertTrue(np.isfinite(derived[SPREAD_LONG_COL]).all())

    def test_5m_buckets_and_intentional_gap(self) -> None:
        df = load_ticks(
            FIXTURE_TICKS,
            coins=["SOL"],
            since_ms=parse_since_ms(SINCE),
            until_ms=parse_since_ms(UNTIL),
        )
        buckets = build_5m_bucket_stats(
            df,
            value_col=SPREAD_LONG_COL,
            start_ms=parse_since_ms(SINCE),
            end_ms=parse_since_ms(UNTIL),
            fill_empty_buckets=True,
        )
        self.assertGreater(len(buckets), 0)
        self.assertEqual(
            int(buckets["bar_end_ms"].iloc[0] - buckets["bar_start_ms"].iloc[0]),
            BAR_MS,
        )
        gaps = detect_gap_intervals(df["event_local_ts_ms"], gap_threshold_ms=30_000)
        self.assertGreaterEqual(len(gaps), 1)
        self.assertTrue(
            ((buckets["gap_fraction"] > 0.2) | (buckets["tick_count"] == 0)).any()
        )

    def test_extension_hook_empty(self) -> None:
        empty = pd.DataFrame()
        self.assertEqual(
            collect_extension_traces(empty, empty, coin="SOL", buckets_short=empty),
            [],
        )


class TestBarInspectPayloads(unittest.TestCase):
    def test_hist_and_temporal_compact(self) -> None:
        df = load_ticks(
            FIXTURE_TICKS,
            coins=["SOL"],
            since_ms=parse_since_ms(SINCE),
            until_ms=parse_since_ms(UNTIL),
        )
        payloads = build_bar_inspect_payloads(
            df,
            value_col=SPREAD_LONG_COL,
            n_bins=24,
            n_temporal=12,
            side="long",
        )
        self.assertGreater(len(payloads), 0)
        sample = next(iter(payloads.values()))
        self.assertEqual(sample["side"], "long")
        self.assertEqual(sample["col"], SPREAD_LONG_COL)
        self.assertEqual(len(sample["c"]), 24)
        self.assertEqual(len(sample["tv"]), 12)
        self.assertEqual(sum(sample["c"]), sample["n"])
        self.assertGreater(sample["n"], 0)
        self.assertIsNotNone(sample["lo"])
        self.assertIsNotNone(sample["hi"])
        # Compact: no per-tick arrays.
        self.assertNotIn("ticks", sample)
        self.assertNotIn("ts", sample)

    def test_align_customdata_matches_ohlc_rows(self) -> None:
        df = load_ticks(
            FIXTURE_TICKS,
            coins=["SOL"],
            since_ms=parse_since_ms(SINCE),
            until_ms=parse_since_ms(UNTIL),
        )
        buckets = build_5m_bucket_stats(
            df,
            value_col=SPREAD_LONG_COL,
            start_ms=parse_since_ms(SINCE),
            end_ms=parse_since_ms(UNTIL),
            fill_empty_buckets=True,
        )
        payloads = build_bar_inspect_payloads(
            df, value_col=SPREAD_LONG_COL, n_bins=DEFAULT_CANDLE_BINS, side="long"
        )
        custom = align_inspect_customdata(buckets, payloads)
        n_ohlc = int((buckets["tick_count"].fillna(0).astype(int) > 0).sum())
        self.assertEqual(len(custom), n_ohlc)
        self.assertTrue(any(cd is not None for cd in custom))

    def test_zero_bins_disables(self) -> None:
        df = load_ticks(
            FIXTURE_TICKS,
            coins=["SOL"],
            since_ms=parse_since_ms(SINCE),
            until_ms=parse_since_ms(UNTIL),
        )
        self.assertEqual(
            build_bar_inspect_payloads(df, value_col=SPREAD_LONG_COL, n_bins=0),
            {},
        )


class TestSmokeHtml(unittest.TestCase):
    def test_cli_writes_html_nav_and_plotly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            paths = run_viz(
                data_root=FIXTURE_TICKS,
                coins=["SOL", "XRP"],
                since=SINCE,
                until=UNTIL,
                out_dir=out,
            )
            self.assertEqual(len(paths), 2)
            self.assertTrue((out / "plotly.min.js").is_file())
            coins_json = json.loads((out / "coins.json").read_text(encoding="utf-8"))
            self.assertEqual(coins_json["coins"], ["SOL", "XRP"])
            for p in paths:
                self.assertTrue(p.is_file())
                text = p.read_text(encoding="utf-8")
                self.assertIn("spread_long", text)
                self.assertIn("spread_short", text)
                self.assertIn("open_long", text)
                self.assertIn("open_short", text)
                self.assertIn("tw_p50", text)
                self.assertIn("histogram", text.lower())
                self.assertIn("ArrowLeft", text)
                self.assertIn("gear22_quiet_regime_XRP.html", text)
                self.assertIn("plotly.min.js", text)
                self.assertIn("Extension point", text)
                self.assertIn("gap_fraction", text)
                # Click-to-inspect panel + compact customdata markers.
                self.assertIn("candle-inspect", text)
                self.assertIn("plotly_click", text)
                self.assertIn("equal-time mean", text)
                self.assertIn('"c":', text)
                self.assertIn('"tv":', text)
                self.assertIn("click for in-bar distribution", text)
                # Must not embed raw tick dumps for inspect.
                self.assertNotIn("full_ticks", text)

    def test_inspect_payload_size_vs_tick_overlay(self) -> None:
        """Inspect customdata should stay << sparse tick overlay cost."""
        df = load_ticks(
            FIXTURE_TICKS,
            coins=["SOL"],
            since_ms=parse_since_ms(SINCE),
            until_ms=parse_since_ms(UNTIL),
        )
        payloads = build_bar_inspect_payloads(
            df,
            value_col=SPREAD_LONG_COL,
            n_bins=32,
            n_temporal=16,
            side="long",
        )
        inspect_bytes = len(json.dumps(payloads, separators=(",", ":")))
        # Rough tick-overlay proxy: 4000 points × 2 floats × ~12 chars.
        tick_overlay_proxy = min(len(df), 4_000) * 2 * 12
        self.assertLess(inspect_bytes, max(tick_overlay_proxy // 4, 2_000))
        self.assertLess(inspect_bytes, 50_000)  # fixture window: tiny


if __name__ == "__main__":
    unittest.main()
