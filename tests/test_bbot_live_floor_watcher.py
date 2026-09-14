"""Live BotRuntime gear-2.2 floor observer (bar-close metrics only)."""

from __future__ import annotations

import ast
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app.bot.floor_watcher import (
    BAR_MS,
    CLOSE_HISTORY,
    DEFAULT_BAR_SAMPLE_CAP,
    FORMULA_ID,
    SMA12_HISTORY,
    FloorJournalWriter,
    LiveFloorObserver,
    _floors,
    causal_sma,
    floor_watch_enabled,
    tw_p05_p95_from_samples,
)
from app.bot.floor_plot import load_floor_rows, render_coin_html, write_coin_plots
from app.bot.paths import floor_metrics_jsonl_path, resolve_data_root


class FloorFlagTests(unittest.TestCase):
    def test_default_on_for_gear2(self) -> None:
        self.assertTrue(floor_watch_enabled("gear2_would_send", {}))
        self.assertTrue(floor_watch_enabled("canary_wal_eden", {}))
        self.assertFalse(floor_watch_enabled("gear1", {}))
        self.assertFalse(floor_watch_enabled("signal_test", {}))

    def test_env_override(self) -> None:
        self.assertFalse(
            floor_watch_enabled("gear2_would_send", {"BBOT_FLOOR_WATCH": "0"})
        )
        self.assertTrue(floor_watch_enabled("gear1", {"BBOT_FLOOR_WATCH": "1"}))


