"""Planned WS reconnect policy: classify, backoff, wave, budget."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from app.utils.ws_reconnect import (
    BOOK_CONNECT_PRIORITY,
    CANDLE_CONNECT_PRIORITY,
    ExchangeConnectScheduler,
    ReconnectController,
    classify_close,
    invalidate_book_quote,
    planned_backoff_sec,
    reconnect_v2_enabled,
    subscribe_batch_size,
)


class DummyClosed(Exception):
    def __init__(self, code=None, reason="", message=""):
        self.code = code
        self.reason = reason
        super().__init__(message or f"{code} {reason}".strip())


class ClassifyCloseTests(unittest.TestCase):
    def test_clean_codes(self) -> None:
        self.assertEqual(classify_close(DummyClosed(1000, "bye")), "clean")
        self.assertEqual(classify_close(DummyClosed(1001, "going away")), "clean")

    def test_connection_closed_ok_name(self) -> None:
        class ConnectionClosedOK(Exception):
            code = 1000
            reason = "bye"

        self.assertEqual(classify_close(ConnectionClosedOK()), "clean")

    def test_abrupt_1006_no_close_frame(self) -> None:
        exc = DummyClosed(
            1006,
            message="no close frame received or sent",
        )
        self.assertEqual(classify_close(exc), "abrupt")

    def test_keepalive_1011_even_if_code_1006(self) -> None:
        exc = DummyClosed(
            1006,
            message="sent 1011 (internal error) keepalive ping timeout",
        )
        self.assertEqual(classify_close(exc), "keepalive")

    def test_protocol_error(self) -> None:
        exc = DummyClosed(message="protocol error while receiving")
        self.assertEqual(classify_close(exc), "protocol_error")


class BackoffTests(unittest.TestCase):
    def test_exponential_then_cap(self) -> None:
        rng = mock.Mock()
        rng.random.return_value = 0.0
        self.assertEqual(planned_backoff_sec(1, rng=rng), 1.0)
        self.assertEqual(planned_backoff_sec(2, rng=rng), 2.0)
        self.assertEqual(planned_backoff_sec(3, rng=rng), 4.0)
        self.assertEqual(planned_backoff_sec(7, rng=rng), 60.0)
        self.assertEqual(planned_backoff_sec(20, rng=rng), 60.0)

    def test_jitter_is_zero_to_twenty_percent(self) -> None:
        rng = mock.Mock()
        rng.random.return_value = 1.0
        self.assertEqual(planned_backoff_sec(1, rng=rng), 1.2)


class FlagTests(unittest.TestCase):
    def test_default_off(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SPREAD_WS_RECONNECT_V2", None)
            self.assertFalse(reconnect_v2_enabled())

    def test_on(self) -> None:
        with mock.patch.dict(os.environ, {"SPREAD_WS_RECONNECT_V2": "1"}):
            self.assertTrue(reconnect_v2_enabled())


class QuoteInvalidateTests(unittest.TestCase):
    def test_clears_one_leg_only(self) -> None:
        quotes = {
            "BTC": {
                "okx": {"bid_price": 1.0, "ask_price": 2.0, "ts_exchange": 9},
                "bybit": {"bid_price": 3.0, "ask_price": 4.0, "ts_exchange": 8},
            }
        }
        invalidate_book_quote(quotes, "BTC", "okx")
        self.assertIsNone(quotes["BTC"]["okx"]["bid_price"])
        self.assertIsNone(quotes["BTC"]["okx"]["ts_exchange"])
        self.assertEqual(quotes["BTC"]["bybit"]["bid_price"], 3.0)


class ControllerTests(unittest.TestCase):
    def test_wave_storm_and_serialize_wait(self) -> None:
        now = [1000.0]

        def mono() -> float:
            return now[0]

        ctrl = ReconnectController(
            wave_storm_threshold=2,
            connect_interval_sec=0.5,
            monotonic=mono,
        )
        self.assertFalse(ctrl.in_storm("okx"))
        ctrl.record_wave("okx")
        ctrl.record_wave("okx")
        self.assertFalse(ctrl.in_storm("okx"))
        ctrl.record_wave("okx")
        self.assertTrue(ctrl.in_storm("okx"))
        ctrl.mark_connect_slot("okx")
        now[0] += 0.2
        self.assertAlmostEqual(ctrl.connect_slot_wait_sec("okx"), 0.3, places=6)
        now[0] += 0.4
        self.assertEqual(ctrl.connect_slot_wait_sec("okx"), 0.0)

    def test_budget_two_per_hour_then_wait(self) -> None:
        now = [0.0]

        def mono() -> float:
            return now[0]

        ctrl = ReconnectController(budget_max=2, budget_window_sec=3600, monotonic=mono)
        key = "okx:books5:BTC"
        self.assertFalse(ctrl.budget_exceeded(key))
        ctrl.record_planned(key)
        ctrl.record_planned(key)
        self.assertTrue(ctrl.budget_exceeded(key))
        self.assertEqual(ctrl.time_until_budget_slot(key), 3600.0)
        now[0] = 3600.1
        self.assertFalse(ctrl.budget_exceeded(key))

    def test_session_planned_path_and_unrecovered(self) -> None:
        ctrl = ReconnectController(unrecovered_after_attempts=2)
        session = ctrl.session("bybit", "orderbook.1", "ETH")
        first = session.on_disconnect(DummyClosed(1006, message="no close frame received or sent"))
        self.assertEqual(first["event"], "ws_disconnect")
        self.assertEqual(first["reason_class"], "abrupt")
        self.assertTrue(first["planned"])
        self.assertFalse(first["unrecovered"])
        plan = session.plan_reconnect(first)
        self.assertEqual(plan["event"], "ws_reconnect_planned")
        second = session.on_disconnect(
            DummyClosed(1006, message="sent 1011 (internal error) keepalive ping timeout")
        )
        self.assertEqual(second["reason_class"], "keepalive")
        self.assertTrue(second["unrecovered"])
        unrecovered = session.unrecovered_event(second)
        self.assertIsNotNone(unrecovered)
        self.assertEqual(unrecovered["event"], "ws_unrecovered")
        ok = session.mark_subscribe_ok()
        self.assertEqual(ok["event"], "ws_subscribe_ok")
        self.assertEqual(session.attempt, 0)
        self.assertEqual(ctrl.heartbeat_fields()["ws_unrecovered_active"], 0)

    def test_protocol_error_is_unplanned(self) -> None:
        ctrl = ReconnectController()
        session = ctrl.session("okx", "books5", "BTC")
        disc = session.on_disconnect(DummyClosed(message="protocol error while receiving"))
        self.assertEqual(disc["reason_class"], "protocol_error")
        self.assertFalse(disc["planned"])
        plan = session.plan_reconnect(disc)
        self.assertEqual(plan["event"], "ws_reconnect_unplanned")
        self.assertEqual(ctrl.counters["planned_reconnects_total"], 0)
        self.assertEqual(ctrl.counters["unplanned_reconnects_total"], 1)
        self.assertEqual(ctrl.counters["protocol_errors_total"], 1)


class SchedulerTests(unittest.TestCase):
    def test_rate_limits_same_exchange(self) -> None:
        now = [100.0]
        sleeps: list[float] = []

        def mono() -> float:
            return now[0]

        async def fake_sleep(sec: float) -> None:
            sleeps.append(sec)
            now[0] += sec

        async def run() -> None:
            sched = ExchangeConnectScheduler(
                connects_per_sec=2.0,
                jitter_frac=0.0,
                monotonic=mono,
                sleeper=fake_sleep,
            )
            await sched.acquire("bybit", priority=BOOK_CONNECT_PRIORITY, coin="AAA")
            await sched.acquire("bybit", priority=BOOK_CONNECT_PRIORITY, coin="BBB")

        import asyncio

        asyncio.run(run())
        self.assertTrue(any(abs(value - 0.5) < 1e-9 for value in sleeps))

    def test_books_before_candles_on_same_exchange(self) -> None:
        order: list[str] = []
        release = None

        async def run() -> None:
            nonlocal release
            import asyncio

            held = asyncio.Event()
            release = asyncio.Event()

            async def gated_sleep(_sec: float) -> None:
                held.set()
                await release.wait()

            sched = ExchangeConnectScheduler(
                connects_per_sec=2.0,
                jitter_frac=0.0,
                sleeper=gated_sleep,
            )
            sched._last["okx"] = sched._monotonic()

            async def named(name: str, priority: int) -> None:
                await sched.acquire("okx", priority=priority, coin="BTC")
                order.append(name)

            t_c = asyncio.create_task(named("candle", CANDLE_CONNECT_PRIORITY))
            t_b = asyncio.create_task(named("book", BOOK_CONNECT_PRIORITY))
            await held.wait()
            release.set()
            await asyncio.gather(t_c, t_b)

        import asyncio

        asyncio.run(run())
        self.assertEqual(order, ["book", "candle"])

    def test_v2_default_batch_size_is_scheduler(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SPREAD_SUBSCRIBE_BATCH_SIZE", None)
            self.assertEqual(subscribe_batch_size(v2=True), 0)
            self.assertEqual(subscribe_batch_size(v2=False), 30)


if __name__ == "__main__":
    unittest.main()
