"""Sign and boundary of the experiment-1 funding join. No hive, no network."""

from __future__ import annotations

import unittest

import numpy as np

from research.gear23_funding.exp1_funding import (
    FundingBook,
    VenueSeries,
    funding_for_hold,
    sum_rates,
)


def _series(*pairs: tuple[int, float]) -> VenueSeries:
    ts = np.array([p[0] for p in pairs], dtype=np.int64)
    rate = np.array([p[1] for p in pairs], dtype=np.float64)
    return VenueSeries(ts_s=ts, rate_pct=rate)


class FundingJoinTest(unittest.TestCase):
    def test_boundary_excludes_open_second_and_includes_close_second(self) -> None:
        series = _series((100, 1.0), (200, 2.0), (300, 4.0))
        total, n = sum_rates(series, 100, 200)
        self.assertEqual(n, 1)
        self.assertEqual(total, 2.0)
        total_end, n_end = sum_rates(series, 100, 300)
        self.assertEqual(n_end, 2)
        self.assertEqual(total_end, 6.0)

    def test_long_spread_receives_positive_bybit_and_pays_positive_okx(self) -> None:
        book = FundingBook(
            bybit={"AAA": _series((150, 0.50))},
            okx={"AAA": _series((150, 0.20))},
        )
        got = funding_for_hold(book, "AAA", "long", 100, 200)
        self.assertFalse(got.missing)
        self.assertAlmostEqual(got.funding_pp, 0.30)
        self.assertEqual((got.n_bybit, got.n_okx), (1, 1))

    def test_short_spread_is_the_opposite_cash(self) -> None:
        book = FundingBook(
            bybit={"AAA": _series((150, 0.50))},
            okx={"AAA": _series((150, 0.20))},
        )
        got = funding_for_hold(book, "AAA", "short", 100, 200)
        self.assertAlmostEqual(got.funding_pp, -0.30)

    def test_negative_bybit_rate_costs_the_short_bybit_leg(self) -> None:
        book = FundingBook(
            bybit={"AAA": _series((150, -4.0))},
            okx={"AAA": _series((150, 0.0))},
        )
        got = funding_for_hold(book, "AAA", "long", 100, 200)
        self.assertAlmostEqual(got.funding_pp, -4.0)

    def test_missing_venue_is_not_a_zero(self) -> None:
        book = FundingBook(bybit={}, okx={})
        got = funding_for_hold(book, "ICX", "long", 100, 200)
        self.assertTrue(got.missing)
        self.assertIsNone(got.funding_pp)

    def test_no_print_inside_the_hold_is_a_real_zero(self) -> None:
        book = FundingBook(
            bybit={"AAA": _series((50, 1.0))},
            okx={"AAA": _series((50, 1.0))},
        )
        got = funding_for_hold(book, "AAA", "long", 100, 200)
        self.assertFalse(got.missing)
        self.assertEqual(got.funding_pp, 0.0)
        self.assertEqual(got.n_bybit + got.n_okx, 0)


if __name__ == "__main__":
    unittest.main()
