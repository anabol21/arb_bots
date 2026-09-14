"""Hermetic tests for the gear-2.2 sweep harness.

The harness only earns trust from parity with `replay_frame`, so most tests
here assert that the two agree trade for trade on frames built to hit the
branches where a vectorized rewrite usually drifts: gate precedence, NaN
fail-closed, same-second close-then-open, and `hold_open_overlap`.
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from research.gear22_backtest.policy import PolicyParams
from research.gear22_backtest.sweep import (
    assert_parity,
    run_combo,
    store_from_frame,
    sweep,
)

_NOTEBOOK = PolicyParams(
    theta_open=0.3,
    p50_open=0.50,
    min_spread_open=None,
    min_profit_pp=0.3,
    min_theta_close=0,
    fee_round_trip_pp=0.30,
)


def _row(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = dict(
        ts_s=1_000_000,
        coin="SOL",
        p50_1m_long=0.10,
        p50_1m_short=0.10,
        floor_long=0.0,
        floor_short=0.0,
        theta_1m_long=0.0,
        theta_1m_short=0.0,
        spread_last_long=0.40,
        spread_last_short=0.40,
        usable_long=True,
        usable_short=True,
    )
    fields.update(overrides)
    return fields


def _qualify(**overrides: object) -> dict[str, object]:
    """Row whose long side passes the notebook open gates (fill 0.40)."""
    fields = dict(
        p50_1m_long=0.60,
        p50_1m_short=0.60,
        theta_1m_long=0.40,
        theta_1m_short=0.40,
    )
    fields.update(overrides)
    return _row(**fields)


def _closeable(**overrides: object) -> dict[str, object]:
    """Row that closes a long held at fill 0.40.

    The unwind runs on the short book, so `min_theta_close=0` needs
    `theta_1m_short > 0`, and `min_profit_pp=0.3` needs
    `0.40 + spread_last_short - 0.30 >= 0.3`. The long side stays below its
    open gates so `hold_open_overlap` does not fire.
    """
    fields = dict(theta_1m_short=0.10, spread_last_short=0.25)
    fields.update(overrides)
    return _row(**fields)


def _lax(**overrides: object) -> dict[str, object]:
    """Long side qualifies with a fatter fill (0.55) than `_qualify`."""
    fields = dict(
        p50_1m_long=0.60,
        p50_1m_short=0.60,
        theta_1m_long=0.40,
        theta_1m_short=0.40,
        spread_last_long=0.55,
    )
    fields.update(overrides)
    return _row(**fields)


def _frame(*rows: dict[str, object]) -> pd.DataFrame:
    return pd.DataFrame(list(rows))


class TestGear22SweepParity(unittest.TestCase):
    def test_open_then_close_matches_replay(self) -> None:
        df = _frame(_qualify(ts_s=1), _closeable(ts_s=2))
        self.assertEqual(assert_parity(df, _NOTEBOOK), 1)

    def test_long_side_nan_blocks_short_open(self) -> None:
        # policy.decide_open returns hold_nan on a long-side NaN without ever
        # evaluating the short side, even when short would qualify.
        df = _frame(
            _qualify(ts_s=1, theta_1m_long=float("nan")),
            _qualify(ts_s=2),
        )
        assert_parity(df, _NOTEBOOK)
        fast = run_combo(store_from_frame(df), _NOTEBOOK)
        self.assertEqual(fast.open_ts, 2)

    def test_gate_order_theta_before_p50(self) -> None:
        # theta NaN with p50 also below must report the theta gate first; the
        # distinction matters because NaN blocks the short side and below does not.
        df = _frame(
            _qualify(ts_s=1, theta_1m_long=float("nan"), p50_1m_long=-9.0),
            _qualify(ts_s=2, theta_1m_long=-9.0, p50_1m_long=float("nan")),
        )
        assert_parity(df, _NOTEBOOK)

    def test_same_second_close_then_other_coin_opens(self) -> None:
        df = _frame(
            _qualify(ts_s=1, coin="SOL"),
            _lax(ts_s=2, coin="BTC"),
            _closeable(ts_s=2, coin="SOL"),
            _closeable(ts_s=3, coin="BTC", spread_last_short=0.10),
        )
        self.assertEqual(assert_parity(df, _NOTEBOOK), 2)

    def test_lexicographically_earlier_coin_can_open_after_close(self) -> None:
        # The close row is scanned mid-group, but the whole second is rescanned
        # afterwards, so a coin sorting before the held one may still open.
        df = _frame(
            _qualify(ts_s=1, coin="SOL"),
            _lax(ts_s=2, coin="AAA"),
            _closeable(ts_s=2, coin="SOL"),
        )
        assert_parity(df, _NOTEBOOK)
        fast = run_combo(store_from_frame(df), _NOTEBOOK)
        self.assertEqual(fast.open_coin, "AAA")

    def test_just_closed_coin_cannot_reopen_same_second(self) -> None:
        # At ts=2 SOL closes the long and its short side qualifies to open, so
        # only the just-closed skip keeps it flat for that second.
        flip = dict(theta_1m_short=0.40, p50_1m_short=0.60, spread_last_short=0.25)
        df = _frame(
            _qualify(ts_s=1),
            _row(ts_s=2, **flip),
            _row(ts_s=3, **flip),
        )
        assert_parity(df, _NOTEBOOK)
        fast = run_combo(store_from_frame(df), _NOTEBOOK)
        self.assertEqual([t.ts_close for t in fast.trades], [2])
        self.assertEqual(fast.open_ts, 3)
        self.assertEqual(fast.open_side, "short")

    def test_open_overlap_holds_position(self) -> None:
        # Close gates pass and the position side still qualifies to open →
        # hold_open_overlap, so no trade is recorded.
        params = PolicyParams(
            theta_open=0.3,
            p50_open=0.50,
            min_profit_pp=0.0,
            min_theta_close=None,
            fee_round_trip_pp=0.30,
        )
        df = _frame(
            _qualify(ts_s=1),
            _qualify(ts_s=2, spread_last_short=0.50),
        )
        self.assertEqual(assert_parity(df, params), 0)

    def test_disabled_gates_match(self) -> None:
        params = PolicyParams(
            theta_open=None,
            p50_open=None,
            min_profit_pp=None,
            min_theta_close=None,
            min_spread_open=None,
        )
        df = _frame(
            _row(ts_s=1, spread_last_long=float("nan")),
            _row(ts_s=2),
            _row(ts_s=3, spread_last_short=float("nan")),
            _row(ts_s=4),
        )
        assert_parity(df, params)

    def test_min_spread_open_gate_matches(self) -> None:
        params = PolicyParams(
            theta_open=0.3,
            p50_open=0.50,
            min_spread_open=0.45,
            min_profit_pp=0.1,
            min_theta_close=0.0,
        )
        df = _frame(
            _qualify(ts_s=1, spread_last_long=0.40, spread_last_short=0.50),
            _qualify(ts_s=2, spread_last_long=0.60),
            _qualify(ts_s=3, spread_last_long=0.05, spread_last_short=0.05),
        )
        assert_parity(df, params)

    def test_not_usable_rows_never_open(self) -> None:
        df = _frame(
            _qualify(ts_s=1, usable_long=False, usable_short=False),
            _qualify(ts_s=2, usable_long=False),
            _qualify(ts_s=3),
        )
        assert_parity(df, _NOTEBOOK)

    def test_coin_major_input_order_is_not_load_bearing(self) -> None:
        rows = [
            _qualify(ts_s=1, coin="SOL"),
            _closeable(ts_s=2, coin="SOL"),
            _qualify(ts_s=1, coin="BTC"),
            _closeable(ts_s=2, coin="BTC"),
        ]
        coin_major = _frame(*rows)
        date_major = coin_major.sort_values(["ts_s", "coin"]).reset_index(drop=True)
        fast_a = run_combo(store_from_frame(coin_major), _NOTEBOOK)
        fast_b = run_combo(store_from_frame(date_major), _NOTEBOOK)
        self.assertEqual(
            [(t.coin, t.ts_open, t.ts_close) for t in fast_a.trades],
            [(t.coin, t.ts_open, t.ts_close) for t in fast_b.trades],
        )
        assert_parity(coin_major, _NOTEBOOK)

    def test_slot_persists_across_a_gap_in_time(self) -> None:
        df = _frame(_qualify(ts_s=1), _closeable(ts_s=90_000))
        self.assertEqual(assert_parity(df, _NOTEBOOK), 1)

    def test_float32_value_exactly_on_a_strict_gate(self) -> None:
        # Regression: float32(0.60) is 0.60000002384 in float64, so a strict
        # `p50_1m > 0.60` gate passes. Comparing in float32 instead (NEP 50
        # weak promotion of the threshold) makes it fail and shifts the open
        # by one row. Real p50 values are quantized and do land here.
        params = PolicyParams(
            theta_open=0.50,
            p50_open=0.60,
            min_profit_pp=0.20,
            min_theta_close=0.05,
            fee_round_trip_pp=0.30,
        )
        df = _frame(
            _row(
                ts_s=1,
                p50_1m_long=np.float32(0.60),
                theta_1m_long=np.float32(0.71),
                spread_last_long=np.float32(0.83),
            ),
            _closeable(ts_s=2, spread_last_short=np.float32(-0.07)),
        )
        df["p50_1m_long"] = df["p50_1m_long"].astype("float32")
        df["theta_1m_long"] = df["theta_1m_long"].astype("float32")
        df["spread_last_long"] = df["spread_last_long"].astype("float32")
        fast = run_combo(store_from_frame(df), params)
        self.assertEqual([t.ts_open for t in fast.trades], [1])
        assert_parity(df, params)

    def test_float32_value_exactly_on_a_close_profit_gate(self) -> None:
        # Mirror of the above on the close side: the profit gate must be
        # evaluated as fill + exit - fee >= min_profit in float64, not as a
        # rearranged float32 threshold on the exit spread.
        params = PolicyParams(
            theta_open=0.50,
            p50_open=0.55,
            min_profit_pp=0.20,
            min_theta_close=None,
            fee_round_trip_pp=0.30,
        )
        df = _frame(
            _row(
                ts_s=1,
                p50_1m_long=np.float32(0.60),
                theta_1m_long=np.float32(0.71),
                spread_last_long=np.float32(0.30),
            ),
            _row(ts_s=2, spread_last_short=np.float32(0.20)),
        )
        for column in ("p50_1m_long", "theta_1m_long", "spread_last_long",
                       "spread_last_short"):
            df[column] = df[column].astype("float32")
        assert_parity(df, params)


class TestGear22SweepFuzzParity(unittest.TestCase):
    def test_random_frames_match_replay(self) -> None:
        rng = np.random.default_rng(20260914)
        coins = ["AAA", "BBB", "CCC"]
        param_sets = (
            PolicyParams(),
            _NOTEBOOK,
            PolicyParams(theta_open=None, p50_open=None, min_profit_pp=None),
            PolicyParams(min_spread_open=0.2, min_profit_pp=0.1, min_theta_close=0.05),
        )
        for _ in range(12):
            n_ts = int(rng.integers(4, 25))
            n_coins = int(rng.integers(1, 4))
            used = coins[:n_coins]
            n = n_ts * len(used)

            def vals(scale: float) -> np.ndarray:
                v = rng.normal(0.0, scale, n).astype(np.float32)
                v[rng.random(n) < 0.2] = np.nan
                return v

            df = pd.DataFrame(
                {
                    "ts_s": np.repeat(np.arange(1, n_ts + 1), len(used)),
                    "coin": np.tile(np.array(used), n_ts),
                    "p50_1m_long": vals(0.6),
                    "p50_1m_short": vals(0.6),
                    "floor_long": vals(0.3),
                    "floor_short": vals(0.3),
                    "theta_1m_long": vals(0.5),
                    "theta_1m_short": vals(0.5),
                    "spread_last_long": vals(0.8),
                    "spread_last_short": vals(0.8),
                    "usable_long": rng.random(n) < 0.75,
                    "usable_short": rng.random(n) < 0.75,
                }
            )
            for params in param_sets:
                assert_parity(df, params)


class TestGear22SweepAccounting(unittest.TestCase):
    def test_total_pp_includes_mark_to_market(self) -> None:
        # Opens at fill 0.40, never closes; last finite opposite spread is -0.90,
        # so the run must be charged 0.40 - 0.90 - 0.30 instead of reporting 0.
        params = PolicyParams(
            theta_open=0.3,
            p50_open=0.50,
            min_profit_pp=9.0,
            min_theta_close=None,
            fee_round_trip_pp=0.30,
        )
        df = _frame(
            _qualify(ts_s=1, spread_last_long=0.40),
            _row(ts_s=2, spread_last_short=0.50),
            _row(ts_s=3, spread_last_short=-0.90),
        )
        fast = run_combo(store_from_frame(df), params)
        self.assertEqual(fast.n_closed, 0)
        self.assertEqual(fast.sum_closed_pp, 0.0)
        self.assertAlmostEqual(fast.mtm_pp, 0.40 - 0.90 - 0.30, places=6)
        self.assertAlmostEqual(fast.total_pp, -0.80, places=6)
        self.assertEqual(fast.mtm_ts, 3)
        self.assertEqual(fast.exposure_s, 2)

    def test_mark_to_market_skips_nan_opposite(self) -> None:
        params = PolicyParams(
            theta_open=0.3, p50_open=0.50, min_profit_pp=9.0, min_theta_close=None
        )
        df = _frame(
            _qualify(ts_s=1, spread_last_long=0.40),
            _row(ts_s=2, spread_last_short=0.25),
            _row(ts_s=3, spread_last_short=float("nan")),
        )
        fast = run_combo(store_from_frame(df), params)
        self.assertEqual(fast.mtm_ts, 2)
        self.assertAlmostEqual(fast.mtm_pp, 0.40 + 0.25 - 0.30, places=6)

    def test_flat_run_has_no_mark_to_market(self) -> None:
        df = _frame(_row(ts_s=1), _row(ts_s=2))
        fast = run_combo(store_from_frame(df), _NOTEBOOK)
        self.assertEqual(fast.n_closed, 0)
        self.assertEqual(fast.mtm_pp, 0.0)
        self.assertIsNone(fast.mtm_ts)
        self.assertEqual(fast.total_pp, 0.0)

    def test_span_and_exposure(self) -> None:
        df = _frame(
            _qualify(ts_s=10, coin="SOL"),
            _row(ts_s=10, coin="BTC"),
            _closeable(ts_s=25, coin="SOL"),
            _row(ts_s=25, coin="BTC"),
        )
        store = store_from_frame(df)
        self.assertEqual(store.span_s, 2)
        self.assertEqual(store.n_rows, 4)
        fast = run_combo(store, _NOTEBOOK)
        self.assertEqual(fast.n_closed, 1)
        self.assertEqual(fast.exposure_s, 15)


class TestGear22SweepGrid(unittest.TestCase):
    def test_sweep_returns_one_row_per_combo(self) -> None:
        df = _frame(
            _qualify(ts_s=1),
            _closeable(ts_s=2),
            _qualify(ts_s=3),
            _closeable(ts_s=4),
        )
        store = store_from_frame(df)
        table = sweep(
            store,
            theta_open=[0.0, 0.3],
            p50_open=[0.30, 0.50],
            min_profit_pp=[0.0, 0.3],
            min_theta_close=[None, 0],
        )
        self.assertEqual(len(table), 16)
        self.assertEqual(table["theta_open"].nunique(), 2)
        for column in ("total_pp", "duty_cycle", "n_closed", "exposure_s"):
            self.assertIn(column, table.columns)
        self.assertTrue((table["duty_cycle"] <= 1.0).all())

    def test_min_theta_close_zero_is_an_enabled_gate(self) -> None:
        # theta_1m_short == 0.0 fails a strict `> 0` gate, so min_theta_close=0
        # must hold where None closes. The sweep has to see that difference.
        df = _frame(
            _qualify(ts_s=1),
            _closeable(ts_s=2, theta_1m_short=0.0),
        )
        store = store_from_frame(df)
        enabled = run_combo(store, PolicyParams(**{**_knobs(), "min_theta_close": 0}))
        disabled = run_combo(
            store, PolicyParams(**{**_knobs(), "min_theta_close": None})
        )
        self.assertEqual(enabled.n_closed, 0)
        self.assertEqual(disabled.n_closed, 1)


class TestObserveParams(unittest.TestCase):
    def test_default_observe_matches_frozen_ridge(self) -> None:
        from research.gear22_backtest.params_frozen import (
            DEFAULT_OBSERVE_PARAMS,
            FROZEN,
            PREVIOUS,
        )

        self.assertIs(DEFAULT_OBSERVE_PARAMS, FROZEN)
        self.assertEqual(FROZEN.theta_open, 0.50)
        self.assertEqual(FROZEN.p50_open, 0.60)
        self.assertEqual(FROZEN.min_profit_pp, 0.20)
        self.assertEqual(FROZEN.min_theta_close, 0.05)
        self.assertIsNone(FROZEN.min_spread_open)
        self.assertEqual(FROZEN.fee_round_trip_pp, 0.30)
        self.assertNotEqual(FROZEN, PREVIOUS)
        self.assertEqual(PREVIOUS.min_theta_close, 0)


def _knobs() -> dict[str, object]:
    return dict(
        theta_open=0.3,
        p50_open=0.50,
        min_profit_pp=0.3,
        fee_round_trip_pp=0.30,
        min_spread_open=None,
    )


if __name__ == "__main__":
    unittest.main()
