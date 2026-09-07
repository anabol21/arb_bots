"""Hermetic tests for gear 2.2 market floor watcher (no VPS / no orders)."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from research.gear22_floor_watcher.builder import (
    FORMULA_ID,
    SNAPSHOT_COLUMNS,
    build_side_floor_rows,
)
from research.gear22_floor_watcher.cli import main, resolve_coins, run_watcher
from research.gear22_floor_watcher.journal import (
    JOURNAL_KEY_COLS,
    journal_max_bar_end_ms,
    merge_snapshot_frames,
    read_journal,
    write_journal,
)
from research.gear22_quiet_regime_viz.candles import BAR_MS, SPREAD_LONG_COL
from research.gear22_quiet_regime_viz.floors import (
    TF_SELECT_25_NAME,
    compute_chosen_floor,
)
from research.gear22_quiet_regime_viz.load import DEFAULT_SINCE_UTC, parse_since_ms

REPO = Path(__file__).resolve().parents[1]
FIXTURE_TICKS = REPO / "research" / "fixtures" / "gear22_quiet_regime_viz" / "ticks"
SINCE = DEFAULT_SINCE_UTC
UNTIL = "2026-09-03T08:40:00Z"


def _tiny_universe(path: Path) -> None:
    fieldnames = [
        "base_coin",
        "okx_symbol",
        "bybit_symbol",
        "okx_tick_size",
        "okx_lot_size",
        "okx_min_size",
        "bybit_tick_size",
        "bybit_qty_step",
        "bybit_min_order_qty",
        "bybit_min_notional_value",
        "take",
    ]
    rows = [
        {
            "base_coin": "SOL",
            "okx_symbol": "SOL-USDT-SWAP",
            "bybit_symbol": "SOLUSDT",
            "okx_tick_size": "0.01",
            "okx_lot_size": "0.01",
            "okx_min_size": "0.01",
            "bybit_tick_size": "0.01",
            "bybit_qty_step": "0.1",
            "bybit_min_order_qty": "0.1",
            "bybit_min_notional_value": "5",
            "take": "yes",
        },
        {
            "base_coin": "XRP",
            "okx_symbol": "XRP-USDT-SWAP",
            "bybit_symbol": "XRPUSDT",
            "okx_tick_size": "0.0001",
            "okx_lot_size": "1",
            "okx_min_size": "1",
            "bybit_tick_size": "0.0001",
            "bybit_qty_step": "1",
            "bybit_min_order_qty": "1",
            "bybit_min_notional_value": "5",
            "take": "yes",
        },
        {
            "base_coin": "BTC",
            "okx_symbol": "BTC-USDT-SWAP",
            "bybit_symbol": "BTCUSDT",
            "okx_tick_size": "0.1",
            "okx_lot_size": "0.01",
            "okx_min_size": "0.01",
            "bybit_tick_size": "0.1",
            "bybit_qty_step": "0.001",
            "bybit_min_order_qty": "0.001",
            "bybit_min_notional_value": "5",
            "take": "no",
        },
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def _synthetic_ticks(
    *,
    coin: str = "SYN",
    n_bars: int = 160,
    start_ms: int = 1_700_000_000_000,
) -> pd.DataFrame:
    """Enough 5m bars for SMA-12 + 12h trim warm-up; one tick per bar."""
    rows = []
    for i in range(n_bars):
        ts = start_ms + i * BAR_MS + 60_000
        # Quiet base + slow drift; prices keep classic spreads finite.
        mid = 100.0 + 0.01 * i
        # Long-ish positive spread ~0.05% with mild noise.
        noise = 0.01 * np.sin(i / 7.0)
        bybit_bid = mid
        okx_ask = mid * (1.0 - (0.0005 + 0.0001 * noise))
        okx_bid = okx_ask - 0.01
        bybit_ask = bybit_bid + 0.01
        rows.append(
            {
                "event_local_ts_ms": ts,
                "base_coin": coin,
                "trigger": "bybit",
                "okx_bid_price": okx_bid,
                "okx_ask_price": okx_ask,
                "bybit_bid_price": bybit_bid,
                "bybit_ask_price": bybit_ask,
                "okx_local_recv_ts_ms": ts,
                "okx_ts_ms": ts - 2,
                "bybit_local_recv_ts_ms": ts,
                "bybit_ts_ms": ts - 1,
            }
        )
    return pd.DataFrame(rows)


class TestResolveCoins(unittest.TestCase):
    def test_take_yes_from_universe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "u.csv"
            _tiny_universe(path)
            coins = resolve_coins(universe=path, coins=None)
            self.assertEqual(coins, ["SOL", "XRP"])
            self.assertEqual(
                resolve_coins(universe=path, coins=["btc"]),
                ["BTC"],
            )


class TestFloorRowsReuseChosenFloor(unittest.TestCase):
    def test_matches_compute_chosen_floor(self) -> None:
        raw = _synthetic_ticks()
        from research.gear22_quiet_regime_viz.load import derive_research_series

        ticks = derive_research_series(raw)
        start = int(ticks["event_local_ts_ms"].min())
        end = int(ticks["event_local_ts_ms"].max()) + BAR_MS
        meta = {
            "computed_at_ms": 1,
            "data_root": "synth",
            "since_ms": start,
            "until_ms": end,
            "lookback_ms": 0,
            "run_mode": "oneshot",
        }
        # Emit only the last 20 bars so earlier history warms the floor.
        emit_start = end - 20 * BAR_MS
        rows = build_side_floor_rows(
            ticks,
            base_coin="SYN",
            side="long",
            value_col=SPREAD_LONG_COL,
            emit_start_ms=emit_start,
            emit_end_ms=end,
            bucket_start_ms=start,
            meta=meta,
        )
        self.assertFalse(rows.empty)
        self.assertEqual(list(rows.columns), list(SNAPSHOT_COLUMNS))
        self.assertTrue((rows["formula_id"] == FORMULA_ID).all())
        self.assertTrue((rows["side"] == "long").all())

        # Rebuild reference floor on full bucket series and compare emit slice.
        from research.gear22_quiet_regime_viz.candles import build_5m_bucket_stats

        buckets = build_5m_bucket_stats(
            ticks,
            value_col=SPREAD_LONG_COL,
            start_ms=start,
            end_ms=end,
            fill_empty_buckets=True,
            ma_bars=(12,),
        )
        sma12 = buckets["ma_12"].to_numpy(dtype="float64")
        ref = compute_chosen_floor(sma12)[TF_SELECT_25_NAME]
        bar_end = buckets["bar_end_ms"].to_numpy(dtype="int64")
        mask = (bar_end > emit_start) & (bar_end <= end)
        ref_emit = ref[mask]
        got = rows["floor_tf_select_a25"].to_numpy(dtype="float64")
        self.assertEqual(got.shape, ref_emit.shape)
        np.testing.assert_allclose(got, ref_emit, equal_nan=True)

        # Edge = close - floor when both finite.
        for _, r in rows.iterrows():
            c, f, e = float(r["close"]), float(r["floor_tf_select_a25"]), float(r["edge"])
            if np.isfinite(c) and np.isfinite(f):
                self.assertTrue(np.isfinite(e))
                self.assertAlmostEqual(e, c - f, places=10)
            else:
                self.assertTrue(np.isnan(e))

        # Warm emit window should have finite floors (160 bars >> 144).
        self.assertGreater(int(np.isfinite(got).sum()), 0)


class TestJournalIdempotent(unittest.TestCase):
    def test_merge_last_wins_and_roundtrip(self) -> None:
        a = pd.DataFrame(
            [
                {
                    "bar_end_ms": 100,
                    "bar_start_ms": 0,
                    "base_coin": "SOL",
                    "side": "long",
                    "close": 1.0,
                    "sma12": 1.0,
                    "floor_tf_select_a25": 0.5,
                    "edge": 0.5,
                    "tick_count": 1,
                    "formula_id": FORMULA_ID,
                    "computed_at_ms": 1,
                    "data_root": "x",
                    "since_ms": 0,
                    "until_ms": 100,
                    "lookback_ms": 0,
                    "run_mode": "oneshot",
                }
            ]
        )
        b = a.copy()
        b.loc[0, "close"] = 2.0
        b.loc[0, "edge"] = 1.5
        b.loc[0, "computed_at_ms"] = 2
        merged = merge_snapshot_frames(a, b)
        self.assertEqual(len(merged), 1)
        self.assertEqual(float(merged.iloc[0]["close"]), 2.0)
        self.assertEqual(list(JOURNAL_KEY_COLS), ["bar_end_ms", "base_coin", "side"])

        with tempfile.TemporaryDirectory() as tmp:
            pq = Path(tmp) / "j.parquet"
            jl = Path(tmp) / "j.jsonl"
            write_journal(pq, merged, fmt="parquet")
            write_journal(jl, merged, fmt="jsonl")
            back_pq = read_journal(pq)
            back_jl = read_journal(jl)
            self.assertEqual(len(back_pq), 1)
            self.assertEqual(len(back_jl), 1)
            self.assertEqual(journal_max_bar_end_ms(back_pq), 100)


class TestFixtureCli(unittest.TestCase):
    def test_oneshot_and_watch_on_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "floor_journal"
            uni = Path(tmp) / "u.csv"
            _tiny_universe(uni)
            summary = run_watcher(
                data_root=FIXTURE_TICKS,
                out=out,
                universe=uni,
                since=SINCE,
                until=UNTIL,
                lookback_hours=0.0,
                watch=False,
                formats=("parquet", "jsonl"),
            )
            self.assertEqual(summary["run_mode"], "oneshot")
            self.assertGreater(summary["n_rows_incoming"], 0)
            self.assertEqual(set(summary["coins_with_rows"]), {"SOL", "XRP"})
            pq = Path(summary["written"][0])
            df = read_journal(pq)
            self.assertTrue(set(SNAPSHOT_COLUMNS).issubset(df.columns))
            self.assertTrue(set(df["side"]).issubset({"long", "short"}))
            # Short fixture → floors mostly NaN (warm-up), but schema holds.
            self.assertTrue((df["formula_id"] == FORMULA_ID).all())

            n1 = len(df)
            # Watch re-run over same window must not duplicate keys.
            summary2 = run_watcher(
                data_root=FIXTURE_TICKS,
                out=out,
                universe=uni,
                since=SINCE,
                until=UNTIL,
                lookback_hours=0.0,
                watch=True,
                formats=("parquet",),
            )
            df2 = read_journal(out.with_suffix(".parquet"))
            self.assertEqual(len(df2), n1)
            self.assertEqual(summary2["run_mode"], "watch")

    def test_main_cli_exit_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "j"
            rc = main(
                [
                    "--data-root",
                    str(FIXTURE_TICKS),
                    "--out",
                    str(out),
                    "--coins",
                    "SOL",
                    "--since",
                    SINCE,
                    "--until",
                    UNTIL,
                    "--lookback-hours",
                    "0",
                    "--formats",
                    "parquet",
                ]
            )
            self.assertEqual(rc, 0)
            self.assertTrue(out.with_suffix(".parquet").is_file())


class TestRepoUniverseWiring(unittest.TestCase):
    def test_default_universe_take_yes_count(self) -> None:
        coins = resolve_coins(universe=REPO / "bybit_okx_universe.csv", coins=None)
        self.assertEqual(len(coins), 189)
        self.assertIn("APT", coins)
        # Majors stay in CSV meta but are take=no (not in live screen).
        self.assertNotIn("BTC", coins)
        self.assertNotIn("SOL", coins)
        self.assertNotIn("XRP", coins)


if __name__ == "__main__":
    unittest.main()
