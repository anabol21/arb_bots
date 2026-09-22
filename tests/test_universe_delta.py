"""Delta contract: atomic write, cap, no universe rewrite."""

from __future__ import annotations

import csv
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.utils.universe_delta import (  # noqa: E402
    DeltaPathError,
    apply_hard_cap,
    assert_delta_path_safe,
    csv_base_coins,
    diff_intersection_against_csv,
    read_delta_rows,
    write_delta_atomic,
)


UNIVERSE_HEADER = [
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


def _universe_row(coin: str, take: str = "yes") -> dict[str, str]:
    return {
        "base_coin": coin,
        "okx_symbol": f"{coin}-USDT-SWAP",
        "bybit_symbol": f"{coin}USDT",
        "okx_tick_size": "0.1",
        "okx_lot_size": "0.01",
        "okx_min_size": "0.01",
        "bybit_tick_size": "0.1",
        "bybit_qty_step": "0.001",
        "bybit_min_order_qty": "0.001",
        "bybit_min_notional_value": "5",
        "take": take,
    }


def _write_universe(path: Path, coins: list[tuple[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=UNIVERSE_HEADER)
        writer.writeheader()
        for coin, take in coins:
            writer.writerow(_universe_row(coin, take))


def _delta_row(coin: str) -> dict[str, str]:
    return {
        "base_coin": coin,
        "okx_symbol": f"{coin}-USDT-SWAP",
        "bybit_symbol": f"{coin}USDT",
        "okx_tick_size": "0.01",
        "okx_lot_size": "1",
        "okx_min_size": "1",
        "bybit_tick_size": "0.01",
        "bybit_qty_step": "1",
        "bybit_min_order_qty": "1",
        "bybit_min_notional_value": "5",
        "discovered_at_utc": "2026-09-18T00:00:00Z",
    }


class UniverseDeltaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_atomic_write_does_not_rewrite_universe(self) -> None:
        universe = self.root / "bybit_okx_universe.csv"
        delta = self.root / "hot_add_delta.csv"
        _write_universe(universe, [("AAA", "yes"), ("BBB", "no")])
        before = universe.read_bytes()
        mtime = universe.stat().st_mtime
        time.sleep(0.05)
        write_delta_atomic(delta, [_delta_row("NEW")], universe_path=universe)
        self.assertEqual(universe.read_bytes(), before)
        self.assertEqual(universe.stat().st_mtime, mtime)
        rows = read_delta_rows(delta)
        self.assertEqual([r["base_coin"] for r in rows], ["NEW"])
        self.assertNotIn("take", rows[0])

    def test_refuse_delta_path_equal_universe(self) -> None:
        universe = self.root / "bybit_okx_universe.csv"
        _write_universe(universe, [("AAA", "yes")])
        with self.assertRaises(DeltaPathError):
            assert_delta_path_safe(universe, universe)
        with self.assertRaises(DeltaPathError):
            write_delta_atomic(universe, [_delta_row("ZZZ")], universe_path=universe)

    def test_refuse_data_live_prefix(self) -> None:
        with self.assertRaises(DeltaPathError):
            assert_delta_path_safe("/data/live/hot_add_delta.csv", self.root / "u.csv")

    def test_diff_skips_take_no_and_take_yes(self) -> None:
        universe = self.root / "u.csv"
        _write_universe(universe, [("AAA", "yes"), ("BBB", "no")])
        coins = csv_base_coins(universe)
        self.assertEqual(coins, {"AAA", "BBB"})
        intersection = [_delta_row("AAA"), _delta_row("BBB"), _delta_row("CCC")]
        fresh = diff_intersection_against_csv(intersection, coins)
        self.assertEqual([r["base_coin"] for r in fresh], ["CCC"])

    def test_hard_cap(self) -> None:
        rows = [_delta_row(c) for c in ("A", "B", "C")]
        kept, dropped = apply_hard_cap(rows, 2)
        self.assertEqual([r["base_coin"] for r in kept], ["A", "B"])
        self.assertEqual(dropped, 1)
        empty, dropped0 = apply_hard_cap(rows, 0)
        self.assertEqual(empty, [])
        self.assertEqual(dropped0, 3)
        with self.assertRaises(ValueError):
            apply_hard_cap(rows, -1)

    def test_missing_required_field_fails_loud(self) -> None:
        delta = self.root / "bad.csv"
        with delta.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["base_coin", "okx_symbol"])
            writer.writeheader()
            writer.writerow({"base_coin": "X", "okx_symbol": "X-USDT-SWAP"})
        with self.assertRaises(ValueError):
            read_delta_rows(delta)

    def test_repo_csv_diff_uses_given_path(self) -> None:
        repo_csv = REPO / "bybit_okx_universe.csv"
        coins = csv_base_coins(repo_csv)
        self.assertIn("BTC", coins)
        # A coin already in the file (any take) must not appear in the delta.
        fake_intersection = [_delta_row("BTC"), _delta_row("___NOT_IN_CSV___")]
        fresh = diff_intersection_against_csv(fake_intersection, coins)
        self.assertEqual([r["base_coin"] for r in fresh], ["___NOT_IN_CSV___"])


if __name__ == "__main__":
    unittest.main()
