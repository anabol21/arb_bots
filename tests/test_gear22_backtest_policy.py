"""Hermetic tests for the gear-2.2 dummy backtest policy (synthetic rows only)."""

from __future__ import annotations

import math
import unittest

from research.gear22_backtest import (
    Decision,
    DummyParams,
    FeatureSnapshot,
    PolicyParams,
    PolicyState,
    decide,
    decide_close,
    decide_open,
    potential_profit_pp,
)
from research.gear22_backtest.policy import (
    SYNTHETIC_CLOSE_ROLL,
    SYNTHETIC_OPEN_ROLL,
    synthetic_roll,
)


def _row(**overrides: object) -> FeatureSnapshot:
    fields = dict(
        ts_s=1_000_000,
        coin="SOL",
        p50_1m_long=0.10,
        p50_1m_short=0.10,
        floor_long=0.0,
        floor_short=0.0,
        theta_1m_long=0.0,
        theta_1m_short=0.0,
        spread_last_long=9.99,
        spread_last_short=9.99,
        usable_long=True,
        usable_short=True,
    )
    fields.update(overrides)
    return FeatureSnapshot(**fields)  # type: ignore[arg-type]


def _qualifying(**overrides: object) -> FeatureSnapshot:
    fields = dict(
        p50_1m_long=0.50,
        p50_1m_short=0.50,
        theta_1m_long=0.10,
        theta_1m_short=0.10,
    )
    fields.update(overrides)
    return _row(**fields)


def _long_state(**overrides: object) -> PolicyState:
    fields = dict(
        position_side="long",
        held_coin="SOL",
        opened_ts_s=1_000_000,
        fill_spread_pp=0.40,
    )
    fields.update(overrides)
    return PolicyState(**fields)  # type: ignore[arg-type]


def _ts_for_roll(target: int, *, seed: int = 7) -> int:
    return next(ts for ts in range(1_000_000, 1_100_000) if synthetic_roll(ts, seed) == target)


class TestSyntheticRollPolicy(unittest.TestCase):
    def test_one_replayable_roll_per_second(self) -> None:
        ts = _ts_for_roll(SYNTHETIC_OPEN_ROLL)
        self.assertEqual(synthetic_roll(ts, 7), SYNTHETIC_OPEN_ROLL)
        self.assertEqual(synthetic_roll(ts, 7), synthetic_roll(ts, 7))

    def test_roll_17_opens_and_roll_32_closes(self) -> None:
        params = PolicyParams(synthetic_roll_seed=7)
        open_ts = _ts_for_roll(SYNTHETIC_OPEN_ROLL)
        opened = decide(_row(ts_s=open_ts), PolicyState(), params)
        self.assertIn(opened.action, {"open_long", "open_short"})
        self.assertEqual(opened.reason, "synthetic_open_17")

        close_ts = _ts_for_roll(SYNTHETIC_CLOSE_ROLL)
        closed = decide(_row(ts_s=close_ts), _long_state(), params)
        self.assertEqual(closed.action, "close")
        self.assertEqual(closed.reason, "synthetic_close_32")

    def test_non_trigger_holds_and_coin_mismatch_stays_fail_closed(self) -> None:
        params = PolicyParams(synthetic_roll_seed=7)
        ts = next(
            value
            for value in range(1_000_000, 1_100_000)
            if synthetic_roll(value, 7) not in {SYNTHETIC_OPEN_ROLL, SYNTHETIC_CLOSE_ROLL}
        )
        self.assertEqual(decide(_row(ts_s=ts), PolicyState(), params).action, "hold")
        mismatch = decide(
            _row(ts_s=_ts_for_roll(SYNTHETIC_CLOSE_ROLL), coin="BTC"),
            _long_state(),
            params,
        )
        self.assertEqual(mismatch.reason, "hold_coin_mismatch")


class TestGear22DummyPolicyOpen(unittest.TestCase):
    def test_both_below_hold(self) -> None:
        d = decide(_row(), PolicyState())
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.reason, "hold_below_threshold")

    def test_long_qualifies_open_long(self) -> None:
        row = _qualifying(p50_1m_short=0.10, theta_1m_short=0.0)
        d = decide(row, PolicyState())
        self.assertEqual(d.action, "open_long")
        self.assertEqual(d.reason, "open_long")
        self.assertEqual(d.theta, row.theta_1m_long)
        self.assertEqual(d.p50, row.p50_1m_long)

    def test_short_only_open_short(self) -> None:
        row = _qualifying(p50_1m_long=0.10, theta_1m_long=0.0)
        d = decide(row, PolicyState())
        self.assertEqual(d.action, "open_short")
        self.assertEqual(d.reason, "open_short")
        self.assertEqual(d.theta, row.theta_1m_short)
        self.assertEqual(d.p50, row.p50_1m_short)

    def test_both_qualify_prefer_long(self) -> None:
        d = decide(_qualifying(), PolicyState())
        self.assertEqual(d.action, "open_long")
        self.assertEqual(d.reason, "open_long")

    def test_not_usable_hold_even_if_theta_p50_huge(self) -> None:
        row = _row(
            usable_long=False,
            usable_short=False,
            theta_1m_long=9.0,
            theta_1m_short=9.0,
            p50_1m_long=9.0,
            p50_1m_short=9.0,
        )
        d = decide(row, PolicyState())
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.reason, "hold_not_usable")

    def test_nan_p50_hold(self) -> None:
        row = _qualifying(p50_1m_long=math.nan)
        d = decide(row, PolicyState())
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.reason, "hold_nan")

    def test_spread_last_unused_for_open_threshold(self) -> None:
        # Huge last tick, p50/theta below open → still hold (open dummy uses p50_1m).
        row = _row(spread_last_long=9.99, spread_last_short=9.99)
        d = decide(row, PolicyState())
        self.assertEqual(d.action, "hold")
        self.assertIsInstance(d, Decision)

    def test_open_nan_spread_last_fail_closed(self) -> None:
        row = _qualifying(spread_last_long=math.nan)
        d = decide(row, PolicyState())
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.reason, "hold_nan")

    def test_decide_open_ignores_position_caller_flat(self) -> None:
        row = _qualifying(p50_1m_short=0.10, theta_1m_short=0.0)
        d = decide_open(row, DummyParams())
        self.assertEqual(d.action, "open_long")


