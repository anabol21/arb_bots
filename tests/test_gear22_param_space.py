"""Hermetic checks for the gear-2.2 parameter-space grid. No hive load."""

from __future__ import annotations

import unittest

from research.gear22_backtest.replay import ClosedTrade
from research.gear22_backtest.sweep import RunResult
from research.gear22_backtest.venue_ledger import LedgerStep
from research.gear22_backtest.venue_param_space import (
    FROZEN_POINT,
    MIN_PROFIT_PP,
    P50_OPEN,
    THETA_OPEN,
    CellRun,
    cell_metrics,
    cell_stem,
    format_eta,
    render_volume_html,
    grid_points,
    mark_keys_for_holds,
    position_time_ratio,
    remaining_seconds,
    reusable_cells,
)


def _closed(pnl_b, pnl_o, fee_b, fee_o, ts_open, ts_close, coin="LA") -> LedgerStep:
    return LedgerStep(
        status="closed",
        coin=coin,
        side="long",
        ts_open=ts_open,
        ts_close=ts_close,
        notional=100.0,
        potential_pp=0.4,
        cash_bybit_after_open=None,
        cash_okx_after_open=None,
        cash_bybit_after_close=None,
        cash_okx_after_close=None,
        cash_bybit=100.0,
        cash_okx=100.0,
        equity_bybit=100.0,
        equity_okx=100.0,
        pnl_bybit=pnl_b,
        pnl_okx=pnl_o,
        fee_bybit=fee_b,
        fee_okx=fee_o,
    )


class TestGrid(unittest.TestCase):
    def test_size_and_frozen_point(self) -> None:
        points = grid_points()
        self.assertEqual(len(points), 13 * 17 * 10)
        self.assertEqual(len(THETA_OPEN), 13)
        self.assertEqual(len(P50_OPEN), 17)
        self.assertEqual(THETA_OPEN[0], 0.20)
        self.assertEqual(THETA_OPEN[-1], 0.80)
        self.assertEqual(P50_OPEN[0], 0.20)
        self.assertEqual(P50_OPEN[-1], 1.00)
        self.assertEqual(len(MIN_PROFIT_PP), 10)
        self.assertIn(0.25, MIN_PROFIT_PP)
        self.assertNotIn(0.26, MIN_PROFIT_PP)
        self.assertIn(FROZEN_POINT, points)

    def test_cell_stem_keeps_two_decimals(self) -> None:
        self.assertEqual(cell_stem(0.50, 0.60, 0.25), "t0.50_p0.60_m0.25")
        self.assertEqual(cell_stem(0.5, 0.6, 0.2), "t0.50_p0.60_m0.20")


class TestVolumePage(unittest.TestCase):
    def test_inlines_grid_and_links_cells(self) -> None:
        page = render_volume_html(
            {
                "metric_labels": {"total_profit": "Суммарная прибыль, $"},
                "cells": [
                    {
                        "i": 0,
                        "theta_open": 0.5,
                        "p50_open": 0.6,
                        "min_profit_pp": 0.2,
                        "total_profit": 61.22,
                        "frozen": True,
                        "href": "cells/t0.50_p0.60_m0.20.html",
                        "open_coin": None,
                        "open_side": None,
                        "top_coin": "ICX",
                    }
                ],
            }
        )
        self.assertIn("<canvas", page)
        self.assertIn("cells/t0.50_p0.60_m0.20.html", page)
        self.assertIn("index.html", page)
        self.assertNotIn("fetch(", page)


class TestEta(unittest.TestCase):
    def test_remaining_is_mean_times_left(self) -> None:
        self.assertEqual(remaining_seconds([10.0, 20.0, 30.0], 3, 250), 20.0 * 247)
        self.assertIn("ч", format_eta(20.0 * 247))


