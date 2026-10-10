from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from app.bot.private.order_sign import LiveCredentials
from app.bot.private.ws_private import (
    PrivateStreamRuntime,
    RestReseedResult,
    SequenceHealth,
)


class PrivateReseedRetryTests(unittest.TestCase):
    def _runtime(self, reseed: mock.Mock) -> PrivateStreamRuntime:
        runtime = PrivateStreamRuntime(
            exchange="okx",
            environment="live",
            symbol_alias="BTC-USDT-SWAP",
            journal=SimpleNamespace(append=lambda row: row),
            run_id="offline-test",
            credentials=LiveCredentials("", "", ""),
            rest_reseed=reseed,
            subscribe_symbols=("BTC-USDT-SWAP", "ETH-USDT-SWAP"),
        )
        return runtime

    def test_retries_one_native_once_then_continues_after_match(self) -> None:
        reseed = mock.Mock()
        reseed.reseed.side_effect = (
            RestReseedResult(matched=False, inconclusive=True),
            RestReseedResult(matched=True),
            RestReseedResult(matched=True),
        )
        runtime = self._runtime(reseed)

        with mock.patch("app.bot.private.ws_private.time.sleep") as sleep:
            result = runtime.run_rest_reseed()

        self.assertEqual(result["reconciliation_state"], "matched")
        self.assertEqual(
            [call.kwargs["symbol_alias"] for call in reseed.reseed.call_args_list],
            ["BTC-USDT-SWAP", "BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        )
        sleep.assert_called_once_with(1.0)
        self.assertEqual(runtime.sequence_state, SequenceHealth.HEALTHY)

    def test_persistent_failure_stops_after_three_attempts_and_keeps_later_native_untried(self) -> None:
        reseed = mock.Mock()
        reseed.reseed.return_value = RestReseedResult(matched=False, inconclusive=True)
        runtime = self._runtime(reseed)

        with mock.patch("app.bot.private.ws_private.time.sleep") as sleep:
            result = runtime.run_rest_reseed()

        self.assertEqual(result["reconciliation_state"], "inconclusive")
        self.assertEqual(reseed.reseed.call_count, 3)
        self.assertTrue(all(
            call.kwargs["symbol_alias"] == "BTC-USDT-SWAP"
            for call in reseed.reseed.call_args_list
        ))
        self.assertEqual(sleep.call_args_list, [mock.call(1.0), mock.call(1.0)])
        self.assertEqual(runtime.sequence_state, SequenceHealth.RESEED_REQUIRED)
        self.assertTrue(runtime.sends_blocked)


if __name__ == "__main__":
    unittest.main()