class FloorFormulaMatchTests(unittest.TestCase):
    def test_bar_close_sma_and_floor_match_compute_chosen_floor(self) -> None:
        """Synthetic 5m closes → observer tip equals batch causal SMA + floor."""
        floors = _floors()
        rng = np.random.default_rng(22)
        n = 160
        closes = 0.05 + 0.01 * rng.standard_normal(n)
        closes[-8:] += 0.2  # late spike so 3h vs 12h diverge
        t0 = (1_725_000_000_000 // BAR_MS) * BAR_MS

        obs = LiveFloorObserver(["SOL"])
        rows: list[dict] = []
        for i, close in enumerate(closes):
            mid = t0 + i * BAR_MS + 60_000
            rows.extend(
                obs.note_spreads(
                    "SOL",
                    mid,
                    float(close),
                    float(close) + 0.001,
                    computed_at_ms=mid,
                )
            )
        # Force final bar close by entering the next bucket.
        final_ts = t0 + n * BAR_MS + 1_000
        rows.extend(
            obs.note_spreads(
                "SOL",
                final_ts,
                0.0,
                0.0,
                computed_at_ms=final_ts,
            )
        )

        long_rows = [r for r in rows if r["side"] == "long"]
        self.assertEqual(len(long_rows), n)
        batch_sma3 = causal_sma(closes, 3)
        batch_sma12 = causal_sma(closes, 12)
        batch_floor = floors.compute_chosen_floor(batch_sma12)[floors.TF_SELECT_25_NAME]

        for i, row in enumerate(long_rows):
            self.assertEqual(row["formula_id"], FORMULA_ID)
            self.assertEqual(row["base_coin"], "SOL")
            self.assertAlmostEqual(float(row["close"]), float(closes[i]), places=12)
            tip3 = row["sma3"]
            tip12 = row["sma12"]
            tip_f = row["floor_tf_select_a25"]
            if math.isfinite(float(batch_sma3[i])):
                self.assertIsNotNone(tip3)
                self.assertAlmostEqual(float(tip3), float(batch_sma3[i]), places=10)
            else:
                self.assertIsNone(tip3)
            if math.isfinite(float(batch_sma12[i])):
                self.assertIsNotNone(tip12)
                self.assertAlmostEqual(float(tip12), float(batch_sma12[i]), places=10)
            else:
                self.assertIsNone(tip12)
            if math.isfinite(float(batch_floor[i])):
                self.assertIsNotNone(tip_f)
                self.assertAlmostEqual(float(tip_f), float(batch_floor[i]), places=10)
            else:
                self.assertIsNone(tip_f)

        self.assertTrue(obs.memory_bound_ok())
        state = obs._states[("SOL", "long")]  # noqa: SLF001
        self.assertLessEqual(len(state.closes), CLOSE_HISTORY)
        self.assertLessEqual(len(state.sma12_hist), SMA12_HISTORY)
        self.assertEqual(state.closes.maxlen, CLOSE_HISTORY)
        self.assertEqual(state.sma12_hist.maxlen, SMA12_HISTORY)


class FloorJournalTests(unittest.TestCase):
    def test_metrics_only_no_tick_files(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        writer = FloorJournalWriter(tmp)
        obs = LiveFloorObserver(["BTC"])
        t0 = (1_725_000_000_000 // BAR_MS) * BAR_MS
        obs.note_spreads("BTC", t0 + 10_000, 0.1, 0.2)
        rows = obs.note_spreads("BTC", t0 + BAR_MS + 10_000, 0.11, 0.21)
        self.assertEqual(len(rows), 2)
        paths = writer.append_rows(rows)
        self.assertEqual(len(paths), 1)
        text = paths[0].read_text(encoding="utf-8")
        parsed = [json.loads(line) for line in text.strip().splitlines()]
        self.assertEqual(len(parsed), 2)
        for rec in parsed:
            self.assertIn("sma3", rec)
            self.assertIn("sma12", rec)
            self.assertIn("floor_tf_select_a25", rec)
            self.assertIn("tw_p05", rec)
            self.assertIn("tw_p95", rec)
            self.assertNotIn("ticks", rec)
            self.assertNotIn("book", rec)

        names = [p.name for p in tmp.rglob("*") if p.is_file()]
        self.assertTrue(any(n == "metrics.jsonl" for n in names))
        self.assertFalse(any("tick" in n.lower() for n in names))
        self.assertFalse(any(n.endswith(".parquet") for n in names))

    def test_refuses_d_paths(self) -> None:
        with self.assertRaises(RuntimeError):
            FloorJournalWriter(Path("/data/live"))
        with self.assertRaises(RuntimeError):
            resolve_data_root({"BBOT_DATA_ROOT": "/data/bars"})


class FloorCorridorTests(unittest.TestCase):
    def test_tw_p05_p95_on_synthetic_in_bar_samples(self) -> None:
        """Known hold pattern: low then high → TW p05 near low, p95 near high."""
        t0 = (1_725_000_000_000 // BAR_MS) * BAR_MS
        bar_end = t0 + BAR_MS
        # 4 minutes at 0.10, then 1 minute at 0.50 (equal spacing 1s for first block).
        ts: list[int] = []
        vals: list[float] = []
        for i in range(240):  # 0..239s
            ts.append(t0 + i * 1000)
            vals.append(0.10)
        ts.append(t0 + 240_000)
        vals.append(0.50)
        p05, p95 = tw_p05_p95_from_samples(ts, vals, bar_end_ms=bar_end)
        self.assertAlmostEqual(p05, 0.10, places=6)
        self.assertAlmostEqual(p95, 0.50, places=6)

    def test_observer_journals_tw_and_discards_open_bar_samples(self) -> None:
        obs = LiveFloorObserver(["ETH"], sample_cap=DEFAULT_BAR_SAMPLE_CAP, rng=__import__("random").Random(0))
        t0 = (1_725_000_000_000 // BAR_MS) * BAR_MS
        # Several in-bar samples.
        for i in range(20):
            obs.note_spreads("ETH", t0 + 5_000 + i * 1_000, 0.1 + i * 0.01, 0.2)
        st = obs._states[("ETH", "long")]  # noqa: SLF001
        self.assertEqual(len(st.sample_vals), 20)
        rows = obs.note_spreads("ETH", t0 + BAR_MS + 1_000, 0.99, 0.88)
        long_row = next(r for r in rows if r["side"] == "long")
        self.assertIsNotNone(long_row["tw_p05"])
        self.assertIsNotNone(long_row["tw_p95"])
        self.assertLessEqual(float(long_row["tw_p05"]), float(long_row["tw_p95"]))
        self.assertEqual(long_row["samples_kept"], 20)
        self.assertEqual(long_row["samples_seen"], 20)
        # Open-bar samples discarded after close; new bar has only the roll tick.
        st = obs._states[("ETH", "long")]  # noqa: SLF001
        self.assertEqual(len(st.sample_vals), 1)
        self.assertTrue(obs.memory_bound_ok())

    def test_sample_cap_reservoir(self) -> None:
        obs = LiveFloorObserver(["XRP"], sample_cap=32, rng=__import__("random").Random(1))
        t0 = (1_725_000_000_000 // BAR_MS) * BAR_MS
        for i in range(100):
            obs.note_spreads("XRP", t0 + 1_000 + i * 100, 0.05, 0.06)
        st = obs._states[("XRP", "long")]  # noqa: SLF001
        self.assertEqual(len(st.sample_vals), 32)
        self.assertEqual(st.samples_seen, 100)
        rows = obs.note_spreads("XRP", t0 + BAR_MS + 500, 0.07, 0.08)
        long_row = next(r for r in rows if r["side"] == "long")
        self.assertEqual(long_row["samples_kept"], 32)
        self.assertEqual(long_row["samples_seen"], 100)
        self.assertIsNotNone(long_row["tw_p05"])
        self.assertIsNotNone(long_row["tw_p95"])


class FloorPlotTests(unittest.TestCase):
    def test_plot_html_from_journal_no_tick_files(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        writer = FloorJournalWriter(tmp)
        obs = LiveFloorObserver(["SOL"])
        t0 = (1_725_000_000_000 // BAR_MS) * BAR_MS
        for i in range(5):
            obs.note_spreads("SOL", t0 + 10_000 + i * 2_000, 0.12 + i * 0.01, 0.22)
        rows = obs.note_spreads("SOL", t0 + BAR_MS + 10_000, 0.15, 0.25)
        writer.append_rows(rows)
        out = tmp / "plots"
        written = write_coin_plots(coin="SOL", rows=rows, out_dir=out, write_png=False)
        html = written["html"].read_text(encoding="utf-8")
        self.assertIn("tw_p05", html)
        self.assertIn("tw_p95", html)
        self.assertIn("SMA-3", html)
        self.assertIn("tf-select", html)
        loaded = load_floor_rows([floor_metrics_jsonl_path(tmp, rows[0]["event_date"])])
        self.assertEqual(len(loaded), 2)
        # Still no tick artifacts under data root.
        self.assertFalse(any("tick" in p.name.lower() for p in tmp.rglob("*") if p.is_file()))
        self.assertIn("floor canary", render_coin_html(coin="SOL", long_rows=rows, short_rows=rows))


class FloorIsolationTests(unittest.TestCase):
    def test_module_has_no_private_imports(self) -> None:
        for rel in ("floor_watcher.py", "floor_plot.py"):
            path = Path(__file__).resolve().parents[1] / "app" / "bot" / rel
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
class FloorRuntimeWireTests(unittest.TestCase):
    def test_gear2_defaults_enable_observer(self) -> None:
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
        self.assertIsNotNone(rt.floor_observer)
        self.assertIsNotNone(rt.floor_journal)
        self.assertTrue((tmp / "floor").is_dir())

    def test_explicit_off(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        env = {
            "BBOT_MODE": "policy",
            "BBOT_PROFILE": "gear2_would_send",
            "BBOT_DATA_ROOT": str(tmp),
            "BBOT_LOG_PATH": str(tmp / "bbot.log"),
            "BBOT_COINS": "BTC",
            "BBOT_BROKER": "stub",
            "BBOT_FLOOR_WATCH": "0",
        }
        with patch.dict(os.environ, env, clear=False):
            from app.bot.runtime import BotRuntime

            rt = BotRuntime()
        self.assertFalse(rt.floor_enabled)
        self.assertIsNone(rt.floor_observer)

    def test_handle_book_queues_bar_close_without_private(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        env = {
            "BBOT_MODE": "policy",
            "BBOT_PROFILE": "gear2_would_send",
            "BBOT_DATA_ROOT": str(tmp),
            "BBOT_LOG_PATH": str(tmp / "bbot.log"),
            "BBOT_COINS": "BTC",
            "BBOT_BROKER": "stub",
            "BBOT_FLOOR_WATCH": "1",
        }
        with patch.dict(os.environ, env, clear=False):
            from app.bot.runtime import BotRuntime

            rt = BotRuntime()

        t0 = (1_725_000_000_000 // BAR_MS) * BAR_MS
        for exch in ("okx", "bybit"):
            rt.quotes["BTC"][exch].update(
                {
                    "bid_price": 100.0,
                    "ask_price": 100.1,
                    "bid_size": 1.0,
                    "ask_size": 1.0,
                    # Keep exchange/local stamps within age gate (~2s of event time).
                    "ts_exchange": float(t0 + 9_500),
                    "local_recv_ts_ms": float(t0 + 9_500),
                    "delivery_latency_ms": 10.0,
                }
            )
        rt.gate.note_subscribe_ok("BTC", "books5")
        rt.gate.note_subscribe_ok("BTC", "orderbook.1")
        # Both legs must share coin_generation before evaluate accepts.
        rt.gate.note_book_update("BTC", "okx", complete_l1=True)
        rt.gate.note_book_update("BTC", "bybit", complete_l1=True)

        with patch("app.bot.runtime.time") as mock_time:
            mock_time.time.return_value = (t0 + 10_000) / 1000.0
            rt._handle_book_sync("BTC", "okx")  # noqa: SLF001
        self.assertEqual(rt._floor_pending, [])  # noqa: SLF001
        # First bar should be open in memory.
        st = rt.floor_observer._states[("BTC", "long")]  # noqa: SLF001
        self.assertIsNotNone(st.bar_start_ms)
        self.assertGreaterEqual(st.tick_count, 1)

        for exch in ("okx", "bybit"):
            rt.quotes["BTC"][exch]["ts_exchange"] = float(t0 + BAR_MS + 9_500)
            rt.quotes["BTC"][exch]["local_recv_ts_ms"] = float(t0 + BAR_MS + 9_500)
        with patch("app.bot.runtime.time") as mock_time:
            mock_time.time.return_value = (t0 + BAR_MS + 10_000) / 1000.0
            rt._handle_book_sync("BTC", "okx")  # noqa: SLF001
        self.assertEqual(len(rt._floor_pending), 2)  # noqa: SLF001
        sides = {r["side"] for r in rt._floor_pending}  # noqa: SLF001
        self.assertEqual(sides, {"long", "short"})
        for rec in rt._floor_pending:  # noqa: SLF001
            self.assertIn("tw_p05", rec)
            self.assertIn("tw_p95", rec)

        import asyncio

        pending = list(rt._floor_pending)  # noqa: SLF001
        asyncio.run(rt._flush_floor_rows(pending))  # noqa: SLF001
        event_date = pending[0]["event_date"]
        path = floor_metrics_jsonl_path(tmp, event_date)
        self.assertTrue(path.is_file())
        self.assertFalse(any(p.name.endswith(".parquet") for p in tmp.rglob("*")))


class FloorsModuleSmokeTests(unittest.TestCase):
    """Ported formula lock: compute_chosen_floor is tf-select α25."""

    def test_chosen_floor_is_tf_select_alpha25(self) -> None:
        floors = _floors()
        n = 144
        s = np.linspace(0.0, 1.0, n)
        s[-5:] = 10.0
        chosen = floors.compute_chosen_floor(s)
        self.assertEqual(
            set(chosen), {floors.SMA12_NAME, floors.TF_SELECT_25_NAME}
        )
        trim3 = floors.causal_trim_floor(s, 36, alpha=0.25)
        trim12 = floors.causal_trim_floor(s, 144, alpha=0.25)
        got = chosen[floors.TF_SELECT_25_NAME]
        ok = np.isfinite(trim3) & np.isfinite(trim12)
        self.assertTrue(ok.any())
        np.testing.assert_allclose(got[ok], np.minimum(trim3[ok], trim12[ok]))


if __name__ == "__main__":
    unittest.main()
