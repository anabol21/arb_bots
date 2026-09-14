"""Hermetic tests for gear-2.2 observation replay (synthetic rows / DataFrame)."""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from research.gear22_backtest import (
    ClosedTrade,
    DummyParams,
    OpenPosition,
    ReplayResult,
    replay_frame,
    replay_hive,
    replay_path,
)

_HIVE_DAY = Path("output/gear22_backtest_features_by_date/event_date=2026-08-12/part-000.parquet")


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
    fields = dict(
        p50_1m_long=0.50,
        p50_1m_short=0.50,
        theta_1m_long=0.10,
        theta_1m_short=0.10,
    )
    fields.update(overrides)
    return _row(**fields)


def _frame(*rows: dict[str, object]) -> pd.DataFrame:
    return pd.DataFrame(list(rows))


class TestGear22ReplayFrame(unittest.TestCase):
    def test_open_then_close_one_trade(self) -> None:
        df = _frame(
            _qualify(ts_s=1, spread_last_long=0.40),
            _row(ts_s=2, spread_last_short=0.0),  # 0.40 + 0.0 - 0.30 = 0.10 >= 0
        )
        result = replay_frame(df, DummyParams(), slot_mode="per_coin")
        self.assertIsInstance(result, ReplayResult)
        trades = result.closed
        self.assertEqual(len(trades), 1)
        self.assertEqual(result.open_positions, [])
        t = trades[0]
        self.assertIsInstance(t, ClosedTrade)
        self.assertEqual(t.coin, "SOL")
        self.assertEqual(t.side, "long")
        self.assertEqual(t.ts_open, 1)
        self.assertEqual(t.ts_close, 2)
        self.assertAlmostEqual(t.fill_spread_pp, 0.40)
        self.assertAlmostEqual(t.exit_spread_pp, 0.0)
        self.assertAlmostEqual(t.potential_pp, 0.10)
        self.assertEqual(t.reason_open, "open_long")
        self.assertEqual(t.reason_close, "close_min_profit")

    def test_global_k1_second_coin_blocked(self) -> None:
        df = _frame(
            _qualify(ts_s=1, coin="SOL", spread_last_long=0.40),
            _qualify(ts_s=2, coin="BTC", spread_last_long=0.50),
            _row(ts_s=3, coin="SOL", spread_last_short=0.0),
            _row(ts_s=4, coin="BTC", spread_last_short=0.0),
        )
        result = replay_frame(df, DummyParams(), slot_mode="global")
        trades = result.closed
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].coin, "SOL")
        self.assertEqual(trades[0].ts_open, 1)
        self.assertEqual(trades[0].ts_close, 3)
        self.assertEqual(result.open_positions, [])

    def test_per_coin_two_coins_both_open(self) -> None:
        df = _frame(
            _qualify(ts_s=1, coin="SOL", spread_last_long=0.40),
            _qualify(ts_s=1, coin="BTC", spread_last_long=0.50),
            _row(ts_s=2, coin="SOL", spread_last_short=0.0),
            _row(ts_s=2, coin="BTC", spread_last_short=0.0),
        )
        result = replay_frame(df, DummyParams(), slot_mode="per_coin")
        trades = result.closed
        self.assertEqual(len(trades), 2)
        self.assertEqual(result.open_positions, [])
        coins = {t.coin for t in trades}
        self.assertEqual(coins, {"SOL", "BTC"})
        by_coin = {t.coin: t for t in trades}
        self.assertEqual(by_coin["SOL"].ts_open, 1)
        self.assertEqual(by_coin["BTC"].ts_open, 1)
        self.assertAlmostEqual(by_coin["SOL"].fill_spread_pp, 0.40)
        self.assertAlmostEqual(by_coin["BTC"].fill_spread_pp, 0.50)

    def test_missing_opposite_spread_stays_open(self) -> None:
        df = _frame(
            _qualify(ts_s=1, spread_last_long=0.40),
            _row(ts_s=2, spread_last_short=math.nan),
            _row(ts_s=3, spread_last_short=math.nan, usable_short=True),
        )
        result = replay_frame(df, DummyParams(), slot_mode="per_coin")
        self.assertEqual(result.closed, [])
        self.assertEqual(len(result.open_positions), 1)
        pos = result.open_positions[0]
        self.assertIsInstance(pos, OpenPosition)
        self.assertEqual(pos.coin, "SOL")
        self.assertEqual(pos.ts_open, 1)

    def test_default_slot_mode_is_global(self) -> None:
        df = _frame(
            _qualify(ts_s=1, coin="SOL", spread_last_long=0.40),
            _qualify(ts_s=1, coin="BTC", spread_last_long=0.50),
            _row(ts_s=2, coin="SOL", spread_last_short=0.0),
            _row(ts_s=2, coin="BTC", spread_last_short=0.0),
        )
        result = replay_frame(df, DummyParams())
        self.assertEqual(len(result.closed), 1)
        self.assertEqual(result.closed[0].coin, "BTC")

    def test_global_coin_major_concat_still_one_position(self) -> None:
        """Coin-major table (all SOL, then all BTC) must still be K=1 after sort."""
        df = _frame(
            _qualify(ts_s=1, coin="SOL", spread_last_long=0.40),
            _row(ts_s=10, coin="SOL", spread_last_short=0.0),
            _qualify(ts_s=2, coin="BTC", spread_last_long=0.50),
            _row(ts_s=11, coin="BTC", spread_last_short=0.0),
        )
        result = replay_frame(df, DummyParams(), slot_mode="global")
        self.assertEqual(len(result.closed), 1)
        self.assertEqual(result.closed[0].coin, "SOL")
        self.assertEqual(result.closed[0].ts_open, 1)
        self.assertEqual(result.closed[0].ts_close, 10)

    def test_global_same_second_opens_at_most_one(self) -> None:
        """Two coins qualify at the same ts_s: lexicographic coin wins."""
        df = _frame(
            _qualify(ts_s=1, coin="SOL", spread_last_long=0.40),
            _qualify(ts_s=1, coin="BTC", spread_last_long=0.50),
            _row(ts_s=2, coin="SOL", spread_last_short=0.0),
            _row(ts_s=2, coin="BTC", spread_last_short=0.0),
        )
        result = replay_frame(df, DummyParams(), slot_mode="global")
        self.assertEqual(len(result.closed), 1)
        self.assertEqual(result.closed[0].coin, "BTC")
        self.assertEqual(result.closed[0].ts_open, 1)
        self.assertEqual(result.closed[0].ts_close, 2)

    def test_global_close_uses_held_coin_row_not_other(self) -> None:
        """AAA opposite would close; SOL opposite holds. Close must use SOL."""
        df = _frame(
            _qualify(ts_s=1, coin="SOL", spread_last_long=0.40),
            _row(ts_s=2, coin="AAA", spread_last_short=0.0),
            _row(ts_s=2, coin="SOL", spread_last_short=-0.20),
        )
        result = replay_frame(df, DummyParams(), slot_mode="global")
        self.assertEqual(result.closed, [])
        self.assertEqual(len(result.open_positions), 1)
        self.assertEqual(result.open_positions[0].coin, "SOL")

    def test_global_missing_held_coin_does_not_close_on_other(self) -> None:
        df = _frame(
            _qualify(ts_s=1, coin="SOL", spread_last_long=0.40),
            _row(ts_s=2, coin="AAA", spread_last_short=0.0),
        )
        result = replay_frame(df, DummyParams(), slot_mode="global")
        self.assertEqual(result.closed, [])
        self.assertEqual(len(result.open_positions), 1)
        self.assertEqual(result.open_positions[0].coin, "SOL")

    def test_unclosed_position_returned(self) -> None:
        df = _frame(_qualify(ts_s=1, spread_last_long=0.40))
        result = replay_frame(df, DummyParams(), slot_mode="global")
        self.assertEqual(result.closed, [])
        self.assertEqual(len(result.open_positions), 1)
        self.assertEqual(result.open_positions[0].coin, "SOL")
        self.assertEqual(result.open_positions[0].ts_open, 1)

    def test_unknown_slot_mode_raises(self) -> None:
        with self.assertRaises(ValueError):
            replay_frame(_frame(_row()), DummyParams(), slot_mode="thirty")  # type: ignore[arg-type]


