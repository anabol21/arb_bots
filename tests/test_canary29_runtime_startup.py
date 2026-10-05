from __future__ import annotations

import asyncio
import sys
from types import ModuleType
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

# This focused test exercises pure startup helpers; the workspace intentionally
# has no websocket dependency installed and must never open a network socket.
sys.modules.setdefault("websockets", ModuleType("websockets"))

from app.bot.runtime import BotRuntime, _canary29_okx_metadata


class Canary29MetadataTests(unittest.TestCase):
    def test_one_snapshot_populates_both_caches_for_full_pool(self) -> None:
        symbols = {f"C{i}-USDT-SWAP" for i in range(29)}
        rows = [
            {
                "instId": symbol,
                "instType": "SWAP",
                "settleCcy": "USDT",
                "ctVal": "1",
                "instIdCode": str(index + 1),
            }
            for index, symbol in enumerate(sorted(symbols))
        ]

        ct_vals, inst_codes = _canary29_okx_metadata(rows, symbols)

        self.assertEqual(set(ct_vals), symbols)
        self.assertEqual(set(inst_codes), symbols)
        self.assertTrue(all(value == Decimal("1") for value in ct_vals.values()))

    def test_incomplete_snapshot_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "okx_canary_metadata_incomplete"):
            _canary29_okx_metadata(
                [{"instId": "A-USDT-SWAP", "instType": "SWAP", "settleCcy": "USDT", "ctVal": "1", "instIdCode": "1"}],
                {"A-USDT-SWAP", "B-USDT-SWAP"},
            )


class Canary29PrivateShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_stop_waits_for_inflight_place(self) -> None:
        runtime = object.__new__(BotRuntime)
        runtime._terminal_private_execution = True
        runtime.stop_event = asyncio.Event()
        runtime._private_stop_event = asyncio.Event()
        runtime._synthetic_sender = None
        runtime._synthetic_sender_session = None
        runtime._private_warm = object()
        runtime.log = Mock()
        started = asyncio.Event()
        finish = asyncio.Event()

        async def blocked_place() -> None:
            started.set()
            await finish.wait()

        task = asyncio.create_task(blocked_place())
        runtime.theta_trade = SimpleNamespace(_terminal_place_task=task)
        runtime.stop_event.set()  # SIGTERM is delivered to the main runtime only.
        await started.wait()
        wait_for_place = asyncio.create_task(runtime._await_terminal_place_before_shutdown())
        await asyncio.sleep(0)
        self.assertFalse(runtime._private_stop_event.is_set())
        self.assertFalse(wait_for_place.done())

        finish.set()
        self.assertTrue(await wait_for_place)
        with patch("app.bot.private.ws_warm_session.clear_process_warm_session"):
            runtime._stop_synthetic_private_send()
        self.assertTrue(runtime._private_stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
