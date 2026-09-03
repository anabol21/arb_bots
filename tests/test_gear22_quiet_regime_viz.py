"""Hermetic tests for Gear 2.2 quiet-regime visualizer (no VPS)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from research.gear22_quiet_regime_viz.candles import (
    BAR_MS,
    build_5m_bucket_stats,
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


class TestLoadAndCandles(unittest.TestCase):
    def test_load_fixture_parquet(self) -> None:
        df = load_ticks(
            FIXTURE_TICKS,
            coins=["SOL", "XRP"],
            since_ms=parse_since_ms(SINCE),
            until_ms=parse_since_ms(UNTIL),
        )
        self.assertEqual(set(df["base_coin"].unique()), {"SOL", "XRP"})
        self.assertIn("edge_pct", df.columns)
        self.assertIn("okx_mid", df.columns)
        self.assertTrue((df["event_local_ts_ms"] >= parse_since_ms(SINCE)).all())

    def test_csv_loader_path(self) -> None:
        csv_path = FIXTURE_TICKS / "ticks_sample.csv"
        self.assertTrue(csv_path.is_file())
        raw = pd.read_csv(csv_path)
        derived = derive_research_series(raw)
        self.assertGreater(len(derived), 0)
        self.assertTrue(np.isfinite(derived["edge_pct"]).all())

    def test_5m_buckets_and_intentional_gap(self) -> None:
        df = load_ticks(
            FIXTURE_TICKS,
            coins=["SOL"],
            since_ms=parse_since_ms(SINCE),
            until_ms=parse_since_ms(UNTIL),
        )
        buckets = build_5m_bucket_stats(
            df,
            value_col="edge_pct",
            start_ms=parse_since_ms(SINCE),
            end_ms=parse_since_ms(UNTIL),
            fill_empty_buckets=True,
        )
        self.assertGreater(len(buckets), 0)
        self.assertEqual(int(buckets["bar_end_ms"].iloc[0] - buckets["bar_start_ms"].iloc[0]), BAR_MS)
        gaps = detect_gap_intervals(df["event_local_ts_ms"], gap_threshold_ms=30_000)
        self.assertGreaterEqual(len(gaps), 1)
        # At least one bucket should show elevated gap_fraction or zero ticks.
        self.assertTrue(((buckets["gap_fraction"] > 0.2) | (buckets["tick_count"] == 0)).any())

    def test_extension_hook_empty(self) -> None:
        empty = pd.DataFrame()
        self.assertEqual(collect_extension_traces(empty, empty, coin="SOL"), [])


class TestSmokeHtml(unittest.TestCase):
    def test_cli_writes_html(self) -> None:
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
            for p in paths:
                self.assertTrue(p.is_file())
                text = p.read_text(encoding="utf-8")
                self.assertIn("OKX", text)
                self.assertIn("gap", text.lower())
                self.assertIn("Extension point", text)
                self.assertIn("plotly", text.lower())


if __name__ == "__main__":
    unittest.main()