class TestGear22TradesFrame(unittest.TestCase):
    def test_unclosed_position_is_one_row_with_null_ts_close(self) -> None:
        df = _frame(_qualify(ts_s=1, spread_last_long=0.40))
        result = replay_frame(df, DummyParams(), slot_mode="global")
        frame = result.to_trades_frame()
        self.assertEqual(len(frame), 1)
        self.assertTrue(pd.isna(frame.loc[0, "ts_close"]))
        self.assertEqual(frame.loc[0, "status"], "open")
        self.assertEqual(frame.loc[0, "reason_close"], "unclosed")
        self.assertEqual(frame.loc[0, "coin"], "SOL")
        self.assertEqual(int(frame.loc[0, "ts_open"]), 1)
        self.assertTrue(pd.isna(frame.loc[0, "exit_spread_pp"]))
        self.assertTrue(pd.isna(frame.loc[0, "potential_pp"]))

    def test_unclosed_row_uses_last_seen_exit_and_potential(self) -> None:
        df = _frame(
            _qualify(ts_s=1, spread_last_long=0.40),
            _row(ts_s=2, spread_last_short=-0.20),
        )
        result = replay_frame(df, DummyParams(), slot_mode="per_coin")
        self.assertEqual(result.closed, [])
        self.assertEqual(len(result.open_positions), 1)
        frame = result.to_trades_frame()
        self.assertEqual(len(frame), 1)
        self.assertTrue(pd.isna(frame.loc[0, "ts_close"]))
        self.assertEqual(frame.loc[0, "status"], "open")
        self.assertAlmostEqual(float(frame.loc[0, "exit_spread_pp"]), -0.20)
        self.assertAlmostEqual(float(frame.loc[0, "potential_pp"]), -0.10)

    def test_closed_and_unclosed_share_columns(self) -> None:
        df = _frame(
            _qualify(ts_s=1, spread_last_long=0.40),
            _row(ts_s=2, spread_last_short=0.0),
            _qualify(ts_s=3, spread_last_long=0.50),
        )
        result = replay_frame(df, DummyParams(), slot_mode="per_coin")
        frame = result.to_trades_frame()
        self.assertEqual(len(frame), 2)
        self.assertEqual(list(frame.columns), [
            "coin",
            "side",
            "ts_open",
            "ts_close",
            "fill_spread_pp",
            "exit_spread_pp",
            "potential_pp",
            "reason_open",
            "reason_close",
            "status",
        ])
        closed = frame.loc[frame["status"] == "closed"].iloc[0]
        opened = frame.loc[frame["status"] == "open"].iloc[0]
        self.assertEqual(int(closed["ts_close"]), 2)
        self.assertEqual(closed["reason_close"], "close_min_profit")
        self.assertTrue(pd.isna(opened["ts_close"]))
        self.assertEqual(opened["reason_close"], "unclosed")

    def test_empty_trades_frame_keeps_schema(self) -> None:
        result = replay_frame(_frame(_row(ts_s=1)), DummyParams())
        frame = result.to_trades_frame()
        self.assertTrue(frame.empty)
        self.assertIn("ts_close", frame.columns)
        self.assertIn("status", frame.columns)


