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
from research.gear22_quiet_regime_viz.floors import (
    MED3_NAME,
    MED12_NAME,
    SMA12_NAME,
    TF_SELECT_25_NAME,
    TF_SELECT_MED_NAME,
    TF_SELECT_NAME,
    TRIM3_25_NAME,
    TRIM12_25_NAME,
    W2_BARS,
    W2_HOURS,
    _trim_mean_impl,
    causal_floor,
    causal_trim_floor,
    compute_chosen_floor,
    compute_median_compare,
    compute_tf_compare,
    min_finite_count,
    tf_select_floor,
    trim_mean_alpha,
)
from research.gear22_quiet_regime_viz.inject_floors import (
    extract_sma12_xy,
    extract_sma3_xy,
    inject_coin_html,
)
from research.gear22_quiet_regime_viz.metrics_ext import collect_extension_traces
from research.gear22_quiet_regime_viz.quantiles import (
    hist_cdf_quantile,
    tick_hold_weights_ms,
    time_weighted_histogram,
    time_weighted_mean,
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


def _html_has(text: str, needle: str) -> bool:
    """True if ``needle`` is in HTML, including Plotly JSON ``\\uXXXX`` escapes."""
    if needle in text:
        return True
    escaped = needle.encode("ascii", "backslashreplace").decode("ascii")
    return escaped in text


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
        self.assertAlmostEqual(time_weighted_mean(y, w), 1.9)
        # p01/p05 land in the short first hold (10ms of 100ms).
        self.assertEqual(q["tw_p01"], 1.0)
        self.assertEqual(q["tw_p05"], 1.0)

    def test_tw_histogram_mass_conserved(self) -> None:
        y = np.asarray([1.0, 2.0, 10.0], dtype="float64")
        w = np.asarray([80.0, 15.0, 5.0], dtype="float64")
        # Robust range excluding the spike: mass of spike clips into last bin.
        mass, lo, hi = time_weighted_histogram(y, w, n_bins=4, lo=1.0, hi=2.0)
        self.assertAlmostEqual(float(np.sum(mass)), 100.0)
        self.assertEqual(lo, 1.0)
        self.assertEqual(hi, 2.0)
        # Spike at 10 clips into last bin → last bin has 15+5.
        self.assertAlmostEqual(float(mass[-1]), 20.0)

    def test_hist_cdf_quantile_interpolates_bins(self) -> None:
        # Two equal-mass bins [0, 10] and [10, 20] → p5 in first bin.
        q05 = hist_cdf_quantile(0.0, 20.0, [10.0, 10.0], 0.05)
        self.assertAlmostEqual(q05, 1.0)
        q95 = hist_cdf_quantile(0.0, 20.0, [10.0, 10.0], 0.95)
        self.assertAlmostEqual(q95, 19.0)
        q01 = hist_cdf_quantile(0.0, 20.0, [10.0, 10.0], 0.01)
        self.assertAlmostEqual(q01, 0.2)
        self.assertTrue(np.isnan(hist_cdf_quantile(None, 1.0, [1], 0.05)))
        self.assertTrue(np.isnan(hist_cdf_quantile(0.0, 1.0, [0, 0], 0.05)))

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
        for col in ("tw_p01", "tw_p05", "tw_p25", "tw_p50", "tw_p95", "tw_p99"):
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
        self.assertIn("trigger", df.columns)
        self.assertTrue(set(df["trigger"].dropna().unique()) <= {"okx", "bybit"})
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
                "okx_local_recv_ts_ms": [1000],
                "okx_ts_ms": [990],
                "bybit_local_recv_ts_ms": [1000],
                "bybit_ts_ms": [995],
            }
        )
        d = derive_research_series(raw).iloc[0]
        self.assertAlmostEqual(float(d["spread_long"]), (102 - 101) / 102 * 100)
        self.assertAlmostEqual(float(d["spread_short"]), (100 - 103) / 100 * 100)
        self.assertAlmostEqual(float(d["okx_latency_ms"]), 10.0)
        self.assertAlmostEqual(float(d["bybit_latency_ms"]), 5.0)

    def test_csv_loader_path(self) -> None:
        csv_path = FIXTURE_TICKS / "ticks_sample.csv"
        self.assertTrue(csv_path.is_file())
        raw = pd.read_csv(csv_path)
        derived = derive_research_series(raw)
        self.assertGreater(len(derived), 0)
        self.assertTrue(np.isfinite(derived[SPREAD_LONG_COL]).all())
        self.assertIn("trigger", derived.columns)
        self.assertTrue(set(derived["trigger"].dropna().unique()) <= {"okx", "bybit"})

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
        self.assertEqual(
            collect_extension_traces(
                empty, empty, coin="SOL", buckets_short=empty, floors=False
            ),
            [],
        )


