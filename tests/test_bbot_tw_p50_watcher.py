"""Live BotRuntime rolling TW p50 watcher (1m/5m, no tick WAL)."""

from __future__ import annotations

import ast
import asyncio
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.bot.paths import resolve_data_root
from app.bot.tw_p50_watcher import (
    DEFAULT_SAMPLE_CAP,
    SCHEMA_VERSION,
    WINDOW_1M_MS,
    WINDOW_5M_MS,
    LiveTwP50Observer,
    TwP50JournalWriter,
    tw_p50_sample_cap,
    tw_p50_watch_enabled,
    tw_p50_window,
)


class TwP50FlagTests(unittest.TestCase):
    def test_default_on_for_gear2(self) -> None:
        self.assertTrue(tw_p50_watch_enabled("gear2_would_send", {}))
        self.assertTrue(tw_p50_watch_enabled("canary_wal_eden", {}))
        self.assertFalse(tw_p50_watch_enabled("gear1", {}))
        self.assertFalse(tw_p50_watch_enabled("signal_test", {}))

    def test_env_override(self) -> None:
        self.assertFalse(
            tw_p50_watch_enabled("gear2_would_send", {"BBOT_TW_P50_WATCH": "0"})
        )
        self.assertTrue(tw_p50_watch_enabled("gear1", {"BBOT_TW_P50_WATCH": "1"}))

    def test_sample_cap_reuses_floor_env(self) -> None:
        self.assertEqual(tw_p50_sample_cap({}), DEFAULT_SAMPLE_CAP)
        self.assertEqual(
            tw_p50_sample_cap({"BBOT_TW_P50_SAMPLE_CAP": "128"}), 128
        )
        self.assertEqual(
            tw_p50_sample_cap({"BBOT_FLOOR_BAR_SAMPLE_CAP": "256"}), 256
        )


class TwP50AlgorithmTests(unittest.TestCase):
    def test_known_hold_durations_p50(self) -> None:
        """Synthetic ticks with known holds → expected duration-weighted median."""
        # Sample at left edge is carry; inside ticks at 10s and 40s.
        # Segments: [0,10s)->1.0, [10s,40s)->2.0, [40s,60s)->3.0
        # weights 10k / 30k / 20k; target 30k → p50 = 2.0
        now = 60_000
        samples = [(0, 1.0), (10_000, 2.0), (40_000, 3.0)]
        p50, n, cov = tw_p50_window(samples, now_ms=now, window_ms=WINDOW_1M_MS)
        self.assertEqual(n, 2)  # in-window only; ts==left is carry
        self.assertAlmostEqual(cov, 1.0, places=9)
        self.assertAlmostEqual(p50, 2.0, places=12)

    def test_carry_in_covers_leading_gap(self) -> None:
        # Sample before left edge holds from left→first inside tick.
        # left=0, now=60_000: carry 1.0 for 20s, then 5.0 for 40s → p50=5.0
        now = 60_000
        samples = [(-5_000, 1.0), (20_000, 5.0)]
        p50, n, cov = tw_p50_window(samples, now_ms=now, window_ms=WINDOW_1M_MS)
        self.assertEqual(n, 1)
        self.assertAlmostEqual(cov, 1.0, places=9)
        self.assertAlmostEqual(p50, 5.0, places=12)

    def test_partial_coverage_without_carry(self) -> None:
        now = 60_000
        samples = [(30_000, 4.0)]
        p50, n, cov = tw_p50_window(samples, now_ms=now, window_ms=WINDOW_1M_MS)
        self.assertEqual(n, 1)
        self.assertAlmostEqual(cov, 0.5, places=9)
        self.assertAlmostEqual(p50, 4.0, places=12)

    def test_5m_window_uses_longer_history(self) -> None:
        now = 300_000
        # Dominant mass at 1.0 for first 200s, then 9.0 for 100s → 5m p50=1.0
        # 1m window is fully covered by carry of 9.0 (no in-window ticks).
        samples = [(0, 1.0), (200_000, 9.0)]
        p50_1m, n_1m, cov_1m = tw_p50_window(
            samples, now_ms=now, window_ms=WINDOW_1M_MS
        )
        p50_5m, n_5m, cov_5m = tw_p50_window(
            samples, now_ms=now, window_ms=WINDOW_5M_MS
        )
        self.assertEqual(n_1m, 0)
        self.assertAlmostEqual(cov_1m, 1.0, places=9)
        self.assertAlmostEqual(p50_1m, 9.0, places=12)
        self.assertEqual(n_5m, 1)
        self.assertAlmostEqual(cov_5m, 1.0, places=9)
        self.assertAlmostEqual(p50_5m, 1.0, places=12)


