"""OKX 64003 fee-tier nack must not become auth_reject / reconnect storm."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.bot.private.journal_v1 import (
    PrivateJournalWriter,
    new_opaque_id,
    scan_all_journal_events,
)
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.ws_messages import build_okx_private_subscribe
from app.bot.private.ws_private import (
    PrivateStreamRuntime,
    RestReseedResult,
    SequenceHealth,
    SubscriptionReadiness,
    is_okx_fee_tier_channel_error,
)
from app.bot.private.ws_socket import FakePrivateWsSocket


def _okx_runtime(td: str) -> PrivateStreamRuntime:
    journal = PrivateJournalWriter(Path(td), run_id=new_opaque_id("run"))
    return PrivateStreamRuntime(
        exchange="okx",
        environment="live",
        symbol_alias="WAL-USDT-SWAP",
        subscribe_symbols=("WAL-USDT-SWAP", "EDEN-USDT-SWAP"),
        journal=journal,
        run_id=journal.run_id,
        credentials=LiveCredentials(
            api_key="okx-live-key-ABCDEF",
            api_secret="okx-live-secret-XYZ",
            passphrase="okx-passphrase-SECRET",
        ),
        gate_env={"VENUE": "live", "LIVE_ORDERS": "0"},
    )


class OkxFeeTierSubscribeTests(unittest.TestCase):
    def test_subscribe_omits_vip_fills_channel(self) -> None:
        sub = json.loads(
            build_okx_private_subscribe(
                symbols=("WAL-USDT-SWAP", "EDEN-USDT-SWAP")
            ).text
        )
        channels = [a["channel"] for a in sub["args"]]
        insts = [a["instId"] for a in sub["args"]]
        self.assertEqual(set(channels), {"orders", "positions"})
        self.assertNotIn("fills", channels)
        self.assertEqual(
            insts,
            [
                "WAL-USDT-SWAP",
                "WAL-USDT-SWAP",
                "EDEN-USDT-SWAP",
                "EDEN-USDT-SWAP",
            ],
        )

    def test_classifier_64003_and_fee_tier_msg(self) -> None:
        self.assertTrue(
            is_okx_fee_tier_channel_error({"code": "64003", "arg": None})
        )
        self.assertTrue(is_okx_fee_tier_channel_error({"code": 64003}))
        self.assertTrue(
            is_okx_fee_tier_channel_error(
                {
                    "msg": (
                        "Your trading fee tier doesn't meet the requirement "
                        "to access this channel."
                    )
                }
            )
        )
        self.assertFalse(is_okx_fee_tier_channel_error({"code": "60007"}))
        self.assertFalse(is_okx_fee_tier_channel_error({"msg": "Invalid sign"}))

    def test_64003_null_arg_is_sub_ack_not_auth_reject(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rt = _okx_runtime(td)
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket()
            rt.bind_sockets(private=priv, trade=trade, env=rt.gate_env)
            rt.handle_inbound_text(json.dumps({"event": "login", "code": "0"}))
            self.assertTrue(rt.authenticated)
            rt.send_subscribe()
            parsed = rt.handle_inbound_text(
                json.dumps(
                    {
                        "event": "error",
                        "code": "64003",
                        "msg": (
                            "Your trading fee tier doesn't meet the "
                            "requirement to access this channel."
                        ),
                        "arg": None,
                    }
                )
            )
            self.assertEqual(parsed.kind, "sub_ack")
            self.assertFalse(parsed.ack_ok)
            self.assertTrue(rt.authenticated)
            auth_events = [
                ev
                for ev in scan_all_journal_events(rt.journal.data_root)
                if ev.get("event_type") == "auth"
            ]
            self.assertTrue(auth_events)
            self.assertEqual(auth_events[-1]["outcome"], "success")
            self.assertFalse(
                any(ev.get("outcome") == "failure" for ev in auth_events)
            )

    def test_64003_after_orders_ack_keeps_ready_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rt = _okx_runtime(td)
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket()
            rt.bind_sockets(private=priv, trade=trade, env=rt.gate_env)
            rt.handle_inbound_text(json.dumps({"event": "login", "code": "0"}))
            rt.send_subscribe()
            rt.handle_inbound_text(
                json.dumps(
                    {
                        "event": "subscribe",
                        "code": "0",
                        "arg": {"channel": "orders", "instId": "WAL-USDT-SWAP"},
                    }
                )
            )
            rt.handle_inbound_text(
                json.dumps(
                    {
                        "event": "subscribe",
                        "code": "0",
                        "arg": {
                            "channel": "orders",
                            "instId": "EDEN-USDT-SWAP",
                        },
                    }
                )
            )
            rt.confirm_rest_reseed(RestReseedResult(matched=True))
            self.assertTrue(rt.authenticated)
            self.assertEqual(rt.subscription_readiness, SubscriptionReadiness.READY)
            self.assertEqual(rt.sequence_state, SequenceHealth.HEALTHY)
            self.assertFalse(rt.sends_blocked)

            parsed = rt.handle_inbound_text(
                json.dumps({"event": "error", "code": "64003", "arg": None})
            )
            self.assertEqual(parsed.kind, "sub_ack")
            self.assertFalse(parsed.ack_ok)
            self.assertTrue(rt.authenticated)
            self.assertEqual(rt.subscription_readiness, SubscriptionReadiness.READY)
            self.assertEqual(rt.sequence_state, SequenceHealth.HEALTHY)
            self.assertFalse(rt.sends_blocked)

    def test_true_login_error_null_arg_still_auth_reject(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rt = _okx_runtime(td)
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket()
            rt.bind_sockets(private=priv, trade=trade, env=rt.gate_env)
            parsed = rt.handle_inbound_text(
                json.dumps(
                    {
                        "event": "error",
                        "code": "60007",
                        "msg": "Invalid sign",
                        "arg": None,
                    }
                )
            )
            self.assertEqual(parsed.kind, "auth_reject")
            self.assertFalse(rt.authenticated)

    def test_orders_filled_update_is_order_update(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rt = _okx_runtime(td)
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket()
            rt.bind_sockets(private=priv, trade=trade, env=rt.gate_env)
            parsed = rt.handle_inbound_text(
                json.dumps(
                    {
                        "arg": {
                            "channel": "orders",
                            "instId": "EDEN-USDT-SWAP",
                            "instType": "SWAP",
                        },
                        "data": [
                            {
                                "instId": "EDEN-USDT-SWAP",
                                "clOrdId": "abc",
                                "state": "filled",
                                "fillPx": "0.12",
                                "avgPx": "0.12",
                                "cTime": "1700000000000",
                                "uTime": "1700000001000",
                            }
                        ],
                    }
                )
            )
            self.assertEqual(parsed.kind, "order_update")
            self.assertEqual(parsed.terminal_state, "filled")
            self.assertEqual(parsed.symbol_alias, "EDEN-USDT-SWAP")


if __name__ == "__main__":
    unittest.main()
