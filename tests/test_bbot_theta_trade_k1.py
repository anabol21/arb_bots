"""Unit tests: gear22 θ K=1 would_send contour (no real orders, no tick WAL)."""

from __future__ import annotations

import ast
import asyncio
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
    SCHEMA_VERSION,
    OpenPosition,
    SlotState,
    ThetaDecision,
    ThetaTradeConfig,
    ThetaTradeJournalWriter,
    ThetaTradeManager,
    decide_theta_k1,
    journal_close_pnl_spread,
    slip_spread,
    theta_trade_enabled,
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


class DecideK1Tests(unittest.TestCase):
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
        self.assertEqual(d.size_event, "open")

    def test_close_size_reject_uses_flatten_legs(self) -> None:
        slot = SlotState(position=_held_long())
        params = _close_params()
        # Open legs (OKX ask / Bybit bid) are deep. Flatten legs are thin.
        quotes = {
            "SOL": _books(
                okx_bid=100.5,
                bybit_ask=100.0,
                okx_ask_sz=10.0,
                bybit_bid_sz=10.0,
                okx_bid_sz=0.0001,
                bybit_ask_sz=0.0001,
            )
        }
        d = decide_theta_k1(
            _close_snaps(),
            slot=slot,
            thr=0.2,
            quotes=quotes,
            notional_usdt=100.0,
            policy_params=params,
        )
        self.assertEqual(d.action, "skip")
        self.assertEqual(d.reject_reason, "insufficient_size")
        self.assertEqual(d.size_event, "close")
        self.assertIsNotNone(d.size_info)
        assert d.size_info is not None
        self.assertFalse(d.size_info["size_ok"])
        self.assertEqual(d.size_info["okx_leg_side"], "sell")
        self.assertEqual(d.size_info["bybit_leg_side"], "buy")
        self.assertAlmostEqual(d.size_info["okx_available_size"], 0.0001)
        self.assertAlmostEqual(d.size_info["bybit_available_size"], 0.0001)
        self.assertIsNotNone(slot.position)

    def test_close_uses_flatten_legs_not_open_legs(self) -> None:
        # Open legs are thin; flatten legs (OKX bid / Bybit ask) are deep.
        quotes = {
            "SOL": _books(
                okx_bid=100.5,
                bybit_ask=100.0,
                okx_ask_sz=0.0001,
                bybit_bid_sz=0.0001,
                okx_bid_sz=10.0,
                bybit_ask_sz=10.0,
            )
        }
        d = decide_theta_k1(
            _close_snaps(),
            slot=SlotState(position=_held_long()),
            thr=0.2,
            quotes=quotes,
            notional_usdt=100.0,
            policy_params=_close_params(),
        )
        self.assertEqual(d.action, "close")
        self.assertEqual(d.reason, "close_min_profit")
        self.assertEqual(d.size_event, "close")
        assert d.size_info is not None
        self.assertTrue(d.size_info["size_ok"])
        self.assertEqual(d.size_info["okx_leg_side"], "sell")
        self.assertAlmostEqual(d.size_info["okx_available_size"], 10.0)


def _close_params():
    from research.gear22_backtest.policy import PolicyParams

    return PolicyParams(
        theta_open=0.50,
        p50_open=0.60,
        min_profit_pp=0.0,
        min_theta_close=0.05,
        fee_round_trip_pp=0.30,
    )


def _held_long(coin: str = "SOL") -> OpenPosition:
    return OpenPosition(
        trade_id="t1",
        base_coin=coin,
        side="long",
        open_signal_ts_ms=1,
        open_fill_ts_ms=71,
        open_fill_spread=0.3,
        open_notional=100.0,
        open_theta_1m=0.3,
        fill_spread_pp=0.3,
    )


def _close_snaps(coin: str = "SOL") -> list[ThetaSnapshot]:
    return [
        _snap(coin, "long", 0.30, p50_1m=0.80, floor=0.50),
        _snap(coin, "short", 0.60, p50_1m=1.20, floor=0.60),
    ]


