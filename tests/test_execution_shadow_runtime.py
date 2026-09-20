"""EV2-09B runtime wiring tests. No network, credentials, or order surface."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from app.bot.execution.contracts import SpreadStatus
from app.bot.execution.shadow_runtime import (
    ExecutionShadowRuntime,
    ShadowRuntimeGateError,
    assert_shadow_runtime_gates,
)
from app.bot.theta_screener import ThetaSnapshot
from app.bot.theta_trade_manager import OpenPosition, SlotState
from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.ws_private import RestReseedResult
from app.bot.private.ws_readonly import run_ws_readonly_preflight
from app.bot.private.ws_socket import FakePrivateWsSocket


def _snap(coin: str, side: str, theta: float, *, floor: float = 0.2, p50: float | None = None) -> ThetaSnapshot:
    value = floor + theta if p50 is None else p50
    return ThetaSnapshot(
        base_coin=coin,
        side=side,
        ts_ms=1_700_000_000_000,
        p50_1m=value,
        p50_5m=value,
        floor_tf_select_a25=floor,
        theta_1m=theta,
        theta_5m=theta,
        computed_at_ms=1_700_000_000_001,
    )


def _quotes(coin: str = "KAITO", *, close: bool = False):
    okx_bid = 100.5 if close else 99.0
    bybit_ask = 100.0 if close else 100.5
    return {
        coin: {
            "okx": {
                "bid_price": okx_bid,
                "ask_price": 100.0,
                "bid_size": 10.0,
                "ask_size": 10.0,
                "local_recv_ts_ms": 1.0,
            },
            "bybit": {
                "bid_price": 100.2,
                "ask_price": bybit_ask,
                "bid_size": 10.0,
                "ask_size": 10.0,
                "local_recv_ts_ms": 1.0,
            },
        }
    }


class ShadowRuntimeGateTests(unittest.TestCase):
    def test_requires_stub_and_live_orders_off(self) -> None:
        assert_shadow_runtime_gates(
            "gear22_would_send",
            {
                "BBOT_EV2_SHADOW": "1",
                "BBOT_BROKER": "stub",
                "LIVE_ORDERS": "0",
                "BBOT_THETA_LIVE_SEND": "0",
            },
        )
        for override in (
            {"BBOT_BROKER": "private_live"},
            {"LIVE_ORDERS": "1"},
            {"BBOT_THETA_LIVE_SEND": "1"},
        ):
            env = {
                "BBOT_EV2_SHADOW": "1",
                "BBOT_BROKER": "stub",
                "LIVE_ORDERS": "0",
                "BBOT_THETA_LIVE_SEND": "0",
                **override,
            }
            with self.assertRaises(ShadowRuntimeGateError):
                assert_shadow_runtime_gates("gear22_would_send", env)

    def test_private_readonly_pool_has_no_trade_socket_or_orders(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            private = FakePrivateWsSocket()
            private.push_inbound(
                json.dumps({"op": "auth", "success": True, "retCode": 0})
            )
            private.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            reseed_calls: list[str] = []

            def probe_fn(**kwargs):
                reseed_calls.append(str(kwargs.get("symbol_alias") or ""))
                return RestReseedResult(matched=True)

            report = run_ws_readonly_preflight(
                exchange="bybit",
                env={"VENUE": "live", "LIVE_ORDERS": "0"},
                private_socket=private,
                rest_probe_fn=probe_fn,
                credentials=LiveCredentials(api_key="k", api_secret="s"),
                journal=journal,
                load_secrets=False,
                max_cycles=1,
                recv_timeout_sec=0.0,
                coins=("KAITO", "HOME"),
            )
            public = report.as_public_dict()
            self.assertEqual(public["subscription_count"], 2)
            self.assertEqual(reseed_calls, ["KAITOUSDT"])
            self.assertEqual(public["orders_sent"], 0)
            self.assertFalse(public["trade_ws_bound"])


class ShadowRuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_close_mirror_and_no_order_probe(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            runtime = ExecutionShadowRuntime(
                data_root=Path(td),
                log=lambda _message: None,
                env={
                    "BBOT_EV2_WARMUP_N": "0",
                    "BBOT_EV2_COUNTED_N": "1",
                    "BBOT_EV2_PROBE_DELAY_SEC": "0",
                    "BBOT_EV2_TARGET_VPS": "1",
                },
            )
            await runtime.start()
            try:
                open_snaps = [
                    _snap("KAITO", "long", 0.60, p50=0.80),
                    _snap("KAITO", "short", 0.01),
                ]
                open_tick = await runtime.before_trade(
                    open_snaps, _quotes(), SlotState()
                )
                self.assertIsNotNone(open_tick.tick.intent)
                self.assertEqual(open_tick.tick.bridge.decision.action, "open")
                await runtime.after_trade(open_tick, [{"event": "open"}], _quotes())
                self.assertEqual(runtime.state.status, SpreadStatus.OPEN)

                context = runtime.lane.bridge.context
                self.assertIsNotNone(context)
                assert context is not None
                slot = SlotState(
                    position=OpenPosition(
                        trade_id=context.trade_id,
                        base_coin=context.coin,
                        side=context.side,
                        open_signal_ts_ms=context.open_signal_ts_ms,
                        open_fill_ts_ms=context.open_fill_ts_ms,
                        open_fill_spread=context.fill_spread_pp,
                        open_notional=float(context.open_notional),
                        open_theta_1m=context.open_theta_1m,
                        fill_spread_pp=context.fill_spread_pp,
                    )
                )
                close_snaps = [
                    _snap("KAITO", "long", 0.30, floor=0.50, p50=0.80),
                    _snap("KAITO", "short", 0.60, floor=0.60, p50=1.20),
                ]
                close_tick = await runtime.before_trade(
                    close_snaps, _quotes(close=True), slot
                )
                self.assertIsNotNone(close_tick.tick.intent)
                self.assertEqual(close_tick.tick.bridge.decision.action, "close")
                await runtime.after_trade(
                    close_tick, [{"event": "close"}], _quotes(close=True)
                )
                self.assertEqual(runtime.state.status, SpreadStatus.FLAT)
                self.assertEqual(runtime.summary["orders_sent"], 0)
                self.assertFalse(runtime.summary["trade_socket_bound"])
                self.assertTrue(runtime.summary["target_vps_gate_eligible"])
                self.assertTrue(
                    runtime.summary["histogram"]["target_vps_gate_eligible"]
                )
            finally:
                await runtime.stop()

            journal = (Path(td) / "execution-v2-shadow.jsonl").read_text(
                encoding="utf-8"
            )
            self.assertIn('"event":"latency_report"', journal)
            self.assertNotIn("api_secret", journal.lower())


if __name__ == "__main__":
    unittest.main()
