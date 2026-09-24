from __future__ import annotations

import unittest
from unittest.mock import patch

from app.bot.execution.live_synthetic_source import (
    LiveSyntheticSourceError,
    parse_live_synthetic_source_request,
)
from app.bot.theta_trade_manager import GEAR22_HTML_TOP30, SyntheticPolicyGateError, ThetaTradeConfig


def armed_env() -> dict[str, str]:
    return {
        "BBOT_EV2_LIVE_SYNTHETIC_SOURCE": "1",
        "BBOT_POLICY_MODE": "synthetic_roll_v1",
        "BBOT_PROFILE": "gear22_live_canary",
        "BBOT_BROKER": "private_live",
        "VENUE": "live",
        "LIVE_ORDERS": "1",
        "BBOT_THETA_LIVE_SEND": "1",
        "BBOT_SLOT_K": "1",
        "BBOT_EV2_MAX_ROUND_TRIPS": "3",
        "BBOT_EV2_MAX_DUAL_LEG_SUBMISSIONS": "6",
        "BBOT_SYNTHETIC_ROLL_SEED": "7",
        "BBOT_NOTIONAL_USDT": "10",
        "BBOT_COINS": ",".join(GEAR22_HTML_TOP30),
        "BBOT_EV2_LIVE_COIN": "KAITO",
        "BBOT_EV2_SHADOW": "0",
        "BBOT_EV2_AUDIT": "0",
    }


class LiveSyntheticSourceRequestTests(unittest.TestCase):
    def test_exact_bounded_request_is_parsed_without_order_capability(self) -> None:
        request = parse_live_synthetic_source_request(armed_env())
        self.assertEqual(request.execution_coin_order, ("KAITO",))
        self.assertEqual(request.notional_usdt_per_leg, 10)
        self.assertEqual(request.max_round_trips, 3)
        self.assertEqual(request.max_planned_dual_leg_submissions, 6)

    def test_missing_or_relaxed_gates_fail_closed(self) -> None:
        for key, value in (
            ("BBOT_EV2_LIVE_SYNTHETIC_SOURCE", "0"),
            ("BBOT_BROKER", "stub"),
            ("LIVE_ORDERS", "0"),
            ("BBOT_THETA_LIVE_SEND", "0"),
            ("BBOT_SLOT_K", "2"),
            ("BBOT_EV2_MAX_ROUND_TRIPS", "4"),
            ("BBOT_EV2_MAX_DUAL_LEG_SUBMISSIONS", "7"),
            ("BBOT_SYNTHETIC_ROLL_SEED", "8"),
            ("BBOT_NOTIONAL_USDT", "20"),
            ("BBOT_EV2_LIVE_COIN", "BTC"),
            ("BBOT_EV2_SHADOW", "1"),
            ("BBOT_EV2_AUDIT", "1"),
        ):
            with self.subTest(key=key, value=value):
                with self.assertRaises(LiveSyntheticSourceError):
                    parse_live_synthetic_source_request({**armed_env(), key: value})

    def test_frozen_feed_membership_and_order_are_required(self) -> None:
        coins = list(GEAR22_HTML_TOP30)
        coins.reverse()
        with self.assertRaises(LiveSyntheticSourceError):
            parse_live_synthetic_source_request({**armed_env(), "BBOT_COINS": ",".join(coins)})
        with self.assertRaises(LiveSyntheticSourceError):
            parse_live_synthetic_source_request({**armed_env(), "BBOT_COINS": ",".join(coins[:-1])})

    def test_existing_synthetic_live_gate_is_not_bypassed(self) -> None:
        with self.assertRaises(SyntheticPolicyGateError):
            ThetaTradeConfig.from_env(armed_env())

    def test_runtime_still_refuses_order_capable_path(self) -> None:
        with patch.dict("os.environ", armed_env(), clear=True):
            from app.bot.runtime import BotRuntime

            with self.assertRaisesRegex(RuntimeError, "ev2_live_execution_adapter_not_integrated"):
                BotRuntime()


if __name__ == "__main__":
    unittest.main()