class TestGear22DummyPolicyClose(unittest.TestCase):
    def test_close_fill_040_opposite_0_potential_010(self) -> None:
        # 0.40 + 0.0 - 0.30 = 0.10 >= 0.0 → close
        row = _row(spread_last_short=0.0)
        d = decide(row, _long_state())
        self.assertEqual(d.action, "close")
        self.assertEqual(d.reason, "close_min_profit")

    def test_close_potential_neg_hold(self) -> None:
        # 0.20 + 0.05 - 0.30 = -0.05 < 0 → hold
        row = _row(spread_last_short=0.05)
        state = _long_state(fill_spread_pp=0.20)
        d = decide(row, state)
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.reason, "hold_below_min_profit")

    def test_min_profit_blocks_close_when_potential_010(self) -> None:
        row = _row(spread_last_short=0.0)
        d = decide(row, _long_state(), DummyParams(min_profit_pp=0.20))
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.reason, "hold_below_min_profit")

    def test_opposite_spread_nan_hold(self) -> None:
        row = _row(spread_last_short=math.nan)
        d = decide(row, _long_state())
        self.assertEqual(d.action, "hold")
        self.assertNotEqual(d.action, "close")
        self.assertEqual(d.reason, "hold_nan")

    def test_opposite_not_usable_hold(self) -> None:
        row = _row(usable_short=False, spread_last_short=0.0)
        d = decide(row, _long_state())
        self.assertEqual(d.action, "hold")
        self.assertNotEqual(d.action, "close")
        self.assertEqual(d.reason, "hold_not_usable")

    def test_close_does_not_use_theta_p50_dummy(self) -> None:
        row = _row(
            spread_last_short=0.05,
            theta_1m_short=9.0,
            p50_1m_short=9.0,
        )
        d = decide(row, _long_state(fill_spread_pp=0.20))
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.reason, "hold_below_min_profit")

    def test_in_position_does_not_open_other_side(self) -> None:
        row = _qualifying(spread_last_short=0.05)
        d = decide(row, _long_state(fill_spread_pp=0.20))
        self.assertEqual(d.action, "hold")
        self.assertNotIn(d.action, ("open_long", "open_short", "close"))

    def test_close_short_uses_long_book(self) -> None:
        row = _row(spread_last_long=0.0, spread_last_short=9.99)
        state = PolicyState(
            position_side="short",
            held_coin="SOL",
            opened_ts_s=1_000_000,
            fill_spread_pp=0.40,
        )
        d = decide(row, state)
        self.assertEqual(d.action, "close")

    def test_coin_mismatch_hold(self) -> None:
        row = _qualifying()
        state = _long_state(held_coin="BTC")
        d = decide(row, state)
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.reason, "hold_coin_mismatch")

    def test_decide_close_dispatch(self) -> None:
        row = _row(spread_last_short=0.0)
        d = decide_close(row, _long_state(), DummyParams())
        self.assertEqual(d.action, "close")


class TestGear22PotentialProfit(unittest.TestCase):
    def test_potential_profit_pp_exact(self) -> None:
        row = _row(spread_last_short=0.0)
        state = _long_state(fill_spread_pp=0.40)
        self.assertAlmostEqual(potential_profit_pp(row, state, 0.30), 0.10)
        row2 = _row(spread_last_short=0.10)
        self.assertAlmostEqual(potential_profit_pp(row2, state, 0.30), 0.20)
        row3 = _row(spread_last_short=0.05)
        state3 = _long_state(fill_spread_pp=0.20)
        self.assertAlmostEqual(potential_profit_pp(row3, state3, 0.30), -0.05)

    def test_potential_profit_pp_nan_returns_none(self) -> None:
        row = _row(spread_last_short=math.nan)
        self.assertIsNone(potential_profit_pp(row, _long_state(), 0.30))

    def test_potential_profit_pp_short_uses_long_last(self) -> None:
        row = _row(spread_last_long=0.05, spread_last_short=9.99)
        state = PolicyState(
            position_side="short",
            held_coin="SOL",
            opened_ts_s=1_000_000,
            fill_spread_pp=0.40,
        )
        self.assertAlmostEqual(potential_profit_pp(row, state, 0.30), 0.15)


