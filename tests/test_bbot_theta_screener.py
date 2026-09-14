"""Live BotRuntime theta screener (p50 − floor, no tick WAL)."""

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

from app.bot.floor_watcher import FloorSnapshot, LiveFloorObserver
from app.bot.paths import resolve_data_root
from app.bot.theta_screener import (
    SCHEMA_VERSION,
    LiveThetaScreener,
    ThetaJournalWriter,
    compute_theta,
    theta_from_inputs,
    theta_watch_enabled,
)
from app.bot.tw_p50_watcher import LiveTwP50Observer, TwP50Snapshot


class ThetaFlagTests(unittest.TestCase):
    def test_default_on_for_gear2(self) -> None:
        self.assertTrue(theta_watch_enabled("gear2_would_send", {}))
        self.assertTrue(theta_watch_enabled("canary_wal_eden", {}))
        self.assertFalse(theta_watch_enabled("gear1", {}))
        self.assertFalse(theta_watch_enabled("signal_test", {}))

    def test_env_override(self) -> None:
        self.assertFalse(
            theta_watch_enabled("gear2_would_send", {"BBOT_THETA_WATCH": "0"})
        )
        self.assertTrue(theta_watch_enabled("gear1", {"BBOT_THETA_WATCH": "1"}))


class ThetaArithmeticTests(unittest.TestCase):
    def test_both_finite(self) -> None:
        floor, th1, th5 = compute_theta(0.10, 0.20, 0.05)
        self.assertAlmostEqual(floor, 0.05)
        self.assertAlmostEqual(th1, 0.05)
        self.assertAlmostEqual(th5, 0.15)

    def test_floor_non_finite_nulls_all(self) -> None:
        for bad in (None, float("nan"), float("inf"), float("-inf"), "x"):
            floor, th1, th5 = compute_theta(0.1, 0.2, bad)
            self.assertIsNone(floor)
            self.assertIsNone(th1)
            self.assertIsNone(th5)

    def test_p50_1m_null_keeps_theta_5m(self) -> None:
        floor, th1, th5 = compute_theta(None, 0.30, 0.10)
        self.assertAlmostEqual(floor, 0.10)
        self.assertIsNone(th1)
        self.assertAlmostEqual(th5, 0.20)

    def test_p50_5m_null_keeps_theta_1m(self) -> None:
        floor, th1, th5 = compute_theta(0.40, float("nan"), 0.10)
        self.assertAlmostEqual(floor, 0.10)
        self.assertAlmostEqual(th1, 0.30)
        self.assertIsNone(th5)

    def test_snapshot_row_schema(self) -> None:
        snap = theta_from_inputs(
            base_coin="btc",
            side="long",
            ts_ms=1_700_000_000_000,
            p50_1m=0.12,
            p50_5m=0.11,
            floor=0.08,
            computed_at_ms=1_700_000_000_100,
        )
        row = snap.as_row()
        self.assertEqual(row["schema_version"], SCHEMA_VERSION)
        self.assertEqual(row["base_coin"], "BTC")
        self.assertEqual(row["side"], "long")
        self.assertAlmostEqual(row["theta_1m"], 0.04)
        self.assertAlmostEqual(row["theta_5m"], 0.03)
        self.assertAlmostEqual(row["floor_tf_select_a25"], 0.08)


class FloorLastApiTests(unittest.TestCase):
    def test_last_floor_stores_finite_only(self) -> None:
        obs = LiveFloorObserver(["BTC"])
        # Inject a closed-bar row via the internal store helper.
        obs._maybe_store_last_floor(  # noqa: SLF001
            {
                "base_coin": "BTC",
                "side": "long",
                "floor_tf_select_a25": 0.07,
                "bar_end_ms": 1000,
                "computed_at_ms": 1001,
            }
        )
        obs._maybe_store_last_floor(  # noqa: SLF001
            {
                "base_coin": "BTC",
                "side": "long",
                "floor_tf_select_a25": float("nan"),
                "bar_end_ms": 2000,
                "computed_at_ms": 2001,
            }
        )
        self.assertAlmostEqual(obs.last_floor("BTC", "long"), 0.07)
        snap = obs.last_floor_snapshot("btc", "long")
        self.assertIsInstance(snap, FloorSnapshot)
        assert snap is not None
        self.assertEqual(snap.bar_end_ms, 1000)
        self.assertIsNone(obs.last_floor("BTC", "short"))


