"""Hermetic checks for local funding shifts. No hive and no settlement file."""

from __future__ import annotations

import unittest

import numpy as np

from research.gear23_funding.exp1_funding import FundingBook, VenueSeries, funding_for_hold
from research.gear23_funding.exp2_timing import TradeClock, manual_funding_pp
from research.gear23_funding.exp3_local import (
    event_key,
    event_set_constant,
    funding_for_opens,
    history_bounds,
    local_summary,
    local_support,
    sample_local_opens,
)


def _book() -> FundingBook:
    ts = np.array([10, 20, 30], dtype=np.int64)
    bybit = np.array([1.0, -2.0, 0.5], dtype=np.float64)
    okx = np.array([0.25, 0.25, 0.25], dtype=np.float64)
    return FundingBook(
        bybit={"AAA": VenueSeries(ts, bybit)},
        okx={"AAA": VenueSeries(ts, okx)},
    )


class LocalSupportTest(unittest.TestCase):
    def test_interior_window_is_the_full_radius(self) -> None:
        lo, hi = local_support(100, 2, open_lo=0, open_hi=1_000)
        self.assertEqual((lo, hi), (98, 102))

    def test_boundary_keeps_only_feasible_seconds(self) -> None:
        # History starts at open 9. Radius 5 would ask for 5..15.
        # The draw is uniform on 9..15, not a clipped uniform that piles up at 9.
        lo, hi = local_support(10, 5, open_lo=9, open_hi=1_000)
        self.assertEqual((lo, hi), (9, 15))
        rng = np.random.default_rng(1)
        draws = [
            int(sample_local_opens(np.array([10]), 5, np.array([9]), np.array([1_000]), rng)[0])
            for _ in range(21_000)
        ]
        counts = {second: 0 for second in range(9, 16)}
        for second in draws:
            self.assertIn(second, counts)
            counts[second] += 1
        for count in counts.values():
            self.assertLess(abs(count / 21_000 - 1 / 7), 0.015)

    def test_close_boundary_limits_the_open(self) -> None:
        # duration is applied by the caller: open_hi = history_end - duration.
        lo, hi = local_support(100, 5, open_lo=0, open_hi=103)
        self.assertEqual((lo, hi), (95, 103))

    def test_empty_intersection_raises(self) -> None:
        with self.assertRaises(ValueError):
            local_support(10, 1, open_lo=20, open_hi=30)

    def test_same_seed_repeats(self) -> None:
        kwargs = dict(
            ts_open=np.array([100, 200], dtype=np.int64),
            radius_s=4,
            open_lo=np.array([0, 0], dtype=np.int64),
            open_hi=np.array([500, 500], dtype=np.int64),
        )
        first = sample_local_opens(**kwargs, rng=np.random.default_rng(7))
        second = sample_local_opens(**kwargs, rng=np.random.default_rng(7))
        self.assertTrue(np.array_equal(first, second))


class HistoryBoundTest(unittest.TestCase):
    def test_a_live_series_may_use_the_scanned_tail(self) -> None:
        # Prints every 10s. The first and last sit within one gap of the horizon.
        ts = np.arange(0, 101, 10, dtype=np.int64)
        book = FundingBook(
            bybit={"AAA": VenueSeries(ts, np.zeros(ts.shape[0]))},
            okx={"AAA": VenueSeries(ts, np.zeros(ts.shape[0]))},
        )
        window = history_bounds(book, "AAA", horizon_lo=-5, horizon_hi=105)
        self.assertEqual(window.open_lo, -5)
        self.assertEqual(window.close_hi, 105)

    def test_a_series_that_ended_stops_at_its_last_print(self) -> None:
        ts = np.arange(0, 50, 10, dtype=np.int64)
        book = FundingBook(
            bybit={"AAA": VenueSeries(ts, np.zeros(ts.shape[0]))},
            okx={"AAA": VenueSeries(ts, np.zeros(ts.shape[0]))},
        )
        window = history_bounds(book, "AAA", horizon_lo=-10_000, horizon_hi=10_000)
        self.assertEqual(window.open_lo, 0)
        self.assertEqual(window.close_hi, 40)

    def test_the_later_venue_binds_the_start(self) -> None:
        early = np.arange(0, 101, 10, dtype=np.int64)
        late = np.arange(30, 101, 10, dtype=np.int64)
        book = FundingBook(
            bybit={"AAA": VenueSeries(early, np.zeros(early.shape[0]))},
            okx={"AAA": VenueSeries(late, np.zeros(late.shape[0]))},
        )
        window = history_bounds(book, "AAA", horizon_lo=-5, horizon_hi=105)
        self.assertEqual(window.open_lo, 30)
        self.assertEqual(window.close_hi, 105)


