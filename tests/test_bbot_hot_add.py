"""B1 bot hot-add: BBOT_* env, poll/cap/skip/drop, fail-closed lot/tick."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.bot.hot_add import (  # noqa: E402
    BotHotAddController,
    bbot_hot_add_delta_path,
    bbot_hot_add_drop_path,
    bbot_hot_add_enabled,
    bbot_hot_add_max_extra,
    lot_tick_complete,
    resolve_hot_add_meta,
    run_hot_add_poller,
)
from app.bot.stub_broker import InstrumentMeta  # noqa: E402
from app.utils.task_supervisor import TaskSupervisor  # noqa: E402


def empty_book() -> dict[str, object]:
    """Local stub — avoid importing app.bot.ws_books (needs websockets)."""
    return {
        "bid_price": None,
        "bid_size": None,
        "ask_price": None,
        "ask_size": None,
        "ts_exchange": None,
        "local_recv_ts_ms": None,
        "delivery_latency_ms": None,
        "cts_exchange": None,
    }

from app.utils.universe_delta import write_delta_atomic, write_drop_atomic  # noqa: E402


def _delta_row(coin: str, **overrides: str) -> dict[str, str]:
    row = {
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
        "discovered_at_utc": "2026-09-30T00:00:00Z",
    }
    row.update(overrides)
    return row


def _meta(coin: str) -> InstrumentMeta:
    return InstrumentMeta(
        base_coin=coin,
        okx_symbol=f"{coin}-USDT-SWAP",
        bybit_symbol=f"{coin}USDT",
        okx_lot_size=1.0,
        okx_min_size=1.0,
        bybit_qty_step=1.0,
        bybit_min_order_qty=1.0,
        okx_tick_size=0.01,
        bybit_tick_size=0.01,
        bybit_min_notional_value=5.0,
    )


async def _hang_until_cancelled(name: str, marker: dict[str, bool]) -> None:
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        marker[name] = True
        raise


class BbotHotAddEnabledTests(unittest.TestCase):
    def test_default_off(self) -> None:
        old = os.environ.pop("BBOT_HOT_ADD", None)
        try:
            self.assertFalse(bbot_hot_add_enabled())
        finally:
            if old is not None:
                os.environ["BBOT_HOT_ADD"] = old

    def test_on_values(self) -> None:
        old = os.environ.get("BBOT_HOT_ADD")
        try:
            for raw in ("1", "true", "YES", "on"):
                os.environ["BBOT_HOT_ADD"] = raw
                self.assertTrue(bbot_hot_add_enabled(), raw)
            os.environ["BBOT_HOT_ADD"] = "0"
            self.assertFalse(bbot_hot_add_enabled())
        finally:
            if old is None:
                os.environ.pop("BBOT_HOT_ADD", None)
            else:
                os.environ["BBOT_HOT_ADD"] = old

    def test_does_not_read_spread_hot_add_env(self) -> None:
        old_bbot = os.environ.pop("BBOT_HOT_ADD", None)
        old_spread = os.environ.get("SPREAD_HOT_ADD")
        try:
            os.environ["SPREAD_HOT_ADD"] = "1"
            self.assertFalse(bbot_hot_add_enabled())
        finally:
            if old_bbot is None:
                os.environ.pop("BBOT_HOT_ADD", None)
            else:
                os.environ["BBOT_HOT_ADD"] = old_bbot
            if old_spread is None:
                os.environ.pop("SPREAD_HOT_ADD", None)
            else:
                os.environ["SPREAD_HOT_ADD"] = old_spread

    def test_max_extra_default(self) -> None:
        old = os.environ.pop("BBOT_HOT_ADD_MAX_EXTRA", None)
        try:
            self.assertEqual(bbot_hot_add_max_extra(), 8)
        finally:
            if old is not None:
                os.environ["BBOT_HOT_ADD_MAX_EXTRA"] = old

    def test_paths_under_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_d = os.environ.pop("BBOT_HOT_ADD_DELTA", None)
            old_r = os.environ.pop("BBOT_HOT_ADD_DROP", None)
            try:
                self.assertEqual(
                    bbot_hot_add_delta_path(root), root / "hot_add_delta.csv"
                )
                self.assertEqual(
                    bbot_hot_add_drop_path(root), root / "hot_add_drop.csv"
                )
            finally:
                if old_d is not None:
                    os.environ["BBOT_HOT_ADD_DELTA"] = old_d
                if old_r is not None:
                    os.environ["BBOT_HOT_ADD_DROP"] = old_r

    def test_paths_refuse_data_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = os.environ.get("BBOT_HOT_ADD_DELTA")
            try:
                os.environ["BBOT_HOT_ADD_DELTA"] = "/data/live/hot_add_delta.csv"
                with self.assertRaises(RuntimeError):
                    bbot_hot_add_delta_path(root)
            finally:
                if old is None:
                    os.environ.pop("BBOT_HOT_ADD_DELTA", None)
                else:
                    os.environ["BBOT_HOT_ADD_DELTA"] = old


class LotTickFailClosedTests(unittest.TestCase):
    def test_lot_tick_complete(self) -> None:
        self.assertTrue(lot_tick_complete(_delta_row("NEW1")))
        bad = _delta_row("NEW1", okx_lot_size="")
        self.assertFalse(lot_tick_complete(bad))
        zero = _delta_row("NEW1", bybit_qty_step="0")
        self.assertFalse(lot_tick_complete(zero))

    def test_missing_universe_without_lot_tick_fails(self) -> None:
        row = _delta_row("GHOST", okx_lot_size="", bybit_qty_step="")
        meta, reason = resolve_hot_add_meta(row, universe={})
        self.assertIsNone(meta)
        self.assertEqual(reason, "missing_from_universe_and_lot_tick")

    def test_universe_hit_with_bad_lot_tick_fails(self) -> None:
        broken = InstrumentMeta(
            base_coin="AAA",
            okx_symbol="AAA-USDT-SWAP",
            bybit_symbol="AAAUSDT",
            okx_lot_size=0.0,
            okx_min_size=1.0,
            bybit_qty_step=1.0,
            bybit_min_order_qty=1.0,
            okx_tick_size=0.01,
            bybit_tick_size=0.01,
        )
        meta, reason = resolve_hot_add_meta(_delta_row("AAA"), {"AAA": broken})
        self.assertIsNone(meta)
        self.assertEqual(reason, "universe_lot_tick_missing")

    def test_delta_lot_tick_allows_absent_universe(self) -> None:
        meta, reason = resolve_hot_add_meta(_delta_row("NEW1"), universe={})
        self.assertIsNone(reason)
        self.assertIsNotNone(meta)
        assert meta is not None
        self.assertEqual(meta.base_coin, "NEW1")
        self.assertEqual(meta.okx_lot_size, 1.0)


class BotHotAddControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = logging.getLogger("test-bbot-hot-add")
        self.logger.handlers.clear()
        self.logger.addHandler(logging.NullHandler())

    def test_skip_non_crypto(self) -> None:
        quotes: dict[str, Any] = {
            "AAA": {"okx": empty_book(), "bybit": empty_book()}
        }
        universe = {"AAA": _meta("AAA")}
        spawned: list[str] = []

        def spawn(row: dict[str, str]) -> None:
            spawned.append(row["base_coin"])
            quotes[row["base_coin"]] = {"okx": empty_book(), "bybit": empty_book()}

        controller = BotHotAddController(
            quotes=quotes,
            universe=universe,
            spawn=spawn,
            max_extra=8,
            initial_pair_count=1,
            logger=self.logger,
        )
        added = controller.apply_rows([_delta_row("AAPL")])
        self.assertEqual(added, [])
        self.assertEqual(spawned, [])

    def test_skip_already_in_quotes(self) -> None:
        quotes: dict[str, Any] = {
            "AAA": {"okx": empty_book(), "bybit": empty_book()}
        }
        universe = {"AAA": _meta("AAA")}
        spawned: list[str] = []

        def spawn(row: dict[str, str]) -> None:
            spawned.append(row["base_coin"])
            quotes[row["base_coin"]] = {"okx": empty_book(), "bybit": empty_book()}

        controller = BotHotAddController(
            quotes=quotes,
            universe=universe,
            spawn=spawn,
            max_extra=8,
            initial_pair_count=1,
            logger=self.logger,
        )
        added = controller.apply_rows([_delta_row("AAA")])
        self.assertEqual(added, [])
        self.assertEqual(spawned, [])

    def test_fail_closed_missing_lot_tick(self) -> None:
        quotes: dict[str, Any] = {
            "AAA": {"okx": empty_book(), "bybit": empty_book()}
        }
        universe = {"AAA": _meta("AAA")}
        spawned: list[str] = []

        def spawn(row: dict[str, str]) -> None:
            spawned.append(row["base_coin"])
            quotes[row["base_coin"]] = {"okx": empty_book(), "bybit": empty_book()}

        controller = BotHotAddController(
            quotes=quotes,
            universe=universe,
            spawn=spawn,
            max_extra=8,
            initial_pair_count=1,
            logger=self.logger,
        )
        bad = _delta_row("NEW1", okx_lot_size="", bybit_tick_size="")
        added = controller.apply_rows([bad])
        self.assertEqual(added, [])
        self.assertEqual(spawned, [])
        self.assertNotIn("NEW1", quotes)

    def test_cap_stops(self) -> None:
        quotes: dict[str, Any] = {}
        universe: dict[str, InstrumentMeta] = {}
        spawned: list[str] = []

        def spawn(row: dict[str, str]) -> None:
            coin = row["base_coin"]
            quotes[coin] = {"okx": empty_book(), "bybit": empty_book()}
            spawned.append(coin)

        controller = BotHotAddController(
            quotes=quotes,
            universe=universe,
            spawn=spawn,
            max_extra=1,
            initial_pair_count=0,
            logger=self.logger,
        )
        added = controller.apply_rows([_delta_row("A"), _delta_row("B")])
        self.assertEqual(added, ["A"])
        self.assertEqual(spawned, ["A"])
        self.assertNotIn("B", quotes)
        self.assertIn("A", universe)  # registered from delta


class FakeDeltaPollerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.logger = logging.getLogger("test-bbot-hot-add-poll")
        self.logger.handlers.clear()
        self.logger.addHandler(logging.NullHandler())

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_poll_spawns_two_channels_and_shutdown_cancels(self) -> None:
        quotes: dict[str, Any] = {
            "AAA": {"okx": empty_book(), "bybit": empty_book()}
        }
        universe = {"AAA": _meta("AAA"), "NEW1": _meta("NEW1"), "NEW2": _meta("NEW2")}
        cancelled: dict[str, bool] = {}
        supervisor = TaskSupervisor()

        def spawn(row: dict[str, str]) -> None:
            coin = row["base_coin"]
            quotes[coin] = {"okx": empty_book(), "bybit": empty_book()}
            supervisor.add(
                _hang_until_cancelled(f"okx:{coin}", cancelled),
                name=f"okx:{coin}",
            )
            supervisor.add(
                _hang_until_cancelled(f"bybit:{coin}", cancelled),
                name=f"bybit:{coin}",
            )

        controller = BotHotAddController(
            quotes=quotes,
            universe=universe,
            spawn=spawn,
            max_extra=8,
            initial_pair_count=1,
            logger=self.logger,
        )
        delta = self.root / "hot_add_delta.csv"
        write_delta_atomic(
            delta,
            [_delta_row("NEW1"), _delta_row("NEW2")],
            universe_path=self.root / "universe.csv",
        )
        reload_event = asyncio.Event()
        supervisor.add(
            run_hot_add_poller(
                controller,
                delta,
                interval_sec=0.05,
                reload_event=reload_event,
                logger=self.logger,
                supervisor=supervisor,
            ),
            name="bbot-hot-add-poller",
        )
        for _ in range(50):
            if controller.extra_count() == 2:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(sorted(controller.spawned), ["NEW1", "NEW2"])
        names = {task.get_name() for task in supervisor.snapshot()}
        self.assertIn("okx:NEW1", names)
        self.assertIn("bybit:NEW1", names)
        self.assertIn("okx:NEW2", names)
        self.assertIn("bybit:NEW2", names)
        supervisor.cancel_all()
        await supervisor.drain()
        self.assertTrue(cancelled["okx:NEW1"])
        self.assertTrue(cancelled["bybit:NEW2"])
        self.assertTrue(supervisor.closed)

    async def test_drop_poller_snapshot(self) -> None:
        quotes: dict[str, Any] = {
            "AAA": {"okx": empty_book(), "bybit": empty_book()},
            "NEW1": {"okx": empty_book(), "bybit": empty_book()},
            "NEW2": {"okx": empty_book(), "bybit": empty_book()},
        }
        dropped: list[str] = []

        async def drop_fn(coin: str) -> None:
            dropped.append(coin)
            quotes.pop(coin, None)

        def spawn(row: dict[str, str]) -> None:
            quotes[row["base_coin"]] = {"okx": empty_book(), "bybit": empty_book()}

        controller = BotHotAddController(
            quotes=quotes,
            universe={"AAA": _meta("AAA")},
            spawn=spawn,
            max_extra=8,
            initial_pair_count=1,
            logger=self.logger,
        )
        drop_file = self.root / "hot_add_drop.csv"
        write_drop_atomic(drop_file, ["NEW1", "NEW2"])
        reload_event = asyncio.Event()
        supervisor = TaskSupervisor()
        supervisor.add(
            run_hot_add_poller(
                controller,
                self.root / "missing_delta.csv",
                interval_sec=0.05,
                reload_event=reload_event,
                logger=self.logger,
                supervisor=supervisor,
                drop_path=drop_file,
                drop_fn=drop_fn,
            ),
            name="bbot-hot-add-poller",
        )
        for _ in range(50):
            if dropped == ["NEW1", "NEW2"]:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(dropped, ["NEW1", "NEW2"])
        self.assertNotIn("NEW1", quotes)
        self.assertIn("AAA", quotes)
        supervisor.cancel_all()
        await supervisor.drain()

    async def test_drop_cancels_named_keeps_bootstrap(self) -> None:
        quotes: dict[str, Any] = {
            "AAA": {"okx": empty_book(), "bybit": empty_book()}
        }
        cancelled: dict[str, bool] = {}
        supervisor = TaskSupervisor()

        def spawn(row: dict[str, str]) -> None:
            coin = row["base_coin"]
            quotes[coin] = {"okx": empty_book(), "bybit": empty_book()}
            supervisor.add(
                _hang_until_cancelled(f"okx:{coin}", cancelled),
                name=f"okx:{coin}",
            )
            supervisor.add(
                _hang_until_cancelled(f"bybit:{coin}", cancelled),
                name=f"bybit:{coin}",
            )

        spawn(_delta_row("AAA"))
        spawn(_delta_row("NEW1"))
        await asyncio.sleep(0.05)

        async def drop_like_runtime(coin: str) -> None:
            if coin not in quotes:
                return
            tasks = supervisor.cancel_named(f"okx:{coin}", f"bybit:{coin}")
            await supervisor.drain_named(tasks)
            del quotes[coin]

        await drop_like_runtime("NEW1")
        self.assertNotIn("NEW1", quotes)
        self.assertIn("AAA", quotes)
        self.assertTrue(cancelled["okx:NEW1"])
        self.assertTrue(cancelled["bybit:NEW1"])
        self.assertFalse(supervisor.closed)
        names = {task.get_name() for task in supervisor.snapshot()}
        self.assertIn("okx:AAA", names)


class DropCoinIdempotentTests(unittest.IsolatedAsyncioTestCase):
    """BotRuntime.drop_coin: cumulative re-drop must not ERROR/Sentry."""

    async def test_double_drop_no_error_event(self) -> None:
        import sys
        import types

        if "websockets" not in sys.modules:
            sys.modules["websockets"] = types.ModuleType("websockets")
        from app.bot.runtime import BotRuntime

        coin = "COAI"
        quotes: dict[str, Any] = {
            coin: {"okx": empty_book(), "bybit": empty_book()},
        }
        cancelled: dict[str, bool] = {}
        supervisor = TaskSupervisor()
        supervisor.add(
            _hang_until_cancelled(f"okx:{coin}", cancelled),
            name=f"okx:{coin}",
        )
        supervisor.add(
            _hang_until_cancelled(f"bybit:{coin}", cancelled),
            name=f"bybit:{coin}",
        )
        await asyncio.sleep(0.02)

        logger = logging.getLogger("test_bbot_drop_idempotent")
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Capture()
        logger.addHandler(handler)
        try:
            rt = object.__new__(BotRuntime)
            rt._task_supervisor = supervisor
            rt.log = logger
            rt.quotes = quotes
            rt.theta_trade = None
            rt._trade_eligible = {coin}
            rt._hot_added_coins = {coin}
            rt.floor_observer = None
            rt.tw_p50_observer = None
            rt.coins = ["AAA", coin]
            rt._book_inflight = {}
            rt._book_dirty = {}
            rt._book_last_exchange = {}
            rt._ma_cache = {}
            rt.ma_windows = {}
            rt.theta_screener = None

            await BotRuntime.drop_coin(rt, coin)
            self.assertNotIn(coin, quotes)
            self.assertTrue(cancelled.get(f"okx:{coin}"))
            self.assertTrue(cancelled.get(f"bybit:{coin}"))
            info_msgs = [
                r.getMessage()
                for r in records
                if r.levelno == logging.INFO
            ]
            self.assertTrue(
                any("bbot_hot_add_dropped" in m and coin in m for m in info_msgs),
                info_msgs,
            )

            records.clear()
            # Cumulative drop CSV re-poll: same coin, already gone.
            await BotRuntime.drop_coin(rt, coin)
            await BotRuntime.drop_coin(rt, coin)

            error_or_warn = [
                r
                for r in records
                if r.levelno >= logging.WARNING
            ]
            self.assertEqual(
                error_or_warn,
                [],
                [f"{r.levelname}:{r.getMessage()}" for r in error_or_warn],
            )
            self.assertFalse(
                any("bbot_drop_coin_missing" in r.getMessage() for r in records)
            )
            debug_msgs = [
                r.getMessage()
                for r in records
                if r.levelno == logging.DEBUG
            ]
            self.assertTrue(
                any(
                    "bbot_drop_coin_skip" in m and "not_in_quotes" in m
                    for m in debug_msgs
                ),
                debug_msgs,
            )
        finally:
            logger.removeHandler(handler)
            supervisor.cancel_all()
            await supervisor.drain()

    async def test_empty_coin_still_errors(self) -> None:
        import sys
        import types

        if "websockets" not in sys.modules:
            sys.modules["websockets"] = types.ModuleType("websockets")
        from app.bot.runtime import BotRuntime

        logger = logging.getLogger("test_bbot_drop_invalid")
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Capture()
        logger.addHandler(handler)
        try:
            rt = object.__new__(BotRuntime)
            rt._task_supervisor = TaskSupervisor()
            rt.log = logger
            rt.quotes = {}
            await BotRuntime.drop_coin(rt, "  ")
            errors = [r for r in records if r.levelno >= logging.ERROR]
            self.assertEqual(len(errors), 1)
            self.assertIn("bbot_drop_coin_invalid", errors[0].getMessage())
        finally:
            logger.removeHandler(handler)


class RuntimeWiringTests(unittest.TestCase):
    def test_runtime_imports_bbot_hot_add_not_spread(self) -> None:
        src = (REPO / "app" / "bot" / "runtime.py").read_text(encoding="utf-8")
        self.assertIn("bbot_hot_add_enabled", src)
        self.assertIn("BotHotAddController", src)
        self.assertIn("TaskSupervisor", src)
        self.assertIn("def spawn_coin(", src)
        self.assertIn("async def drop_coin(", src)
        self.assertIn("run_okx_books5", src)
        self.assertIn("run_bybit_orderbook1", src)
        self.assertIn("hot_add_warm", src)
        self.assertIn("_warm_hot_added_coin", src)
        self.assertIn("_trade_coin_order", src)
        # Must not wire collector SPREAD_HOT_ADD_* into the bot process.
        self.assertNotIn("SPREAD_HOT_ADD", src)
        self.assertIn("await asyncio.gather(*tasks)", src)  # off-path preserved

    def test_hot_add_module_env_names(self) -> None:
        src = (REPO / "app" / "bot" / "hot_add.py").read_text(encoding="utf-8")
        self.assertIn('HOT_ADD_ENV = "BBOT_HOT_ADD"', src)
        self.assertIn('MAX_EXTRA_ENV = "BBOT_HOT_ADD_MAX_EXTRA"', src)
        # No collector env assignment / os.environ.get of SPREAD_HOT_ADD_*.
        self.assertNotIn('os.environ.get("SPREAD_HOT_ADD', src)
        self.assertNotIn('SPREAD_HOT_ADD =', src)
        self.assertNotIn('"SPREAD_HOT_ADD"', src)


if __name__ == "__main__":
    unittest.main()