class CloseSizeGateTests(unittest.TestCase):
    def _manager(
        self,
        *,
        live_send: bool = False,
        place_fn=None,
        slept: list[float] | None = None,
    ) -> ThetaTradeManager:
        tmp = Path(tempfile.mkdtemp())
        slept = slept if slept is not None else []
        kwargs = {}
        if live_send:
            kwargs = {
                "live_send": True,
                "place_fn": place_fn,
                "meta_fn": lambda _coin: {"ok": True},
            }
        mgr = ThetaTradeManager(
            data_root=tmp,
            config=ThetaTradeConfig(
                fill_delay_ms=70,
                notional_usdt=100.0,
                policy_params=_close_params(),
            ),
            sleep_fn=lambda seconds: slept.append(seconds),
            **kwargs,
        )
        mgr.slot.position = _held_long()
        return mgr

    def test_thin_close_keeps_position_until_later_emit(self) -> None:
        slept: list[float] = []
        mgr = self._manager(slept=slept)
        thin = {
            "SOL": _books(
                okx_bid=100.5,
                bybit_ask=100.0,
                okx_ask_sz=10.0,
                bybit_bid_sz=10.0,
                okx_bid_sz=0.0001,
                bybit_ask_sz=0.0001,
            )
        }
        rows = mgr.on_theta_snapshots(_close_snaps(), quotes=thin, now_ms=1_000_000)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "skip")
        self.assertEqual(rows[0]["reject_reason"], "insufficient_size")
        self.assertFalse(rows[0]["would_send"])
        self.assertFalse(rows[0]["send"])
        self.assertIsNotNone(mgr.slot.position)
        self.assertEqual(mgr.slot.position.trade_id, "t1")
        self.assertFalse(mgr.slot.pending)
        self.assertEqual(slept, [])

        fat = {"SOL": _books(okx_bid=100.5, bybit_ask=100.0)}
        rows2 = mgr.on_theta_snapshots(_close_snaps(), quotes=fat, now_ms=2_000_000)
        self.assertEqual(len(rows2), 1)
        self.assertEqual(rows2[0]["event"], "close")
        self.assertEqual(rows2[0]["trade_id"], "t1")
        self.assertTrue(rows2[0]["would_send"])
        self.assertEqual(slept, [0.07])
        self.assertIsNone(mgr.slot.position)

    def test_live_close_reject_does_not_place(self) -> None:
        slept: list[float] = []
        calls: list[dict] = []

        def _place(**kwargs):
            calls.append(kwargs)
            return None

        mgr = self._manager(live_send=True, place_fn=_place, slept=slept)
        quotes = {
            "SOL": _books(
                okx_bid=100.5,
                bybit_ask=100.0,
                okx_bid_sz=0.0001,
                bybit_ask_sz=0.0001,
            )
        }
        rows = mgr.on_theta_snapshots(_close_snaps(), quotes=quotes, now_ms=5_000)
        self.assertEqual(rows[0]["event"], "skip")
        self.assertEqual(rows[0]["reject_reason"], "insufficient_size")
        self.assertFalse(rows[0]["would_send"])
        self.assertEqual(calls, [])
        self.assertEqual(slept, [])
        self.assertEqual(mgr.slot.position.trade_id, "t1")

    def test_execute_rechecks_close_size(self) -> None:
        slept: list[float] = []
        mgr = self._manager(slept=slept)
        decision = ThetaDecision(
            action="close",
            base_coin="SOL",
            side="long",
            reason="close_min_profit",
            size_info={"size_ok": False},
        )
        rows = mgr.execute_decision(
            decision,
            snapshots=_close_snaps(),
            quotes={"SOL": _books(okx_bid=100.5, bybit_ask=100.0)},
            now_ms=3_000,
        )
        self.assertEqual(rows[0]["event"], "skip")
        self.assertEqual(rows[0]["reject_reason"], "insufficient_size")
        self.assertFalse(rows[0]["would_send"])
        self.assertEqual(slept, [])
        self.assertEqual(mgr.slot.position.trade_id, "t1")

    def test_execute_close_without_size_info_uses_flatten_legs(self) -> None:
        slept: list[float] = []
        mgr = self._manager(slept=slept)
        decision = ThetaDecision(
            action="close",
            base_coin="SOL",
            side="long",
            reason="close_min_profit",
        )
        quotes = {
            "SOL": _books(
                okx_bid=100.5,
                bybit_ask=100.0,
                okx_ask_sz=50.0,
                bybit_bid_sz=40.0,
                okx_bid_sz=0.01,
                bybit_ask_sz=0.02,
            )
        }
        rows = mgr.execute_decision(
            decision,
            snapshots=_close_snaps(),
            quotes=quotes,
            now_ms=4_000,
        )
        self.assertEqual(rows[0]["reject_reason"], "insufficient_size")
        self.assertFalse(rows[0]["would_send"])
        self.assertAlmostEqual(rows[0]["signal_okx_available_size"], 0.01)
        self.assertAlmostEqual(rows[0]["signal_bybit_available_size"], 0.02)
        self.assertEqual(slept, [])
        self.assertEqual(mgr.slot.position.trade_id, "t1")

    def test_async_execute_rechecks_close_size(self) -> None:
        slept: list[float] = []
        mgr = self._manager(slept=slept)
        decision = ThetaDecision(
            action="close",
            base_coin="SOL",
            side="long",
            reason="close_min_profit",
            size_info={"size_ok": False},
        )

        async def _run():
            return await mgr._execute_decision_async(  # noqa: SLF001
                decision,
                snapshots=_close_snaps(),
                quotes={"SOL": _books(okx_bid=100.5, bybit_ask=100.0)},
                now_ms=6_000,
            )

        rows = asyncio.run(_run())
        self.assertEqual(rows[0]["event"], "skip")
        self.assertEqual(rows[0]["reject_reason"], "insufficient_size")
        self.assertFalse(rows[0]["would_send"])
        self.assertEqual(slept, [])
        self.assertEqual(mgr.slot.position.trade_id, "t1")
        self.assertFalse(mgr.slot.pending)

    def test_skip_journal_fallback_uses_close_legs(self) -> None:
        from research.gear22_backtest.policy import Decision

        quotes = {
            "SOL": _books(
                okx_bid=100.5,
                bybit_ask=100.0,
                okx_ask_sz=50.0,
                bybit_bid_sz=40.0,
                okx_bid_sz=0.01,
                bybit_ask_sz=0.02,
            )
        }
        cases = [
            ThetaDecision(
                action="skip",
                base_coin="SOL",
                side="long",
                reason="reject",
                reject_reason="insufficient_size",
                size_event="close",
            ),
            ThetaDecision(
                action="skip",
                base_coin="SOL",
                side="long",
                reason="reject",
                reject_reason="insufficient_size",
                policy_decision=Decision(action="close", reason="close_min_profit"),
            ),
            ThetaDecision(
                action="skip",
                base_coin="SOL",
                side="long",
                reason="reject",
                reject_reason="insufficient_size",
            ),
        ]
        for decision in cases:
            slept: list[float] = []
            mgr = self._manager(slept=slept)
            rows = mgr.execute_decision(
                decision,
                snapshots=_close_snaps(),
                quotes=quotes,
                now_ms=7_000,
            )
            self.assertEqual(rows[0]["event"], "skip")
            self.assertEqual(rows[0]["reject_reason"], "insufficient_size")
            self.assertFalse(rows[0]["would_send"])
            self.assertAlmostEqual(rows[0]["signal_okx_available_size"], 0.01)
            self.assertAlmostEqual(rows[0]["signal_bybit_available_size"], 0.02)
            self.assertEqual(mgr.slot.position.trade_id, "t1")
            self.assertEqual(slept, [])

        # Open reject with no held coin still measures the open legs.
        flat = ThetaTradeManager(
            data_root=Path(tempfile.mkdtemp()),
            config=ThetaTradeConfig(fill_delay_ms=70, notional_usdt=100.0),
            sleep_fn=lambda _s: None,
        )
        rows = flat.execute_decision(
            ThetaDecision(
                action="skip",
                base_coin="SOL",
                side="long",
                reason="reject",
                reject_reason="insufficient_size",
            ),
            snapshots=_close_snaps(),
            quotes=quotes,
            now_ms=8_000,
        )
        self.assertFalse(rows[0]["would_send"])
        self.assertAlmostEqual(rows[0]["signal_okx_available_size"], 50.0)
        self.assertAlmostEqual(rows[0]["signal_bybit_available_size"], 40.0)


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


