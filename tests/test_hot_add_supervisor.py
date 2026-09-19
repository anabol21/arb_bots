"""Hot-add supervisor: fake delta, spawn two channels, SIGTERM-style cancel."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.utils.hot_add import (  # noqa: E402
    HotAddController,
    hot_add_enabled,
    quote_state_for_row,
    run_hot_add_poller,
)
from app.utils.task_supervisor import TaskSupervisor  # noqa: E402
from app.utils.universe_delta import (  # noqa: E402
    write_delta_atomic,
    write_drop_atomic,
)


def _delta_row(coin: str) -> dict[str, str]:
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
        "discovered_at_utc": "2026-09-18T00:00:00Z",
    }


async def _hang_until_cancelled(name: str, marker: dict[str, bool]) -> None:
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        marker[name] = True
        raise


class HotAddEnabledTests(unittest.TestCase):
    def test_default_off(self) -> None:
        old = os.environ.pop("SPREAD_HOT_ADD", None)
        try:
            self.assertFalse(hot_add_enabled())
        finally:
            if old is not None:
                os.environ["SPREAD_HOT_ADD"] = old

    def test_on_values(self) -> None:
        old = os.environ.get("SPREAD_HOT_ADD")
        try:
            for raw in ("1", "true", "YES", "on"):
                os.environ["SPREAD_HOT_ADD"] = raw
                self.assertTrue(hot_add_enabled(), raw)
            os.environ["SPREAD_HOT_ADD"] = "0"
            self.assertFalse(hot_add_enabled())
        finally:
            if old is None:
                os.environ.pop("SPREAD_HOT_ADD", None)
            else:
                os.environ["SPREAD_HOT_ADD"] = old


class TaskSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_add_is_awaited_and_cancelled(self) -> None:
        supervisor = TaskSupervisor()
        cancelled: dict[str, bool] = {}

        async def bootstrap() -> None:
            await asyncio.sleep(0.01)
            supervisor.add(
                _hang_until_cancelled("late", cancelled),
                name="late",
            )
            await asyncio.sleep(0.01)
            supervisor.cancel_all()

        supervisor.add(bootstrap(), name="bootstrap")
        await supervisor.wait()
        self.assertTrue(cancelled.get("late"))
        self.assertTrue(supervisor.closed)
        self.assertEqual(len(supervisor), 0)

    async def test_add_after_close_fails_loud(self) -> None:
        supervisor = TaskSupervisor()
        supervisor.cancel_all()

        async def noop() -> None:
            return None

        with self.assertRaises(RuntimeError):
            supervisor.add(noop(), name="too-late")

    async def test_cancel_named_does_not_close_supervisor(self) -> None:
        supervisor = TaskSupervisor()
        cancelled: dict[str, bool] = {}

        supervisor.add(
            _hang_until_cancelled("okx:X", cancelled),
            name="okx:X",
        )
        supervisor.add(
            _hang_until_cancelled("bybit:X", cancelled),
            name="bybit:X",
        )
        supervisor.add(
            _hang_until_cancelled("okx:AAA", cancelled),
            name="okx:AAA",
        )
        await asyncio.sleep(0.05)
        tasks = supervisor.cancel_named("okx:X", "bybit:X")
        await supervisor.drain_named(tasks)
        await asyncio.sleep(0.01)
        self.assertFalse(supervisor.closed)
        self.assertTrue(cancelled["okx:X"])
        self.assertTrue(cancelled["bybit:X"])
        self.assertNotIn("okx:AAA", cancelled)
        names = {task.get_name() for task in supervisor.snapshot()}
        self.assertIn("okx:AAA", names)


class FakeDeltaHotAddTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data_probe = Path("/data")
        self.data_mtime = None
        if self.data_probe.exists():
            self.data_mtime = self.data_probe.stat().st_mtime

    def tearDown(self) -> None:
        self.temp.cleanup()
        if self.data_mtime is not None and self.data_probe.exists():
            self.assertEqual(self.data_probe.stat().st_mtime, self.data_mtime)

    async def test_fake_delta_spawns_two_channels_per_coin_and_shutdown_cancels(self) -> None:
        quotes = {
            "AAA": quote_state_for_row(
                {
                    "okx_symbol": "AAA-USDT-SWAP",
                    "bybit_symbol": "AAAUSDT",
                }
            )
        }
        cancelled: dict[str, bool] = {}
        supervisor = TaskSupervisor()

        def spawn(row: dict[str, str]) -> None:
            coin = row["base_coin"]
            quotes[coin] = quote_state_for_row(row)
            supervisor.add(
                _hang_until_cancelled(f"okx:{coin}", cancelled),
                name=f"okx:{coin}",
            )
            supervisor.add(
                _hang_until_cancelled(f"bybit:{coin}", cancelled),
                name=f"bybit:{coin}",
            )

        logger = logging.getLogger("test-hot-add")
        logger.addHandler(logging.NullHandler())
        controller = HotAddController(
            quotes=quotes,
            spawn=spawn,
            max_extra=8,
            initial_pair_count=1,
            logger=logger,
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
                logger=logger,
                supervisor=supervisor,
            ),
            name="hot-add-poller",
        )
        for _ in range(50):
            if controller.extra_count() == 2:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(sorted(controller.spawned), ["NEW1", "NEW2"])
        self.assertIn("NEW1", quotes)
        self.assertIn("NEW2", quotes)
        self.assertIn("AAA", quotes)
        names = {task.get_name() for task in supervisor.snapshot()}
        self.assertIn("okx:NEW1", names)
        self.assertIn("bybit:NEW1", names)
        self.assertIn("okx:NEW2", names)
        self.assertIn("bybit:NEW2", names)
        supervisor.cancel_all()
        await supervisor.drain()
        self.assertTrue(cancelled["okx:NEW1"])
        self.assertTrue(cancelled["bybit:NEW1"])
        self.assertTrue(cancelled["okx:NEW2"])
        self.assertTrue(cancelled["bybit:NEW2"])
        self.assertTrue(supervisor.closed)

    async def test_cap_stops_before_unbounded_listings(self) -> None:
        quotes: dict[str, object] = {}
        spawned: list[str] = []

        def spawn(row: dict[str, str]) -> None:
            quotes[row["base_coin"]] = quote_state_for_row(row)
            spawned.append(row["base_coin"])

        logger = logging.getLogger("test-hot-add-cap")
        logger.addHandler(logging.NullHandler())
        controller = HotAddController(
            quotes=quotes,
            spawn=spawn,
            max_extra=1,
            initial_pair_count=0,
            logger=logger,
        )
        added = controller.apply_rows([_delta_row("A"), _delta_row("B")])
        self.assertEqual(added, ["A"])
        self.assertEqual(spawned, ["A"])
        self.assertNotIn("B", quotes)

    async def test_does_not_write_data_paths(self) -> None:
        forbidden = [
            Path("/data/live"),
            Path("/data/spool"),
            Path("/data/bars"),
        ]
        existing = {path: path.exists() for path in forbidden}
        quotes: dict[str, object] = {}

        def spawn(row: dict[str, str]) -> None:
            quotes[row["base_coin"]] = quote_state_for_row(row)

        logger = logging.getLogger("test-hot-add-nodata")
        logger.addHandler(logging.NullHandler())
        controller = HotAddController(
            quotes=quotes,
            spawn=spawn,
            max_extra=8,
            initial_pair_count=0,
            logger=logger,
        )
        controller.apply_rows([_delta_row("Z")])
        for path, was in existing.items():
            self.assertEqual(path.exists(), was)

    async def test_drop_coin_cancels_hot_add_and_keeps_bootstrap(self) -> None:
        quotes = {
            "AAA": quote_state_for_row(
                {
                    "okx_symbol": "AAA-USDT-SWAP",
                    "bybit_symbol": "AAAUSDT",
                }
            )
        }
        cancelled: dict[str, bool] = {}
        supervisor = TaskSupervisor()

        def spawn(row: dict[str, str]) -> None:
            coin = row["base_coin"]
            quotes[coin] = quote_state_for_row(row)
            supervisor.add(
                _hang_until_cancelled(f"okx:{coin}", cancelled),
                name=f"okx:{coin}",
            )
            supervisor.add(
                _hang_until_cancelled(f"bybit:{coin}", cancelled),
                name=f"bybit:{coin}",
            )

        async def drop_coin_like_screaner(coin: str) -> None:
            if coin not in quotes:
                return
            tasks = supervisor.cancel_named(f"okx:{coin}", f"bybit:{coin}")
            await supervisor.drain_named(tasks)
            del quotes[coin]

        spawn(_delta_row("NEW1"))
        await asyncio.sleep(0.05)
        await drop_coin_like_screaner("NEW1")
        self.assertNotIn("NEW1", quotes)
        self.assertIn("AAA", quotes)
        self.assertTrue(cancelled["okx:NEW1"])
        self.assertTrue(cancelled["bybit:NEW1"])
        self.assertFalse(supervisor.closed)
        names = {task.get_name() for task in supervisor.snapshot()}
        self.assertIn("okx:AAA", names)

    async def test_drop_poller_invokes_drop_fn_on_snapshot(self) -> None:
        quotes = {"AAA": quote_state_for_row({"okx_symbol": "A", "bybit_symbol": "A"})}
        dropped: list[str] = []

        async def drop_fn(coin: str) -> None:
            dropped.append(coin)
            quotes.pop(coin, None)

        spawn_calls: list[str] = []

        def spawn(row: dict[str, str]) -> None:
            spawn_calls.append(row["base_coin"])
            quotes[row["base_coin"]] = quote_state_for_row(row)

        logger = logging.getLogger("test-hot-add-drop")
        logger.addHandler(logging.NullHandler())
        controller = HotAddController(
            quotes=quotes,
            spawn=spawn,
            max_extra=8,
            initial_pair_count=1,
            logger=logger,
        )
        drop_file = self.root / "hot_add_drop.csv"
        write_drop_atomic(drop_file, ["NEW1", "NEW2"])
        quotes["NEW1"] = quote_state_for_row(_delta_row("NEW1"))
        quotes["NEW2"] = quote_state_for_row(_delta_row("NEW2"))
        reload_event = asyncio.Event()
        supervisor = TaskSupervisor()
        supervisor.add(
            run_hot_add_poller(
                controller,
                self.root / "missing_delta.csv",
                interval_sec=0.05,
                reload_event=reload_event,
                logger=logger,
                supervisor=supervisor,
                drop_path=drop_file,
                drop_fn=drop_fn,
            ),
            name="hot-add-poller",
        )
        for _ in range(50):
            if dropped == ["NEW1", "NEW2"]:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(dropped, ["NEW1", "NEW2"])
        self.assertNotIn("NEW1", quotes)
        self.assertNotIn("NEW2", quotes)
        self.assertIn("AAA", quotes)
        supervisor.cancel_all()
        await supervisor.drain()


class CollectorWiringTests(unittest.TestCase):
    def test_screaner_uses_supervisor_and_spawn_coin(self) -> None:
        src = (REPO / "app" / "screaner_b_o.py").read_text(encoding="utf-8")
        self.assertIn("def spawn_coin(", src)
        self.assertIn("async def drop_coin(", src)
        self.assertIn("cancel_named", src)
        self.assertIn("TaskSupervisor", src)
        self.assertIn("hot_add_enabled()", src)
        self.assertNotIn("await asyncio.gather(*tasks)", src)
        self.assertIn("supervisor.add(okx_listener(", src)
        self.assertIn("supervisor.add(bybit_listener(", src)
        # Frozen listener entrypoints still present; hot-add must call them.
        self.assertIn("async def okx_listener(", src)
        self.assertIn("async def bybit_listener(", src)

    def test_systemd_unit_does_not_enable_hot_add(self) -> None:
        unit = (REPO / "deploy" / "systemd" / "spread-collector.service").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("SPREAD_HOT_ADD=1", unit)
        self.assertNotIn("SPREAD_HOT_ADD=true", unit)

    def test_canary_systemd_enables_hot_add_only_there(self) -> None:
        unit = (
            REPO / "deploy" / "systemd" / "spread-collector-hotadd-canary.service"
        ).read_text(encoding="utf-8")
        self.assertIn("SPREAD_HOT_ADD=1", unit)
        self.assertIn("/data/live-hotadd-canary", unit)
        self.assertIn("InaccessiblePaths", unit)
        parquet_lines = [
            line
            for line in unit.splitlines()
            if line.startswith("Environment=SPREAD_PARQUET_ROOT=")
        ]
        self.assertEqual(
            parquet_lines,
            ["Environment=SPREAD_PARQUET_ROOT=/data/live-hotadd-canary"],
        )


if __name__ == "__main__":
    unittest.main()
