"""Canary public L1 ring: retention, wrap vs signal snapshot, canary gate."""

from __future__ import annotations

import unittest

from app.bot.private.l1_tick_ring import (
    L1Tick,
    L1TickRing,
    capture_signal_book,
    clear_process_rings,
    fill_spread_pct,
    get_signal_book,
    record_public_l1,
    should_record_canary_l1,
    spread_long_pct,
    spread_short_pct,
    store_signal_book,
)


def _tick(
    *,
    wall_ms: int,
    venue: str,
    bid: float,
    ask: float,
    mono_ns: int = 0,
) -> L1Tick:
    return L1Tick(
        wall_ms=wall_ms,
        mono_ns=mono_ns or wall_ms * 1_000_000,
        venue=venue,
        bid=bid,
        ask=ask,
        bid_size=10.0,
        ask_size=11.0,
        event_local_ts_ms=wall_ms,
    )


class SpreadFormulaTests(unittest.TestCase):
    def test_policy_long_and_short(self) -> None:
        self.assertAlmostEqual(spread_long_pct(100.0, 99.5), 0.5)
        self.assertAlmostEqual(spread_short_pct(100.0, 99.2), 0.8)

    def test_zero_bid_is_none(self) -> None:
        self.assertIsNone(spread_long_pct(0.0, 1.0))
        self.assertIsNone(spread_short_pct(0.0, 1.0))

    def test_fill_uses_same_formula(self) -> None:
        self.assertAlmostEqual(
            fill_spread_pct(spread_kind="long", bybit_exec=99.9, okx_exec=99.6),
            (99.9 - 99.6) / 99.9 * 100.0,
        )
        self.assertAlmostEqual(
            fill_spread_pct(spread_kind="short", bybit_exec=100.2, okx_exec=100.0),
            (100.0 - 100.2) / 100.0 * 100.0,
        )


class RingRetentionTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_process_rings()

    def test_age_eviction(self) -> None:
        ring = L1TickRing(max_age_ms=1_000, max_ticks=100)
        ring.append(_tick(wall_ms=1_000, venue="bybit", bid=1.0, ask=1.01))
        ring.append(_tick(wall_ms=1_400, venue="okx", bid=0.99, ask=1.00))
        ring.append(_tick(wall_ms=2_200, venue="bybit", bid=1.02, ask=1.03))
        kept = ring.snapshot()
        self.assertEqual(len(kept), 2)
        self.assertGreaterEqual(kept[0].wall_ms, 1_200)

    def test_count_eviction(self) -> None:
        ring = L1TickRing(max_age_ms=60_000, max_ticks=3)
        for i in range(5):
            ring.append(
                _tick(wall_ms=1_000 + i, venue="bybit" if i % 2 == 0 else "okx", bid=1.0 + i, ask=1.1 + i)
            )
        self.assertEqual(len(ring), 3)
        walls = [t.wall_ms for t in ring.snapshot()]
        self.assertEqual(walls, [1_002, 1_003, 1_004])

    def test_window_freeze(self) -> None:
        ring = L1TickRing(max_age_ms=60_000, max_ticks=50)
        for i in range(10):
            ring.append(_tick(wall_ms=10_000 + i * 100, venue="bybit", bid=2.0, ask=2.1))
        frozen = ring.snapshot(start_ms=10_300, end_ms=10_700)
        self.assertEqual([t.wall_ms for t in frozen], [10_300, 10_400, 10_500, 10_600, 10_700])

    def test_spread_from_last_other_venue(self) -> None:
        ring = L1TickRing(max_age_ms=60_000, max_ticks=10)
        ring.append(_tick(wall_ms=1, venue="okx", bid=99.0, ask=99.5))
        ring.append(_tick(wall_ms=2, venue="bybit", bid=100.0, ask=100.4))
        last = ring.snapshot()[-1]
        self.assertAlmostEqual(last.spread_long_pct, 0.5)
        self.assertAlmostEqual(last.spread_short_pct, (99.0 - 100.4) / 99.0 * 100.0)

    def test_signal_snapshot_survives_wrap(self) -> None:
        ring = L1TickRing(max_age_ms=60_000, max_ticks=2)
        snap = capture_signal_book(
            {"bid_price": 9.0, "ask_price": 9.1, "bid_size": 1, "ask_size": 1},
            {"bid_price": 10.0, "ask_price": 10.2, "bid_size": 2, "ask_size": 2},
            event_local_ts_ms=50,
        )
        store_signal_book("intent-wrap", snap)
        ring.append(_tick(wall_ms=1, venue="bybit", bid=10.0, ask=10.2))
        ring.append(_tick(wall_ms=2, venue="okx", bid=9.0, ask=9.1))
        ring.append(_tick(wall_ms=3, venue="bybit", bid=11.0, ask=11.2))
        self.assertEqual(len(ring), 2)
        kept = get_signal_book("intent-wrap")
        assert kept is not None
        self.assertAlmostEqual(kept.bybit_bid, 10.0)
        self.assertAlmostEqual(kept.okx_ask, 9.1)
        self.assertAlmostEqual(kept.signal_spread_pct("long"), 9.0)

    def test_record_public_l1_canary_only(self) -> None:
        clear_process_rings()
        book = {"bid_price": 1.0, "ask_price": 1.1, "bid_size": 5, "ask_size": 6}
        self.assertIsNone(
            record_public_l1(
                coin="WAL",
                venue="bybit",
                book=book,
                profile="gear2_would_send",
                env={"BBOT_PROFILE": "gear2_would_send"},
            )
        )
        tick = record_public_l1(
            coin="WAL",
            venue="bybit",
            book=book,
            profile="canary_wal_eden",
            env={"BBOT_PROFILE": "canary_wal_eden"},
            wall_ms=123,
            event_local_ts_ms=120,
        )
        assert tick is not None
        self.assertEqual(tick.venue, "bybit")
        self.assertEqual(tick.event_local_ts_ms, 120)
        self.assertTrue(should_record_canary_l1("canary", "EDEN"))
        self.assertFalse(should_record_canary_l1("gear2_would_send", "WAL"))


if __name__ == "__main__":
    unittest.main()
