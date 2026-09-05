"""Universe CSV take=yes screen for live pair loaders."""

from __future__ import annotations

import csv
import importlib
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.utils.universe_csv import (  # noqa: E402
    MissingTakeColumnError,
    filter_take_yes_rows,
    require_take_column,
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
]


def _row(coin: str, take: str | None) -> dict[str, str]:
    row = {
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
    }
    if take is not None:
        row["take"] = take
    return row


def _write_csv(path: Path, rows: list[dict[str, str]], *, include_take: bool) -> None:
    fieldnames = list(UNIVERSE_HEADER)
    if include_take:
        fieldnames.append("take")
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


class FilterTakeYesHelperTests(unittest.TestCase):
    def test_mixed_keep_yes_only(self) -> None:
        rows = [
            _row("AAA", "yes"),
            _row("BBB", "no"),
            _row("CCC", " YES "),
            _row("DDD", ""),
        ]
        kept = filter_take_yes_rows(rows, fieldnames=list(rows[0].keys()))
        self.assertEqual([r["base_coin"] for r in kept], ["AAA", "CCC"])

    def test_missing_take_column_fails_loud(self) -> None:
        rows = [_row("AAA", None)]
        with self.assertRaises(MissingTakeColumnError) as ctx:
            filter_take_yes_rows(rows, fieldnames=UNIVERSE_HEADER)
        self.assertIn("take", str(ctx.exception))
        self.assertIn("silently widen", str(ctx.exception))

    def test_empty_file_without_take_fails(self) -> None:
        with self.assertRaises(MissingTakeColumnError):
            require_take_column(["base_coin", "okx_symbol"])


class CollectorLoadPairsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _mixed_csv(self) -> Path:
        path = self.root / "mixed.csv"
        _write_csv(
            path,
            [
                _row("AAA", "yes"),
                _row("BBB", "no"),
                _row("CCC", "yes"),
                _row("DDD", "yes"),
                _row("EEE", "no"),
            ],
            include_take=True,
        )
        return path

    def test_prod_loader_filters_then_slices(self) -> None:
        from app.screaner_b_o import load_pairs_from_csv

        path = self._mixed_csv()
        coins = [p["base_coin"] for p in load_pairs_from_csv(path, 0, 10)]
        self.assertEqual(coins, ["AAA", "CCC", "DDD"])
        sliced = [p["base_coin"] for p in load_pairs_from_csv(path, 1, 3)]
        self.assertEqual(sliced, ["CCC", "DDD"])

    def test_lean_loader_filters_then_slices(self) -> None:
        if "screaner_local_lean" in sys.modules:
            lean = importlib.reload(sys.modules["screaner_local_lean"])
        else:
            lean = importlib.import_module("screaner_local_lean")
        path = self._mixed_csv()
        coins = [p["base_coin"] for p in lean.load_pairs_from_csv(path, 0, 10)]
        self.assertEqual(coins, ["AAA", "CCC", "DDD"])

    def test_prod_loader_missing_take_fails(self) -> None:
        from app.screaner_b_o import load_pairs_from_csv
        from app.utils.universe_csv import MissingTakeColumnError

        path = self.root / "no_take.csv"
        _write_csv(path, [_row("AAA", None)], include_take=False)
        with self.assertRaises(MissingTakeColumnError):
            load_pairs_from_csv(path, 0, 10)

    def test_lean_loader_missing_take_fails(self) -> None:
        from app.utils.universe_csv import MissingTakeColumnError

        if "screaner_local_lean" in sys.modules:
            lean = importlib.reload(sys.modules["screaner_local_lean"])
        else:
            lean = importlib.import_module("screaner_local_lean")
        path = self.root / "no_take.csv"
        _write_csv(path, [_row("AAA", None)], include_take=False)
        with self.assertRaises(MissingTakeColumnError):
            lean.load_pairs_from_csv(path, 0, 10)


class BotUniverseTakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_meta_keeps_take_no_helper_lists_yes(self) -> None:
        from app.bot.runtime import load_take_yes_coins, load_universe

        path = self.root / "mixed.csv"
        _write_csv(
            path,
            [_row("AAA", "yes"), _row("BBB", "no"), _row("CCC", "yes")],
            include_take=True,
        )
        universe = load_universe(path)
        self.assertEqual(set(universe), {"AAA", "BBB", "CCC"})
        self.assertEqual(universe["BBB"].okx_lot_size, 0.01)
        self.assertEqual(load_take_yes_coins(path), ["AAA", "CCC"])

    def test_missing_take_fails_closed(self) -> None:
        from app.bot.runtime import load_take_yes_coins, load_universe
        from app.utils.universe_csv import MissingTakeColumnError

        path = self.root / "no_take.csv"
        _write_csv(path, [_row("AAA", None)], include_take=False)
        with self.assertRaises(MissingTakeColumnError):
            load_universe(path)
        with self.assertRaises(MissingTakeColumnError):
            load_take_yes_coins(path)


if __name__ == "__main__":
    unittest.main()
