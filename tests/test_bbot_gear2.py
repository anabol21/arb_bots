"""Gear2 would_send contour: Arm A, four 0.02 thresholds, MA 10s, global K=1."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.bot.broker import make_broker
from app.bot.journal import JournalWriter
from app.bot.stub_broker import InstrumentMeta, StubBroker
from app.policy.features import CausalMaWindow
from app.policy.gear2_market_manager import MarketState, decide_market_tick
from app.policy.trade_manager import (
    DEFAULT_HYPER,
    DEFAULT_VARIATION,
    GEAR2_WOULD_SEND_COINS,
    GEAR2_WOULD_SEND_HYPER,
    GEAR2_WOULD_SEND_VARIATION,
    TickView,
    hyper_for_profile,
    variation_for_profile,
)
from validation.check_bbot_gear2 import check_gear2_journal


def _tick(*, coin_unused: str = "BTC", **kwargs: object) -> TickView:
    spread_long = float(kwargs.get("spread_long", 0.0))  # type: ignore[arg-type]
    spread_short = float(kwargs.get("spread_short", 0.0))  # type: ignore[arg-type]
    fields = dict(
        event_local_ts_ms=1_000_000.0,
        spread_long=spread_long,
        spread_short=spread_short,
        ma_long=kwargs.get("ma_long", spread_long if spread_long else 0.2),
        ma_short=kwargs.get("ma_short", spread_short if spread_short else 0.2),
        okx_latency_ms=30.0,
        bybit_latency_ms=20.0,
        valid=True,
        suppressed=False,
        stale=False,
    )
    fields.update(kwargs)
    return TickView(**fields)  # type: ignore[arg-type]


def _decide(tick: TickView, coin: str, state: MarketState | None = None) -> object:
    return decide_market_tick(
        tick,
        coin,
        state if state is not None else MarketState(),
        GEAR2_WOULD_SEND_VARIATION,
        GEAR2_WOULD_SEND_HYPER,
    )


class Gear2ContractTests(unittest.TestCase):
    def test_frozen_gear1_untouched(self) -> None:
        self.assertEqual(DEFAULT_VARIATION["thresh_open_long"], 0.5)
        self.assertEqual(DEFAULT_VARIATION["thresh_close_short"], 0.5)
        self.assertEqual(DEFAULT_HYPER["avg_window_sec"], 2.0)
        self.assertEqual(variation_for_profile("gear1")["thresh_open_long"], 0.5)
        self.assertEqual(hyper_for_profile("gear1")["avg_window_sec"], 2.0)

    def test_four_thresholds_point_zero_two(self) -> None:
        v = variation_for_profile("gear2_would_send")
        for key in (
            "thresh_open_long",
            "thresh_open_short",
            "thresh_close_long",
            "thresh_close_short",
        ):
            self.assertEqual(v[key], 0.02)
        self.assertEqual(v["open_frac"], 0.7)
        self.assertEqual(v["close_frac"], 0.7)

    def test_hyper_ma10_latency_caps(self) -> None:
        h = hyper_for_profile("gear2_would_send")
        self.assertEqual(h["avg_window_sec"], 10.0)
        self.assertEqual(h["max_latency_okx_ms"], 54.0)
        self.assertEqual(h["max_latency_bybit_ms"], 35.0)
        self.assertEqual(h["Trade_Lat"], 100)
        self.assertIs(h["Check_volume"], False)
        self.assertIsNone(h["regime_topn"])
        self.assertEqual(h["k"], 1)

    def test_coins_and_ma_window(self) -> None:
        self.assertEqual(GEAR2_WOULD_SEND_COINS, ("BTC", "ETH", "SOL", "XRP"))
        window = CausalMaWindow(avg_window_sec=float(GEAR2_WOULD_SEND_HYPER["avg_window_sec"]))
        self.assertEqual(window.avg_window_sec, 10.0)
        window.update(ts_ms=0.0, spread_long=1.0, spread_short=1.0, avg_valid=True)
        ma_l, _ = window.update(
            ts_ms=10_001.0, spread_long=2.0, spread_short=2.0, avg_valid=True
        )
        # Sample at 0ms is older than the 10s window and is dropped.
        self.assertEqual(ma_l, 2.0)


class Gear2ElifTests(unittest.TestCase):
    def test_strict_greater_than(self) -> None:
        equal = _decide(_tick(spread_long=0.02, spread_short=0.0), "BTC")
        self.assertEqual(equal.action, "flat")
        above = _decide(_tick(spread_long=0.0200001, spread_short=0.0), "BTC")
        self.assertEqual(above.action, "open_long")

    def test_long_open_before_short(self) -> None:
        both = _decide(_tick(spread_long=0.2, spread_short=0.3), "ETH")
        self.assertEqual(both.action, "open_long")

    def test_short_open_when_long_quiet(self) -> None:
        d = _decide(_tick(spread_long=0.0, spread_short=0.2), "SOL")
        self.assertEqual(d.action, "open_short")

    def test_slot_busy_other_coin(self) -> None:
        state = MarketState(position_side="long", held_coin="BTC")
        d = _decide(_tick(spread_long=0.2), "ETH", state)
        self.assertEqual(d.action, "flat")
        self.assertEqual(d.reason, "slot_busy")
        self.assertEqual(state.n_filtered_slot_busy, 1)

    def test_close_only_held_coin(self) -> None:
        state = MarketState(position_side="long", held_coin="BTC")
        other = _decide(_tick(spread_long=0.0, spread_short=0.2), "ETH", state)
        self.assertEqual(other.action, "flat")
        self.assertNotEqual(other.reason, "signal")
        held = _decide(_tick(spread_long=0.0, spread_short=0.2), "BTC", state)
        self.assertEqual(held.action, "close")

    def test_close_short_uses_spread_long(self) -> None:
        state = MarketState(position_side="short", held_coin="XRP")
        d = _decide(_tick(spread_long=0.2, spread_short=0.0), "XRP", state)
        self.assertEqual(d.action, "close")

    def test_pending_skip_other_coin(self) -> None:
        state = MarketState(pending_fill=True, pending_coin="BTC")
        skip = _decide(_tick(spread_long=0.2), "ETH", state)
        self.assertEqual(skip.reason, "pending_skip")
        self.assertEqual(state.n_filtered_pending_skip, 1)
        same = _decide(_tick(spread_long=0.2), "BTC", state)
        self.assertEqual(same.reason, "pending")
        self.assertEqual(state.n_filtered_pending_skip, 1)

    def test_topn_ignored_arm_a(self) -> None:
        hyper = dict(GEAR2_WOULD_SEND_HYPER)
        hyper["regime_topn"] = {"not": "used"}
        d = decide_market_tick(
            _tick(spread_long=0.2),
            "BTC",
            MarketState(),
            GEAR2_WOULD_SEND_VARIATION,
            hyper,
        )
        self.assertEqual(d.action, "open_long")
        self.assertEqual(d.reason, "signal")


def _meta(coin: str = "BTC") -> InstrumentMeta:
    return InstrumentMeta(
        base_coin=coin,
        okx_symbol=f"{coin}-USDT-SWAP",
        bybit_symbol=f"{coin}USDT",
        okx_lot_size=0.001,
        okx_min_size=0.001,
        bybit_qty_step=0.001,
        bybit_min_order_qty=0.001,
    )


def _books() -> tuple[dict, dict]:
    okx = {"bid_price": 100.0, "ask_price": 100.1, "bid_size": 10.0, "ask_size": 10.0}
    bybit = {"bid_price": 100.2, "ask_price": 100.3, "bid_size": 10.0, "ask_size": 10.0}
    return okx, bybit


class Gear2BrokerTests(unittest.TestCase):
    def _broker(self, tmp: Path) -> StubBroker:
        return StubBroker(
            data_root=tmp,
            journal=JournalWriter(tmp),
            trade_lat_ms=100,
            notional_usdt=100.0,
        )

    def test_same_coin_fill_and_held_coin(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        broker = self._broker(tmp)
        okx, bybit = _books()
        abort = broker.place(
            spread_side="open_long",
            base_coin="BTC",
            signal_ts_ms=1_000_000,
            okx_book=okx,
            bybit_book=bybit,
            meta=_meta("BTC"),
        )
        self.assertIsNone(abort)
        self.assertFalse(
            broker.on_valid_tick(
                base_coin="ETH",
                event_local_ts_ms=1_000_200,
                okx_book=okx,
                bybit_book=bybit,
            )
        )
        self.assertTrue(broker.has_pending())
        self.assertTrue(
            broker.on_valid_tick(
                base_coin="BTC",
                event_local_ts_ms=1_000_200,
                okx_book=okx,
                bybit_book=bybit,
            )
        )
        self.assertEqual(broker.position, "open_long")
        self.assertEqual(broker.held_coin, "BTC")
        self.assertFalse(broker.can_open())

    def test_close_wrong_coin_rejected(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        broker = self._broker(tmp)
        okx, bybit = _books()
        broker.place(
            spread_side="open_long",
            base_coin="BTC",
            signal_ts_ms=1_000_000,
            okx_book=okx,
            bybit_book=bybit,
            meta=_meta("BTC"),
        )
        broker.on_valid_tick(
            base_coin="BTC",
            event_local_ts_ms=1_000_200,
            okx_book=okx,
            bybit_book=bybit,
        )
        abort = broker.place(
            spread_side="close",
            base_coin="ETH",
            signal_ts_ms=1_000_400,
            okx_book=okx,
            bybit_book=bybit,
            meta=_meta("ETH"),
            close_of="open_long",
        )
        self.assertEqual(abort, "not_held_coin")

    def test_restart_restores_held_coin(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        broker = self._broker(tmp)
        okx, bybit = _books()
        broker.place(
            spread_side="open_short",
            base_coin="SOL",
            signal_ts_ms=1_000_000,
            okx_book=okx,
            bybit_book=bybit,
            meta=_meta("SOL"),
        )
        broker.on_valid_tick(
            base_coin="SOL",
            event_local_ts_ms=1_000_200,
            okx_book=okx,
            bybit_book=bybit,
        )
        restarted = self._broker(tmp)
        self.assertEqual(restarted.position, "open_short")
        self.assertEqual(restarted.held_coin, "SOL")
        self.assertFalse(restarted.can_open())


class Gear2RuntimeProfileTests(unittest.TestCase):
    def test_runtime_defaults(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        env = {
            "BBOT_MODE": "policy",
            "BBOT_PROFILE": "gear2_would_send",
            "BBOT_DATA_ROOT": str(tmp),
            "BBOT_LOG_PATH": str(tmp / "bbot.log"),
            "BBOT_COINS": "",
            "BBOT_BROKER": "stub",
        }
        with patch.dict(os.environ, env, clear=False):
            from app.bot.runtime import BotRuntime

            rt = BotRuntime()
        self.assertEqual(rt.coins, ["BTC", "ETH", "SOL", "XRP"])
        self.assertEqual(rt.variation["thresh_open_long"], 0.02)
        self.assertEqual(rt.variation["thresh_close_short"], 0.02)
        self.assertEqual(rt.hyper["avg_window_sec"], 10.0)
        self.assertEqual(rt.ma_windows["BTC"].avg_window_sec, 10.0)
        self.assertEqual(rt.market_state.k_live, 1)
        self.assertIsNone(rt.market_state.held_coin)


class Gear2BrokerFactoryTests(unittest.TestCase):
    def test_private_testnet_refuses_live(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        journal = JournalWriter(tmp)
        with self.assertRaises(RuntimeError):
            make_broker(
                data_root=tmp,
                journal=journal,
                trade_lat_ms=100,
                notional_usdt=100.0,
                log=lambda _m: None,
                env={"BBOT_BROKER": "private_testnet", "VENUE": "live"},
            )

    def test_private_testnet_refuses_live_orders_flag(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        journal = JournalWriter(tmp)
        with self.assertRaises(RuntimeError):
            make_broker(
                data_root=tmp,
                journal=journal,
                trade_lat_ms=100,
                notional_usdt=100.0,
                log=lambda _m: None,
                env={
                    "BBOT_BROKER": "private_testnet",
                    "VENUE": "testnet",
                    "LIVE_ORDERS": "1",
                },
            )

    def test_private_testnet_still_stub(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        journal = JournalWriter(tmp)
        broker = make_broker(
            data_root=tmp,
            journal=journal,
            trade_lat_ms=100,
            notional_usdt=100.0,
            log=lambda _m: None,
            env={"BBOT_BROKER": "private_testnet", "VENUE": "testnet", "LIVE_ORDERS": "0"},
        )
        self.assertIsInstance(broker, StubBroker)


class Gear2JournalValidatorTests(unittest.TestCase):
    def test_empty_journal_is_not_mechanical_fail(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        (tmp / "journal").mkdir(parents=True, exist_ok=True)
        errors = check_gear2_journal(tmp)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
