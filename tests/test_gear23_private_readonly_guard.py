from __future__ import annotations

import asyncio
from collections import Counter
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import websockets  # noqa: F401
except ModuleNotFoundError:
    sys.modules["websockets"] = types.ModuleType("websockets")

from validation.gear23_private_readonly_child import _guard_socket, _start_readonly_session
from app.bot.private.ws_warm_session import LiveCredentials


class _Socket:
    exchange = "okx"
    channel = "trade"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_text(self, text: str) -> None:
        self.sent.append(text)

    def send_text_timed(self, text: str, **_kwargs) -> None:
        self.sent.append(text)

    async def asend(self, text: str) -> None:
        self.sent.append(text)


class Gear23ReadonlyGuardTests(unittest.TestCase):
    def test_auth_and_heartbeat_are_allowed_but_order_and_cancel_are_blocked(self) -> None:
        socket = _Socket()
        blocked: list[str] = []
        _guard_socket(socket, blocked, Counter())
        socket.send_text('{"op":"login"}')
        socket.send_text("ping")
        with self.assertRaisesRegex(RuntimeError, "blocked_ws_operation"):
            socket.send_text('{"id":"x","op":"order"}')
        with self.assertRaisesRegex(RuntimeError, "blocked_ws_operation"):
            socket.send_text_timed('{"id":"y","op":"cancel-order"}')
        with self.assertRaisesRegex(RuntimeError, "blocked_ws_operation"):
            asyncio.run(socket.asend('{"op":"batch-orders"}'))
        self.assertEqual(socket.sent, ['{"op":"login"}', "ping"])
        self.assertEqual(
            blocked,
            ["okx:trade:order", "okx:trade:cancel-order", "okx:trade:batch-orders"],
        )

    def test_child_session_start_supplies_credentials_without_network(self) -> None:
        env = {"VENUE": "live", "LIVE_ORDERS": "1"}
        bybit = LiveCredentials(api_key="bybit-key", api_secret="bybit-secret")
        okx = LiveCredentials(api_key="okx-key", api_secret="okx-secret", passphrase="p")
        session = object()
        with patch("validation.gear23_private_readonly_child.load_live_secrets", return_value=object()), \
             patch("validation.gear23_private_readonly_child._creds_from_live_secrets", side_effect=[bybit, okx]), \
             patch("validation.gear23_private_readonly_child.start_warm_private_session", return_value=session) as start:
            result = _start_readonly_session(
                env=env, coins=("BTC",), data_root=Path("/tmp/readonly"),
                socket_provider=lambda: None, stop_event=object(),
            )
        self.assertIs(result, session)
        kwargs = start.call_args.kwargs
        self.assertIs(kwargs["bybit_credentials"], bybit)
        self.assertIs(kwargs["okx_credentials"], okx)


if __name__ == "__main__":
    unittest.main()
