"""Optional timestamp marks the yielded WebSocket message before parsing."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from app.bot.ws_books import _listen_loop


class _Socket:
    def __init__(self, messages):
        self.messages = list(messages)

    async def send(self, _payload):
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)

    async def close(self):
        return None


class _Connector:
    def __init__(self, socket):
        self.socket = socket

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, *_args):
        return None


class WebSocketIngressTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_marks_message_yield_before_parse_and_preserves_default_callback(self):
        stop = asyncio.Event()
        parsed_ns = []
        callback = []
        socket = _Socket(["message"])

        def parse(_message):
            parsed_ns.append(__import__("time").monotonic_ns())
            return {"bid_price": 1.0}

        def on_book(_coin, _exchange, _book, received_ns):
            callback.append(received_ns)
            stop.set()

        with patch("app.bot.ws_books.websockets.connect", return_value=_Connector(socket), create=True):
            await _listen_loop(
                exchange="okx", channel="test", base_coin="BTC", url="wss://example",
                subscribe_payload={"op": "subscribe"}, parse_message=parse,
                book_store={}, on_book=on_book, stop_event=stop,
                capture_monotonic=True,
            )
        self.assertEqual(len(callback), 1)
        self.assertLess(callback[0], parsed_ns[0])

    async def test_capture_disabled_keeps_three_argument_callback(self):
        stop = asyncio.Event()
        calls = []
        socket = _Socket(["message"])

        def parse(_message):
            return {"bid_price": 1.0}

        def on_book(coin, exchange, _book):
            calls.append((coin, exchange))
            stop.set()

        with patch("app.bot.ws_books.websockets.connect", return_value=_Connector(socket), create=True):
            await _listen_loop(
                exchange="okx", channel="test", base_coin="BTC", url="wss://example",
                subscribe_payload={"op": "subscribe"}, parse_message=parse,
                book_store={}, on_book=on_book, stop_event=stop,
            )
        self.assertEqual(calls, [("BTC", "okx")])


if __name__ == "__main__":
    unittest.main()