class JournalClosePnlSignTests(unittest.TestCase):
    """SAND long d0c91dee-316e-4080-9246-4abbe77d1180 close 2026-10-04.

    close_fill_spread is already the opposite (short) spread. open − close
    flipped that sign to +0.8501; gear 2.2 records open + close − 0.30.
    """

    SAND_OPEN = 0.6779661016949238
    SAND_CLOSE = -0.1721626274665675
    SAND_PNL = 0.20580347422835626
    SAND_WRONG = 0.8501287291614913

    def test_formula_locks_negative_close_sign(self) -> None:
        got = journal_close_pnl_spread(self.SAND_OPEN, self.SAND_CLOSE, 0.30)
        self.assertEqual(got, self.SAND_PNL)
        self.assertNotEqual(got, self.SAND_WRONG)
        self.assertAlmostEqual(got, 0.2058034742283563, places=15)
        # A positive opposite close is added, not subtracted.
        self.assertAlmostEqual(journal_close_pnl_spread(0.50, 0.20, 0.30), 0.40)
        self.assertIsNone(journal_close_pnl_spread(None, self.SAND_CLOSE, 0.30))
        self.assertIsNone(journal_close_pnl_spread(self.SAND_OPEN, None, 0.30))

    def _held(self) -> OpenPosition:
        return OpenPosition(
            trade_id="d0c91dee-316e-4080-9246-4abbe77d1180",
            base_coin="SAND",
            side="long",
            open_signal_ts_ms=1,
            open_fill_ts_ms=71,
            open_fill_spread=self.SAND_OPEN,
            open_notional=100.0,
            open_theta_1m=0.3,
            fill_spread_pp=self.SAND_OPEN,
        )

    def _decision(self) -> ThetaDecision:
        return ThetaDecision(
            action="close",
            base_coin="SAND",
            side="long",
            reason="close_min_profit",
            size_info={"size_ok": True},
            potential_pp=self.SAND_PNL,
        )

    def _mgr(self, *, live: bool = False) -> ThetaTradeManager:
        tmp = Path(tempfile.mkdtemp())
        kwargs = {}
        if live:
            kwargs = {
                "live_send": True,
                "place_fn": lambda **_kwargs: None,
                "meta_fn": lambda _coin: {"ok": True},
            }
        mgr = ThetaTradeManager(
            data_root=tmp,
            config=ThetaTradeConfig(
                fill_delay_ms=0,
                notional_usdt=100.0,
                policy_params=_close_params(),
            ),
            sleep_fn=lambda _seconds: None,
            **kwargs,
        )
        mgr.slot.position = self._held()
        return mgr

    def _assert_sand_row(self, row: dict) -> None:
        self.assertEqual(row["event"], "close")
        self.assertEqual(row["trade_id"], "d0c91dee-316e-4080-9246-4abbe77d1180")
        self.assertEqual(row["open_fill_spread"], self.SAND_OPEN)
        self.assertEqual(row["close_fill_spread"], self.SAND_CLOSE)
        self.assertEqual(row["pnl_spread"], self.SAND_PNL)
        self.assertNotEqual(row["pnl_spread"], self.SAND_WRONG)
        self.assertEqual(row["potential_pp"], self.SAND_PNL)
        self.assertAlmostEqual(row["pnl_usdt_approx"], self.SAND_PNL)
        self.assertTrue(row["would_send"])

    def test_would_send_close_journals_sand_pnl(self) -> None:
        mgr = self._mgr()
        with patch(
            "app.bot.theta_trade_manager.spread_for_side",
            return_value=self.SAND_CLOSE,
        ):
            rows = mgr.execute_decision(
                self._decision(),
                snapshots=[],
                quotes={"SAND": _books()},
                now_ms=4_000,
            )
        self.assertEqual(len(rows), 1)
        self._assert_sand_row(rows[0])
        self.assertFalse(rows[0]["send"])
        self.assertIsNone(mgr.slot.position)

    def test_live_send_close_uses_the_same_pnl_write(self) -> None:
        mgr = self._mgr(live=True)
        with patch(
            "app.bot.theta_trade_manager.spread_for_side",
            return_value=self.SAND_CLOSE,
        ):
            rows = mgr.execute_decision(
                self._decision(),
                snapshots=[],
                quotes={"SAND": _books()},
                now_ms=5_000,
            )
        self.assertEqual(len(rows), 1)
        self._assert_sand_row(rows[0])
        self.assertTrue(rows[0]["send"])
        self.assertIsNone(mgr.slot.position)

    def test_async_would_send_close_journals_sand_pnl(self) -> None:
        mgr = self._mgr()

        async def _run():
            with patch(
                "app.bot.theta_trade_manager.spread_for_side",
                return_value=self.SAND_CLOSE,
            ):
                return await mgr._execute_decision_async(
                    self._decision(),
                    snapshots=[],
                    quotes={"SAND": _books()},
                    now_ms=6_000,
                )

        rows = asyncio.run(_run())
        self.assertEqual(len(rows), 1)
        self._assert_sand_row(rows[0])
        self.assertFalse(rows[0]["send"])
        self.assertIsNone(mgr.slot.position)

