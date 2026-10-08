from __future__ import annotations

import asyncio
import logging
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:
    import websockets  # noqa: F401
except ModuleNotFoundError:
    import sys
    import types

    sys.modules["websockets"] = types.ModuleType("websockets")

from app.bot.hot_add import bbot_hot_add_set_leverage_enabled
from app.bot.private.leverage_one import (
    LeverageTarget,
    set_and_verify_leverage_one,
)
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.venue import endpoints_for_venue
from app.bot.runtime import BotRuntime
from app.bot.stub_broker import InstrumentMeta
from app.utils.task_supervisor import TaskSupervisor
from app.utils.tick_validity import TickValidityGate
from app.bot.theta_screener import ThetaSnapshot


class HotAddLeverageTests(unittest.TestCase):
    def test_feature_flag_defaults_off(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(bbot_hot_add_set_leverage_enabled())
        with patch.dict("os.environ", {"BBOT_HOT_ADD_SET_LEVERAGE": "1"}, clear=True):
            self.assertTrue(bbot_hot_add_set_leverage_enabled())

    def _runtime(self, *, enabled: bool) -> BotRuntime:
        now = time.time() * 1000
        runtime = object.__new__(BotRuntime)
        runtime.coins = ["BASE"]
        runtime._gear23_base_coins = ("BASE",)
        runtime._gear23_hot_add_enabled = True
        runtime._gear23_hot_add_set_leverage = enabled
        runtime._gear23_leverage_scheduled = set()
        runtime._gear23_leverage_ready = True
        runtime._gear23_leverage_lock = asyncio.Lock()
        runtime._gear23_last_leverage_start = 0.0
        runtime._gear23_pool_started = False
        runtime._gear23_floor_warm = {"NEW": True}
        runtime._gear23_last_status = {}
        runtime._gear23_confirmed_1x = frozenset()
        runtime._hot_add_supervisor = TaskSupervisor()
        runtime.log = logging.getLogger("test-hot-add-leverage")
        runtime.quotes = {"BASE": {"okx": {}, "bybit": {}}}
        runtime.universe = {
            "NEW": InstrumentMeta(
                base_coin="NEW", okx_symbol="NEW-USDT-SWAP", bybit_symbol="NEWUSDT",
                okx_lot_size=1, okx_min_size=1, bybit_qty_step=1,
                bybit_min_order_qty=1, okx_tick_size=0.01, bybit_tick_size=0.01,
            )
        }
        runtime._book_inflight = {"BASE": False}
        runtime._book_dirty = {"BASE": False}
        runtime._book_last_exchange = {"BASE": "okx"}
        runtime._ma_cache = {"BASE": (None, None)}
        runtime.theta_screener = None
        runtime._private_warm = SimpleNamespace(
            okx_credentials=LiveCredentials("key", "secret", "pass"),
            bybit_credentials=LiveCredentials("key", "secret"),
            coin_ready=lambda **_: True,
        )
        runtime._leverage_one = {}
        runtime._okx_ct_vals = {"NEW-USDT-SWAP": 1}
        runtime._okx_inst_id_codes = {"NEW-USDT-SWAP": 1}
        runtime.gate = TickValidityGate(skew_max_ms=100, age_max_ms=1000)
        runtime.gate.coin_generation["NEW"] = 1
        runtime.gate.leg_generation.update({("NEW", "okx"): 1, ("NEW", "bybit"): 1})
        runtime.theta_trade = None
        runtime.stop_event = asyncio.Event()
        runtime.test_now_ms = now
        return runtime

    def _make_candidate_ready(self, runtime: BotRuntime) -> None:
        now = runtime.test_now_ms
        runtime.quotes["NEW"] = {
            venue: {"bid_price": 10, "bid_size": 1, "ask_price": 11,
                    "ask_size": 1, "ts_exchange": now, "local_recv_ts_ms": now}
            for venue in ("okx", "bybit")
        }
        runtime._gear23_floor_warm["NEW"] = True
        runtime.snapshot = ThetaSnapshot(
            base_coin="NEW", side="long", ts_ms=int(now), p50_1m=1,
            p50_5m=1, floor_tf_select_a25=0, theta_1m=1, theta_5m=1,
            computed_at_ms=int(now),
        )

    def test_flag_off_does_not_schedule_leverage_task(self) -> None:
        runtime = self._runtime(enabled=False)
        runtime._gear23_spawn_coin({"base_coin": "NEW"})
        self.assertFalse(any(
            task.get_name() == "leverage-1x-NEW"
            for task in runtime._hot_add_supervisor.snapshot()
        ))
        asyncio.run(runtime._hot_add_supervisor.drain())

    def test_already_confirmed_extra_is_not_scheduled_again(self) -> None:
        runtime = self._runtime(enabled=True)
        runtime._gear23_confirmed_1x = frozenset({"NEW"})
        runtime._gear23_spawn_coin({"base_coin": "NEW"})
        self.assertFalse(any(
            task.get_name() == "leverage-1x-NEW"
            for task in runtime._hot_add_supervisor.snapshot()
        ))

    def test_startup_extra_waits_for_existing_private_startup_guards(self) -> None:
        runtime = self._runtime(enabled=True)
        runtime._gear23_leverage_ready = False

        async def exercise() -> None:
            with patch(
                "app.bot.private.leverage_one.set_and_verify_leverage_one",
                return_value=True,
            ):
                runtime._gear23_spawn_coin({"base_coin": "NEW"})
                self.assertFalse(runtime._gear23_leverage_scheduled)
                runtime._gear23_leverage_ready = True
                runtime._gear23_schedule_hot_add_leverage("NEW")
                self.assertIn("NEW", runtime._gear23_leverage_scheduled)
                self.assertEqual(
                    runtime._hot_add_supervisor.snapshot()[0].get_name(),
                    "leverage-1x-NEW",
                )
                await asyncio.gather(*runtime._hot_add_supervisor.snapshot())
                await runtime._hot_add_supervisor.drain()

        asyncio.run(exercise())

    def test_leverage_jobs_are_serialized_and_spaced(self) -> None:
        runtime = self._runtime(enabled=True)
        runtime.universe["NEXT"] = runtime.universe["NEW"]
        starts: list[float] = []

        def prepare(*_args, **_kwargs):
            starts.append(time.monotonic())
            return True

        async def exercise() -> None:
            with patch(
                "app.bot.private.leverage_one.set_and_verify_leverage_one",
                side_effect=prepare,
            ):
                await asyncio.gather(
                    runtime._gear23_set_hot_add_leverage("NEW"),
                    runtime._gear23_set_hot_add_leverage("NEXT"),
                )

        asyncio.run(exercise())
        self.assertEqual(len(starts), 2)
        self.assertGreaterEqual(starts[1] - starts[0], 0.95)

    def test_enabled_hot_add_confirms_then_candidate_becomes_eligible_once(self) -> None:
        runtime = self._runtime(enabled=True)

        async def exercise() -> None:
            with patch(
                "app.bot.private.leverage_one.set_and_verify_leverage_one",
                return_value=True,
            ) as prepare:
                runtime._gear23_spawn_coin({"base_coin": "NEW"})
                runtime._gear23_spawn_coin({"base_coin": "NEW"})
                self._make_candidate_ready(runtime)
                tasks = runtime._hot_add_supervisor.snapshot()
                leverage_tasks = [t for t in tasks if t.get_name() == "leverage-1x-NEW"]
                self.assertEqual(len(leverage_tasks), 1)
                await asyncio.gather(*leverage_tasks)
                prepare.assert_called_once()
                self.assertIn("NEW", runtime._gear23_confirmed_1x)
                self.assertEqual(runtime._leverage_one["okx", "NEW-USDT-SWAP"], "1")
                self.assertEqual(runtime._leverage_one["bybit", "NEWUSDT"], "1")
                self.assertTrue(runtime._gear23_candidate_ready("NEW", [runtime.snapshot]))
            await runtime._hot_add_supervisor.drain()

        asyncio.run(exercise())

    def test_readback_failure_keeps_candidate_blocked(self) -> None:
        runtime = self._runtime(enabled=True)

        async def exercise() -> None:
            with patch(
                "app.bot.private.leverage_one.set_and_verify_leverage_one",
                side_effect=RuntimeError("leverage_readback_unconfirmed"),
            ):
                runtime._gear23_spawn_coin({"base_coin": "NEW"})
                self._make_candidate_ready(runtime)
                task = next(
                    task for task in runtime._hot_add_supervisor.snapshot()
                    if task.get_name() == "leverage-1x-NEW"
                )
                await task
                self.assertNotIn("NEW", runtime._gear23_confirmed_1x)
                self.assertFalse(runtime._leverage_one)
                self.assertFalse(runtime._gear23_candidate_ready("NEW", [runtime.snapshot]))
                self.assertEqual(runtime._gear23_last_status["NEW"], "one_x_unconfirmed")
            await runtime._hot_add_supervisor.drain()

        with self.assertLogs("test-hot-add-leverage", level="WARNING") as logs:
            asyncio.run(exercise())
        self.assertIn("reason=leverage_readback_unconfirmed", "\n".join(logs.output))

    def test_setter_and_readback_require_both_venues_at_one(self) -> None:
        target = LeverageTarget("NEW", "NEW-USDT-SWAP", "NEWUSDT")
        creds = LiveCredentials("key", "secret", "pass")
        posted: list[str] = []

        def post(url, _headers, _body):
            posted.append(url)
            if "okx" in url:
                return 200, {"code": "0", "data": [{"lever": "1"}]}
            return 200, {"retCode": 0, "result": {}}

        def get(url, _headers, *, timeout_sec):
            self.assertEqual(timeout_sec, 15.0)
            if "/api/v5/account/positions" in url:
                return {"code": "0", "data": []}
            if "/api/v5/trade/orders-pending" in url:
                return {"code": "0", "data": []}
            if "/api/v5/account/leverage-info" in url:
                return {"code": "0", "data": [{"instId": target.okx_symbol,
                        "lever": "1", "mgnMode": "cross"}]}
            if "/v5/order/realtime" in url:
                return {"retCode": 0, "result": {"list": []}}
            return {"retCode": 0, "result": {"list": [{"symbol": target.bybit_symbol,
                    "leverage": "1", "buyLeverage": "1", "sellLeverage": "1",
                    "size": "0", "positionIdx": 0}]}}

        self.assertTrue(set_and_verify_leverage_one(
            target,
            okx_credentials=creds,
            bybit_credentials=creds,
            endpoints=endpoints_for_venue("live"),
            post_fn=post,
            get_fn=get,
        ))
        self.assertEqual(len(posted), 2)

        for bad_url in ("okx", "bybit"):
            bybit_position_calls = 0

            def bad_get(url, headers, *, timeout_sec):
                nonlocal bybit_position_calls
                data = get(url, headers, timeout_sec=timeout_sec)
                if bad_url == "okx" and "/account/leverage-info" in url:
                    data["data"][0]["lever"] = "2"
                if bad_url == "bybit" and "/v5/position/list" in url:
                    bybit_position_calls += 1
                    if bybit_position_calls > 1:
                        data["result"]["list"][0]["buyLeverage"] = "2"
                return data

            with self.subTest(venue=bad_url), self.assertRaises(RuntimeError):
                set_and_verify_leverage_one(
                    target,
                    okx_credentials=creds,
                    bybit_credentials=creds,
                    endpoints=endpoints_for_venue("live"),
                    post_fn=post,
                    get_fn=bad_get,
                )

    def test_preflight_exposure_prevents_all_setter_calls(self) -> None:
        target = LeverageTarget("NEW", "NEW-USDT-SWAP", "NEWUSDT")
        creds = LiveCredentials("key", "secret", "pass")

        exposure_paths = (
            "/v5/position/list",
            "/v5/order/realtime",
            "/api/v5/account/positions",
            "/api/v5/trade/orders-pending",
        )
        for exposure_path in exposure_paths:
            posts: list[str] = []

            def post(url, _headers, _body):
                posts.append(url)
                return 200, {}

            def get(url, _headers, *, timeout_sec):
                self.assertEqual(timeout_sec, 15.0)
                if "/api/v5/account/positions" in url:
                    rows = ([{"instId": target.okx_symbol, "pos": "0.01"}]
                            if exposure_path in url else [])
                    return {"code": "0", "data": rows}
                if "/api/v5/trade/orders-pending" in url:
                    rows = ([{"instId": target.okx_symbol}] if exposure_path in url else [])
                    return {"code": "0", "data": rows}
                if "/v5/order/realtime" in url:
                    rows = ([{"symbol": target.bybit_symbol}]
                            if exposure_path in url else [])
                    return {"retCode": 0, "result": {"list": rows}}
                if "/v5/position/list" in url:
                    size = "0.01" if exposure_path in url else "0"
                    return {"retCode": 0, "result": {"list": [{
                        "symbol": target.bybit_symbol, "size": size, "positionIdx": 0,
                    }]}}
                self.fail("readback must not run after failed preflight")

            with self.subTest(path=exposure_path), self.assertRaisesRegex(
                RuntimeError, "leverage_preflight_not_flat"
            ):
                set_and_verify_leverage_one(
                    target,
                    okx_credentials=creds,
                    bybit_credentials=creds,
                    endpoints=endpoints_for_venue("live"),
                    post_fn=post,
                    get_fn=get,
                )
            self.assertEqual(posts, [])


if __name__ == "__main__":
    unittest.main()