class TestFloorEstimators(unittest.TestCase):
    def test_window_lengths(self) -> None:
        self.assertEqual(W2_HOURS, (3, 6, 12))
        self.assertEqual(W2_BARS, (36, 72, 144))
        self.assertEqual(tuple(h * 12 for h in W2_HOURS), W2_BARS)
        self.assertEqual(min_finite_count(36), 8)
        self.assertEqual(min_finite_count(72), 15)
        self.assertEqual(min_finite_count(144), 29)

    def test_trim_mean_10_numeric(self) -> None:
        # n=10, α=0.10 → k=int(10*0.10)=1; drop 1 and 10 → mean(2..9)=5.5
        x = np.arange(1, 11, dtype="float64")
        self.assertAlmostEqual(trim_mean_alpha(x, 0.10), 5.5)
        self.assertAlmostEqual(_trim_mean_impl(x, 0.10), 5.5)
        try:
            from scipy.stats import trim_mean

            self.assertAlmostEqual(_trim_mean_impl(x, 0.10), float(trim_mean(x, 0.10)))
        except ImportError:
            pass

    def test_trim_mean_25_numeric(self) -> None:
        # Spiked sample so α=10% and α=25% differ.
        # n=10, α=0.25 → k=int(10*0.25)=2; drop 0,0 and 50,100 → mean(1..6)=3.5
        x = np.array([0.0, 0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 50.0, 100.0])
        self.assertAlmostEqual(trim_mean_alpha(x, 0.10), 8.875)
        self.assertAlmostEqual(trim_mean_alpha(x, 0.25), 3.5)
        self.assertAlmostEqual(_trim_mean_impl(x, 0.25), 3.5)
        y25 = causal_floor(x, window=10, estimator="trim10", alpha=0.25, min_finite=10)
        y_helper = causal_trim_floor(x, 10, alpha=0.25, min_finite=10)
        y10 = causal_floor(x, window=10, estimator="trim10", min_finite=10)
        self.assertAlmostEqual(float(y25[-1]), 3.5)
        self.assertAlmostEqual(float(y_helper[-1]), 3.5)
        self.assertAlmostEqual(float(y10[-1]), 8.875)

    def test_causal_independent_of_future(self) -> None:
        s = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 10.0, 11.0, 12.0], dtype="float64")
        w = 4
        need = 3
        med = causal_floor(s, w, estimator="median", min_finite=need)
        mean = causal_floor(s, w, estimator="mean", min_finite=need)
        trim = causal_floor(s, w, estimator="trim10", min_finite=need)
        # Index 3 uses only s[0:4] = 1,2,3,4
        self.assertAlmostEqual(float(med[3]), 2.5)
        self.assertAlmostEqual(float(mean[3]), 2.5)
        s_future = np.concatenate([s, np.array([1e6, -1e6], dtype="float64")])
        for est, series in (("median", med), ("mean", mean), ("trim10", trim)):
            later = causal_floor(s_future, w, estimator=est, min_finite=need)
            np.testing.assert_allclose(later[: s.size], series, equal_nan=True)

    def test_nan_and_empty_policy(self) -> None:
        # Holes excluded; do not interpolate. Too few finite → NaN.
        s = np.array([1.0, np.nan, 3.0, np.nan, 5.0, 6.0, 7.0], dtype="float64")
        out = causal_floor(s, window=5, estimator="mean", min_finite=3)
        self.assertTrue(np.isnan(out[0]))
        self.assertTrue(np.isnan(out[1]))
        # t=4 lookback {1,nan,3,nan,5} → finite {1,3,5} mean=3
        self.assertAlmostEqual(float(out[4]), 3.0)
        empty = causal_floor(np.array([], dtype="float64"), 36, estimator="median")
        self.assertEqual(empty.size, 0)
        all_nan = causal_floor(
            np.array([np.nan, np.nan, np.nan], dtype="float64"),
            36,
            estimator="mean",
        )
        self.assertTrue(np.all(np.isnan(all_nan)))

    def test_windows_36_72_144_warmup(self) -> None:
        n = 160
        s = np.linspace(0.0, 1.0, n)
        for w in W2_BARS:
            need = min_finite_count(w)
            y = causal_floor(s, w, estimator="median")
            self.assertEqual(y.size, n)
            self.assertTrue(np.all(np.isnan(y[: need - 1])))
            self.assertTrue(np.isfinite(y[need - 1]))
            self.assertTrue(np.isfinite(y[-1]))

    def test_tf_select_is_min_of_finite_pair(self) -> None:
        short = np.array([1.0, 3.0, np.nan, 2.0], dtype="float64")
        long = np.array([2.0, 1.0, 4.0, np.nan], dtype="float64")
        out = tf_select_floor(short, long)
        self.assertAlmostEqual(float(out[0]), 1.0)  # short below → follow 3h
        self.assertAlmostEqual(float(out[1]), 1.0)  # short above → hold 12h
        self.assertTrue(np.isnan(out[2]))
        self.assertTrue(np.isnan(out[3]))

    def test_tf_compare_selects_min_at_both_alphas(self) -> None:
        n = 144
        s = np.linspace(0.0, 1.0, n)
        s[-5:] = 10.0  # late spike: 3h trim reacts more than 12h
        bundle = compute_tf_compare(s)
        for sel, a, b in (
            (TF_SELECT_NAME, "trim 3h", "trim 12h"),
            (TF_SELECT_25_NAME, TRIM3_25_NAME, TRIM12_25_NAME),
        ):
            left = bundle[a]
            right = bundle[b]
            got = bundle[sel]
            ok = np.isfinite(left) & np.isfinite(right)
            self.assertTrue(ok.any())
            np.testing.assert_allclose(got[ok], np.minimum(left[ok], right[ok]))
            self.assertTrue(np.all(np.isnan(got[~ok])))
        self.assertIn(TRIM3_25_NAME, bundle)
        self.assertIn(TRIM12_25_NAME, bundle)

    def test_chosen_floor_is_tf_select_alpha25(self) -> None:
        n = 144
        s = np.linspace(0.0, 1.0, n)
        s[-5:] = 10.0
        chosen = compute_chosen_floor(s)
        self.assertEqual(set(chosen), {SMA12_NAME, TF_SELECT_25_NAME})
        trim3 = causal_trim_floor(s, 36, alpha=0.25)
        trim12 = causal_trim_floor(s, 144, alpha=0.25)
        got = chosen[TF_SELECT_25_NAME]
        ok = np.isfinite(trim3) & np.isfinite(trim12)
        self.assertTrue(ok.any())
        np.testing.assert_allclose(got[ok], np.minimum(trim3[ok], trim12[ok]))
        self.assertTrue(np.all(np.isnan(got[~ok])))
        self.assertNotIn(TRIM3_25_NAME, chosen)
        self.assertNotIn("median 3h", chosen)

    def test_median_compare_selects_min_of_medians(self) -> None:
        n = 144
        s = np.linspace(0.0, 1.0, n)
        s[-5:] = 10.0  # late spike: 3h median moves more than 12h
        bundle = compute_median_compare(s)
        self.assertEqual(
            set(bundle),
            {SMA12_NAME, MED3_NAME, MED12_NAME, TF_SELECT_MED_NAME},
        )
        np.testing.assert_allclose(
            bundle[MED3_NAME],
            causal_floor(s, 36, estimator="median"),
            equal_nan=True,
        )
        np.testing.assert_allclose(
            bundle[MED12_NAME],
            causal_floor(s, 144, estimator="median"),
            equal_nan=True,
        )
        left = bundle[MED3_NAME]
        right = bundle[MED12_NAME]
        got = bundle[TF_SELECT_MED_NAME]
        ok = np.isfinite(left) & np.isfinite(right)
        self.assertTrue(ok.any())
        np.testing.assert_allclose(got[ok], np.minimum(left[ok], right[ok]))
        self.assertTrue(np.all(np.isnan(got[~ok])))
        self.assertNotIn("median 3h α25", bundle)
        self.assertNotIn("tf-select med α25", bundle)


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
        self.assertEqual(sample["c_w"], "tw_ms")
        self.assertEqual(sample["tv_w"], "equal_time")
        # TW mass sums to total hold weight (not tick count).
        self.assertAlmostEqual(sum(sample["c"]), sample["w_ms"], places=2)
        self.assertNotEqual(sum(sample["c"]), sample["n"])  # mass ≠ count
        self.assertGreater(sample["n"], 0)
        self.assertIsNotNone(sample["lo"])
        self.assertIsNotNone(sample["hi"])
        # TW summary percentiles present.
        self.assertIn("tw", sample)
        for k in ("mean", "p50", "p95", "p99"):
            self.assertIn(k, sample["tw"])
            self.assertIsNotNone(sample["tw"][k])
        # Compact: no per-tick arrays.
        self.assertNotIn("ticks", sample)
        self.assertNotIn("ts", sample)
        # Latency nested payloads when columns present.
        self.assertIn("lat", sample)
        self.assertIn("okx", sample["lat"])
        self.assertIn("bybit", sample["lat"])
        self.assertEqual(len(sample["lat"]["okx"]["c"]), 24)  # DEFAULT_LATENCY_BINS
        self.assertGreater(sample["lat"]["okx"]["n"], 0)
        self.assertLessEqual(sample["lat"]["okx"]["n"], sample["n"])
        self.assertEqual(sample["lat"]["okx"]["c_w"], "count")
        self.assertEqual(sample["lat"]["bybit"]["c_w"], "count")
        # Latency hist mass is tick counts, not TW ms.
        self.assertEqual(sum(sample["lat"]["okx"]["c"]), sample["lat"]["okx"]["n"])
        self.assertEqual(sum(sample["lat"]["bybit"]["c"]), sample["lat"]["bybit"]["n"])
        self.assertIn("p50", sample["lat"]["okx"]["tw"])

    def test_robust_range_vs_spike_max(self) -> None:
        """Rare max spike must not stretch hist axis (crushed-left failure)."""
        bar_start = 1_788_423_600_000
        # Bulk near 0.3–0.5; one rare spike with ~1ms hold (equal-weight + hi=max
        # would stretch axis to ~250; TW p01–p99 must stay near the bulk).
        ts = bar_start + np.asarray(
            [0, 1, 30_000, 60_000, 90_000, 120_000, 150_000, 180_000, 210_000, 240_000],
            dtype="int64",
        )
        y = np.asarray(
            [250.0, 0.30, 0.32, 0.35, 0.38, 0.40, 0.42, 0.45, 0.48, 0.50],
            dtype="float64",
        )
        lat = np.asarray(
            [200.0, 8.0, 9.0, 10.0, 11.0, 10.0, 12.0, 11.0, 9.0, 10.0],
            dtype="float64",
        )
        df = pd.DataFrame(
            {
                "event_local_ts_ms": ts,
                SPREAD_LONG_COL: y,
                "trigger": ["okx"] * int(ts.size),
                "okx_latency_ms": lat,
                "bybit_latency_ms": lat * 0.8,
            }
        )
        payloads = build_bar_inspect_payloads(
            df,
            value_col=SPREAD_LONG_COL,
            n_bins=16,
            n_temporal=8,
            latency_bins=12,
            side="long",
        )
        self.assertEqual(len(payloads), 1)
        sample = payloads[bar_start]
        # Axis must be near the bulk, not ~250.
        self.assertLess(float(sample["hi"]), 5.0)
        self.assertGreater(float(sample["lo"]), -1.0)
        self.assertLess(float(sample["hi"]) - float(sample["lo"]), 5.0)
        # TW p50 near the cluster.
        self.assertLess(float(sample["tw"]["p50"]), 1.0)
        self.assertEqual(sample["c_w"], "tw_ms")
        self.assertAlmostEqual(sum(sample["c"]), sample["w_ms"], places=2)
        # Latency is equal-weight: a 1-of-10 spike at 200ms *does* affect p99
        # (unlike spread TW). Hist mass is tick counts, not hold-ms.
        okx = sample["lat"]["okx"]
        self.assertEqual(okx["c_w"], "count")
        self.assertEqual(sum(okx["c"]), okx["n"])
        self.assertEqual(okx["n"], int(ts.size))
        self.assertNotIn("w_ms", okx)

    def test_latency_trigger_scoped_unequal_n_and_count_mass(self) -> None:
        """Mixed-trigger bar: venue n differs; hist mass is tick counts; no leak."""
        bar_start = 1_788_423_600_000
        ts = bar_start + np.asarray(
            [0, 10_000, 20_000, 30_000, 40_000],
            dtype="int64",
        )
        df = pd.DataFrame(
            {
                "event_local_ts_ms": ts,
                SPREAD_LONG_COL: [0.30, 0.31, 0.32, 0.33, 0.34],
                # 3 okx / 2 bybit — n_okx must != n_bybit.
                "trigger": ["okx", "okx", "bybit", "okx", "bybit"],
                # Non-trigger venue latencies are decoys (must not enter that hist).
                "okx_latency_ms": [10.0, 12.0, 999.0, 14.0, 888.0],
                "bybit_latency_ms": [777.0, 666.0, 20.0, 555.0, 22.0],
            }
        )
        payloads = build_bar_inspect_payloads(
            df,
            value_col=SPREAD_LONG_COL,
            n_bins=16,
            n_temporal=8,
            latency_bins=12,
            side="long",
        )
        self.assertEqual(len(payloads), 1)
        sample = payloads[bar_start]
        self.assertEqual(sample["c_w"], "tw_ms")
        self.assertAlmostEqual(sum(sample["c"]), sample["w_ms"], places=2)
        okx = sample["lat"]["okx"]
        bybit = sample["lat"]["bybit"]
        self.assertEqual(okx["n"], 3)
        self.assertEqual(bybit["n"], 2)
        self.assertNotEqual(okx["n"], bybit["n"])
        self.assertEqual(okx["c_w"], "count")
        self.assertEqual(bybit["c_w"], "count")
        self.assertEqual(sum(okx["c"]), okx["n"])
        self.assertEqual(sum(bybit["c"]), bybit["n"])
        # Mass is counts, not latency-ms (10+12+14=36 would fail this).
        self.assertEqual(sum(okx["c"]), 3)
        self.assertAlmostEqual(float(okx["tw"]["mean"]), 12.0)
        self.assertAlmostEqual(float(bybit["tw"]["mean"]), 21.0)
        self.assertLess(float(okx["hi"]), 50.0)
        self.assertLess(float(bybit["hi"]), 50.0)
        self.assertLess(float(okx["tw"]["p99"]), 50.0)
        self.assertLess(float(bybit["tw"]["p99"]), 50.0)

    def test_trigger_missing_omits_lat(self) -> None:
        """Legacy dumps without trigger must not emit unscoped latency hists."""
        df = load_ticks(
            FIXTURE_TICKS,
            coins=["SOL"],
            since_ms=parse_since_ms(SINCE),
            until_ms=parse_since_ms(UNTIL),
        )
        slim = df.drop(columns=["trigger"])
        self.assertNotIn("trigger", slim.columns)
        self.assertIn("okx_latency_ms", slim.columns)
        payloads = build_bar_inspect_payloads(
            slim, value_col=SPREAD_LONG_COL, n_bins=16, side="long"
        )
        sample = next(iter(payloads.values()))
        self.assertNotIn("lat", sample)
        self.assertEqual(sample["c_w"], "tw_ms")

    def test_latency_nan_skipped(self) -> None:
        df = load_ticks(
            FIXTURE_TICKS,
            coins=["SOL"],
            since_ms=parse_since_ms(SINCE),
            until_ms=parse_since_ms(UNTIL),
        )
        self.assertIn("okx_latency_ms", df.columns)
        self.assertTrue(np.isfinite(df["okx_latency_ms"]).any())
        # Inject all-NaN bybit for one bar's worth of check via payload builder.
        dirty = df.copy()
        dirty["bybit_latency_ms"] = np.nan
        payloads = build_bar_inspect_payloads(
            dirty,
            value_col=SPREAD_LONG_COL,
            n_bins=16,
            latency_bins=12,
            side="long",
        )
        sample = next(iter(payloads.values()))
        self.assertEqual(sample["lat"]["bybit"]["n"], 0)
        self.assertEqual(sum(sample["lat"]["bybit"]["c"]), 0)
        self.assertGreater(sample["lat"]["okx"]["n"], 0)

    def test_latency_cols_absent_omits_lat(self) -> None:
        df = load_ticks(
            FIXTURE_TICKS,
            coins=["SOL"],
            since_ms=parse_since_ms(SINCE),
            until_ms=parse_since_ms(UNTIL),
        )
        slim = df.drop(columns=["okx_latency_ms", "bybit_latency_ms"])
        payloads = build_bar_inspect_payloads(
            slim, value_col=SPREAD_LONG_COL, n_bins=16, side="long"
        )
        sample = next(iter(payloads.values()))
        self.assertNotIn("lat", sample)

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
        nonempty = next(cd for cd in custom if cd is not None)
        self.assertIn("lat", nonempty)
        self.assertIn("tw", nonempty)
        self.assertEqual(nonempty["c_w"], "tw_ms")

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
                self.assertTrue(_html_has(text, "tf-select α25"))
                self.assertIn('"name":"SMA-3"', text)
                self.assertNotIn('"name":"SMA-12"', text)
                self.assertIn("SMA-12", text)
                self.assertIn("p5–p95", text)
                self.assertIn("p1–p99", text)
                self.assertNotIn("median 3h", text)
                self.assertNotIn("median 12h", text)
                self.assertFalse(_html_has(text, "tf-select med"))
                self.assertFalse(_html_has(text, "trim 3h α25"))
                self.assertFalse(_html_has(text, "trim 12h α25"))
                self.assertNotIn("median / mean / trim10", text)
                self.assertNotIn("mean 6h", text)
                self.assertIn("Floor panel", text)
                # One standalone floor figure per side (not extra 6-row rows).
                self.assertGreaterEqual(text.count("tf-select α25"), 2)
                self.assertIn("rgba(140, 170, 200, 0.28)", text)
                self.assertIn("gap_fraction", text)
                # Click-to-inspect panel + compact customdata markers.
                self.assertIn("candle-inspect", text)
                self.assertIn("plotly_click", text)
                self.assertIn("equal-time mean", text)
                self.assertIn("TW mass", text)
                self.assertIn("triggering venue", text)
                self.assertIn("c_w=count", text)
                self.assertIn("okx_latency_ms", text)
                self.assertIn("bybit_latency_ms", text)
                self.assertIn('"lat":', text)
                self.assertIn('"c":', text)
                self.assertIn('"tv":', text)
                self.assertIn('"tw":', text)
                self.assertIn('"c_w":', text)
                self.assertIn("click for in-bar distribution", text)
                # Overlay clicks must resolve via candlestick x + customdata.
                self.assertIn("lookupCandleByTime", text)
                self.assertNotIn(
                    'if (!tr || tr.type !== "candlestick") return',
                    text,
                )
                # Must not embed raw tick dumps for inspect.
                self.assertNotIn("full_ticks", text)

    def test_candle_inspect_script_resolves_overlay_clicks(self) -> None:
        """Binder must look up customdata by time, not only candlestick hits."""
        from research.gear22_quiet_regime_viz.plot import _candle_inspect_script

        script = _candle_inspect_script()
        self.assertIn("plotly_click", script)
        self.assertIn("lookupCandleByTime", script)
        self.assertIn("customdata", script)
        self.assertIn("bar_ms", script)
        self.assertIn("twVlines", script)
        self.assertIn("TW p50", script)
        self.assertIn("TW mass", script)
        self.assertIn("c_w=count", script)
        self.assertIn("trigger venue only", script)
        # Regression: old binder rejected MA / sparse-tick steals outright.
        self.assertNotIn(
            'if (!tr || tr.type !== "candlestick") return',
            script,
        )
        # Candle-row only: lower panels must not open inspect via fallback.
        self.assertIn('axisId(ct, "yaxis", "y") === clickY', script)

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
        # Compact bins (spread + optional latency) must stay well below overlay cost.
        self.assertLess(inspect_bytes, max(tick_overlay_proxy, 8_000))
        self.assertLess(inspect_bytes, 80_000)  # fixture window: still tiny

    def test_inject_floors_into_six_row_html(self) -> None:
        """Existing 6-row pages get floor graphs from embedded SMA-3 / SMA-12."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            paths = run_viz(
                data_root=FIXTURE_TICKS,
                coins=["SOL"],
                since=SINCE,
                until=UNTIL,
                out_dir=out,
                floors=False,
            )
            self.assertEqual(len(paths), 1)
            page = paths[0]
            before = page.read_text(encoding="utf-8")
            self.assertNotIn("tf-select", before)
            self.assertIn("long SMA-12", before)
            self.assertIn("short SMA-12", before)
            self.assertIn("long SMA-3", before)
            self.assertIn("short SMA-3", before)
            lx, ly = extract_sma12_xy(before, "long")
            sx, sy = extract_sma12_xy(before, "short")
            lx3, ly3 = extract_sma3_xy(before, "long")
            sx3, sy3 = extract_sma3_xy(before, "short")
            self.assertEqual(len(lx), int(ly.size))
            self.assertEqual(len(sx), int(sy.size))
            self.assertEqual(len(lx3), int(ly3.size))
            self.assertEqual(len(sx3), int(sy3.size))
            self.assertGreater(int(ly.size), 0)
            self.assertGreater(int(ly3.size), 0)
            inject_coin_html(page)
            after = page.read_text(encoding="utf-8")
            self.assertIn("gear22-floors-injected-start", after)
            self.assertTrue(_html_has(after, "tf-select α25"))
            self.assertIn("p5–p95", after)
            self.assertIn("p1–p99", after)
            self.assertIn('"name":"SMA-3"', after)
            self.assertNotIn('"name":"SMA-12"', after)
            self.assertIn("SMA-12", after)
            self.assertNotIn("median 3h", after)
            self.assertNotIn("median 12h", after)
            self.assertFalse(_html_has(after, "tf-select med"))
            self.assertFalse(_html_has(after, "trim 3h α25"))
            self.assertFalse(_html_has(after, "trim 12h α25"))
            self.assertNotIn("floor median of SMA-12", after)
            self.assertNotIn("median / mean / trim10", after)
            self.assertIn("long 5m OHLC", after)
            self.assertIn("short tw_p99", after)
            self.assertIn("one corridor graph per side", after)
            self.assertIn("rgba(140, 170, 200, 0.28)", after)
            self.assertEqual(after.count("gear22-floors-injected-start"), 2)
            # Idempotent: second pass still one pair of inject blocks.
            inject_coin_html(page)
            again = page.read_text(encoding="utf-8")
            self.assertEqual(
                again.count("gear22-floors-injected-start"), 2
            )


if __name__ == "__main__":
    unittest.main()
