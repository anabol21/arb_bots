"""Universe CSV take=yes screen for live pair loaders."""

from __future__ import annotations

import csv
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
    load_take_yes_base_coins,
    load_take_yes_pairs,
    read_universe_dicts,
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

    def test_empty_header_without_take_fails(self) -> None:
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

    def test_filters_then_slices(self) -> None:
        path = self._mixed_csv()
        coins = [p["base_coin"] for p in load_take_yes_pairs(path, 0, 10)]
        self.assertEqual(coins, ["AAA", "CCC", "DDD"])
        sliced = [p["base_coin"] for p in load_take_yes_pairs(path, 1, 3)]
        self.assertEqual(sliced, ["CCC", "DDD"])
        first = load_take_yes_pairs(path, 0, 1)[0]
        self.assertEqual(first["okx_symbol"], "AAA-USDT-SWAP")
        self.assertEqual(first["bybit_symbol"], "AAAUSDT")

    def test_missing_take_fails(self) -> None:
        path = self.root / "no_take.csv"
        _write_csv(path, [_row("AAA", None)], include_take=False)
        with self.assertRaises(MissingTakeColumnError):
            load_take_yes_pairs(path, 0, 10)


class BotUniverseTakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_meta_rows_keep_take_no_helper_lists_yes(self) -> None:
        path = self.root / "mixed.csv"
        _write_csv(
            path,
            [_row("AAA", "yes"), _row("BBB", "no"), _row("CCC", "yes")],
            include_take=True,
        )
        coins = [row["base_coin"].strip().upper() for row in read_universe_dicts(path)]
        self.assertEqual(coins, ["AAA", "BBB", "CCC"])
        self.assertEqual(load_take_yes_base_coins(path), ["AAA", "CCC"])

    def test_missing_take_fails_closed(self) -> None:
        path = self.root / "no_take.csv"
        _write_csv(path, [_row("AAA", None)], include_take=False)
        with self.assertRaises(MissingTakeColumnError):
            read_universe_dicts(path)
        with self.assertRaises(MissingTakeColumnError):
            load_take_yes_base_coins(path)


class LoaderWiringTests(unittest.TestCase):
    def test_collectors_and_bot_call_shared_helpers(self) -> None:
        prod = (REPO / "app" / "screaner_b_o.py").read_text(encoding="utf-8")
        lean = (REPO / "app" / "screaner_local_lean.py").read_text(encoding="utf-8")
        bot = (REPO / "app" / "bot" / "runtime.py").read_text(encoding="utf-8")
        self.assertIn("return load_take_yes_pairs(path, row_start, row_end)", prod)
        self.assertIn("return load_take_yes_pairs(path, row_start, row_end)", lean)
        self.assertIn("read_universe_dicts", bot)
        self.assertIn("return load_take_yes_base_coins(csv_path)", bot)


class RepoUniverseCsvTests(unittest.TestCase):
    def test_repo_csv_take_yes_count_and_majors_remain_in_meta(self) -> None:
        path = REPO / "bybit_okx_universe.csv"
        rows = read_universe_dicts(path)
        yes = load_take_yes_base_coins(path)
        self.assertEqual(len(yes), 198)
        self.assertNotIn("BTC", yes)
        all_coins = {row["base_coin"].strip().upper() for row in rows}
        self.assertIn("BTC", all_coins)
        pairs = load_take_yes_pairs(path, 0, 337)
        self.assertEqual([p["base_coin"] for p in pairs], yes)
        self.assertTrue(all("okx_symbol" in p and "bybit_symbol" in p for p in pairs))


if __name__ == "__main__":
    unittest.main()
