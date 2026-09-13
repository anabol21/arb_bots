"""Test floor warm pickle persistence: signal handling and periodic save."""

from __future__ import annotations

import asyncio
import os
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.bot.floor_warm import (
    apply_warm_pickle_to_observer,
    export_observer_warm_pickle,
    load_floor_warm_pickle,
    save_floor_warm_pickle,
)
from app.bot.floor_watcher import BAR_MS, LiveFloorObserver
from app.bot.paths import floor_warm_pickle_path


class FloorWarmPickleTests(unittest.TestCase):
    def test_save_and_load_roundtrip(self) -> None:
        """Warm pickle saves and reloads observer state."""
        tmp = Path(tempfile.mkdtemp())
        pickle_path = tmp / "state" / "floor_warm.pkl"
        
        obs = LiveFloorObserver(["BTC", "ETH"])
        t0 = (1_725_000_000_000 // BAR_MS) * BAR_MS
        
        # Feed some data
        for i in range(20):
            obs.note_spreads("BTC", t0 + 10_000 + i * 10_000, 0.1 + i * 0.001, 0.2)
            obs.note_spreads("ETH", t0 + 10_000 + i * 10_000, 0.15 + i * 0.001, 0.25)
        
        # Close the bar
        obs.note_spreads("BTC", t0 + BAR_MS + 1_000, 0.3, 0.4)
        obs.note_spreads("ETH", t0 + BAR_MS + 1_000, 0.35, 0.45)
        
        # Save state
        saved_path = export_observer_warm_pickle(obs, pickle_path)
        self.assertTrue(saved_path.is_file())
        
        # Load into a fresh observer
        obs2 = LiveFloorObserver(["BTC", "ETH"])
        n = apply_warm_pickle_to_observer(obs2, pickle_path)
        self.assertGreater(n, 0)
        
        # Check that SMA history was restored
        state1 = obs._states[("BTC", "long")]
        state2 = obs2._states[("BTC", "long")]
        self.assertEqual(len(state1.closes), len(state2.closes))
        self.assertEqual(len(state1.sma12_hist), len(state2.sma12_hist))
        
        # Check last floor was restored
        floor1 = obs.last_floor("BTC", "long")
        floor2 = obs2.last_floor("BTC", "long")
        if floor1 is not None:
            self.assertIsNotNone(floor2)
            self.assertAlmostEqual(floor1, floor2, places=10)
    
    def test_refuses_d_paths(self) -> None:
        """Refuse to save warm pickle under D collector paths."""
        obs = LiveFloorObserver(["BTC"])
        for bad_path in [
            Path("/data/live/floor_warm.pkl"),
            Path("/data/bars/state/floor_warm.pkl"),
            Path("/data/compacted/floor_warm.pkl"),
            Path("/data/spool/floor_warm.pkl"),
        ]:
            with self.assertRaises(RuntimeError):
                save_floor_warm_pickle(bad_path, obs.export_warm_state())


try:
    import websockets  # noqa: F401
    _HAS_WEBSOCKETS = True
except ImportError:
    _HAS_WEBSOCKETS = False


@unittest.skipUnless(_HAS_WEBSOCKETS, "runtime import needs websockets")
class FloorWarmRuntimePersistenceTests(unittest.TestCase):
    """Test signal handling and periodic save in BotRuntime."""
    
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.env = {
            "BBOT_MODE": "policy",
            "BBOT_PROFILE": "gear22_would_send",
            "BBOT_DATA_ROOT": str(self.tmp),
            "BBOT_LOG_PATH": str(self.tmp / "bbot.log"),
            "BBOT_COINS": "BTC",
            "BBOT_BROKER": "stub",
            "BBOT_FLOOR_WATCH": "1",
            "BBOT_FLOOR_WARM": "0",  # Don't auto-load during init
        }
    
    def test_save_helper_creates_pickle(self) -> None:
        """_save_floor_warm_pickle creates pickle file."""
        with patch.dict(os.environ, self.env, clear=False):
            from app.bot.runtime import BotRuntime
            rt = BotRuntime()
        
        # Feed some data to observer
        t0 = (1_725_000_000_000 // BAR_MS) * BAR_MS
        for i in range(15):
            rt.floor_observer.note_spreads("BTC", t0 + 10_000 + i * 10_000, 0.1, 0.2)
        # Close bar
        rt.floor_observer.note_spreads("BTC", t0 + BAR_MS + 1_000, 0.3, 0.4)
        
        pickle_path = rt._floor_warm_path
        self.assertFalse(pickle_path.exists())
        
        # Mark as loaded so save will happen
        rt._floor_warm_loaded = True
        rt._save_floor_warm_pickle()
        
        self.assertTrue(pickle_path.exists())
        payload = load_floor_warm_pickle(pickle_path)
        self.assertIn("sides", payload)
        self.assertIn("last_floors", payload)
    
    def test_periodic_save_task_runs(self) -> None:
        """_floor_warm_periodic_save saves pickle periodically."""
        with patch.dict(os.environ, self.env, clear=False):
            from app.bot.runtime import BotRuntime
            from app.bot.runtime import FLOOR_WARM_SAVE_INTERVAL_SEC
            rt = BotRuntime()
        
        rt._floor_warm_loaded = True
        pickle_path = rt._floor_warm_path
        
        # Feed some data
        t0 = (1_725_000_000_000 // BAR_MS) * BAR_MS
        rt.floor_observer.note_spreads("BTC", t0 + 10_000, 0.1, 0.2)
        rt.floor_observer.note_spreads("BTC", t0 + BAR_MS + 1_000, 0.3, 0.4)
        
        async def _run_periodic_save_for_short_time():
            # Override the save interval for testing
            with patch("app.bot.runtime.FLOOR_WARM_SAVE_INTERVAL_SEC", 0.1):
                task = asyncio.create_task(rt._floor_warm_periodic_save())
                await asyncio.sleep(0.25)  # Wait for at least one save cycle
                rt.stop_event.set()
                await task
        
        asyncio.run(_run_periodic_save_for_short_time())
        
        # Pickle should have been created by periodic save
        self.assertTrue(pickle_path.exists())
    
    def test_signal_handler_sets_stop_event(self) -> None:
        """SIGTERM/SIGHUP signal handlers set stop_event."""
        with patch.dict(os.environ, self.env, clear=False):
            from app.bot.runtime import BotRuntime
            rt = BotRuntime()
        
        async def _test_signal():
            # Start the run method in the background
            run_task = asyncio.create_task(self._run_with_mock_ws(rt))
            
            # Give it a moment to set up signal handlers
            await asyncio.sleep(0.1)
            
            # Verify stop_event is not set initially
            self.assertFalse(rt.stop_event.is_set())
            
            # Send SIGTERM to current process
            os.kill(os.getpid(), signal.SIGTERM)
            
            # Wait for signal to be processed
            await asyncio.sleep(0.1)
            
            # Verify stop_event was set
            self.assertTrue(rt.stop_event.is_set())
            
            # Clean up
            await run_task
        
        try:
            asyncio.run(_test_signal())
        except asyncio.CancelledError:
            pass  # Expected when gather is cancelled
    
    def test_finally_block_saves_pickle(self) -> None:
        """run() finally block saves pickle on exit."""
        with patch.dict(os.environ, self.env, clear=False):
            from app.bot.runtime import BotRuntime
            rt = BotRuntime()
        
        rt._floor_warm_loaded = True
        pickle_path = rt._floor_warm_path
        
        # Feed some data
        t0 = (1_725_000_000_000 // BAR_MS) * BAR_MS
        rt.floor_observer.note_spreads("BTC", t0 + 10_000, 0.1, 0.2)
        rt.floor_observer.note_spreads("BTC", t0 + BAR_MS + 1_000, 0.3, 0.4)
        
        async def _quick_run():
            # Mock private warm and WS tasks to exit immediately
            rt._private_warm = None
            
            # Start run and immediately stop it
            run_task = asyncio.create_task(self._run_with_mock_ws(rt))
            await asyncio.sleep(0.1)
            rt.stop_event.set()
            await run_task
        
        asyncio.run(_quick_run())
        
        # Pickle should have been saved in finally block
        self.assertTrue(pickle_path.exists())
    
    async def _run_with_mock_ws(self, rt):
        """Helper to run BotRuntime with mocked WS tasks."""
        # Mock the WS book tasks to return immediately
        with patch("app.bot.runtime.run_okx_books5", new_callable=AsyncMock) as mock_okx, \
             patch("app.bot.runtime.run_bybit_orderbook1", new_callable=AsyncMock) as mock_bybit:
            
            async def _wait_for_stop(**kwargs):
                await rt.stop_event.wait()
            
            mock_okx.side_effect = _wait_for_stop
            mock_bybit.side_effect = _wait_for_stop
            
            try:
                await rt.run()
            except asyncio.CancelledError:
                pass


class FloorWarmMtimeTest(unittest.TestCase):
    """Test that pickle mtime updates on save."""
    
    def test_pickle_mtime_updates_on_save(self) -> None:
        """Saving a pickle updates its mtime."""
        tmp = Path(tempfile.mkdtemp())
        pickle_path = tmp / "floor_warm.pkl"
        
        obs = LiveFloorObserver(["BTC"])
        t0 = (1_725_000_000_000 // BAR_MS) * BAR_MS
        
        # Initial save
        obs.note_spreads("BTC", t0 + 10_000, 0.1, 0.2)
        obs.note_spreads("BTC", t0 + BAR_MS + 1_000, 0.3, 0.4)
        export_observer_warm_pickle(obs, pickle_path)
        
        mtime1 = pickle_path.stat().st_mtime
        
        # Wait a bit
        time.sleep(0.1)
        
        # Update observer and save again
        obs.note_spreads("BTC", t0 + 2 * BAR_MS + 1_000, 0.5, 0.6)
        export_observer_warm_pickle(obs, pickle_path)
        
        mtime2 = pickle_path.stat().st_mtime
        
        # mtime should have increased
        self.assertGreater(mtime2, mtime1)


if __name__ == "__main__":
    unittest.main()