class TestGear22DummyParams(unittest.TestCase):
    def test_default_params_match_occupancy_fee(self) -> None:
        params = DummyParams()
        self.assertIs(DummyParams, PolicyParams)
        self.assertEqual(params.p50_open, 0.30)
        self.assertEqual(params.theta_open, 0.0)
        self.assertEqual(params.min_profit_pp, 0.0)
        self.assertEqual(params.fee_round_trip_pp, 0.30)
        self.assertIsNone(params.min_spread_open)
        self.assertIsNone(params.min_theta_close)


class TestGear22OpenGates(unittest.TestCase):
    def test_min_spread_open_blocks_small_spread(self) -> None:
        row = _qualifying(spread_last_long=0.2, p50_1m_short=0.10, theta_1m_short=0.0)
        d = decide(row, PolicyState(), DummyParams(min_spread_open=0.5))
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.reason, "hold_below_threshold")

    def test_min_spread_open_none_ignores_spread_last(self) -> None:
        row = _qualifying(spread_last_long=0.2, p50_1m_short=0.10, theta_1m_short=0.0)
        d = decide(row, PolicyState(), DummyParams(min_spread_open=None))
        self.assertEqual(d.action, "open_long")

    def test_theta_open_none_does_not_require_theta(self) -> None:
        row = _qualifying(
            theta_1m_long=math.nan,
            p50_1m_short=0.10,
            theta_1m_short=0.0,
        )
        d = decide(row, PolicyState(), DummyParams(theta_open=None))
        self.assertEqual(d.action, "open_long")


class TestGear22CloseSideAndOverlap(unittest.TestCase):
    def test_close_long_uses_theta_1m_short_not_long(self) -> None:
        params = DummyParams(min_theta_close=0.05)
        blocked = _row(
            spread_last_short=0.0,
            theta_1m_long=9.0,
            p50_1m_long=0.10,
            theta_1m_short=0.0,
        )
        d_block = decide(blocked, _long_state(), params)
        self.assertEqual(d_block.action, "hold")
        self.assertEqual(d_block.reason, "hold_below_min_theta")

        allowed = _row(
            spread_last_short=0.0,
            theta_1m_long=0.0,
            p50_1m_long=0.10,
            theta_1m_short=0.10,
        )
        d_ok = decide(allowed, _long_state(), params)
        self.assertEqual(d_ok.action, "close")

    def test_close_short_uses_theta_1m_long(self) -> None:
        params = DummyParams(min_theta_close=0.05)
        state = PolicyState(
            position_side="short",
            held_coin="SOL",
            opened_ts_s=1_000_000,
            fill_spread_pp=0.40,
        )
        blocked = _row(
            spread_last_long=0.0,
            spread_last_short=9.99,
            theta_1m_long=0.0,
            theta_1m_short=9.0,
            p50_1m_short=0.10,
        )
        d_block = decide(blocked, state, params)
        self.assertEqual(d_block.action, "hold")
        self.assertEqual(d_block.reason, "hold_below_min_theta")

        allowed = _row(
            spread_last_long=0.0,
            spread_last_short=9.99,
            theta_1m_long=0.10,
            theta_1m_short=0.0,
            p50_1m_short=0.10,
        )
        d_ok = decide(allowed, state, params)
        self.assertEqual(d_ok.action, "close")

    def test_same_type_open_overlap_holds(self) -> None:
        row = _qualifying(spread_last_short=0.0)
        d = decide(row, _long_state(), DummyParams())
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.reason, "hold_open_overlap")
        self.assertNotEqual(d.action, "close")

    def test_close_when_same_type_open_does_not_qualify(self) -> None:
        row = _row(spread_last_short=0.0)
        d = decide(row, _long_state(), DummyParams())
        self.assertEqual(d.action, "close")
        self.assertEqual(d.reason, "close_min_profit")

    def test_flip_side_open_does_not_block_close(self) -> None:
        row = _qualifying(
            spread_last_short=0.0,
            p50_1m_long=0.10,
            theta_1m_long=0.0,
        )
        d = decide(row, _long_state(), DummyParams())
        self.assertEqual(d.action, "close")

    def test_min_theta_close_nan_fail_closed(self) -> None:
        row = _row(spread_last_short=0.0, theta_1m_short=math.nan, p50_1m_long=0.10)
        d = decide(row, _long_state(), DummyParams(min_theta_close=0.0))
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.reason, "hold_nan")
        self.assertNotEqual(d.action, "close")

    def test_min_profit_pp_none_disables_profit_gate(self) -> None:
        row = _row(spread_last_short=0.05)
        d = decide(row, _long_state(fill_spread_pp=0.20), DummyParams(min_profit_pp=None))
        self.assertEqual(d.action, "close")
