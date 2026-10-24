"""Hermetic checks for the experiment-2 clocks. No hive and no settlement file."""

from __future__ import annotations

import unittest

import numpy as np

from research.gear23_funding.exp1_funding import FundingBook, VenueSeries
from research.gear23_funding.exp2_timing import (
    TradeClock,
    manual_funding_pp,
    overlaps,
    sample_gaps,
    sample_independent,
    sample_k1,
    schedule_funding,
    streams,
)


def _book() -> FundingBook:
    ts = np.array([10, 20, 30], dtype=np.int64)
    bybit = np.array([1.0, -2.0, 0.5], dtype=np.float64)
    okx = np.array([0.25, 0.25, 0.25], dtype=np.float64)
    return FundingBook(
        bybit={"AAA": VenueSeries(ts, bybit)},
        okx={"AAA": VenueSeries(ts, okx)},
    )


class GapAndClockTest(unittest.TestCase):
    def test_gaps_sum_and_stay_non_negative(self) -> None:
        rng = np.random.default_rng(1)
        for _ in range(30):
            gaps = sample_gaps(10, 4, rng)
            self.assertEqual(int(gaps.sum()), 10)
            self.assertTrue(bool(np.all(gaps >= 0)))

    def test_one_trade_opens_are_uniform(self) -> None:
        rng = np.random.default_rng(2)
        durations = np.array([2], dtype=np.int64)
        counts = {0: 0, 1: 0, 2: 0, 3: 0}
        for _ in range(20000):
            open_ts = int(sample_k1(durations, 0, 5, rng)[0])
            counts[open_ts] += 1
        for count in counts.values():
            self.assertLess(abs(count / 20000 - 0.25), 0.02)

    def test_two_trade_schedules_are_uniform(self) -> None:
        rng = np.random.default_rng(3)
        durations = np.array([1, 1], dtype=np.int64)
        counts: dict[tuple[int, int], int] = {}
        n = 36000
        for _ in range(n):
            opens = sample_k1(durations, 0, 4, rng)
            key = (int(opens[0]), int(opens[1]))
            counts[key] = counts.get(key, 0) + 1
        self.assertEqual(len(counts), 12)
        for count in counts.values():
            self.assertLess(abs(count / n - 1 / 12), 0.015)

    def test_k1_keeps_identity_and_forbids_overlap(self) -> None:
        rng = np.random.default_rng(4)
        durations = np.array([100, 40, 5], dtype=np.int64)
        opens = sample_k1(durations, 1_000, 2_000, rng)
        self.assertFalse(overlaps(opens, durations))
        self.assertTrue(bool(np.all(opens >= 1_000)))
        self.assertTrue(bool(np.all(opens + durations <= 2_000)))
        self.assertEqual(opens.shape[0], 3)

    def test_k1_refuses_an_impossible_packing(self) -> None:
        rng = np.random.default_rng(5)
        with self.assertRaises(RuntimeError):
            sample_k1(np.array([8, 8], dtype=np.int64), 0, 10, rng)

    def test_independent_allows_overlap_and_stays_inside(self) -> None:
        rng = np.random.default_rng(6)
        durations = np.array([8, 8], dtype=np.int64)
        saw_overlap = False
        for _ in range(30):
            opens = sample_independent(durations, 0, 10, rng)
            self.assertTrue(bool(np.all(opens >= 0)))
            self.assertTrue(bool(np.all(opens + durations <= 10)))
            saw_overlap = saw_overlap or overlaps(opens, durations)
        self.assertTrue(saw_overlap)

    def test_same_seed_repeats_the_k1_draw(self) -> None:
        durations = np.array([3, 4, 5], dtype=np.int64)
        first = sample_k1(durations, 0, 40, streams(11)[1])
        second = sample_k1(durations, 0, 40, streams(11)[1])
        self.assertTrue(np.array_equal(first, second))

    def test_manual_walk_matches_the_experiment1_rule(self) -> None:
        book = _book()
        trades = [
            TradeClock(0, "AAA", "long", 25, 0, 25, 0.0),
        ]
        total, parts = schedule_funding(book, trades, np.array([0], dtype=np.int64))
        # prints at 10 and 20: long cash (1 - 0.25) + (-2 - 0.25) = -1.5
        self.assertAlmostEqual(manual_funding_pp(book, "AAA", "long", 0, 25), -1.5)
        self.assertAlmostEqual(parts[0], -1.5)
        self.assertAlmostEqual(total, -1.5)
        # open second excluded, close second included
        self.assertAlmostEqual(manual_funding_pp(book, "AAA", "short", 10, 30), -(-2.0 + 0.5) + (0.25 + 0.25))


if __name__ == "__main__":
    unittest.main()
