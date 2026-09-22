"""Unit tests: gear22 θ K=1 would_send contour (no real orders, no tick WAL)."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.bot.floor_warm import (
    build_warm_state_from_floor_journal,
    load_floor_warm_pickle,
    save_floor_warm_pickle,
)
from app.bot.floor_watcher import LiveFloorObserver
from app.bot.paths import resolve_data_root
from app.bot.theta_screener import ThetaSnapshot
from app.bot.theta_trade_manager import (
    POLICY_ID,
    SCHEMA_VERSION,
    SYNTHETIC_POLICY_MODE,
    SyntheticPolicyGateError,
    SlotState,
    ThetaTradeConfig,
    ThetaTradeJournalWriter,
    ThetaTradeManager,
    ThetaTradeRecoveryError,
    decide_theta_k1,
    replay_theta_trade_history,
    slip_spread,
    theta_trade_enabled,
)
from research.gear22_backtest.policy import (
    SYNTHETIC_CLOSE_ROLL,
    SYNTHETIC_OPEN_ROLL,
    SYNTHETIC_ROLL_POLICY_ID,
    PolicyParams,
    synthetic_roll,
)


def _snap(
    coin: str,
    side: str,
    theta_1m: float | None,
    *,
    floor: float = 0.05,
    p50_1m: float | None = None,
    p50_5m: float | None = None,
    ts_ms: int = 1_700_000_000_000,
) -> ThetaSnapshot:
    p1 = p50_1m if p50_1m is not None else (
        (floor + theta_1m) if theta_1m is not None else None
    )
    return ThetaSnapshot(
        base_coin=coin,
        side=side,
        ts_ms=ts_ms,
        p50_1m=p1,
        p50_5m=p50_5m if p50_5m is not None else p1,
        floor_tf_select_a25=floor,
        theta_1m=theta_1m,
        theta_5m=(p1 - floor) if p1 is not None else None,
        computed_at_ms=ts_ms + 1,
    )


def _books(
    *,
    okx_ask: float = 100.0,
    okx_bid: float = 99.0,
    bybit_ask: float = 100.5,
    bybit_bid: float = 100.2,
    okx_ask_sz: float = 10.0,
    okx_bid_sz: float = 10.0,
    bybit_ask_sz: float = 10.0,
    bybit_bid_sz: float = 10.0,
) -> dict:
    return {
        "okx": {
            "bid_price": okx_bid,
            "ask_price": okx_ask,
            "bid_size": okx_bid_sz,
            "ask_size": okx_ask_sz,
            "local_recv_ts_ms": 1,
        },
        "bybit": {
            "bid_price": bybit_bid,
            "ask_price": bybit_ask,
            "bid_size": bybit_bid_sz,
            "ask_size": bybit_ask_sz,
            "local_recv_ts_ms": 1,
        },
    }


def _ts_for_roll(target: int, *, seed: int = 7) -> int:
    return next(ts for ts in range(1_000_000, 1_100_000) if synthetic_roll(ts, seed) == target)


class ThetaTradeFlagTests(unittest.TestCase):
    def test_default_on_for_gear22(self) -> None:
        self.assertTrue(theta_trade_enabled("gear22_would_send", {}))
        self.assertTrue(theta_trade_enabled("gear22", {}))
        self.assertTrue(theta_trade_enabled("gear22_live_canary", {}))
        self.assertFalse(theta_trade_enabled("gear2_would_send", {}))
        self.assertFalse(theta_trade_enabled("gear1", {}))

    def test_env_override(self) -> None:
        self.assertFalse(
            theta_trade_enabled("gear22_would_send", {"BBOT_THETA_TRADE": "0"})
        )
        self.assertTrue(
            theta_trade_enabled("gear2_would_send", {"BBOT_THETA_TRADE": "1"})
        )

    def test_synthetic_policy_is_explicit_stub_only(self) -> None:
        safe = {
            "BBOT_POLICY_MODE": SYNTHETIC_POLICY_MODE,
            "BBOT_PROFILE": "gear22_would_send",
            "BBOT_BROKER": "stub",
            "LIVE_ORDERS": "0",
            "BBOT_THETA_LIVE_SEND": "0",
            "BBOT_SYNTHETIC_ROLL_SEED": "7",
        }
        config = ThetaTradeConfig.from_env(safe)
        self.assertEqual(config.policy_id, SYNTHETIC_ROLL_POLICY_ID)
        self.assertEqual(config.policy_params.synthetic_roll_seed, 7)

        for override in (
            {"BBOT_BROKER": "private_live"},
            {"LIVE_ORDERS": "1"},
            {"BBOT_THETA_LIVE_SEND": "1"},
            {"BBOT_PROFILE": "gear22_live_canary"},
        ):
            with self.assertRaises(SyntheticPolicyGateError):
                ThetaTradeConfig.from_env({**safe, **override})


class DecideK1Tests(unittest.TestCase):
    def test_synthetic_roll_drives_k1_open_and_close(self) -> None:
        seed = 7
        params = PolicyParams(synthetic_roll_seed=seed)
        snaps = [
            _snap("BTC", "long", 0.01),
            _snap("BTC", "short", 0.01),
            _snap("ETH", "long", 0.01),
            _snap("ETH", "short", 0.01),
        ]
        quotes = {"BTC": _books(), "ETH": _books()}
        opened = decide_theta_k1(
            snaps,
            slot=SlotState(),
            thr=0.2,
            quotes=quotes,
            notional_usdt=20.0,
            coin_order=("ETH", "BTC"),
            policy_params=params,
            decision_ts_s=_ts_for_roll(SYNTHETIC_OPEN_ROLL, seed=seed),
        )
        self.assertEqual(opened.action, "open")
        self.assertEqual(opened.base_coin, "ETH")
        self.assertEqual(opened.reason, "synthetic_open_17")

        from app.bot.theta_trade_manager import OpenPosition

        slot = SlotState(
            position=OpenPosition(
                trade_id="t1",
                base_coin="ETH",
                side=opened.side,
                open_signal_ts_ms=1,
                open_fill_ts_ms=71,
                open_fill_spread=0.1,
                open_notional=20.0,
                open_theta_1m=0.01,
                fill_spread_pp=0.1,
            )
        )
        closed = decide_theta_k1(
            snaps,
            slot=slot,
            thr=0.2,
            quotes=quotes,
            notional_usdt=20.0,
            coin_order=("ETH", "BTC"),
            policy_params=params,
            decision_ts_s=_ts_for_roll(SYNTHETIC_CLOSE_ROLL, seed=seed),
        )
        self.assertEqual(closed.action, "close")
        self.assertEqual(closed.base_coin, "ETH")
        self.assertEqual(closed.reason, "synthetic_close_32")

    def test_entry_when_policy_qualifies(self) -> None:
        from research.gear22_backtest.policy import PolicyParams
        
        snaps = [
            _snap("BTC", "long", 0.60, p50_1m=0.80, floor=0.20),
            _snap("BTC", "short", 0.01),
        ]
        quotes = {"BTC": _books()}
        params = PolicyParams(theta_open=0.50, p50_open=0.60, min_profit_pp=0.20, min_theta_close=0.05)
        d = decide_theta_k1(
            snaps,
            slot=SlotState(),
            thr=0.2,
            quotes=quotes,
            notional_usdt=100.0,
            policy_params=params,
        )
        self.assertEqual(d.action, "open")
        self.assertEqual(d.side, "long")
        self.assertEqual(d.reason, "open_long")

    def test_policy_gate(self) -> None:
        from research.gear22_backtest.policy import PolicyParams
        
        snaps = [
            _snap("BTC", "long", 0.30, p50_1m=0.40, floor=0.10),
            _snap("BTC", "short", 0.30, p50_1m=0.40, floor=0.10),
        ]
        params = PolicyParams(theta_open=0.50, p50_open=0.60, min_profit_pp=0.20, min_theta_close=0.05)
        d = decide_theta_k1(
            snaps,
            slot=SlotState(),
            thr=0.2,
            quotes={"BTC": _books()},
            notional_usdt=100.0,
            policy_params=params,
        )
        self.assertEqual(d.action, "skip")
        self.assertIn(d.reason, ("no_signal", "hold_not_usable", "hold_below_threshold"))

    def test_slot_busy_skips_entry(self) -> None:
        from app.bot.theta_trade_manager import OpenPosition
        from research.gear22_backtest.policy import PolicyParams

        slot = SlotState(
            position=OpenPosition(
                trade_id="t1",
                base_coin="ETH",
                side="long",
                open_signal_ts_ms=1,
                open_fill_ts_ms=71,
                open_fill_spread=0.8,
                open_notional=100.0,
                open_theta_1m=0.3,
                fill_spread_pp=0.8,
            )
        )
        snaps = [
            _snap("BTC", "long", 0.5, p50_1m=0.90, floor=0.40),
            _snap("BTC", "short", 0.0),
            _snap("ETH", "long", 0.60, p50_1m=0.80, floor=0.20),
            _snap("ETH", "short", 0.01, p50_1m=0.10, floor=0.09),
        ]
        params = PolicyParams(theta_open=0.50, p50_open=0.60, min_profit_pp=0.20, min_theta_close=0.05)
        d = decide_theta_k1(
            snaps,
            slot=slot,
            thr=0.2,
            quotes={"BTC": _books(), "ETH": _books()},
            notional_usdt=100.0,
            policy_params=params,
        )
        self.assertEqual(d.action, "skip")
        self.assertIn(d.reason, ("slot_busy", "hold_open_overlap", "hold_below_min_theta", "hold_below_min_profit"))

    def test_exit_on_policy_close(self) -> None:
        from app.bot.theta_trade_manager import OpenPosition
        from research.gear22_backtest.policy import PolicyParams
        
        slot = SlotState(
            position=OpenPosition(
                trade_id="t1",
                base_coin="SOL",
                side="long",
                open_signal_ts_ms=1,
                open_fill_ts_ms=71,
                open_fill_spread=0.3,
                open_notional=100.0,
                open_theta_1m=0.3,
                fill_spread_pp=0.3,
            )
        )
        snaps = [
            _snap("SOL", "long", 0.30, p50_1m=0.80, floor=0.50),
            _snap("SOL", "short", 0.60, p50_1m=1.20, floor=0.60),
        ]
        params = PolicyParams(theta_open=0.50, p50_open=0.60, min_profit_pp=0.0, min_theta_close=0.05, fee_round_trip_pp=0.30)
        d = decide_theta_k1(
            snaps,
            slot=slot,
            thr=0.2,
            quotes={"SOL": _books(okx_bid=100.5, bybit_ask=100.0)},
            notional_usdt=100.0,
            policy_params=params,
        )
        self.assertEqual(d.action, "close")
        self.assertEqual(d.reason, "close_min_profit")

    def test_size_reject(self) -> None:
        snaps = [
            _snap("BTC", "long", 0.3),
            _snap("BTC", "short", 0.0),
        ]
        quotes = {
            "BTC": _books(okx_ask_sz=0.01, bybit_bid_sz=0.01),
        }
        d = decide_theta_k1(
            snaps,
            slot=SlotState(),
            thr=0.2,
            quotes=quotes,
            notional_usdt=100.0,
        )
        self.assertEqual(d.action, "skip")
        self.assertEqual(d.reject_reason, "insufficient_size")


class SlipAndFillTests(unittest.TestCase):
    def test_slip_sign_positive_when_worse(self) -> None:
        # Edge compressed 0.20 → 0.10 ⇒ slip = +0.10 (worse).
        self.assertAlmostEqual(slip_spread(signal_spread=0.20, fill_spread=0.10), 0.10)
        # Edge improved ⇒ negative slip.
        self.assertAlmostEqual(slip_spread(signal_spread=0.10, fill_spread=0.20), -0.10)

    def test_fill_ts_equals_signal_plus_70(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        mgr = ThetaTradeManager(
            data_root=tmp,
            config=ThetaTradeConfig(fill_delay_ms=70, theta_thr=0.2, notional_usdt=100),
            sleep_fn=lambda _s: None,
        )
        snaps = [
            _snap("BTC", "long", 0.3),
            _snap("BTC", "short", 0.0),
        ]
        rows = mgr.on_theta_snapshots(
            snaps, quotes={"BTC": _books()}, now_ms=1_000_000
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "open")
        self.assertEqual(rows[0]["signal_ts_ms"], 1_000_000)
        self.assertEqual(rows[0]["fill_ts_ms"], 1_000_070)
        self.assertEqual(rows[0]["latency_ms"], 70)
        self.assertEqual(rows[0]["schema_version"], SCHEMA_VERSION)

    def test_fill_size_bad_still_journals(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        mgr = ThetaTradeManager(
            data_root=tmp,
            config=ThetaTradeConfig(fill_delay_ms=70, notional_usdt=100),
            sleep_fn=lambda _s: None,
        )
        snaps = [
            _snap("BTC", "long", 0.3),
            _snap("BTC", "short", 0.0),
        ]
        quotes = {"BTC": _books()}
        # After sleep, shrink books so fill size fails.
        def _sleep(_s: float) -> None:
            quotes["BTC"] = _books(okx_ask_sz=0.001, bybit_bid_sz=0.001)

        mgr._sleep_fn = _sleep  # noqa: SLF001
        rows = mgr.on_theta_snapshots(snaps, quotes=quotes, now_ms=5_000)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["signal_size_ok"])
        self.assertFalse(rows[0]["fill_size_ok"])
        self.assertEqual(rows[0]["event"], "open")


class JournalSchemaTests(unittest.TestCase):
    def test_journal_schema_and_no_tick_wal(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        writer = ThetaTradeJournalWriter(tmp)
        row = {
            "schema_version": SCHEMA_VERSION,
            "trade_id": "abc",
            "base_coin": "ETH",
            "side": "short",
            "event": "open",
            "reason": "theta_entry",
            "signal_ts_ms": 1_725_000_000_000,
            "fill_ts_ms": 1_725_000_000_070,
            "latency_ms": 70,
            "theta_1m": 0.25,
            "theta_5m": 0.2,
            "floor": 0.05,
            "p50_1m": 0.3,
            "p50_5m": 0.25,
            "opposite_theta_1m": 0.01,
            "slip_spread": 0.0,
            "notional_usdt": 100.0,
        }
        paths = writer.append_rows([row])
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].name, "trades.jsonl")
        self.assertIn("theta_trades", str(paths[0]))
        loaded = json.loads(paths[0].read_text(encoding="utf-8").strip())
        for key in (
            "trade_id",
            "base_coin",
            "side",
            "event",
            "reason",
            "signal_ts_ms",
            "fill_ts_ms",
            "latency_ms",
            "theta_1m",
            "floor",
            "p50_1m",
            "slip_spread",
        ):
            self.assertIn(key, loaded)
        files = [p for p in tmp.rglob("*") if p.is_file()]
        self.assertTrue(all(p.name == "trades.jsonl" for p in files))
        self.assertFalse(any("tick" in p.name.lower() for p in tmp.rglob("*")))
        with self.assertRaises(RuntimeError):
            ThetaTradeJournalWriter(Path("/data/live"))
        with self.assertRaises(RuntimeError):
            ThetaTradeJournalWriter(Path("/data/compacted"))

    def test_resolve_data_root_makes_theta_trades(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        root = resolve_data_root({"BBOT_DATA_ROOT": str(tmp / "bbot")})
        self.assertTrue((root / "theta_trades").is_dir())
        self.assertTrue((root / "theta").is_dir())


class TradeHistoryRecoveryTests(unittest.TestCase):
    def _synthetic_config(self) -> ThetaTradeConfig:
        return ThetaTradeConfig(
            fill_delay_ms=0,
            notional_usdt=20.0,
            policy_params=PolicyParams(synthetic_roll_seed=7),
            policy_id=SYNTHETIC_ROLL_POLICY_ID,
        )

    def test_open_survives_restart_and_close_uses_same_trade(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snaps = [_snap("BTC", "long", 0.01), _snap("BTC", "short", 0.01)]
            quotes = {"BTC": _books()}
            opened_at = _ts_for_roll(SYNTHETIC_OPEN_ROLL, seed=7) * 1000
            closed_at = _ts_for_roll(SYNTHETIC_CLOSE_ROLL, seed=7) * 1000

            first = ThetaTradeManager(
                data_root=root,
                config=self._synthetic_config(),
                sleep_fn=lambda _s: None,
            )
            open_rows = first.on_theta_snapshots(
                snaps, quotes=quotes, now_ms=opened_at
            )
            self.assertEqual(open_rows[0]["event"], "open")
            trade_id = open_rows[0]["trade_id"]

            restarted = ThetaTradeManager(
                data_root=root,
                config=self._synthetic_config(),
                sleep_fn=lambda _s: None,
            )
            self.assertIsNotNone(restarted.slot.position)
            assert restarted.slot.position is not None
            self.assertEqual(restarted.slot.position.trade_id, trade_id)
            self.assertEqual(restarted.slot.position.base_coin, "BTC")

            close_rows = restarted.on_theta_snapshots(
                snaps, quotes=quotes, now_ms=closed_at
            )
            self.assertEqual(close_rows[0]["event"], "close")
            self.assertEqual(close_rows[0]["trade_id"], trade_id)
            self.assertIsNone(restarted.slot.position)
            replayed = replay_theta_trade_history(
                root, expected_policy_id=SYNTHETIC_ROLL_POLICY_ID
            )
            self.assertIsNone(replayed.position)
            self.assertEqual(replayed.lifecycle_rows, 2)

    def test_torn_tail_and_overlapping_open_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "theta_trades" / "event_date=2026-09-22" / "trades.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text('{"schema_version":"bbot.theta_trade.v1"', encoding="utf-8")
            with self.assertRaisesRegex(ThetaTradeRecoveryError, "truncated_history_tail"):
                replay_theta_trade_history(root)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            writer = ThetaTradeJournalWriter(root)
            base = {
                "schema_version": SCHEMA_VERSION,
                "trade_id": "trade-a",
                "base_coin": "BTC",
                "side": "long",
                "event": "open",
                "would_send": True,
                "send": False,
                "live_send": False,
                "signal_ts_ms": 1_700_000_000_000,
                "fill_ts_ms": 1_700_000_000_070,
                "spread_fill": 0.2,
                "notional_usdt": 20.0,
                "theta_1m": 0.3,
                "policy_id": POLICY_ID,
            }
            writer.append_rows([base, {**base, "trade_id": "trade-b"}])
            with self.assertRaisesRegex(ThetaTradeRecoveryError, "overlapping_open"):
                replay_theta_trade_history(root)

    def test_journal_failure_never_publishes_open_position(self) -> None:
        class BrokenJournal:
            def append_rows(self, _rows):
                raise OSError("disk full")

        with tempfile.TemporaryDirectory() as td:
            mgr = ThetaTradeManager(
                data_root=Path(td),
                config=ThetaTradeConfig(fill_delay_ms=0, notional_usdt=20.0),
                journal=BrokenJournal(),  # type: ignore[arg-type]
                sleep_fn=lambda _s: None,
            )
            snaps = [_snap("BTC", "long", 0.6, p50_1m=0.8), _snap("BTC", "short", 0.01)]
            with self.assertRaisesRegex(ThetaTradeRecoveryError, "trade_history_write_failed"):
                mgr.on_theta_snapshots(snaps, quotes={"BTC": _books()}, now_ms=1_000_000)
            self.assertIsNone(mgr.slot.position)
            self.assertTrue(mgr.recovery_blocked)
            with self.assertRaisesRegex(ThetaTradeRecoveryError, "trade_history_unhealthy"):
                mgr.on_theta_snapshots(snaps, quotes={"BTC": _books()}, now_ms=1_001_000)


class FloorWarmTests(unittest.TestCase):
    def test_warm_pickle_roundtrip(self) -> None:
        obs = LiveFloorObserver(["BTC"])
        obs.seed_last_floor("BTC", "long", 0.07, bar_end_ms=1000)
        state = obs._states[("BTC", "long")]  # noqa: SLF001
        state.closes.extend([0.1, 0.11, 0.12])
        state.sma12_hist.extend([0.1] * 30)
        tmp = Path(tempfile.mkdtemp()) / "floor_warm.pkl"
        save_floor_warm_pickle(tmp, obs.export_warm_state())
        obs2 = LiveFloorObserver(["BTC"])
        payload = load_floor_warm_pickle(tmp)
        n = obs2.apply_warm_state(payload)
        self.assertGreater(n, 0)
        self.assertAlmostEqual(obs2.last_floor("BTC", "long") or 0.0, 0.07)
        self.assertEqual(len(obs2._states[("BTC", "long")].sma12_hist), 30)  # noqa: SLF001

    def test_warm_from_floor_journal(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        day = tmp / "floor" / "event_date=2026-09-01"
        day.mkdir(parents=True)
        rows = []
        for i in range(40):
            rows.append(
                {
                    "base_coin": "SOL",
                    "side": "long",
                    "bar_end_ms": 1_725_000_000_000 + i * 300_000,
                    "close": 0.1 + i * 0.001,
                    "sma12": 0.1,
                    "floor_tf_select_a25": 0.05 if i >= 29 else None,
                    "computed_at_ms": 1,
                }
            )
        (day / "metrics.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )
        payload = build_warm_state_from_floor_journal(tmp, coins=["SOL"])
        self.assertIn("SOL|long", payload["sides"])
        self.assertIn("SOL|long", payload["last_floors"])


class IsolationTests(unittest.TestCase):
    def test_modules_have_no_private_imports(self) -> None:
        root = Path(__file__).resolve().parents[1] / "app" / "bot"
        for name in (
            "theta_trade_manager.py",
            "theta_trade_plot.py",
            "floor_warm.py",
        ):
            path = root / name
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn("private", alias.name)
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    self.assertNotIn("private", mod)
                    self.assertFalse(mod.startswith("app.bot.private"))
                    self.assertFalse(mod.startswith("app.screaner"))


class RuntimeWireTests(unittest.TestCase):
    @unittest.skipUnless(
        __import__("importlib").util.find_spec("websockets") is not None,
        "websockets not installed",
    )
    def test_runtime_enables_theta_trade_for_gear22(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        env = {
            "BBOT_MODE": "policy",
            "BBOT_PROFILE": "gear22_would_send",
            "BBOT_BROKER": "stub",
            "BBOT_COINS": "BTC,ETH",
            "BBOT_DATA_ROOT": str(tmp / "bbot"),
            "BBOT_LOG_PATH": str(tmp / "bbot.log"),
            "BBOT_THETA_TRADE": "1",
            "BBOT_FLOOR_WATCH": "1",
            "BBOT_TW_P50_WATCH": "1",
            "BBOT_THETA_WATCH": "1",
            "BBOT_FLOOR_WARM": "0",
        }
        with patch.dict("os.environ", env, clear=False):
            from app.bot.runtime import BotRuntime

            rt = BotRuntime()
            self.assertEqual(rt.profile, "gear22_would_send")
            self.assertTrue(rt.theta_trade_enabled)
            self.assertIsNotNone(rt.theta_trade)
            self.assertTrue(rt.floor_enabled)
            self.assertTrue(rt.tw_p50_enabled)
            self.assertTrue(rt.theta_enabled)
            self.assertFalse(rt._uses_market_manager())  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
