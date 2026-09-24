"""Hermetic signed read-only EV2 flat-proof tests; no network or orders."""

from __future__ import annotations

import unittest
from typing import Any, Mapping

from app.bot.execution.engine import ReadinessSnapshot
from app.bot.execution.live_rest_flat import CompleteLiveRestReader, LiveRestFlatError
from app.bot.private.order_sign import LiveCredentials


def _ready(generation: int = 1) -> ReadinessSnapshot:
    return ReadinessSnapshot(
        bybit_trade_ready=True, okx_trade_ready=True,
        bybit_private_ready=True, okx_private_ready=True,
        bybit_generation=generation, okx_generation=generation,
        kill_switch=False, pause=False,
    )


class LiveRestFlatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[str] = []
        self.bybit_pos: list[Mapping[str, Any]] = []
        self.bybit_orders: list[Mapping[str, Any]] = []
        self.okx_pos: list[Mapping[str, Any]] = []
        self.okx_orders: list[Mapping[str, Any]] = []
        self.generation = 1

    def _get(self, url: str, _headers: Mapping[str, str], **_kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(url)
        if "/v5/position/list" in url:
            return {"retCode": 0, "result": {"list": self.bybit_pos, "nextPageCursor": ""}}
        if "/v5/order/realtime" in url:
            return {"retCode": 0, "result": {"list": self.bybit_orders, "nextPageCursor": ""}}
        if "/api/v5/account/positions" in url:
            return {"code": "0", "data": self.okx_pos}
        if "/api/v5/trade/orders-pending" in url:
            return {"code": "0", "data": self.okx_orders}
        raise AssertionError("unexpected signed path")

    def _reader(self) -> CompleteLiveRestReader:
        return CompleteLiveRestReader(
            bybit_credentials=LiveCredentials("bybit", "secret"),
            okx_credentials=LiveCredentials("okx", "secret", "passphrase"),
            readiness=lambda: _ready(self.generation),
            http_get_json=self._get,
        )

    def test_complete_empty_account_is_flat(self) -> None:
        result = self._reader().capture()
        result.assert_account_flat()
        self.assertEqual(len(self.calls), 4)
        self.assertNotIn("secret", repr(result))

    def test_existing_position_or_order_blocks(self) -> None:
        self.bybit_pos = [{"symbol": "CAPUSDT", "positionIdx": 0, "size": "100"}]
        with self.assertRaisesRegex(LiveRestFlatError, "bybit_position_not_flat"):
            self._reader().capture().assert_account_flat()
        self.bybit_pos = []
        self.okx_orders = [{"ordId": "123", "instId": "CAP-USDT-SWAP"}]
        with self.assertRaisesRegex(LiveRestFlatError, "open_orders_remain"):
            self._reader().capture().assert_account_flat()

    def test_hedge_mode_row_blocks_even_at_zero(self) -> None:
        self.bybit_pos = [{"symbol": "CAPUSDT", "positionIdx": 1, "size": "0"}]
        with self.assertRaisesRegex(LiveRestFlatError, "bybit_position_mode"):
            self._reader().capture().assert_account_flat()

    def test_bybit_cursor_must_be_consumed(self) -> None:
        pages = 0

        def paged(url: str, headers: Mapping[str, str], **kwargs: Any) -> Mapping[str, Any]:
            nonlocal pages
            if "/v5/order/realtime" in url:
                pages += 1
                if pages == 1:
                    return {"retCode": 0, "result": {"list": [], "nextPageCursor": "next"}}
                self.assertIn("cursor=next", url)
                return {"retCode": 0, "result": {
                    "list": [{"orderId": "123", "symbol": "CAPUSDT"}],
                    "nextPageCursor": "",
                }}
            return self._get(url, headers, **kwargs)

        reader = CompleteLiveRestReader(
            bybit_credentials=LiveCredentials("bybit", "secret"),
            okx_credentials=LiveCredentials("okx", "secret", "passphrase"),
            readiness=_ready,
            http_get_json=paged,
        )
        with self.assertRaisesRegex(LiveRestFlatError, "open_orders_remain"):
            reader.capture().assert_account_flat()
        self.assertEqual(pages, 2)

    def test_generation_change_rejects_snapshot(self) -> None:
        calls = 0

        def changing() -> ReadinessSnapshot:
            nonlocal calls
            calls += 1
            return _ready(calls)

        reader = CompleteLiveRestReader(
            bybit_credentials=LiveCredentials("bybit", "secret"),
            okx_credentials=LiveCredentials("okx", "secret", "passphrase"),
            readiness=changing,
            http_get_json=self._get,
        )
        with self.assertRaisesRegex(LiveRestFlatError, "generation_changed"):
            reader.capture()

    def test_repeating_cursor_rejects_snapshot(self) -> None:
        def repeating(url: str, headers: Mapping[str, str], **kwargs: Any) -> Mapping[str, Any]:
            if "/v5/order/realtime" in url:
                return {"retCode": 0, "result": {"list": [], "nextPageCursor": "same"}}
            return self._get(url, headers, **kwargs)

        reader = CompleteLiveRestReader(
            bybit_credentials=LiveCredentials("bybit", "secret"),
            okx_credentials=LiveCredentials("okx", "secret", "passphrase"),
            readiness=_ready,
            http_get_json=repeating,
        )
        with self.assertRaisesRegex(LiveRestFlatError, "cursor_invalid"):
            reader.capture()


if __name__ == "__main__":
    unittest.main()