class LocalFundingTest(unittest.TestCase):
    def test_vector_sum_matches_the_hold_rule(self) -> None:
        book = _book()
        opens = np.array([0, 5, 10, 15], dtype=np.int64)
        got = funding_for_opens(book, "AAA", "long", 25, opens)
        for open_ts, value in zip(opens, got):
            manual = manual_funding_pp(book, "AAA", "long", int(open_ts), int(open_ts) + 25)
            single = funding_for_hold(book, "AAA", "long", int(open_ts), int(open_ts) + 25)
            self.assertAlmostEqual(float(value), manual)
            self.assertAlmostEqual(float(value), float(single.funding_pp))

    def test_gap_shift_does_not_change_the_event_set(self) -> None:
        ts = np.array([0, 100], dtype=np.int64)
        rates = np.array([1.0, 1.0], dtype=np.float64)
        book = FundingBook(
            bybit={"AAA": VenueSeries(ts, rates)},
            okx={"AAA": VenueSeries(ts, rates)},
        )
        trade_open = 40
        duration = 10
        lo, hi = local_support(trade_open, 5, open_lo=0, open_hi=80)
        self.assertTrue(event_set_constant(book, "AAA", duration, lo, hi))
        opens = np.arange(lo, hi + 1, dtype=np.int64)
        values = funding_for_opens(book, "AAA", "long", duration, opens)
        self.assertTrue(bool(np.all(values == 0.0)))
        key = event_key(book, "AAA", trade_open, trade_open + duration)
        for open_ts in opens:
            self.assertEqual(event_key(book, "AAA", int(open_ts), int(open_ts) + duration), key)

    def test_crossing_a_print_changes_the_set(self) -> None:
        ts = np.array([50], dtype=np.int64)
        book = FundingBook(
            bybit={"AAA": VenueSeries(ts, np.array([1.0]))},
            okx={"AAA": VenueSeries(ts, np.array([0.0]))},
        )
        # Actual hold 40..60 includes 50. A shift of -15 opens at 25 and closes at 45.
        self.assertFalse(event_set_constant(book, "AAA", 20, 25, 40))
        included = funding_for_opens(book, "AAA", "long", 20, np.array([40], dtype=np.int64))
        excluded = funding_for_opens(book, "AAA", "long", 20, np.array([25], dtype=np.int64))
        self.assertAlmostEqual(float(included[0]), 1.0)
        self.assertAlmostEqual(float(excluded[0]), 0.0)

    def test_summary_reports_the_tail_share_not_a_padded_probability(self) -> None:
        samples = np.array([-2.0, -1.0, 0.0, 1.0], dtype=np.float64)
        summary = local_summary(samples, actual=-1.0)
        self.assertEqual(summary["share_le_actual"], 0.5)
        self.assertEqual(summary["share_negative"], 0.5)
        self.assertEqual(summary["share_exact"], 0.25)
        self.assertAlmostEqual(summary["delta_actual_minus_median"], -0.5)
        self.assertNotIn("p_add_one", summary)


if __name__ == "__main__":
    unittest.main()