class ThetaScreenerTests(unittest.TestCase):
    def test_from_tw_snapshots_uses_last_floor(self) -> None:
        floor_obs = LiveFloorObserver(["ETH"])
        floor_obs._maybe_store_last_floor(  # noqa: SLF001
            {
                "base_coin": "ETH",
                "side": "short",
                "floor_tf_select_a25": 0.02,
                "bar_end_ms": 50,
                "computed_at_ms": 51,
            }
        )
        tw_obs = LiveTwP50Observer(["ETH"])
        screener = LiveThetaScreener(
            ["ETH"], floor_observer=floor_obs, tw_p50_observer=tw_obs
        )
        tw = TwP50Snapshot(
            base_coin="ETH",
            side="short",
            ts_ms=99,
            p50_1m=0.05,
            p50_5m=0.04,
            n_1m=3,
            n_5m=5,
            coverage_1m=1.0,
            coverage_5m=1.0,
            computed_at_ms=100,
        )
        out = screener.compute_from_tw_snapshots([tw], computed_at_ms=101)
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0].theta_1m, 0.03)
        self.assertAlmostEqual(out[0].theta_5m, 0.02)
        self.assertAlmostEqual(out[0].floor_tf_select_a25, 0.02)

    def test_null_when_no_floor(self) -> None:
        screener = LiveThetaScreener(["BTC"], floor_observer=None, tw_p50_observer=None)
        tw = TwP50Snapshot(
            base_coin="BTC",
            side="long",
            ts_ms=1,
            p50_1m=0.1,
            p50_5m=0.1,
            n_1m=1,
            n_5m=1,
            coverage_1m=0.5,
            coverage_5m=0.5,
            computed_at_ms=2,
        )
        out = screener.compute_from_tw_snapshots([tw], computed_at_ms=3)
        self.assertIsNone(out[0].floor_tf_select_a25)
        self.assertIsNone(out[0].theta_1m)
        self.assertIsNone(out[0].theta_5m)

    def test_journal_no_tick_files_and_refuses_d(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        writer = ThetaJournalWriter(tmp)
        snap = theta_from_inputs(
            base_coin="SOL",
            side="long",
            ts_ms=1_725_000_000_000,
            p50_1m=0.2,
            p50_5m=0.15,
            floor=0.05,
            computed_at_ms=1_725_000_000_050,
        )
        paths = writer.append_rows([snap.as_row()])
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].name, "metrics.jsonl")
        self.assertIn("theta", str(paths[0]))
        line = paths[0].read_text(encoding="utf-8").strip().splitlines()[0]
        row = json.loads(line)
        self.assertEqual(row["schema_version"], SCHEMA_VERSION)
        for key in (
            "base_coin",
            "side",
            "ts_ms",
            "p50_1m",
            "p50_5m",
            "floor_tf_select_a25",
            "theta_1m",
            "theta_5m",
            "computed_at_ms",
        ):
            self.assertIn(key, row)
        files = [p for p in tmp.rglob("*") if p.is_file()]
        self.assertTrue(all(p.name == "metrics.jsonl" for p in files))
        self.assertFalse(any("tick" in p.name.lower() for p in tmp.rglob("*")))
        with self.assertRaises(RuntimeError):
            ThetaJournalWriter(Path("/data/live"))
        with self.assertRaises(RuntimeError):
            ThetaJournalWriter(Path("/data/bars"))

    def test_resolve_data_root_makes_theta_dir(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        root = resolve_data_root({"BBOT_DATA_ROOT": str(tmp / "bbot")})
        self.assertTrue((root / "theta").is_dir())
        self.assertTrue((root / "tw_p50").is_dir())
        self.assertTrue((root / "floor").is_dir())


class ThetaIsolationTests(unittest.TestCase):
    def test_module_has_no_private_imports(self) -> None:
        path = Path(__file__).resolve().parents[1] / "app" / "bot" / "theta_screener.py"
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
class ThetaRuntimeWireTests(unittest.TestCase):
    def test_gear2_defaults_enable_all_three(self) -> None:
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
        self.assertTrue(rt.theta_enabled)
        self.assertIsNotNone(rt.floor_observer)
        self.assertIsNotNone(rt.tw_p50_observer)
        self.assertIsNotNone(rt.theta_screener)
        self.assertTrue((tmp / "theta").is_dir())

    def test_explicit_theta_off(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        env = {
            "BBOT_MODE": "policy",
            "BBOT_PROFILE": "gear2_would_send",
            "BBOT_DATA_ROOT": str(tmp),
            "BBOT_LOG_PATH": str(tmp / "bbot.log"),
            "BBOT_COINS": "BTC",
            "BBOT_BROKER": "stub",
            "BBOT_THETA_WATCH": "0",
        }
        with patch.dict(os.environ, env, clear=False):
            from app.bot.runtime import BotRuntime

            rt = BotRuntime()
        self.assertFalse(rt.theta_enabled)
        self.assertIsNone(rt.theta_screener)
        self.assertTrue(rt.tw_p50_enabled)
        self.assertTrue(rt.floor_enabled)

    def test_emit_follow_on_journals_theta_no_ticks(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        env = {
            "BBOT_MODE": "policy",
            "BBOT_PROFILE": "gear2_would_send",
            "BBOT_DATA_ROOT": str(tmp),
            "BBOT_LOG_PATH": str(tmp / "bbot.log"),
            "BBOT_COINS": "BTC",
            "BBOT_BROKER": "stub",
            "BBOT_FLOOR_WATCH": "1",
            "BBOT_TW_P50_WATCH": "1",
            "BBOT_THETA_WATCH": "1",
        }
        with patch.dict(os.environ, env, clear=False):
            from app.bot.runtime import BotRuntime

            rt = BotRuntime()

        assert rt.floor_observer is not None
        rt.floor_observer._maybe_store_last_floor(  # noqa: SLF001
            {
                "base_coin": "BTC",
                "side": "long",
                "floor_tf_select_a25": 0.01,
                "bar_end_ms": 1,
                "computed_at_ms": 2,
            }
        )
        rt.floor_observer._maybe_store_last_floor(  # noqa: SLF001
            {
                "base_coin": "BTC",
                "side": "short",
                "floor_tf_select_a25": 0.02,
                "bar_end_ms": 1,
                "computed_at_ms": 2,
            }
        )

        t0 = 1_725_300_000_000
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
            await rt._emit_theta_from_tw(snaps)

        asyncio.run(_once())
        theta_metrics = list(tmp.joinpath("theta").rglob("metrics.jsonl"))
        self.assertEqual(len(theta_metrics), 1)
        body = theta_metrics[0].read_text(encoding="utf-8").strip().splitlines()
        self.assertGreaterEqual(len(body), 1)
        parsed = [json.loads(line) for line in body]
        long_row = next(r for r in parsed if r["side"] == "long")
        self.assertEqual(long_row["schema_version"], SCHEMA_VERSION)
        self.assertIsNotNone(long_row["theta_1m"])
        self.assertTrue(math.isfinite(long_row["theta_1m"]))
        self.assertFalse(any("tick" in p.name.lower() for p in tmp.rglob("*")))
        # Only metrics.jsonl under theta/ and tw_p50/
        for sub in ("theta", "tw_p50"):
            files = [p for p in tmp.joinpath(sub).rglob("*") if p.is_file()]
            self.assertTrue(all(p.name == "metrics.jsonl" for p in files))


if __name__ == "__main__":
    unittest.main()
