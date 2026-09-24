"""EV2 live WS frame construction is hermetic and does not arm transport."""

from __future__ import annotations

import json
import unittest

from app.bot.execution.contracts import Venue, derive_client_id
from app.bot.execution.live_ws_frames import Ev2LiveWsFinalizer
from app.bot.execution.transport import FrozenStaticFrame, TransportError
from app.bot.private.order_sign import LiveCredentials

INTENT = "95790b7c-8b21-4e82-bae2-1832ddc5bee1"


def _frame(venue: Venue, *, reduce_only: bool = False) -> FrozenStaticFrame:
    return FrozenStaticFrame(
        venue=venue,
        leg_id=f"leg_{venue.value}",
        instrument="CAPUSDT" if venue is Venue.BYBIT else "CAP-USDT-SWAP",
        side="buy" if venue is Venue.BYBIT else "sell",
        quantity="200" if venue is Venue.BYBIT else "2",
        reduce_only=reduce_only,
        client_id=derive_client_id(INTENT, venue, reduce_only=reduce_only),
        inst_id_code=None if venue is Venue.BYBIT else 333127,
    )


class LiveWsFrameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.finalize = Ev2LiveWsFinalizer(
            bybit_credentials=LiveCredentials("test_key", "test_secret")
        )

    def _text(self, frame: FrozenStaticFrame) -> dict[str, object]:
        return json.loads(self.finalize(
            frame,
            timestamp_ms=1_790_000_000_000,
            request_id=frame.client_id,
            client_id=frame.client_id,
        ))

    def test_bybit_market_frame_signed_at_boundary(self) -> None:
        frame = _frame(Venue.BYBIT)
        raw = self._text(frame)
        self.assertEqual(raw["op"], "order.create")
        self.assertEqual(raw["reqId"], frame.client_id)
        self.assertEqual(raw["args"][0]["qty"], "200")
        self.assertEqual(raw["args"][0]["orderType"], "Market")
        self.assertEqual(raw["args"][0]["timeInForce"], "IOC")
        self.assertEqual(raw["args"][0]["orderLinkId"], frame.client_id)
        self.assertNotIn("reduceOnly", raw["args"][0])
        self.assertEqual(raw["header"]["X-BAPI-API-KEY"], "test_key")
        self.assertIn("X-BAPI-SIGN", raw["header"])
        self.assertNotIn("test_secret", repr(self.finalize))

    def test_okx_market_frame_and_reduce_only(self) -> None:
        frame = _frame(Venue.OKX, reduce_only=True)
        raw = self._text(frame)
        self.assertEqual(raw["op"], "order")
        self.assertEqual(raw["id"], frame.client_id)
        self.assertEqual(raw["args"][0]["instIdCode"], 333127)
        self.assertEqual(raw["args"][0]["sz"], "2")
        self.assertEqual(raw["args"][0]["ordType"], "market")
        self.assertTrue(raw["args"][0]["reduceOnly"])
        self.assertNotIn("posSide", raw["args"][0])

    def test_fails_closed_on_id_quantity_or_metadata(self) -> None:
        frame = _frame(Venue.OKX)
        with self.assertRaises(TransportError):
            self.finalize(frame, timestamp_ms=1, request_id="other", client_id=frame.client_id)
        bad_qty = FrozenStaticFrame(**{**frame.__dict__, "quantity": "NaN"})
        with self.assertRaises(TransportError):
            self._text(bad_qty)
        bad_code = FrozenStaticFrame(**{**frame.__dict__, "inst_id_code": None})
        with self.assertRaises(TransportError):
            self._text(bad_code)
