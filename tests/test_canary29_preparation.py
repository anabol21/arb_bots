from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from validation import run_response_manager_experiment as prep


class Canary29PreparationTests(unittest.TestCase):
    def test_prepare_sets_and_reads_back_only_new26(self) -> None:
        runtime = SimpleNamespace(
            universe={
                coin: SimpleNamespace(
                    okx_symbol=f"{coin}-USDT-SWAP",
                    bybit_symbol=f"{coin}USDT",
                )
                for coin in prep.CANARY29_POOL
            }
        )
        sent: list[object] = []
        bybit_reads: list[str] = []
        okx_reads: list[str] = []

        def set_one(targets: list[object], **_kwargs: object) -> dict[tuple[str, str], str]:
            sent.extend(targets)
            return {
                (venue, symbol): "1"
                for target in targets
                for venue, symbol in (
                    ("okx", target.okx_symbol),
                    ("bybit", target.bybit_symbol),
                )
            }

        def bybit_get(**kwargs: object) -> dict[str, object]:
            symbol = str(kwargs["query"]).split("symbol=", 1)[1].split("&", 1)[0]
            bybit_reads.append(symbol)
            return {"retCode": 0, "result": {"list": [{"symbol": symbol, "leverage": "1"}]}}

        def okx_get(url: str, _headers: object, *, timeout_sec: float) -> dict[str, object]:
            del timeout_sec
            okx_reads.append(url)
            return {"code": "0", "data": [{"lever": "1"}]}

        with (
            patch("app.bot.private.leverage_one.set_leverage_one", side_effect=set_one),
            patch.object(
                prep,
                "_credentials",
                return_value=(
                    SimpleNamespace(api_key="key", api_secret="secret", passphrase=None),
                    SimpleNamespace(api_key="key", api_secret="secret", passphrase="pass"),
                ),
            ),
            patch("app.bot.private.venue.endpoints_for_venue", return_value=SimpleNamespace(venue="live", okx_rest="okx", bybit_rest="bybit")),
            patch("app.bot.private.rest_readonly.build_okx_readonly_headers", return_value={}),
            patch("app.bot.private.ws_w4_baseline._bybit_signed_get", side_effect=bybit_get),
            patch("app.bot.private.ws_w4_baseline._http_get_json", side_effect=okx_get),
        ):
            confirmed = prep._set_and_readback(
                runtime,
                coins=prep.CANARY29_POOL,
                previously_confirmed=prep.PREVIOUSLY_CONFIRMED_1X,
            )

        self.assertEqual(len(sent), 26)
        self.assertTrue(prep.PREVIOUSLY_CONFIRMED_1X.isdisjoint({t.coin for t in sent}))
        self.assertEqual(len(bybit_reads), 26)
        self.assertEqual(len(okx_reads), 26)
        for old_coin in prep.PREVIOUSLY_CONFIRMED_1X:
            self.assertFalse(any(old_coin in symbol for symbol in bybit_reads + okx_reads))
        self.assertEqual(len(confirmed), 58)
        self.assertEqual(set(confirmed.values()), {"1"})


if __name__ == "__main__":
    unittest.main()