class TestMetrics(unittest.TestCase):
    def test_applied_closes_only(self) -> None:
        steps = [
            _closed(2.0, 1.0, 0.4, 0.2, 1_000, 1_000 + 3600),
            _closed(0.5, 0.5, 0.1, 0.1, 5_000, 5_000 + 1800, coin="ONT"),
            LedgerStep(
                status="price_mismatch",
                coin="CAP",
                side="long",
                ts_open=9_000,
                ts_close=9_100,
                notional=None,
                potential_pp=0.3,
                cash_bybit_after_open=None,
                cash_okx_after_open=None,
                cash_bybit_after_close=None,
                cash_okx_after_close=None,
                cash_bybit=100.0,
                cash_okx=100.0,
                equity_bybit=None,
                equity_okx=None,
                pnl_bybit=None,
                pnl_okx=None,
                fee_bybit=None,
                fee_okx=None,
            ),
            LedgerStep(
                status="open",
                coin="LA",
                side="short",
                ts_open=20_000,
                ts_close=None,
                notional=100.0,
                potential_pp=None,
                cash_bybit_after_open=99.0,
                cash_okx_after_open=99.0,
                cash_bybit_after_close=None,
                cash_okx_after_close=None,
                cash_bybit=99.0,
                cash_okx=99.0,
                equity_bybit=99.0,
                equity_okx=99.0,
                pnl_bybit=None,
                pnl_okx=None,
                fee_bybit=0.2,
                fee_okx=0.1,
            ),
        ]
        metrics = cell_metrics(steps)
        # nets: (2+1-0.4-0.2)=2.4 and (0.5+0.5-0.1-0.1)=0.8
        self.assertAlmostEqual(metrics["total_profit"], 3.2)
        self.assertAlmostEqual(metrics["avg_profit"], 1.6)
        self.assertEqual(metrics["n_trades"], 2)
        self.assertAlmostEqual(metrics["mean_hold_h"], (1.0 + 0.5) / 2)
        self.assertEqual(metrics["n_mismatch"], 1)
        self.assertEqual(metrics["open_coin"], "LA")
        self.assertEqual(metrics["open_side"], "short")
        self.assertEqual(metrics["n_coins"], 2)
        self.assertAlmostEqual(metrics["coin_top_share"], 0.5)
        self.assertEqual(metrics["top_coin"] in {"LA", "ONT"}, True)
        self.assertAlmostEqual(metrics["max_gap_h"], 2200 / 3600)
        self.assertIsNone(cell_metrics([]).get("coin_top_share") if False else cell_metrics([
            LedgerStep(
                status="price_mismatch",
                coin="CAP",
                side="long",
                ts_open=1,
                ts_close=2,
                notional=None,
                potential_pp=None,
                cash_bybit_after_open=None,
                cash_okx_after_open=None,
                cash_bybit_after_close=None,
                cash_okx_after_close=None,
                cash_bybit=100.0,
                cash_okx=100.0,
                equity_bybit=None,
                equity_okx=None,
                pnl_bybit=None,
                pnl_okx=None,
                fee_bybit=None,
                fee_okx=None,
            )
        ])["n_coins"])


class TestOccupancy(unittest.TestCase):
    def test_ratio_and_full_slot(self) -> None:
        self.assertAlmostEqual(position_time_ratio(100, 300), 0.5)
        self.assertIsNone(position_time_ratio(300, 300))

    def test_reuse_drops_off_grid(self) -> None:
        def cell(theta, p50, profit) -> CellRun:
            return CellRun(
                theta_open=theta,
                p50_open=p50,
                min_profit_pp=profit,
                run=RunResult([], None, None, None, 0.0, None, 0),
                open_fill_pp=None,
                open_exit_pp=None,
                combo_s=1.0,
            )
        kept = reusable_cells(
            [cell(0.50, 0.60, 0.20), cell(0.50, 0.60, 0.26), cell(0.20, 0.20, 0.15)],
            [(0.50, 0.60, 0.20), (0.20, 0.20, 0.15)],
        )
        self.assertEqual(
            [(c.theta_open, c.p50_open, c.min_profit_pp) for c in kept],
            [(0.50, 0.60, 0.20), (0.20, 0.20, 0.15)],
        )


class TestMarkKeys(unittest.TestCase):
    def test_marks_inside_hold_only(self) -> None:
        trade = ClosedTrade(
            coin="la",
            side="long",
            ts_open=100,
            ts_close=400,
            fill_spread_pp=0.4,
            exit_spread_pp=0.2,
            potential_pp=0.3,
            reason_open="open_long",
            reason_close="close_min_profit",
        )
        run = RunResult(
            trades=[trade],
            open_coin=None,
            open_side=None,
            open_ts=None,
            mtm_pp=0.0,
            mtm_ts=None,
            exposure_s=300,
        )
        keys = mark_keys_for_holds(run, first_bucket_s=0, last_bucket_s=900)
        self.assertIn(("LA", 299), keys)
        self.assertNotIn(("LA", 599), keys)


if __name__ == "__main__":
    unittest.main()