class TwP50ObserverTests(unittest.TestCase):
    def test_age_prune_keeps_carry_no_tick_files(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        obs = LiveTwP50Observer(["BTC"], sample_cap=64)
        t0 = 1_725_000_000_000
        # Spread samples across >5m so older body is pruned but carry remains.
        for i in range(20):
            ts = t0 + i * 30_000  # 30s steps → 9.5 minutes span
            obs.note_spreads("BTC", ts, 0.1 + i * 0.01, 0.2)
        snaps = obs.compute_snapshots(now_ms=t0 + 19 * 30_000, computed_at_ms=t0)
        self.assertTrue(obs.memory_bound_ok())
        long_snap = next(s for s in snaps if s.side == "long")
        self.assertEqual(long_snap.base_coin, "BTC")
        self.assertIsNotNone(long_snap.p50_5m)
        self.assertGreater(long_snap.n_5m, 0)
        # No tick WAL artifacts.
        writer = TwP50JournalWriter(tmp)
        paths = writer.append_rows([long_snap.as_row()])
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].name == "metrics.jsonl")
        self.assertFalse(any("tick" in p.name.lower() for p in tmp.rglob("*")))
        line = paths[0].read_text(encoding="utf-8").strip().splitlines()[0]
        row = json.loads(line)
        self.assertEqual(row["schema_version"], SCHEMA_VERSION)
        for key in (
            "base_coin",
            "side",
            "ts_ms",
            "p50_1m",
            "p50_5m",
            "n_1m",
            "n_5m",
            "coverage_1m",
            "coverage_5m",
            "computed_at_ms",
        ):
            self.assertIn(key, row)

    def test_journal_refuses_d_path(self) -> None:
        with self.assertRaises(RuntimeError):
            TwP50JournalWriter(Path("/data/live"))

    def test_resolve_data_root_makes_tw_p50_dir(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        root = resolve_data_root({"BBOT_DATA_ROOT": str(tmp / "bbot")})
        self.assertTrue((root / "tw_p50").is_dir())
        self.assertTrue((root / "floor").is_dir())


class TwP50EmitPathTests(unittest.TestCase):
    def test_compute_and_journal_1hz_path(self) -> None:
        """Simulate the 1s emit path: note → compute_snapshots → journal."""
        tmp = Path(tempfile.mkdtemp())
        obs = LiveTwP50Observer(["ETH"], sample_cap=128)
        journal = TwP50JournalWriter(tmp)
        t0 = 1_725_100_000_000
        for i in range(5):
            obs.note_spreads("ETH", t0 + i * 5_000, 0.05, -0.02)
        now = t0 + 20_000
        snaps = obs.compute_snapshots(now_ms=now, computed_at_ms=now + 1)
        rows = [s.as_row() for s in snaps if s.n_1m > 0 or s.coverage_1m > 0]
        self.assertEqual(len(rows), 2)  # long + short
        written = journal.append_rows(rows)
        self.assertEqual(len(written), 1)
        path = written[0]
        self.assertEqual(path.name, "metrics.jsonl")
        self.assertTrue(path.is_file())
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        # Second emit appends (at most 1 Hz in runtime; here we just call twice).
        snaps2 = obs.compute_snapshots(now_ms=now + 1_000, computed_at_ms=now + 1_001)
        journal.append_rows([s.as_row() for s in snaps2 if s.n_1m > 0])
        lines2 = path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines2), 4)
        # Still only metrics.jsonl — no tick files.
        files = [p for p in tmp.rglob("*") if p.is_file()]
        self.assertTrue(all(p.name == "metrics.jsonl" for p in files))


