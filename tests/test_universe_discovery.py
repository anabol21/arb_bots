"""Discovery intersection: notebook filters, join, cap — no live REST required."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.discovery.intersection import (  # noqa: E402
    DiscoveryError,
    build_intersection_rows,
    fetch_bybit_linear_instruments,
    fetch_okx_swap_instruments,
    filter_bybit_live_usdt_linear,
    filter_okx_live_usdt_swap,
    normalize_bybit_item,
    normalize_okx_item,
    run_discovery,
)
from app.utils.universe_delta import read_delta_rows  # noqa: E402


def _okx_item(
    base: str,
    *,
    state: str = "live",
    settle: str = "USDT",
    inst_type: str = "SWAP",
) -> dict[str, str]:
    return {
        "instId": f"{base}-USDT-SWAP",
        "baseCcy": base,
        "quoteCcy": "USDT",
        "instType": inst_type,
        "state": state,
        "settleCcy": settle,
        "tickSz": "0.01",
        "lotSz": "1",
        "minSz": "1",
    }


def _bybit_item(
    base: str,
    *,
    status: str = "Trading",
    quote: str = "USDT",
    settle: str = "USDT",
    category: str = "linear",
    symbol: str | None = None,
) -> dict[str, Any]:
    return {
        "symbol": symbol or f"{base}USDT",
        "baseCoin": base,
        "quoteCoin": quote,
        "settleCoin": settle,
        "category": category,
        "status": status,
        "priceFilter": {"tickSize": "0.01"},
        "lotSizeFilter": {
            "qtyStep": "1",
            "minOrderQty": "1",
            "minNotionalValue": "5",
        },
    }


class FilterJoinTests(unittest.TestCase):
    def test_okx_keeps_live_usdt_swap_only(self) -> None:
        rows = [
            normalize_okx_item(_okx_item("AAA")),
            normalize_okx_item(_okx_item("BBB", state="suspend")),
            normalize_okx_item(_okx_item("CCC", settle="USD")),
        ]
        kept = filter_okx_live_usdt_swap(rows)
        self.assertEqual([r["symbol_norm"] for r in kept], ["AAA"])

    def test_bybit_keeps_trading_usdt_linear_only(self) -> None:
        rows = [
            normalize_bybit_item(_bybit_item("AAA")),
            normalize_bybit_item(_bybit_item("BBB", status="Closed")),
            normalize_bybit_item(_bybit_item("CCC", quote="USDC")),
        ]
        kept = filter_bybit_live_usdt_linear(rows)
        self.assertEqual([r["symbol_norm"] for r in kept], ["AAA"])

    def test_dedupe_keeps_first_symbol_raw(self) -> None:
        rows = [
            normalize_bybit_item(_bybit_item("AAA", symbol="AAAUSDT-B")),
            normalize_bybit_item(_bybit_item("AAA", symbol="AAAUSDT")),
        ]
        kept = filter_bybit_live_usdt_linear(rows)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["symbol_raw"], "AAAUSDT")

    def test_inner_join_on_symbol_norm(self) -> None:
        okx = filter_okx_live_usdt_swap(
            [normalize_okx_item(_okx_item("AAA")), normalize_okx_item(_okx_item("ONLYOKX"))]
        )
        bybit = filter_bybit_live_usdt_linear(
            [
                normalize_bybit_item(_bybit_item("AAA")),
                normalize_bybit_item(_bybit_item("ONLYBYBIT")),
            ]
        )
        rows = build_intersection_rows(
            okx, bybit, discovered_at_utc="2026-09-18T00:00:00Z"
        )
        self.assertEqual([r["base_coin"] for r in rows], ["AAA"])
        self.assertEqual(rows[0]["okx_symbol"], "AAA-USDT-SWAP")
        self.assertEqual(rows[0]["bybit_symbol"], "AAAUSDT")
        self.assertEqual(rows[0]["okx_lot_size"], "1")
        self.assertEqual(rows[0]["bybit_qty_step"], "1")


class RunDiscoveryMockHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _http(self, universe_coins: list[str] = ("KEEP",)) -> Any:
        def http_get_json(url: str, params: Mapping[str, str]) -> dict[str, Any]:
            parsed = urlparse(url)
            if parsed.path.endswith("/instruments-info"):
                query = parse_qs(parsed.query)
                if query.get("cursor") or params.get("cursor"):
                    return {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}
                return {
                    "retCode": 0,
                    "result": {
                        "list": [
                            _bybit_item("KEEP"),
                            _bybit_item("NEWCOIN"),
                            _bybit_item("DEAD", status="Closed"),
                        ],
                        "nextPageCursor": "",
                    },
                }
            if parsed.path.endswith("/instruments"):
                return {
                    "code": "0",
                    "data": [
                        _okx_item("KEEP"),
                        _okx_item("NEWCOIN"),
                        _okx_item("DEADOKX", state="suspend"),
                    ],
                }
            raise AssertionError(f"unexpected url {url}")

        return http_get_json

    def _write_universe(self, path: Path, coins: list[str]) -> None:
        import csv

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
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for coin in coins:
                writer.writerow(
                    {
                        "base_coin": coin,
                        "okx_symbol": f"{coin}-USDT-SWAP",
                        "bybit_symbol": f"{coin}USDT",
                        "okx_tick_size": "0.1",
                        "okx_lot_size": "1",
                        "okx_min_size": "1",
                        "bybit_tick_size": "0.1",
                        "bybit_qty_step": "1",
                        "bybit_min_order_qty": "1",
                        "bybit_min_notional_value": "5",
                        "take": "yes",
                    }
                )

    def test_run_discovery_writes_capped_delta_not_universe(self) -> None:
        universe = self.root / "bybit_okx_universe.csv"
        delta = self.root / "hot_add_delta.csv"
        self._write_universe(universe, ["KEEP"])
        before = universe.read_bytes()
        summary = run_discovery(
            universe_path=universe,
            delta_path=delta,
            max_new=8,
            http_get_json=self._http(),
        )
        self.assertEqual(universe.read_bytes(), before)
        self.assertEqual(summary["delta_rows"], 1)
        self.assertEqual(summary["coins"], ["NEWCOIN"])
        rows = read_delta_rows(delta)
        self.assertEqual(rows[0]["base_coin"], "NEWCOIN")
        self.assertEqual(rows[0]["okx_symbol"], "NEWCOIN-USDT-SWAP")

    def test_hard_cap_drops_excess(self) -> None:
        universe = self.root / "u.csv"
        delta = self.root / "d.csv"
        self._write_universe(universe, ["KEEP"])
        summary = run_discovery(
            universe_path=universe,
            delta_path=delta,
            max_new=0,
            http_get_json=self._http(),
        )
        self.assertEqual(summary["delta_rows"], 0)
        self.assertEqual(summary["dropped_by_cap"], 1)
        self.assertEqual(read_delta_rows(delta), [])

    def test_refuse_rewrite_universe_path(self) -> None:
        universe = self.root / "u.csv"
        self._write_universe(universe, ["KEEP"])
        with self.assertRaises(Exception):
            run_discovery(
                universe_path=universe,
                delta_path=universe,
                max_new=8,
                http_get_json=self._http(),
            )

    def test_bybit_api_error_fails_loud(self) -> None:
        def http_get_json(url: str, params: Mapping[str, str]) -> dict[str, Any]:
            return {"retCode": 10001, "retMsg": "boom"}

        universe = self.root / "u.csv"
        self._write_universe(universe, ["KEEP"])
        with self.assertRaises(DiscoveryError):
            run_discovery(
                universe_path=universe,
                delta_path=self.root / "d.csv",
                http_get_json=http_get_json,
            )

    def test_fetch_pagination_uses_cursor(self) -> None:
        calls: list[Mapping[str, str]] = []

        def http_get_json(url: str, params: Mapping[str, str]) -> dict[str, Any]:
            calls.append(dict(params))
            if not params.get("cursor"):
                return {
                    "retCode": 0,
                    "result": {
                        "list": [_bybit_item("PAGE1")],
                        "nextPageCursor": "next",
                    },
                }
            return {
                "retCode": 0,
                "result": {"list": [_bybit_item("PAGE2")], "nextPageCursor": ""},
            }

        items = fetch_bybit_linear_instruments(
            http_get_json=http_get_json, page_pause_sec=0
        )
        self.assertEqual(len(items), 2)
        self.assertEqual(calls[1]["cursor"], "next")

    def test_okx_nonzero_code_fails_loud(self) -> None:
        def http_get_json(url: str, params: Mapping[str, str]) -> dict[str, Any]:
            return {"code": "50000", "msg": "nope"}

        with self.assertRaises(DiscoveryError):
            fetch_okx_swap_instruments(http_get_json=http_get_json)


if __name__ == "__main__":
    unittest.main()
