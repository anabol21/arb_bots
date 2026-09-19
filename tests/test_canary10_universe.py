"""Canary10 universe backfill, is_crypto, dry-run delta_rows=0."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.discovery.intersection import build_intersection_rows  # noqa: E402
from app.utils.canary10_universe import (  # noqa: E402
    apply_take_yes_mask,
    backfill_rows_from_intersection,
    build_canary10_universe_rows,
    pick_take_yes_coins,
)
from app.utils.universe_delta import (  # noqa: E402
    csv_base_coins,
    diff_intersection_against_csv,
)
from research.is_crypto import is_crypto  # noqa: E402
from app.utils.canary10_guards import assert_dry_run_discovery_summary  # noqa: E402


def _prod_row(coin: str, take: str = "yes") -> dict[str, str]:
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


def _ix_row(coin: str) -> dict[str, str]:
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
        "discovered_at_utc": "2026-09-19T00:00:00Z",
    }


class BackfillTests(unittest.TestCase):
    def test_backfill_adds_missing_intersection_with_take_no(self) -> None:
        prod = [_prod_row("BTC"), _prod_row("ETH")]
        ix = [_ix_row("BTC"), _ix_row("ETH"), _ix_row("BRANDNEW")]
        merged, added = backfill_rows_from_intersection(prod, ix)
        self.assertEqual(added, 1)
        coins = {r["base_coin"] for r in merged}
        self.assertIn("BRANDNEW", coins)
        new_row = next(r for r in merged if r["base_coin"] == "BRANDNEW")
        self.assertEqual(new_row["take"], "no")

    def test_pick_ten_crypto_includes_core(self) -> None:
        prod = [
            _prod_row("BTC"),
            _prod_row("ETH"),
            _prod_row("SOL"),
            _prod_row("XRP"),
            _prod_row("ADA"),
            _prod_row("DOGE"),
            _prod_row("LINK"),
            _prod_row("AVAX"),
            _prod_row("DOT"),
            _prod_row("BNB"),
            _prod_row("LTC"),
        ]
        picked = pick_take_yes_coins(prod, take_yes_count=10)
        self.assertEqual(len(picked), 10)
        for core in ("BTC", "ETH", "SOL", "XRP"):
            self.assertIn(core, picked)

    def test_take_yes_exactly_ten(self) -> None:
        prod = [_prod_row(c) for c in ("BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LINK", "AVAX", "DOT", "BNB", "LTC")]
        ix = [_ix_row(c) for c in ("BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LINK", "AVAX", "DOT", "BNB", "LTC", "EXTRA")]
        final, take_yes, _ = build_canary10_universe_rows(prod, ix, take_yes_count=10)
        self.assertEqual(len(take_yes), 10)
        yes_rows = [r for r in final if r["take"] == "yes"]
        self.assertEqual(len(yes_rows), 10)
        self.assertEqual({r["base_coin"] for r in yes_rows}, set(take_yes))

    def test_dry_run_delta_zero_after_backfill(self) -> None:
        prod = [_prod_row(c) for c in ("BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LINK", "AVAX", "DOT", "BNB")]
        ix = [_ix_row(c) for c in ("BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LINK", "AVAX", "DOT", "BNB", "ZZZBACKFILLED")]
        final, _, _ = build_canary10_universe_rows(prod, ix, take_yes_count=10)
        csv_coins = {r["base_coin"] for r in final}
        fresh = diff_intersection_against_csv(ix, csv_coins)
        self.assertEqual(fresh, [])
        summary = {
            "csv_coins": len(csv_coins),
            "delta_rows": 0,
            "new_before_cap": 0,
            "intersection": len(ix),
        }
        assert_dry_run_discovery_summary(summary, min_csv_coins=10)

    def test_non_crypto_in_copy_but_not_in_crypto_delta_diff(self) -> None:
        self.assertFalse(is_crypto("AAPL"))
        ix = build_intersection_rows(
            [
                {
                    "symbol_norm": "AAPL",
                    "symbol_raw": "AAPL-USDT-SWAP",
                    "tick_size": "0.01",
                    "lot_size": "1",
                    "min_size": "1",
                },
                {
                    "symbol_norm": "BTC",
                    "symbol_raw": "BTC-USDT-SWAP",
                    "tick_size": "0.01",
                    "lot_size": "1",
                    "min_size": "1",
                },
            ],
            [
                {
                    "symbol_norm": "AAPL",
                    "symbol_raw": "AAPLUSDT",
                    "tick_size": "0.01",
                    "qty_step": "1",
                    "min_order_qty": "1",
                    "min_notional_value": "5",
                },
                {
                    "symbol_norm": "BTC",
                    "symbol_raw": "BTCUSDT",
                    "tick_size": "0.01",
                    "qty_step": "1",
                    "min_order_qty": "1",
                    "min_notional_value": "5",
                },
            ],
        )
        prod = [_prod_row("BTC")]
        merged, added = backfill_rows_from_intersection(prod, ix)
        self.assertEqual(added, 1)
        self.assertIn("AAPL", {r["base_coin"] for r in merged})
        csv_set = {r["base_coin"] for r in merged}
        crypto_ix = [r for r in ix if is_crypto(r["base_coin"])]
        fresh = diff_intersection_against_csv(crypto_ix, csv_set)
        self.assertEqual(fresh, [])


class HotAddCryptoFilterTests(unittest.TestCase):
    def test_apply_take_yes_rejects_non_crypto(self) -> None:
        rows = [_prod_row("BTC", take="no")]
        with self.assertRaises(ValueError):
            apply_take_yes_mask(rows, ["AAPL"])


if __name__ == "__main__":
    unittest.main()