class TestGear22ReplayPath(unittest.TestCase):
    def test_replay_path_reads_tiny_hive(self) -> None:
        df = _frame(
            _qualify(ts_s=1, spread_last_long=0.40),
            _row(ts_s=2, spread_last_short=0.0),
        )
        with tempfile.TemporaryDirectory() as td:
            part = Path(td) / "event_date=2026-08-12" / "part-000.parquet"
            part.parent.mkdir(parents=True)
            df.to_parquet(part, index=False)
            trades = replay_path(td, event_date="2026-08-12", coins=["SOL"]).closed
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].coin, "SOL")


class TestGear22ReplayHive(unittest.TestCase):
    def test_replay_hive_dates_none_vs_subset(self) -> None:
        day_a = _frame(
            _qualify(ts_s=1, spread_last_long=0.40),
            _row(ts_s=2, spread_last_short=0.0),
        )
        day_b = _frame(
            _qualify(ts_s=10, spread_last_long=0.50),
            _row(ts_s=11, spread_last_short=0.0),
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p_a = root / "event_date=2026-08-12" / "part-000.parquet"
            p_b = root / "event_date=2026-08-13" / "part-000.parquet"
            p_a.parent.mkdir(parents=True)
            p_b.parent.mkdir(parents=True)
            day_a.to_parquet(p_a, index=False)
            day_b.to_parquet(p_b, index=False)
            all_trades = replay_hive(root, coins=["SOL"]).closed
            one_day = replay_hive(root, coins=["SOL"], dates=["2026-08-12"]).closed
        self.assertEqual(len(all_trades), 2)
        self.assertEqual({t.ts_open for t in all_trades}, {1, 10})
        self.assertEqual(len(one_day), 1)
        self.assertEqual(one_day[0].ts_open, 1)

    def test_categorical_coin_filter_and_str_coin(self) -> None:
        df = _frame(
            _qualify(ts_s=1, coin="KAITO", spread_last_long=0.40),
            _row(ts_s=2, coin="KAITO", spread_last_short=0.0),
            _qualify(ts_s=1, coin="SOL", spread_last_long=0.50),
            _row(ts_s=2, coin="SOL", spread_last_short=0.0),
        )
        df["coin"] = df["coin"].astype("category")
        with tempfile.TemporaryDirectory() as td:
            part = Path(td) / "event_date=2026-08-12" / "part-000.parquet"
            part.parent.mkdir(parents=True)
            df.to_parquet(part, index=False)
            import pyarrow.parquet as pq

            coin_type = pq.read_table(part, columns=["coin"]).column("coin").type
            self.assertTrue(
                str(coin_type).startswith("dictionary") or str(df["coin"].dtype) == "category"
            )
            listed = replay_hive(td, coins=["KAITO"], dates=["2026-08-12"])
            as_str = replay_hive(td, coins="KAITO", dates=["2026-08-12"])
        self.assertEqual(len(listed.closed), 1)
        self.assertEqual(listed.closed[0].coin, "KAITO")
        self.assertEqual(len(as_str.closed), 1)
        self.assertEqual(as_str.closed[0].coin, "KAITO")

    def test_replay_hive_missing_date_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileNotFoundError):
                replay_hive(td, dates=["2099-01-01"])


@unittest.skipUnless(_HIVE_DAY.is_file(), "by_date hive day 2026-08-12 missing")
class TestGear22ReplaySmoke(unittest.TestCase):
    def test_one_coin_one_day_count_only(self) -> None:
        result = replay_path(
            "output/gear22_backtest_features_by_date",
            event_date="2026-08-12",
            coins=["KAITO"],
            slot_mode="per_coin",
        )
        self.assertIsInstance(result, ReplayResult)
        self.assertIsInstance(result.closed, list)
        self.assertGreaterEqual(len(result.closed), 0)
        print(f"n_trades={len(result.closed)} n_open={len(result.open_positions)}")
