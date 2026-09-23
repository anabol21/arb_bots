"""EV2-12C1: exact native quantity comparison, not signed approval."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from app.bot.execution.contracts import Venue
from app.bot.execution.venue_quantity_compare import (
    QuantityCompareError,
    compare_candidate_quantities,
)
from tests.test_execution_manager_candidate_journal import _candidate


_INSTRUMENTS = {Venue.BYBIT: "BTCUSDT", Venue.OKX: "BTC-USDT-SWAP"}
_POOL = {
    Venue.BYBIT: ["BTCUSDT", "ETHUSDT"],
    Venue.OKX: ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
}


def _responses():
    return {
        "bybit_positions": {
            "retCode": 0,
            "result": {
                "list": [{
                    "symbol": "BTCUSDT", "positionIdx": 0,
                    "side": "Sell", "size": "1",
                }],
                "nextPageCursor": "",
            },
        },
        "okx_positions": {
            "code": "0",
            "data": [{"instId": "BTC-USDT-SWAP", "posSide": "net", "pos": "1"}],
        },
        "bybit_open_orders": {
            "retCode": 0,
            "result": {"list": [], "nextPageCursor": ""},
        },
        "okx_open_orders": {"code": "0", "data": []},
    }


class VenueQuantityCompareTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.candidate = _candidate(Path(temporary.name))

    def compare(self, responses):
        return compare_candidate_quantities(
            candidate=self.candidate,
            instruments=_INSTRUMENTS,
            pool_symbols=_POOL,
            **responses,
        )

    def test_exact_native_quantities_match_but_do_not_authorize_publication(self) -> None:
        result = self.compare(_responses())
        self.assertTrue(result.matched)
        self.assertEqual(result.reason, "quantity_matched")
        self.assertEqual(result.bybit_quantity, Decimal("1"))
        self.assertEqual(result.okx_quantity, Decimal("1"))
        self.assertTrue(result.requires_signed_fresh_source)

    def test_quantity_mismatch_or_unilateral_position_is_not_matched(self) -> None:
        responses = _responses()
        responses["okx_positions"]["data"][0]["pos"] = "0.5"
        self.assertFalse(self.compare(responses).matched)
        responses["okx_positions"]["data"] = []
        self.assertFalse(self.compare(responses).matched)

    def test_extra_pool_position_fails_closed(self) -> None:
        responses = _responses()
        responses["bybit_positions"]["result"]["list"].append({
            "symbol": "ETHUSDT", "positionIdx": 0,
            "side": "Buy", "size": "2",
        })
        with self.assertRaisesRegex(QuantityCompareError, "extra_pool_position"):
            self.compare(responses)

    def test_open_order_or_unread_bybit_page_cannot_match(self) -> None:
        responses = _responses()
        responses["okx_open_orders"]["data"] = [{"instId": "ETH-USDT-SWAP"}]
        self.assertEqual(self.compare(responses).reason, "pool_open_orders_present")
        responses = _responses()
        responses["bybit_positions"]["result"]["nextPageCursor"] = "more"
        with self.assertRaisesRegex(QuantityCompareError, "bybit_pagination_incomplete"):
            self.compare(responses)
        responses = _responses()
        del responses["bybit_open_orders"]["result"]["nextPageCursor"]
        with self.assertRaisesRegex(QuantityCompareError, "bybit_pagination_unknown"):
            self.compare(responses)

    def test_unsupported_position_modes_and_bad_numbers_fail_closed(self) -> None:
        responses = _responses()
        responses["bybit_positions"]["result"]["list"][0]["positionIdx"] = 2
        with self.assertRaisesRegex(QuantityCompareError, "bybit_position_mode_unsupported"):
            self.compare(responses)
        responses = _responses()
        responses["okx_positions"]["data"][0]["posSide"] = "long"
        with self.assertRaisesRegex(QuantityCompareError, "okx_position_mode_unsupported"):
            self.compare(responses)
        responses = _responses()
        responses["okx_positions"]["data"][0]["pos"] = "NaN"
        with self.assertRaisesRegex(QuantityCompareError, "invalid_quantity"):
            self.compare(responses)

    def test_close_requires_zero_on_both_venues_and_no_orders(self) -> None:
        close_projection = replace(
            self.candidate.projection,
            publication="close",
            legs=tuple(
                replace(leg, effective_open_quantity=Decimal("0"))
                for leg in self.candidate.projection.legs
            ),
        )
        self.candidate = replace(self.candidate, projection=close_projection)
        responses = _responses()
        self.assertEqual(self.compare(responses).reason, "not_flat")
        responses["bybit_positions"]["result"]["list"] = []
        responses["okx_positions"]["data"] = []
        result = self.compare(responses)
        self.assertTrue(result.matched)
        self.assertTrue(result.requires_signed_fresh_source)


if __name__ == "__main__":
    unittest.main()