class TwP50IsolationTests(unittest.TestCase):
    def test_module_has_no_private_imports(self) -> None:
        path = Path(__file__).resolve().parents[1] / "app" / "bot" / "tw_p50_watcher.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn("private", alias.name)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                self.assertNotIn("private", mod)
                self.assertFalse(mod.startswith("app.bot.private"))


try:
    import websockets  # noqa: F401

    _HAS_WEBSOCKETS = True
except ImportError:
    _HAS_WEBSOCKETS = False


@unittest.skipUnless(_HAS_WEBSOCKETS, "runtime import needs websockets")
class TwP50RuntimeWireTests(unittest.TestCase):
    def test_gear2_defaults_enable_both_observers(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        env = {
            "BBOT_MODE": "policy",
            "BBOT_PROFILE": "gear2_would_send",
            "BBOT_DATA_ROOT": str(tmp),
            "BBOT_LOG_PATH": str(tmp / "bbot.log"),
            "BBOT_COINS": "BTC,ETH",
            "BBOT_BROKER": "stub",
        }
        with patch.dict(os.environ, env, clear=False):
            from app.bot.runtime import BotRuntime

            rt = BotRuntime()
        self.assertTrue(rt.floor_enabled)
        self.assertTrue(rt.tw_p50_enabled)
        self.assertIsNotNone(rt.floor_observer)
        self.assertIsNotNone(rt.tw_p50_observer)
        self.assertTrue((tmp / "floor").is_dir())
        self.assertTrue((tmp / "tw_p50").is_dir())

    def test_explicit_off(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        env = {
            "BBOT_MODE": "policy",
            "BBOT_PROFILE": "gear2_would_send",
            "BBOT_DATA_ROOT": str(tmp),
            "BBOT_LOG_PATH": str(tmp / "bbot.log"),
            "BBOT_COINS": "BTC",
            "BBOT_BROKER": "stub",
            "BBOT_TW_P50_WATCH": "0",
        }
        with patch.dict(os.environ, env, clear=False):
            from app.bot.runtime import BotRuntime

            rt = BotRuntime()
        self.assertFalse(rt.tw_p50_enabled)
        self.assertIsNone(rt.tw_p50_observer)
        # Floor remains independently configurable.
        self.assertTrue(rt.floor_enabled)

    def test_emit_loop_journals_without_blocking_note(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        env = {
            "BBOT_MODE": "policy",
            "BBOT_PROFILE": "gear2_would_send",
            "BBOT_DATA_ROOT": str(tmp),
            "BBOT_LOG_PATH": str(tmp / "bbot.log"),
            "BBOT_COINS": "BTC",
            "BBOT_BROKER": "stub",
            "BBOT_TW_P50_WATCH": "1",
            "BBOT_FLOOR_WATCH": "0",
        }
        with patch.dict(os.environ, env, clear=False):
            from app.bot.runtime import BotRuntime

            rt = BotRuntime()

        t0 = 1_725_200_000_000
        rt._tw_p50_note_spreads("BTC", t0, 0.11, 0.22)
        rt._tw_p50_note_spreads("BTC", t0 + 2_000, 0.12, 0.21)

        async def _once() -> None:
            assert rt.tw_p50_observer is not None
            snaps = await asyncio.to_thread(
                rt.tw_p50_observer.compute_snapshots,
                now_ms=t0 + 5_000,
                computed_at_ms=t0 + 5_000,
            )
            rows = [s.as_row() for s in snaps if s.n_1m > 0]
            await rt._flush_tw_p50_rows(rows)

        asyncio.run(_once())
        metrics = list(tmp.joinpath("tw_p50").rglob("metrics.jsonl"))
        self.assertEqual(len(metrics), 1)
        body = metrics[0].read_text(encoding="utf-8").strip().splitlines()
        self.assertGreaterEqual(len(body), 1)
        self.assertFalse(any("tick" in p.name.lower() for p in tmp.rglob("*")))


if __name__ == "__main__":
    unittest.main()
