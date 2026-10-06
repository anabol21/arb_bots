from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

try:
    import websockets  # noqa: F401
except ModuleNotFoundError:
    # Runtime wiring test replaces the network coroutines; no WS client is used.
    sys.modules["websockets"] = types.ModuleType("websockets")

import app.bot.runtime as runtime_module
from app.bot.hot_add import BotHotAddController
from app.bot.floor_watcher import LiveFloorObserver
from app.bot.hot_add_warm import warm_floor_from_history
from app.bot.runtime import BotRuntime
from app.bot.stub_broker import InstrumentMeta
from app.bot.theta_screener import LiveThetaScreener
from app.bot.tw_p50_watcher import LiveTwP50Observer
from app.utils.task_supervisor import TaskSupervisor
from app.utils.universe_delta import read_delta_rows, write_delta_atomic


def _row(coin: str) -> dict[str, str]:
    return {
        "base_coin": coin,
        "okx_symbol": f"{coin}-USDT-SWAP",
        "bybit_symbol": f"{coin}USDT",
        "okx_tick_size": "0.01",
        "okx_lot_size": "1",
        "okx_min_size": "1",
        "bybit_tick_size": "0.01",
        "bybit_qty_step": "1",
        "bybit_min_order_qty": "1",
        "bybit_min_notional_value": "5",
    }


class Gear23HotAddTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.quotes: dict[str, object] = {"BASE": {}}
        self.universe = {}
        self.spawned: list[str] = []

        def spawn(row: dict[str, str]) -> None:
            self.spawned.append(row["base_coin"])
            self.quotes[row["base_coin"]] = {}

        self.controller = BotHotAddController(
            quotes=self.quotes,
            universe=self.universe,
            spawn=spawn,
            max_extra=4,
            initial_pair_count=1,
            logger=logging.getLogger("test-gear23-hot-add"),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_production_snapshot_parser_adds_two_then_is_idempotent(self) -> None:
        path = self.root / "hot_add_delta.csv"
        write_delta_atomic(path, [_row("NEWONE"), _row("NEWTWO")])
        rows = read_delta_rows(path)
        self.assertEqual(self.controller.apply_rows(rows), ["NEWONE", "NEWTWO"])
        self.assertEqual(self.controller.apply_rows(read_delta_rows(path)), [])
        self.assertEqual(self.spawned, ["NEWONE", "NEWTWO"])
        self.assertEqual(self.controller.extra_count(), 2)

    def test_single_candidate_and_duplicate_row_spawn_once(self) -> None:
        row = _row("NEWONE")
        self.assertEqual(self.controller.apply_rows([row, row]), ["NEWONE"])
        self.assertEqual(self.spawned, ["NEWONE"])

    def test_invalid_metadata_is_rejected_without_spawn(self) -> None:
        path = self.root / "invalid.csv"
        row = _row("NEWONE")
        row["bybit_qty_step"] = "0"
        write_delta_atomic(path, [row])
        self.assertEqual(self.controller.apply_rows(read_delta_rows(path)), [])
        self.assertEqual(self.spawned, [])
        self.assertNotIn("NEWONE", self.quotes)

    def test_history_warm_failure_is_explicit(self) -> None:
        result = warm_floor_from_history(
            LiveFloorObserver(["BASE"]), "NEWONE", self.root / "missing-history"
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "history_root_missing")

    def test_runtime_wiring_starts_two_public_feeds_and_pins_trade_pool(self) -> None:
        async def fake_ws(*, base_coin: str, stop_event, **_kwargs) -> None:
            started.append(base_coin)
            try:
                await stop_event.wait()
            finally:
                stopped.append(base_coin)

        started: list[str] = []
        stopped: list[str] = []
        async def exercise() -> None:
            floor = LiveFloorObserver(["BASE"])
            tw = LiveTwP50Observer(["BASE"])
            runtime = object.__new__(BotRuntime)
            runtime.coins = ["BASE"]
            runtime._gear23_base_coins = ("BASE",)
            runtime._gear23_hot_add_enabled = True
            runtime._hot_add_supervisor = TaskSupervisor()
            runtime.stop_event = asyncio.Event()
            runtime.data_root = self.root
            runtime.log = logging.getLogger("test-gear23-runtime")
            runtime.quotes = {"BASE": {"okx": {}, "bybit": {}}}
            runtime.universe = {
                "NEWONE": InstrumentMeta(
                    base_coin="NEWONE",
                    okx_symbol="NEWONE-USDT-SWAP",
                    bybit_symbol="NEWONEUSDT",
                    okx_lot_size=1,
                    okx_min_size=1,
                    bybit_qty_step=1,
                    bybit_min_order_qty=1,
                    okx_tick_size=0.01,
                    bybit_tick_size=0.01,
                )
            }
            runtime._book_inflight = {"BASE": False}
            runtime._book_dirty = {"BASE": False}
            runtime._book_last_exchange = {"BASE": "okx"}
            runtime._ma_cache = {"BASE": (None, None)}
            runtime.theta_screener = LiveThetaScreener(
                ["BASE"], floor_observer=floor, tw_p50_observer=tw
            )
            runtime.floor_observer = floor
            runtime.tw_p50_observer = tw

            with patch.dict("os.environ", {"BBOT_HOT_ADD_WARM": "0"}, clear=False), patch.object(
                runtime_module, "run_okx_books5", fake_ws
            ), patch.object(runtime_module, "run_bybit_orderbook1", fake_ws):
                controller = BotHotAddController(
                    quotes=runtime.quotes,
                    universe=runtime.universe,
                    spawn=runtime._gear23_spawn_coin,
                    max_extra=4,
                    initial_pair_count=1,
                    logger=runtime.log,
                )
                self.assertEqual(
                    controller.apply_rows([_row("NEWONE"), _row("NEWONE")]),
                    ["NEWONE"],
                )
                self.assertEqual(runtime._gear23_trade_coin_order(), ("BASE",))
                self.assertEqual(runtime.theta_screener.coins, ["BASE", "NEWONE"])
                self.assertEqual(len(runtime._hot_add_supervisor), 2)
                await asyncio.sleep(0)
                self.assertEqual(sorted(started), ["NEWONE", "NEWONE"])
                await runtime._hot_add_supervisor.drain()

        asyncio.run(exercise())
        self.assertEqual(len(stopped), 2)

    def test_patch_a_constructor_refuses_private_broker(self) -> None:
        env = {
            "BBOT_MODE": "probe",
            "BBOT_PROFILE": "gear22_live_canary",
            "BBOT_HOT_ADD": "1",
            "BBOT_THETA_LIVE_SEND": "0",
            "BBOT_BROKER": "private_live",
            "LIVE_ORDERS": "0",
        }
        with patch.dict("os.environ", env, clear=True), patch(
            "app.bot.sentry_setup.init_sentry", return_value=False
        ), self.assertRaisesRegex(ValueError, "BBOT_HOT_ADD Patch A"):
            BotRuntime()


if __name__ == "__main__":
    unittest.main()
