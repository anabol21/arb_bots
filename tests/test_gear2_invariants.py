"""Gear 2 Stage 2/3: K=1 occupancy, pending-skip counter, A vs B open-only gate."""

from __future__ import annotations

import unittest

import pandas as pd

from research.gear2_backtest import (
    assert_k1_invariants,
    closeout_row,
    run_backtest_market,
)
from research.gear2_regime_topn import completed_bar_start_ms

VARIATION = {
    "thresh_open_long": 0.5,
    "thresh_open_short": 0.5,
    "thresh_close_long": 0.5,
    "thresh_close_short": 0.5,
    "open_frac": 0.7,
    "close_frac": 0.7,
}

HYPER_LOOSE = {
    "max_freshness_ms": None,
    "max_latency_okx_ms": 10_000.0,
    "max_latency_bybit_ms": 10_000.0,
    "avg_window_sec": None,
    "Trade_Lat": 100,
    "Check_volume": False,
    "position_size": 10.0,
    "position_frac": 1.0,
    "fee_rate": 0.0,
    "reject_fill_across_gap": False,
    "gap_fill_slack_ms": 1000,
}


def _synth_row(ts, coin, sl, ss):
    return {
        "event_local_ts_ms": ts,
        "base_coin": coin,
        "spread_long": sl,
        "spread_short": ss,
        "okx_latency_ms": 1.0,
        "bybit_latency_ms": 1.0,
        "event_dt": pd.Timestamp(ts, unit="ms", tz="UTC"),
        "okx_bid_size": 100.0,
        "okx_ask_size": 100.0,
        "bybit_bid_size": 100.0,
        "bybit_ask_size": 100.0,
    }


class K1NoOverlapTests(unittest.TestCase):
    def test_two_coins_one_slot_no_overlap(self) -> None:
        synth = pd.DataFrame(
            [
                _synth_row(0, "AAA", 2.0, 0.0),
                _synth_row(50, "BBB", 2.0, 0.0),  # pending-skip (other coin)
                _synth_row(200, "AAA", 2.0, 0.0),  # fill AAA open
                _synth_row(250, "BBB", 2.0, 0.0),  # slot_busy
                _synth_row(400, "AAA", 0.0, 2.0),  # close long
                _synth_row(550, "AAA", 0.0, 2.0),  # fill close
            ]
        )
        res = run_backtest_market(synth, variation=VARIATION, hyper=HYPER_LOOSE, k=1)
        assert_k1_invariants(res)
        closed = [t for t in res.trades if t.status == "closed"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].base_coin, "AAA")
        self.assertIsNone(res.open_position)
        self.assertEqual(sum(1 for t in res.trades if t.status == "open"), 0)

    def test_k_not_one_rejected(self) -> None:
        df = pd.DataFrame([_synth_row(0, "AAA", 2.0, 0.0)])
        with self.assertRaises(ValueError):
            run_backtest_market(df, variation=VARIATION, hyper=HYPER_LOOSE, k=2)


