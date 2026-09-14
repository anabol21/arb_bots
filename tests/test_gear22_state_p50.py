"""Hermetic tests for locked gear-2.2 rolling / closed-bar TW-p50."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from research.gear22_quiet_regime_viz.quantiles import (
    ROLL_P50_MIN_MASS_FRAC,
    WINDOW_1M_MS,
    WINDOW_5M_MS,
    eval_grid_ms,
    rolling_tw_p50,
    rolling_tw_window_stats,
    tick_hold_weights_ms,
    time_weighted_quantiles,
    tw_p50,
)
from research.gear22_quiet_regime_viz.state_p50 import (
    build_state_p50_series,
    p50_bar5m_from_buckets,
    write_state_p50_html,
)


class TestTwP50MatchesExisting(unittest.TestCase):
    def test_hold_weights_median_is_tw_p50(self) -> None:
        ts = np.asarray([0, 10], dtype="int64")
        y = np.asarray([1.0, 2.0], dtype="float64")
        w = tick_hold_weights_ms(ts, last_end_ms=100)
        self.assertEqual(list(w), [10.0, 90.0])
        self.assertEqual(tw_p50(y, w), 2.0)
        self.assertEqual(time_weighted_quantiles(y, w)["tw_p50"], 2.0)


class TestRollingTwP50(unittest.TestCase):
    def test_known_median_at_eval(self) -> None:
        # Value 1 for 10ms, then 2 for 90ms → TW p50 = 2 at t=100, W=100.
        ts = np.asarray([0, 10], dtype="int64")
        y = np.asarray([1.0, 2.0], dtype="float64")
        out = rolling_tw_p50(
            ts,
            y,
            window_ms=100,
            eval_ts_ms=np.asarray([100], dtype="int64"),
            min_mass_frac=0.20,
            min_ticks=2,
        )
        self.assertEqual(out.shape, (1,))
        self.assertEqual(float(out[0]), 2.0)

    def test_causal_future_ticks_ignored(self) -> None:
        ts = np.asarray([0, 10, 50], dtype="int64")
        y = np.asarray([1.0, 2.0, 99.0], dtype="float64")
        out = rolling_tw_p50(
            ts,
            y,
            window_ms=100,
            eval_ts_ms=np.asarray([40], dtype="int64"),
            min_mass_frac=0.20,
            min_ticks=2,
        )
        # At t=40 only ticks 0 and 10: holds 10ms of 1, 30ms of 2 → p50=2.
        self.assertEqual(float(out[0]), 2.0)

    def test_nan_when_mass_too_small(self) -> None:
        # One tick at t=0, eval at t=5: mass=5ms < 20% of W=100.
        ts = np.asarray([0], dtype="int64")
        y = np.asarray([1.0], dtype="float64")
        out = rolling_tw_p50(
            ts,
            y,
            window_ms=100,
            eval_ts_ms=np.asarray([5], dtype="int64"),
            min_mass_frac=0.20,
            min_ticks=1,
        )
        self.assertTrue(np.isnan(out[0]))

    def test_nan_when_fewer_than_min_ticks(self) -> None:
        ts = np.asarray([0], dtype="int64")
        y = np.asarray([1.0], dtype="float64")
        out = rolling_tw_p50(
            ts,
            y,
            window_ms=100,
            eval_ts_ms=np.asarray([50], dtype="int64"),
            min_mass_frac=0.20,
            min_ticks=2,
        )
        # Mass 50 >= 20, but only 1 positive-hold tick.
        self.assertTrue(np.isnan(out[0]))

    def test_leading_gap_has_no_mass(self) -> None:
        # Window [0, 100], first tick at 80: unobserved 80ms, hold 20ms of 7.
        ts = np.asarray([80], dtype="int64")
        y = np.asarray([7.0], dtype="float64")
        out = rolling_tw_p50(
            ts,
            y,
            window_ms=100,
            eval_ts_ms=np.asarray([100], dtype="int64"),
            min_mass_frac=0.20,
            min_ticks=1,
        )
        self.assertEqual(float(out[0]), 7.0)
        # Same setup but 15ms hold < 20% of 100 → NaN.
        out2 = rolling_tw_p50(
            ts,
            y,
            window_ms=100,
            eval_ts_ms=np.asarray([95], dtype="int64"),
            min_mass_frac=0.20,
            min_ticks=1,
        )
        self.assertTrue(np.isnan(out2[0]))

    def test_holes_are_hold_not_linear_interp(self) -> None:
        # Tick 1 at 0, tick 9 at 80, eval 100. Gap is a 80ms hold of 1, then 20ms of 9.
        ts = np.asarray([0, 80], dtype="int64")
        y = np.asarray([1.0, 9.0], dtype="float64")
        out = rolling_tw_p50(
            ts,
            y,
            window_ms=100,
            eval_ts_ms=np.asarray([100], dtype="int64"),
            min_mass_frac=0.20,
            min_ticks=2,
        )
        self.assertEqual(float(out[0]), 1.0)

    def test_default_eval_is_each_tick(self) -> None:
        ts = np.asarray([0, 30, 80], dtype="int64")
        y = np.asarray([1.0, 2.0, 3.0], dtype="float64")
        out = rolling_tw_p50(ts, y, window_ms=100, min_mass_frac=0.0, min_ticks=1)
        self.assertEqual(out.shape, (3,))
        # At first tick last-hold is 0 → mass 0 → NaN even with min_mass_frac=0
        # if the only tick has zero hold. min_ticks=1 still needs positive hold.
        self.assertTrue(np.isnan(out[0]))
        self.assertTrue(np.isfinite(out[1]))
        self.assertTrue(np.isfinite(out[2]))

    def test_rolling_5m_differs_from_closed_utc_bar(self) -> None:
        # UTC bar [0, 300000): 1.0 for first 60s, then 9.0 until bar end.
        ts = np.asarray([0, 60_000], dtype="int64")
        y = np.asarray([1.0, 9.0], dtype="float64")
        closed = tw_p50(
            y, tick_hold_weights_ms(ts, last_end_ms=300_000)
        )
        # Closed bar: 60s of 1 + 240s of 9 → p50 = 9.
        self.assertEqual(closed, 9.0)
        # Rolling 5m at t=60s: window almost all 1s (last hold of 1 is 0 at the
        # second tick time if we eval at 60_000 using both ticks... eval at 59_000).
        roll = rolling_tw_p50(
            ts,
            y,
            window_ms=WINDOW_5M_MS,
            eval_ts_ms=np.asarray([59_000], dtype="int64"),
            min_mass_frac=0.10,
            min_ticks=1,
        )
        self.assertEqual(float(roll[0]), 1.0)
        self.assertNotEqual(float(roll[0]), closed)


class TestEvalGridAndBarHelper(unittest.TestCase):
    def test_eval_grid_half_open(self) -> None:
        g = eval_grid_ms(0, 5_000, 1_000)
        self.assertEqual(list(g), [0, 1000, 2000, 3000, 4000])

    def test_p50_bar5m_uses_tw_p50_at_bar_end(self) -> None:
        buckets = pd.DataFrame(
            {
                "bar_start_ms": [0],
                "bar_end_ms": [300_000],
                "tw_p50": [1.5],
            }
        )
        bar = p50_bar5m_from_buckets(buckets)
        self.assertEqual(int(bar["bar_end_ms"].iloc[0]), 300_000)
        self.assertEqual(float(bar["p50_bar5m"].iloc[0]), 1.5)

    def test_constants_match_lock(self) -> None:
        self.assertEqual(WINDOW_1M_MS, 60_000)
        self.assertEqual(WINDOW_5M_MS, 300_000)
        self.assertEqual(ROLL_P50_MIN_MASS_FRAC, 0.20)


class TestStateP50HtmlSmoke(unittest.TestCase):
    def test_series_and_html_contain_three_names(self) -> None:
        # 3 minutes of 1s ticks, values 1 then a jump, covering one 5m lookback.
        ts = np.arange(0, 180_000, 1_000, dtype="int64")
        y_long = np.where(ts < 90_000, 1.0, 3.0)
        y_short = np.where(ts < 90_000, 2.0, 4.0)
        ticks = pd.DataFrame(
            {
                "event_local_ts_ms": np.concatenate([ts, ts]),
                "event_dt": pd.to_datetime(np.concatenate([ts, ts]), unit="ms", utc=True),
                "base_coin": ["SOL"] * (2 * ts.size),
                "spread_long": np.concatenate([y_long, y_long]),
                "spread_short": np.concatenate([y_short, y_short]),
            }
        )
        # Unique timestamps once (duplicate rows would confuse counts, not TW).
        ticks = ticks.drop_duplicates("event_local_ts_ms").reset_index(drop=True)
        packed = build_state_p50_series(
            ticks,
            value_col="spread_long",
            plot_start_ms=0,
            plot_end_ms=180_000,
            eval_step_ms=1_000,
        )
        self.assertTrue(np.isfinite(packed["p50_roll_1m"]).any())
        self.assertTrue(np.isfinite(packed["p50_roll_5m"]).any())
        series_by_side = {
            "long": packed,
            "short": build_state_p50_series(
                ticks,
                value_col="spread_short",
                plot_start_ms=0,
                plot_end_ms=180_000,
                eval_step_ms=1_000,
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gear22_state_p50_SOL.html"
            write_state_p50_html(
                path,
                coin="SOL",
                ticks=ticks,
                series_by_side=series_by_side,
                meta={"coin": "SOL", "since": "synthetic"},
                inline_plotly=True,
            )
            text = path.read_text(encoding="utf-8")

            def _has(needle: str) -> bool:
                if needle in text:
                    return True
                escaped = needle.encode("ascii", "backslashreplace").decode("ascii")
                return escaped in text

            self.assertTrue(_has("p50_bar5m"))
            self.assertTrue(_has("p50_roll_1m"))
            self.assertTrue(_has("p50_roll_5m"))
            self.assertTrue('"shape":"hv"' in text or '"shape": "hv"' in text)


class TestRollingTwWindowStats(unittest.TestCase):
    def test_p50_matches_rolling_tw_p50(self) -> None:
        ts = np.asarray([0, 10, 40, 80], dtype="int64")
        y = np.asarray([1.0, 2.0, 3.0, 4.0], dtype="float64")
        evals = np.asarray([50, 100], dtype="int64")
        a = rolling_tw_p50(ts, y, window_ms=100, eval_ts_ms=evals)
        b = rolling_tw_window_stats(ts, y, window_ms=100, eval_ts_ms=evals)
        np.testing.assert_array_equal(a, b.p50)

    def test_occupancy_mass_fraction(self) -> None:
        # y=1 for 10ms, y=2 for 90ms; threshold 1.5 → only y=2 occupies.
        ts = np.asarray([0, 10], dtype="int64")
        y = np.asarray([1.0, 2.0], dtype="float64")
        evals = np.asarray([100], dtype="int64")
        thr = np.asarray([1.5], dtype="float64")
        st = rolling_tw_window_stats(
            ts, y, window_ms=100, eval_ts_ms=evals, occ_threshold=thr
        )
        self.assertEqual(float(st.p50[0]), 2.0)
        self.assertAlmostEqual(float(st.cov[0]), 1.0)
        self.assertAlmostEqual(float(st.occ[0]), 0.90)
        self.assertEqual(int(st.n_ticks[0]), 2)

    def test_occ_nan_when_threshold_nan(self) -> None:
        ts = np.asarray([0, 10], dtype="int64")
        y = np.asarray([1.0, 2.0], dtype="float64")
        evals = np.asarray([100], dtype="int64")
        thr = np.asarray([np.nan], dtype="float64")
        st = rolling_tw_window_stats(
            ts, y, window_ms=100, eval_ts_ms=evals, occ_threshold=thr
        )
        self.assertTrue(np.isnan(st.occ[0]))


if __name__ == "__main__":
    unittest.main()
