"""B2 hot-add history warm → would_send eligibility (no VPS)."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.bot.floor_watcher import BAR_MS, LiveFloorObserver  # noqa: E402
from app.bot.hot_add_warm import (  # noqa: E402
    HotAddWarmResult,
    bbot_hot_add_warm_enabled,
    build_warm_state_from_slim_spreads,
    drop_floor_coin_state,
    drop_tw_p50_coin_state,
    resolve_hot_add_history_root,
    seed_tw_p50_from_slim_ticks,
    warm_floor_from_history,
)
from app.bot.tw_p50_watcher import LiveTwP50Observer  # noqa: E402


def _write_floor_journal(
    root: Path,
    coin: str,
    *,
    n_bars: int = 60,
    t0_ms: int = 1_725_000_000_000,
) -> None:
    """Write enough floor metrics.jsonl rows for warm (both sides)."""
    day = "2024-09-01"
    path = root / "floor" / f"event_date={day}" / "metrics.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(n_bars):
        bar_end = t0_ms + (i + 1) * BAR_MS
        close = 0.10 + 0.001 * (i % 7)
        # Fake causal sma12 tip (finite after bar 12).
        sma12 = close if i >= 11 else float("nan")
        # Finite floor after enough SMA tips (~40).
        floor = (close - 0.02) if i >= 39 else None
        for side in ("long", "short"):
            rec = {
                "base_coin": coin,
                "side": side,
                "bar_end_ms": bar_end,
                "computed_at_ms": bar_end + 5,
                "close": close if side == "long" else close + 0.01,
                "sma12": (sma12 if side == "long" else sma12 + 0.01)
                if i >= 11
                else None,
            }
            if floor is not None:
                rec["floor_tf_select_a25"] = (
                    floor if side == "long" else floor + 0.01
                )
            rows.append(rec)
    with path.open("w", encoding="utf-8") as fh:
        for rec in rows:
            # JSON null for missing sma12
            out = dict(rec)
            if out.get("sma12") is not None and out["sma12"] != out["sma12"]:
                out["sma12"] = None
            fh.write(json.dumps(out) + "\n")


def _write_slim_ticks(
    root: Path,
    coin: str,
    *,
    n_bars: int = 80,
    t0_ms: int = 1_725_000_000_000,
) -> Path:
    """One tick per 5m bar so replay closes bars and fills SMA-12 hist."""
    path = root / "spread_ticks.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for i in range(n_bars):
            ts = t0_ms + i * BAR_MS + 10_000
            rec = {
                "event_local_ts_ms": ts,
                "base_coin": coin,
                "spread_long": 0.12 + 0.0005 * (i % 11),
                "spread_short": 0.15 + 0.0005 * (i % 9),
            }
            fh.write(json.dumps(rec) + "\n")
    return path


class EnvFlagTests(unittest.TestCase):
    def test_warm_default_on(self) -> None:
        old = os.environ.pop("BBOT_HOT_ADD_WARM", None)
        try:
            self.assertTrue(bbot_hot_add_warm_enabled())
        finally:
            if old is not None:
                os.environ["BBOT_HOT_ADD_WARM"] = old

    def test_warm_off(self) -> None:
        old = os.environ.get("BBOT_HOT_ADD_WARM")
        try:
            os.environ["BBOT_HOT_ADD_WARM"] = "0"
            self.assertFalse(bbot_hot_add_warm_enabled())
        finally:
            if old is None:
                os.environ.pop("BBOT_HOT_ADD_WARM", None)
            else:
                os.environ["BBOT_HOT_ADD_WARM"] = old

    def test_history_root_relative_under_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = os.environ.get("BBOT_HOT_ADD_HISTORY_ROOT")
            try:
                os.environ["BBOT_HOT_ADD_HISTORY_ROOT"] = "hist_copy"
                got = resolve_hot_add_history_root(root)
                self.assertEqual(got, root / "hist_copy")
            finally:
                if old is None:
                    os.environ.pop("BBOT_HOT_ADD_HISTORY_ROOT", None)
                else:
                    os.environ["BBOT_HOT_ADD_HISTORY_ROOT"] = old

    def test_history_root_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = os.environ.pop("BBOT_HOT_ADD_HISTORY_ROOT", None)
            try:
                self.assertIsNone(resolve_hot_add_history_root(root))
            finally:
                if old is not None:
                    os.environ["BBOT_HOT_ADD_HISTORY_ROOT"] = old


class WarmFromJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log = logging.getLogger("test-hot-add-warm")
        self.log.handlers.clear()
        self.log.addHandler(logging.NullHandler())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_warm_from_floor_journal(self) -> None:
        _write_floor_journal(self.root, "NEW1", n_bars=60)
        obs = LiveFloorObserver(["BTC"])
        old = os.environ.get("BBOT_HOT_ADD_HISTORY_MIN_SMA12")
        try:
            os.environ["BBOT_HOT_ADD_HISTORY_MIN_SMA12"] = "20"
            result = warm_floor_from_history(
                obs, "NEW1", self.root, min_sma12=20, logger=self.log
            )
        finally:
            if old is None:
                os.environ.pop("BBOT_HOT_ADD_HISTORY_MIN_SMA12", None)
            else:
                os.environ["BBOT_HOT_ADD_HISTORY_MIN_SMA12"] = old
        self.assertTrue(result.ok, result)
        self.assertEqual(result.source, "floor_journal")
        self.assertIsNotNone(obs.last_floor("NEW1", "long"))
        self.assertIsNotNone(obs.last_floor("NEW1", "short"))

    def test_skip_if_history_missing(self) -> None:
        obs = LiveFloorObserver(["BTC"])
        result = warm_floor_from_history(obs, "GHOST", self.root, logger=self.log)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "history_missing")
        self.assertIsNone(obs.last_floor("GHOST", "long"))

    def test_skip_if_history_root_missing(self) -> None:
        obs = LiveFloorObserver(["BTC"])
        missing = self.root / "no_such_dir"
        result = warm_floor_from_history(obs, "NEW1", missing, logger=self.log)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "history_root_missing")

    def test_skip_if_journal_short(self) -> None:
        _write_floor_journal(self.root, "NEW1", n_bars=5)
        obs = LiveFloorObserver([])
        result = warm_floor_from_history(
            obs, "NEW1", self.root, min_sma12=40, logger=self.log
        )
        self.assertFalse(result.ok)
        self.assertIn(result.reason, {"sma12_history_short", "floor_not_finite", "history_missing", "history_span_short"})


class WarmFromSlimTicksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log = logging.getLogger("test-hot-add-warm-slim")
        self.log.handlers.clear()
        self.log.addHandler(logging.NullHandler())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_warm_from_slim_spreads(self) -> None:
        _write_slim_ticks(self.root, "NEW2", n_bars=90)
        obs = LiveFloorObserver([])
        # Use a lower min for unit speed; still requires finite floors.
        result = warm_floor_from_history(
            obs,
            "NEW2",
            self.root,
            hours=12.0,
            min_sma12=20,
            now_ms=1_725_000_000_000 + 90 * BAR_MS,
            logger=self.log,
        )
        self.assertTrue(result.ok, result)
        self.assertEqual(result.source, "slim_spreads")
        self.assertGreater(result.ticks_fed, 0)
        self.assertIsNotNone(obs.last_floor("NEW2", "long"))

    def test_span_short_fail_closed(self) -> None:
        # Only ~30 minutes of ticks → span short vs 12h * 0.5.
        _write_slim_ticks(self.root, "NEW3", n_bars=6)
        obs = LiveFloorObserver([])
        result = warm_floor_from_history(
            obs,
            "NEW3",
            self.root,
            hours=12.0,
            min_sma12=5,
            now_ms=1_725_000_000_000 + 6 * BAR_MS,
            logger=self.log,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "history_span_short")

    def test_build_warm_state_helper(self) -> None:
        ticks = [
            (1_725_000_000_000 + i * BAR_MS + 1000, 0.1, 0.2) for i in range(50)
        ]
        payload = build_warm_state_from_slim_spreads(ticks, "AAA")
        self.assertIn("sides", payload)
        self.assertTrue(any("AAA" in k for k in payload["sides"]))


class DropStateTests(unittest.TestCase):
    def test_drop_floor_and_tw_p50(self) -> None:
        obs = LiveFloorObserver(["NEW1"])
        tw = LiveTwP50Observer(["NEW1"])
        t0 = 1_725_000_000_000
        for i in range(5):
            obs.note_spreads("NEW1", t0 + i * 10_000, 0.1, 0.2)
            tw.note_spreads("NEW1", t0 + i * 10_000, 0.1, 0.2)
        self.assertIn(("NEW1", "long"), obs._states)
        n1 = drop_floor_coin_state(obs, "NEW1")
        n2 = drop_tw_p50_coin_state(tw, "NEW1")
        self.assertGreater(n1, 0)
        self.assertGreater(n2, 0)
        self.assertNotIn(("NEW1", "long"), obs._states)
        self.assertNotIn(("NEW1", "long"), tw._rings)

    def test_seed_tw_p50(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_slim_ticks(root, "NEW1", n_bars=20)
            tw = LiveTwP50Observer([])
            now = 1_725_000_000_000 + 19 * BAR_MS + 10_000
            fed = seed_tw_p50_from_slim_ticks(
                tw, "NEW1", root, retain_ms=60 * 60 * 1000, now_ms=now
            )
            self.assertGreater(fed, 0)
            self.assertIn(("NEW1", "long"), tw._rings)


class TradeEligibilityRuntimeTests(unittest.TestCase):
    """Controller + eligibility without full BotRuntime (no websockets)."""

    def test_cap_still_works_with_warm_hook(self) -> None:
        from app.bot.hot_add import BotHotAddController
        from app.bot.stub_broker import InstrumentMeta

        def empty_book():
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

        def meta(coin: str) -> InstrumentMeta:
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

        quotes = {}
        universe = {}
        spawned = []
        eligible: set[str] = set()

        def spawn(row):
            coin = row["base_coin"]
            quotes[coin] = {"okx": empty_book(), "bybit": empty_book()}
            spawned.append(coin)
            # Simulate fail-closed warm miss → not eligible.
            eligible.discard(coin)

        logger = logging.getLogger("test-cap-warm")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        ctrl = BotHotAddController(
            quotes=quotes,
            universe=universe,
            spawn=spawn,
            max_extra=1,
            initial_pair_count=0,
            logger=logger,
        )

        def delta(coin: str):
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
                "discovered_at_utc": "2026-09-30T00:00:00Z",
            }

        added = ctrl.apply_rows([delta("A"), delta("B")])
        self.assertEqual(added, ["A"])
        self.assertEqual(spawned, ["A"])
        self.assertNotIn("B", quotes)
        self.assertNotIn("A", eligible)


class AbortCoinTests(unittest.TestCase):
    def test_abort_clears_slot(self) -> None:
        from app.bot.theta_trade_manager import OpenPosition, ThetaTradeManager

        with tempfile.TemporaryDirectory() as tmp:
            mgr = ThetaTradeManager(data_root=Path(tmp), log=lambda _m: None)
            mgr.slot.position = OpenPosition(
                trade_id="t1",
                base_coin="NEW1",
                side="long",
                open_signal_ts_ms=1,
                open_fill_ts_ms=2,
                open_fill_spread=0.1,
                open_notional=50.0,
                open_theta_1m=0.5,
            )
            mgr.slot.pending = False
            self.assertTrue(mgr.abort_coin_if_held("NEW1"))
            self.assertIsNone(mgr.slot.position)
            self.assertFalse(mgr.abort_coin_if_held("NEW1"))
            self.assertFalse(mgr.abort_coin_if_held("OTHER"))


if __name__ == "__main__":
    unittest.main()