class PendingSkipTests(unittest.TestCase):
    def test_other_coin_during_pending_increments_pending_skip_not_slot_busy(self) -> None:
        synth = pd.DataFrame(
            [
                _synth_row(0, "AAA", 2.0, 0.0),
                _synth_row(50, "BBB", 2.0, 0.0),  # during pending open
                _synth_row(80, "CCC", 0.0, 2.0),  # during pending, short thresh
                _synth_row(90, "DDD", 0.0, 0.0),  # during pending, below thresh
                _synth_row(100, "AAA", 2.0, 0.0),  # same coin, not pending-skip
                _synth_row(200, "AAA", 2.0, 0.0),  # fill
                _synth_row(250, "BBB", 2.0, 0.0),  # held → slot_busy
            ]
        )
        res = run_backtest_market(synth, variation=VARIATION, hyper=HYPER_LOOSE, k=1)
        assert_k1_invariants(res)
        self.assertEqual(res.n_filtered_pending_skip, 2)
        self.assertGreaterEqual(res.n_filtered_slot_busy, 1)
        self.assertEqual(res.open_position.base_coin if res.open_position else None, "AAA")

    def test_flag_off_trades_and_slot_busy_match_without_touching_elif(self) -> None:
        """New counter only: slot_busy / trades / raw / passed stay the v0 occupancy split."""
        synth = pd.DataFrame(
            [
                _synth_row(0, "AAA", 2.0, 0.0),
                _synth_row(50, "BBB", 2.0, 0.0),
                _synth_row(200, "AAA", 2.0, 0.0),
                _synth_row(250, "BBB", 2.0, 0.0),
                _synth_row(400, "AAA", 0.0, 2.0),
                _synth_row(550, "AAA", 0.0, 2.0),
            ]
        )
        res = run_backtest_market(
            synth, variation=VARIATION, hyper=HYPER_LOOSE, k=1, regime_topn=None
        )
        assert_k1_invariants(res)
        closed = [t for t in res.trades if t.status == "closed"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].base_coin, "AAA")
        self.assertEqual(res.n_filtered_slot_busy, 1)
        self.assertEqual(res.n_filtered_pending_skip, 1)
        self.assertEqual(res.n_filtered_not_topn, 0)
        self.assertGreaterEqual(res.n_signals_passed, 2)  # open + close


class ArmAvsBOpenOnlyTests(unittest.TestCase):
    def test_b_gates_open_only_not_close(self) -> None:
        # Separate 5m bars: open at t uses [t−5m, t); close bar must not collide.
        t_open = 1_200_000
        t_fill = t_open + 200
        t_close = 1_800_000
        t_cfill = t_close + 200
        topn = {
            completed_bar_start_ms(t_open): frozenset({"AAA"}),
            completed_bar_start_ms(t_fill): frozenset({"AAA"}),
            completed_bar_start_ms(t_close): frozenset({"ZZZ"}),
        }
        synth = pd.DataFrame(
            [
                _synth_row(t_open, "AAA", 2.0, 0.0),
                _synth_row(t_fill, "AAA", 2.0, 0.0),
                _synth_row(t_close, "AAA", 0.0, 2.0),
                _synth_row(t_cfill, "AAA", 0.0, 2.0),
            ]
        )
        arm_a = run_backtest_market(
            synth, variation=VARIATION, hyper=HYPER_LOOSE, k=1, regime_topn=None
        )
        arm_b = run_backtest_market(
            synth, variation=VARIATION, hyper=HYPER_LOOSE, k=1, regime_topn=topn
        )
        assert_k1_invariants(arm_a)
        assert_k1_invariants(arm_b)
        self.assertEqual(arm_a.n_filtered_not_topn, 0)
        self.assertEqual(arm_b.n_filtered_not_topn, 0)  # open was in Top-N
        closed_a = [t for t in arm_a.trades if t.status == "closed"]
        closed_b = [t for t in arm_b.trades if t.status == "closed"]
        self.assertEqual(len(closed_a), 1)
        self.assertEqual(len(closed_b), 1)

    def test_b_blocks_open_when_not_in_topn_a_does_not(self) -> None:
        b0 = completed_bar_start_ms(0)
        topn = {b0: frozenset({"AAA"})}
        synth = pd.DataFrame(
            [
                _synth_row(0, "BBB", 2.0, 0.0),
                _synth_row(200, "BBB", 2.0, 0.0),
            ]
        )
        arm_a = run_backtest_market(
            synth, variation=VARIATION, hyper=HYPER_LOOSE, k=1, regime_topn=None
        )
        arm_b = run_backtest_market(
            synth, variation=VARIATION, hyper=HYPER_LOOSE, k=1, regime_topn=topn
        )
        self.assertGreaterEqual(arm_b.n_filtered_not_topn, 1)
        self.assertEqual(arm_a.n_filtered_not_topn, 0)
        self.assertIsNone(arm_b.open_position)
        self.assertFalse(any(t.status == "closed" for t in arm_b.trades))
        self.assertTrue(
            arm_a.open_position is not None
            or any(t.status == "closed" for t in arm_a.trades)
        )
        row_a = closeout_row("A", arm_a)
        row_b = closeout_row("B", arm_b)
        self.assertNotIn("metric", row_a)
        self.assertNotIn("pnl", row_b)
        self.assertGreater(row_b["not_topn"], row_a["not_topn"])


