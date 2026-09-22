from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping

from app.bot.journal import JournalWriter
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.position_reconcile import SignedRestRestartPositionReconciler
from app.bot.stub_broker import StubBroker
from app.bot.theta_trade_manager import OpenPosition


def _open(side: str = "long", coin: str = "KAITO") -> OpenPosition:
    return OpenPosition(
        trade_id="trade-1",
        base_coin=coin,
        side=side,
        open_signal_ts_ms=1,
        open_fill_ts_ms=2,
        open_fill_spread=0.5,
        open_notional=20.0,
        open_theta_1m=0.6,
    )


class _Responses:
    def __init__(
        self,
        *,
        bybit_positions: list[dict[str, Any]] | None = None,
        bybit_orders: list[dict[str, Any]] | None = None,
        okx_positions: list[dict[str, Any]] | None = None,
        okx_orders: list[dict[str, Any]] | None = None,
        bybit_cursor: str = "",
    ) -> None:
        self.bybit_positions = bybit_positions or []
        self.bybit_orders = bybit_orders or []
        self.okx_positions = okx_positions or []
        self.okx_orders = okx_orders or []
        self.bybit_cursor = bybit_cursor
        self.calls: list[str] = []

    def __call__(
        self,
        url: str,
        _headers: Mapping[str, str],
        *,
        timeout_sec: float,
    ) -> Mapping[str, Any]:
        self.calls.append(url)
        self.assert_timeout(timeout_sec)
        if "/v5/position/list" in url:
            return {"retCode": 0, "result": {"list": self.bybit_positions}}
        if "/v5/order/realtime" in url:
            return {
                "retCode": 0,
                "result": {
                    "list": self.bybit_orders,
                    "nextPageCursor": self.bybit_cursor,
                },
            }
        if "/api/v5/account/positions" in url:
            return {"code": "0", "data": self.okx_positions}
        if "/api/v5/trade/orders-pending" in url:
            return {"code": "0", "data": self.okx_orders}
        raise AssertionError(f"unexpected URL: {url}")

    @staticmethod
    def assert_timeout(timeout_sec: float) -> None:
        if timeout_sec <= 0:
            raise AssertionError("timeout must be positive")


def _reconciler(responses: _Responses) -> SignedRestRestartPositionReconciler:
    return SignedRestRestartPositionReconciler(
        bybit_credentials=LiveCredentials(api_key="bybit-k", api_secret="bybit-s"),
        okx_credentials=LiveCredentials(
            api_key="okx-k", api_secret="okx-s", passphrase="okx-p"
        ),
        http_get_json=responses,
    )


def _check(
    reconciler: SignedRestRestartPositionReconciler,
    expected: OpenPosition | None,
):
    return reconciler.check(
        expected=expected,
        bybit_symbols=["KAITOUSDT", "WALUSDT"],
        okx_symbols=["KAITO-USDT-SWAP", "WAL-USDT-SWAP"],
        expected_bybit_symbol="KAITOUSDT" if expected is not None else None,
        expected_okx_symbol=(
            "KAITO-USDT-SWAP" if expected is not None else None
        ),
    )


class SignedRestRestartPositionReconcilerTests(unittest.TestCase):
    def test_flat_matches_only_when_both_venues_and_orders_are_flat(self) -> None:
        responses = _Responses()
        result = _check(_reconciler(responses), None)
        self.assertTrue(result.matched)
        self.assertEqual(result.reason, "matched")
        self.assertEqual(len(responses.calls), 4)

    def test_long_matches_inverse_venue_legs_for_same_coin(self) -> None:
        responses = _Responses(
            bybit_positions=[{"symbol": "KAITOUSDT", "side": "Sell", "size": "2"}],
            okx_positions=[
                {
                    "instId": "KAITO-USDT-SWAP",
                    "posSide": "long",
                    "pos": "2",
                }
            ],
        )
        result = _check(_reconciler(responses), _open("long"))
        self.assertTrue(result.matched)
        self.assertEqual((result.bybit_state, result.okx_state), ("sell", "buy"))

    def test_short_matches_inverse_venue_legs_for_same_coin(self) -> None:
        responses = _Responses(
            bybit_positions=[{"symbol": "KAITOUSDT", "side": "Buy", "size": "2"}],
            okx_positions=[
                {
                    "instId": "KAITO-USDT-SWAP",
                    "posSide": "short",
                    "pos": "2",
                }
            ],
        )
        result = _check(_reconciler(responses), _open("short"))
        self.assertTrue(result.matched)

    def test_wrong_side_or_symbol_fails_closed(self) -> None:
        responses = _Responses(
            bybit_positions=[{"symbol": "WALUSDT", "side": "Sell", "size": "2"}],
            okx_positions=[
                {"instId": "WAL-USDT-SWAP", "posSide": "long", "pos": "2"}
            ],
        )
        result = _check(_reconciler(responses), _open("long"))
        self.assertFalse(result.matched)
        self.assertEqual(result.reason, "expected_open_mismatch")

    def test_any_pool_open_order_fails_closed(self) -> None:
        responses = _Responses(bybit_orders=[{"symbol": "WALUSDT"}])
        result = _check(_reconciler(responses), None)
        self.assertFalse(result.matched)
        self.assertEqual(result.reason, "open_orders_present")

    def test_incomplete_pagination_is_inconclusive(self) -> None:
        responses = _Responses(bybit_cursor="more")
        result = _check(_reconciler(responses), None)
        self.assertFalse(result.matched)
        self.assertEqual(result.reason, "reconciliation_inconclusive")


class StubBrokerCommittedPositionTests(unittest.TestCase):
    def test_restore_committed_position_is_durable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            broker = StubBroker(data_root=root, journal=JournalWriter(root))
            broker.restore_committed_position(
                position="open_short", held_coin="kaito"
            )
            state = json.loads(
                (root / "state" / "position.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                state, {"position": "open_short", "held_coin": "KAITO"}
            )
            restarted = StubBroker(data_root=root, journal=JournalWriter(root))
            self.assertEqual(restarted.position, "open_short")
            self.assertEqual(restarted.held_coin, "KAITO")
            restarted.restore_committed_position(position=None, held_coin=None)
            self.assertFalse((root / "state" / "position.json").exists())

    def test_restore_rejects_inconsistent_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            broker = StubBroker(data_root=root, journal=JournalWriter(root))
            with self.assertRaisesRegex(ValueError, "committed_position_coin_mismatch"):
                broker.restore_committed_position(
                    position="open_long", held_coin=None
                )


if __name__ == "__main__":
    unittest.main()
