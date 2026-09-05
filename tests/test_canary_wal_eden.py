"""WAL/EDEN canary: variation 0.1, pre-signal L1 depth, LIVE_SIZE allowlist."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.bot.private.order_symbols import (
    SymbolGateError,
    resolve_allowed_futures_symbol,
    resolve_live_size_futures_symbol,
)
from app.policy.gear2_market_manager import MarketState, decide_market_tick
from app.policy.trade_manager import (
    CANARY_WAL_EDEN_COINS,
    CANARY_WAL_EDEN_HYPER,
    CANARY_WAL_EDEN_VARIATION,
    GEAR2_WOULD_SEND_HYPER,
    GEAR2_WOULD_SEND_VARIATION,
    LIVE_SIZE_COINS,
    TickView,
    hyper_for_profile,
    live_size_coin_allowed,
    live_size_coins_for_profile,
    uses_gear2_market_manager,
    variation_for_profile,
)


def _tick(**kwargs: object) -> TickView:
    spread_long = float(kwargs.get("spread_long", 0.2))  # type: ignore[arg-type]
    spread_short = float(kwargs.get("spread_short", 0.0))  # type: ignore[arg-type]
    fields = dict(
        event_local_ts_ms=1_000_000.0,
        spread_long=spread_long,
        spread_short=spread_short,
        ma_long=kwargs.get("ma_long", 0.2),
        ma_short=kwargs.get("ma_short", 0.2),
        okx_latency_ms=30.0,
        bybit_latency_ms=20.0,
        okx_bid=1.0,
        okx_ask=1.0,
        bybit_bid=1.0,
        bybit_ask=1.0,
        okx_bid_size=100.0,
        okx_ask_size=100.0,
        bybit_bid_size=100.0,
        bybit_ask_size=100.0,
        valid=True,
        suppressed=False,
        stale=False,
    )
    fields.update(kwargs)
    return TickView(**fields)  # type: ignore[arg-type]


def _decide(tick: TickView, coin: str = "WAL", state: MarketState | None = None) -> object:
    return decide_market_tick(
        tick,
        coin,
        state if state is not None else MarketState(),
        CANARY_WAL_EDEN_VARIATION,
        CANARY_WAL_EDEN_HYPER,
    )


class CanaryVariationTests(unittest.TestCase):
    def test_canary_thresholds_frac_and_hyper(self) -> None:
        v = variation_for_profile("canary_wal_eden")
        for key in (
            "thresh_open_long",
            "thresh_open_short",
            "thresh_close_long",
            "thresh_close_short",
        ):
            self.assertEqual(v[key], 0.1)
        self.assertEqual(v["open_frac"], 0.7)
        self.assertEqual(v["close_frac"], 0.7)
        h = hyper_for_profile("canary")
        self.assertEqual(h["avg_window_sec"], 10.0)
        self.assertEqual(h["k"], 1)
        self.assertTrue(h["Check_l1_depth"])
        self.assertFalse(h["Check_volume"])
        self.assertEqual(h["position_size"], 10.0)
        self.assertEqual(CANARY_WAL_EDEN_COINS, ("WAL", "EDEN"))

    def test_gear2_would_send_defaults_untouched(self) -> None:
        v = variation_for_profile("gear2_would_send")
        self.assertEqual(v["thresh_open_long"], 0.02)
        self.assertEqual(v["thresh_close_short"], 0.02)
        h = hyper_for_profile("gear2_would_send")
        self.assertFalse(h.get("Check_l1_depth", False))
        self.assertEqual(GEAR2_WOULD_SEND_VARIATION["thresh_open_long"], 0.02)
        self.assertFalse(GEAR2_WOULD_SEND_HYPER.get("Check_l1_depth", False))

    def test_uses_shared_market_manager(self) -> None:
        self.assertTrue(uses_gear2_market_manager("canary_wal_eden"))
        self.assertTrue(uses_gear2_market_manager("canary"))
        self.assertTrue(uses_gear2_market_manager("gear2_would_send"))
        self.assertFalse(uses_gear2_market_manager("gear1"))


class CanaryDepthGateTests(unittest.TestCase):
    def test_fail_closed_missing_size(self) -> None:
        d = _decide(_tick(okx_ask_size=None, bybit_bid_size=100.0))
        self.assertEqual(d.action, "flat")
        self.assertEqual(d.reason, "gate_l1_depth")
        self.assertEqual(d.counters["n_filtered_by_l1_depth"], 1)

    def test_fail_closed_thin_book(self) -> None:
        # notional 10 / price 1 → planned qty 10; size 9.9 is thin.
        d = _decide(_tick(okx_ask_size=9.9, bybit_bid_size=100.0))
        self.assertEqual(d.action, "flat")
        self.assertEqual(d.reason, "gate_l1_depth")

    def test_fail_closed_missing_price(self) -> None:
        d = _decide(_tick(okx_ask=None, okx_ask_size=100.0, bybit_bid_size=100.0))
        self.assertEqual(d.action, "flat")
        self.assertEqual(d.reason, "gate_l1_depth")

    def test_pass_when_both_venues_deep(self) -> None:
        d = _decide(_tick(okx_ask_size=10.0, bybit_bid_size=10.0))
        self.assertEqual(d.action, "open_long")
        self.assertEqual(d.reason, "signal")

    def test_short_open_uses_bid_ask_sides(self) -> None:
        # open short: sell OKX bid, buy Bybit ask.
        thin_wrong_side = _decide(
            _tick(
                spread_long=0.0,
                spread_short=0.2,
                okx_bid_size=1.0,
                okx_ask_size=100.0,
                bybit_ask_size=1.0,
                bybit_bid_size=100.0,
            )
        )
        self.assertEqual(thin_wrong_side.action, "flat")
        deep = _decide(
            _tick(
                spread_long=0.0,
                spread_short=0.2,
                okx_bid_size=20.0,
                bybit_ask_size=20.0,
            )
        )
        self.assertEqual(deep.action, "open_short")

    def test_gear2_still_emits_without_sizes(self) -> None:
        tick = TickView(
            event_local_ts_ms=1_000_000.0,
            spread_long=0.03,
            spread_short=0.0,
            ma_long=0.03,
            ma_short=0.0,
            okx_latency_ms=30.0,
            bybit_latency_ms=20.0,
            valid=True,
        )
        d = decide_market_tick(
            tick, "SOL", MarketState(), GEAR2_WOULD_SEND_VARIATION, GEAR2_WOULD_SEND_HYPER
        )
        self.assertEqual(d.action, "open_long")


class CanaryLiveSizeAllowTests(unittest.TestCase):
    def test_wal_eden_allowed_for_canary_only(self) -> None:
        self.assertEqual(LIVE_SIZE_COINS, ("SOL", "XRP"))
        self.assertEqual(live_size_coins_for_profile("canary_wal_eden"), ("WAL", "EDEN"))
        self.assertEqual(live_size_coins_for_profile("gear2_would_send"), ("SOL", "XRP"))
        self.assertTrue(live_size_coin_allowed("WAL", "canary_wal_eden"))
        self.assertTrue(live_size_coin_allowed("EDEN", "canary"))
        self.assertFalse(live_size_coin_allowed("SOL", "canary_wal_eden"))
        self.assertTrue(live_size_coin_allowed("SOL", "live_size"))
        self.assertFalse(live_size_coin_allowed("WAL", "gear2_would_send"))

    def test_private_symbols_canary_vs_w6(self) -> None:
        wal = resolve_live_size_futures_symbol(
            "bybit_live", "WALUSDT", contour="canary_wal_eden"
        )
        self.assertEqual(wal.symbol, "WALUSDT")
        eden = resolve_live_size_futures_symbol(
            "okx_live", "EDEN-USDT-SWAP", contour="canary"
        )
        self.assertEqual(eden.symbol, "EDEN-USDT-SWAP")
        with self.assertRaises(SymbolGateError):
            resolve_live_size_futures_symbol(
                "bybit_live", "SOLUSDT", contour="canary_wal_eden"
            )
        sol = resolve_live_size_futures_symbol("bybit_live", "SOLUSDT", contour="live_size")
        self.assertEqual(sol.symbol, "SOLUSDT")
        # W6 / R3 allowlist is still BTC+TRUMP only.
        resolve_allowed_futures_symbol("bybit_live", "BTCUSDT")
        with self.assertRaises(SymbolGateError):
            resolve_allowed_futures_symbol("bybit_live", "WALUSDT")


try:
    import websockets  # noqa: F401

    _HAS_WEBSOCKETS = True
except ImportError:
    _HAS_WEBSOCKETS = False


@unittest.skipUnless(_HAS_WEBSOCKETS, "runtime import needs websockets (pre-existing env gap)")
class CanaryRuntimeProfileTests(unittest.TestCase):
    def test_runtime_defaults(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        env = {
            "BBOT_MODE": "policy",
            "BBOT_PROFILE": "canary_wal_eden",
            "BBOT_DATA_ROOT": str(tmp),
            "BBOT_LOG_PATH": str(tmp / "bbot.log"),
            "BBOT_COINS": "",
            "BBOT_BROKER": "stub",
            "BBOT_NOTIONAL_USDT": "",
        }
        # Clear notional so the canary default (10) is used.
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("BBOT_NOTIONAL_USDT", None)
            from app.bot.runtime import BotRuntime

            rt = BotRuntime()
        self.assertEqual(rt.coins, ["WAL", "EDEN"])
        self.assertEqual(rt.notional, 10.0)
        self.assertEqual(rt.variation["thresh_open_long"], 0.1)
        self.assertEqual(rt.hyper["avg_window_sec"], 10.0)
        self.assertTrue(rt.hyper["Check_l1_depth"])
        self.assertEqual(rt.hyper["position_size"], 10.0)
        self.assertEqual(rt.market_state.k_live, 1)
        self.assertTrue(rt._uses_market_manager())


if __name__ == "__main__":
    unittest.main()