if __name__ == "__main__":
    unittest.main()


class ChunkedCarryTests(unittest.TestCase):
    def test_chunked_matches_monolithic_across_boundary(self) -> None:
        """Open near boundary + fill/close in next chunk equals one-shot run."""
        from research.gear2_backtest import MarketCarryState, run_backtest_market

        # signal open at 900, fill at 1000; close signal 1500 fill 1600
        synth = pd.DataFrame(
            [
                _synth_row(800, "AAA", 0.0, 0.0),
                _synth_row(900, "AAA", 2.0, 0.0),
                _synth_row(950, "BBB", 2.0, 0.0),  # pending-skip
                _synth_row(1000, "AAA", 2.0, 0.0),
                _synth_row(1100, "BBB", 2.0, 0.0),  # slot_busy
                _synth_row(1500, "AAA", 0.0, 2.0),
                _synth_row(1600, "AAA", 0.0, 2.0),
            ]
        )
        mono = run_backtest_market(synth, variation=VARIATION, hyper=HYPER_LOOSE, k=1)
        assert_k1_invariants(mono)

        # chunk1: [0, 1050) with warmup none; chunk2: [1050, 2000)
        c1 = synth[synth["event_local_ts_ms"] < 1050]
        c2 = synth[synth["event_local_ts_ms"] >= 900]  # warmup overlap for MA (none here)
        r1, carry = run_backtest_market(
            c1,
            variation=VARIATION,
            hyper=HYPER_LOOSE,
            k=1,
            decision_start_ms=0,
            decision_end_ms=1050,
            return_carry=True,
            finalize_open=False,
        )
        self.assertIsNotNone(carry.whatpos)
        self.assertEqual(carry.held_coin, "AAA")
        r2, carry2 = run_backtest_market(
            c2,
            variation=VARIATION,
            hyper=HYPER_LOOSE,
            k=1,
            decision_start_ms=1050,
            decision_end_ms=2000,
            carry_in=carry,
            return_carry=True,
            finalize_open=True,
        )
        closed_m = [t for t in mono.trades if t.status == "closed"]
        closed_c = [t for t in (r1.trades + r2.trades) if t.status == "closed"]
        self.assertEqual(len(closed_m), 1)
        self.assertEqual(len(closed_c), 1)
        self.assertEqual(closed_m[0].base_coin, closed_c[0].base_coin)
        self.assertAlmostEqual(float(closed_m[0].open_ts), float(closed_c[0].open_ts))
        self.assertAlmostEqual(float(closed_m[0].close_ts), float(closed_c[0].close_ts))
        self.assertIsNone(carry2.whatpos)

    def test_pending_fill_crosses_chunk(self) -> None:
        from research.gear2_backtest import run_backtest_market

        synth = pd.DataFrame(
            [
                _synth_row(0, "AAA", 2.0, 0.0),  # signal; fill needs ts>=100
                _synth_row(50, "BBB", 2.0, 0.0),
                _synth_row(200, "AAA", 2.0, 0.0),  # fill in "next" chunk
            ]
        )
        c1 = synth[synth["event_local_ts_ms"] < 100]
        c2 = synth
        r1, carry = run_backtest_market(
            c1,
            variation=VARIATION,
            hyper=HYPER_LOOSE,
            k=1,
            decision_start_ms=0,
            decision_end_ms=100,
            return_carry=True,
            finalize_open=False,
        )
        self.assertIsNotNone(carry.pending)
        self.assertIsNone(carry.whatpos)
        r2, carry2 = run_backtest_market(
            c2,
            variation=VARIATION,
            hyper=HYPER_LOOSE,
            k=1,
            decision_start_ms=100,
            decision_end_ms=300,
            carry_in=carry,
            return_carry=True,
            finalize_open=True,
        )
        self.assertIsNotNone(carry2.whatpos or r2.open_position)
        self.assertEqual(
            (r2.open_position or type("T", (), {"base_coin": carry2.held_coin})).base_coin,
            "AAA",
        )
