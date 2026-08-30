"""Signal-test profile: low thresholds fire; gear1 0.5 does not on the same tick."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.bot.journal import JournalWriter, build_leg_record, event_date_utc_from_signal_ts_ms
from app.policy.trade_manager import (
    DEFAULT_HYPER,
    DEFAULT_VARIATION,
    SIGNAL_TEST_VARIATION,
    BotState,
    TickView,
    decide,
    variation_for_profile,
)
from validation.check_bbot_signals import check_signals


def _tick(*, spread_long: float, spread_short: float = 0.0) -> TickView:
    return TickView(
        event_local_ts_ms=1_000_000.0,
        spread_long=spread_long,
        spread_short=spread_short,
        ma_long=spread_long,
        ma_short=spread_short,
        okx_latency_ms=30.0,
        bybit_latency_ms=20.0,
        valid=True,
    )


class VariationProfileTests(unittest.TestCase):
    def test_frozen_gear1_untouched(self) -> None:
        self.assertEqual(DEFAULT_VARIATION["thresh_open_long"], 0.5)
        self.assertEqual(variation_for_profile("gear1")["thresh_open_long"], 0.5)
        self.assertEqual(SIGNAL_TEST_VARIATION["thresh_open_long"], 0.05)

    def test_mid_spread_fires_only_on_signal_test(self) -> None:
        tick = _tick(spread_long=0.12)
        gear1 = decide(tick, BotState(), DEFAULT_VARIATION, DEFAULT_HYPER)
        test = decide(tick, BotState(), SIGNAL_TEST_VARIATION, DEFAULT_HYPER)
        self.assertEqual(gear1.action, "flat")
        self.assertEqual(test.action, "open_long")


class JournalContextTests(unittest.TestCase):
    def test_checker_accepts_open_long_with_context(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        writer = JournalWriter(tmp)
        sig = 1_780_000_000_000
        extra = {
            "spread_long": 0.12,
            "spread_short": 0.01,
            "ma_long": 0.12,
            "ma_short": 0.01,
            "okx_latency_ms": 30.0,
            "bybit_latency_ms": 20.0,
            "bbot_profile": "signal_test",
            "thresh_open_long": 0.05,
        }
        common = dict(
            intent_id="t1",
            base_coin="BTC",
            spread_side="open_long",
            signal_ts_ms=sig,
            place_ts_ms=sig,
            ack_ts_ms=sig,
            fill_ts_ms=sig + 100,
            trade_lat_ms=100,
            notional=100.0,
            status="filled",
            tick_valid=True,
            extra=extra,
        )
        okx = build_leg_record(
            exchange="okx",
            leg_side="buy",
            signal_price=50000.0,
            fill_price=50001.0,
            qty=0.002,
            **common,
        )
        bybit = build_leg_record(
            exchange="bybit",
            leg_side="sell",
            signal_price=50010.0,
            fill_price=50009.0,
            qty=0.002,
            **common,
        )
        writer.append_dual_legs(okx, bybit)
        day = event_date_utc_from_signal_ts_ms(sig)
        self.assertTrue((tmp / "journal" / f"event_date={day}" / "legs.jsonl").is_file())
        errors = check_signals(tmp, profile="signal_test")
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
