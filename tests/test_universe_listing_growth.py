"""Synthetic catalog tests for universe listing growth (no live HTTP)."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from research.universe_listing_growth import (
    ANCHOR_CSV_FREEZE,
    JointName,
    build_report,
    daily_cadence,
    intersect_live,
    is_new,
    later_venue,
    load_csv_names,
    ms_to_iso,
    parse_bybit_linear,
    parse_okx_swap,
    write_html,
    write_names_csv,
)


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _joint(
    base: str,
    *,
    bybit_iso: str,
    okx_iso: str,
    in_csv: bool,
    crypto: bool,
) -> JointName:
    b = _ms(bybit_iso)
    o = _ms(okx_iso)
    return JointName(
        base_coin=base,
        bybit_symbol=f"{base}USDT",
        okx_symbol=f"{base}-USDT-SWAP",
        bybit_listed_ms=b,
        okx_listed_ms=o,
        joint_listed_ms=max(b, o),
        later_venue=later_venue(b, o),
        in_csv=in_csv,
        is_crypto=crypto,
    )


class ParseCatalogs(unittest.TestCase):
    def test_bybit_keeps_usdt_trading_perp(self):
        payload = {
            "result": {
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "baseCoin": "BTC",
                        "settleCoin": "USDT",
                        "status": "Trading",
                        "contractType": "LinearPerpetual",
                        "launchTime": "1000",
                    },
                    {
                        "symbol": "ETHUSD",
                        "baseCoin": "ETH",
                        "settleCoin": "USD",
                        "status": "Trading",
                        "contractType": "InversePerpetual",
                        "launchTime": "1000",
                    },
                    {
                        "symbol": "SOLUSDT",
                        "baseCoin": "SOL",
                        "settleCoin": "USDT",
                        "status": "Closed",
                        "contractType": "LinearPerpetual",
                        "launchTime": "1000",
                    },
                    {
                        "symbol": "BTCUSDT-26SEP",
                        "baseCoin": "BTC",
                        "settleCoin": "USDT",
                        "status": "Trading",
                        "contractType": "LinearFutures",
                        "launchTime": "1",
                    },
                ]
            }
        }
        out = parse_bybit_linear(payload)
        self.assertEqual(set(out), {"BTC"})
        self.assertEqual(out["BTC"].symbol, "BTCUSDT")
        self.assertEqual(out["BTC"].listed_ms, 1000)

    def test_bybit_keeps_earliest_launch_for_duplicate_base(self):
        payload = [
            {
                "symbol": "PEPEUSDT",
                "baseCoin": "PEPE",
                "settleCoin": "USDT",
                "status": "Trading",
                "contractType": "LinearPerpetual",
                "launchTime": "2000",
            },
            {
                "symbol": "1000PEPEUSDT",
                "baseCoin": "PEPE",
                "settleCoin": "USDT",
                "status": "Trading",
                "contractType": "LinearPerpetual",
                "launchTime": "500",
            },
        ]
        out = parse_bybit_linear(payload)
        self.assertEqual(out["PEPE"].listed_ms, 500)
        self.assertEqual(out["PEPE"].symbol, "1000PEPEUSDT")

    def test_okx_keeps_live_usdt_swap(self):
        payload = {
            "code": "0",
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "state": "live",
                    "settleCcy": "USDT",
                    "listTime": "2000",
                },
                {
                    "instId": "BTC-USD-SWAP",
                    "state": "live",
                    "settleCcy": "USD",
                    "listTime": "2000",
                },
                {
                    "instId": "ETH-USDT-SWAP",
                    "state": "suspend",
                    "settleCcy": "USDT",
                    "listTime": "2000",
                },
            ],
        }
        out = parse_okx_swap(payload)
        self.assertEqual(set(out), {"BTC"})
        self.assertEqual(out["BTC"].symbol, "BTC-USDT-SWAP")


class Intersection(unittest.TestCase):
    def test_joint_listed_is_max_and_later_venue(self):
        bybit = parse_bybit_linear(
            [
                {
                    "symbol": "NEWUSDT",
                    "baseCoin": "NEW",
                    "settleCoin": "USDT",
                    "status": "Trading",
                    "contractType": "LinearPerpetual",
                    "launchTime": str(_ms("2026-09-01T00:00:00Z")),
                },
                {
                    "symbol": "OLDUSDT",
                    "baseCoin": "OLD",
                    "settleCoin": "USDT",
                    "status": "Trading",
                    "contractType": "LinearPerpetual",
                    "launchTime": str(_ms("2025-01-01T00:00:00Z")),
                },
            ]
        )
        okx = parse_okx_swap(
            [
                {
                    "instId": "NEW-USDT-SWAP",
                    "state": "live",
                    "settleCcy": "USDT",
                    "listTime": str(_ms("2026-08-01T00:00:00Z")),
                },
                {
                    "instId": "OLD-USDT-SWAP",
                    "state": "live",
                    "settleCcy": "USDT",
                    "listTime": str(_ms("2025-06-01T00:00:00Z")),
                },
                {
                    "instId": "OKXONLY-USDT-SWAP",
                    "state": "live",
                    "settleCcy": "USDT",
                    "listTime": "1",
                },
            ]
        )
        names = intersect_live(bybit, okx, csv_names=["OLD"])
        by_base = {n.base_coin: n for n in names}
        self.assertEqual(set(by_base), {"NEW", "OLD"})
        self.assertEqual(by_base["NEW"].later_venue, "bybit")
        self.assertEqual(by_base["NEW"].joint_listed_utc.date().isoformat(), "2026-09-01")
        self.assertFalse(by_base["NEW"].in_csv)
        self.assertTrue(by_base["OLD"].in_csv)
        self.assertEqual(by_base["OLD"].later_venue, "okx")

    def test_live_minus_csv_and_delist(self):
        names = [
            _joint("BTC", bybit_iso="2020-01-01T00:00:00Z", okx_iso="2020-01-02T00:00:00Z", in_csv=True, crypto=True),
            _joint("ZZZ", bybit_iso="2026-09-01T00:00:00Z", okx_iso="2026-09-01T00:00:00Z", in_csv=False, crypto=True),
        ]
        report = build_report(names, ["BTC", "GONE"])
        self.assertEqual(report.n_csv, 2)
        self.assertEqual(report.n_live, 2)
        self.assertEqual(report.live_minus_csv, ["ZZZ"])
        self.assertEqual(report.csv_minus_live, ["GONE"])


class Cadence(unittest.TestCase):
    def test_daily_bars_ignore_pre_anchor_and_equities_when_crypto_only(self):
        names = [
            _joint(  # listed before freeze, not in CSV → snapshot gap, not a bar
                "MISS",
                bybit_iso="2026-06-01T00:00:00Z",
                okx_iso="2026-06-02T00:00:00Z",
                in_csv=False,
                crypto=True,
            ),
            _joint(
                "CRYP",
                bybit_iso="2026-08-20T00:00:00Z",
                okx_iso="2026-08-18T00:00:00Z",
                in_csv=False,
                crypto=True,
            ),
            _joint(
                "AAPL",
                bybit_iso="2026-08-21T00:00:00Z",
                okx_iso="2026-08-21T00:00:00Z",
                in_csv=False,
                crypto=False,
            ),
        ]
        until = datetime(2026, 8, 31, tzinfo=timezone.utc)
        all_rows = daily_cadence(names, anchor=ANCHOR_CSV_FREEZE, until=until, crypto_only=False)
        cr_rows = daily_cadence(names, anchor=ANCHOR_CSV_FREEZE, until=until, crypto_only=True)
        by_day_all = {r["day_utc"]: r for r in all_rows}
        by_day_cr = {r["day_utc"]: r for r in cr_rows}
        self.assertEqual(by_day_all["2026-08-14"]["n_new"], 0)
        self.assertEqual(by_day_all["2026-08-20"]["n_new"], 1)
        self.assertEqual(by_day_all["2026-08-21"]["n_new"], 1)
        self.assertEqual(by_day_cr["2026-08-20"]["n_new"], 1)
        self.assertEqual(by_day_cr["2026-08-21"]["n_new"], 0)
        self.assertEqual(all_rows[-1]["n_cum"], 2)
        self.assertEqual(cr_rows[-1]["n_cum"], 1)
        self.assertEqual(len(all_rows), 18)  # 14 Aug .. 31 Aug inclusive
        self.assertTrue(is_new(names[0], anchor=ANCHOR_CSV_FREEZE))
        self.assertFalse(
            is_new(
                _joint("BTC", bybit_iso="2020-01-01T00:00:00Z", okx_iso="2020-01-01T00:00:00Z", in_csv=True, crypto=True),
                anchor=ANCHOR_CSV_FREEZE,
            )
        )

    def test_july_anchor_includes_august_listings(self):
        names = [
            _joint(
                "JUL",
                bybit_iso="2026-07-10T00:00:00Z",
                okx_iso="2026-07-10T00:00:00Z",
                in_csv=False,
                crypto=True,
            ),
            _joint(
                "AUG",
                bybit_iso="2026-08-20T00:00:00Z",
                okx_iso="2026-08-20T00:00:00Z",
                in_csv=False,
                crypto=True,
            ),
        ]
        july = datetime(2026, 7, 1, tzinfo=timezone.utc)
        until = datetime(2026, 8, 31, tzinfo=timezone.utc)
        rows = daily_cadence(names, anchor=july, until=until, crypto_only=True)
        by_day = {r["day_utc"]: r for r in rows}
        self.assertEqual(by_day["2026-07-10"]["n_new"], 1)
        self.assertEqual(by_day["2026-08-20"]["n_new"], 1)
        self.assertEqual(rows[-1]["n_cum"], 2)


class Io(unittest.TestCase):
    def test_load_csv_names_and_html(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            uni = root / "u.csv"
            uni.write_text("base_coin,take\nBTC,yes\naapl,no\n", encoding="utf-8")
            self.assertEqual(load_csv_names(uni), ["BTC", "AAPL"])
            names = [
                _joint("BTC", bybit_iso="2020-01-01T00:00:00Z", okx_iso="2020-01-01T00:00:00Z", in_csv=True, crypto=True),
                _joint("NEW", bybit_iso="2026-09-01T00:00:00Z", okx_iso="2026-09-01T00:00:00Z", in_csv=False, crypto=True),
            ]
            report = build_report(names, ["BTC"])
            html_path = root / "out.html"
            csv_path = root / "out.csv"
            empty = [{"day_utc": "2026-08-14", "n_new": 1, "n_cum": 1}]
            write_html(
                html_path,
                report,
                daily_all_freeze=empty,
                daily_crypto_freeze=empty,
                daily_all_july=empty,
                daily_crypto_july=empty,
            )
            write_names_csv(csv_path, names)
            text = html_path.read_text(encoding="utf-8")
            self.assertIn("n_csv", text)
            self.assertIn("NEW", text)
            self.assertIn(ms_to_iso(names[1].joint_listed_ms), csv_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
