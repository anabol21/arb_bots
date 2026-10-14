"""Coalesced TW p50 event wakeup behavior."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from app.bot.tw_p50_trigger import (
    CoalescedTrigger,
    assert_shadow_canary_context,
    tw_p50_event_driven_enabled,
    tw_p50_min_interval_sec,
    wait_for_trigger,
    wait_until_allowed,
)


class CoalescedTriggerTests(unittest.IsolatedAsyncioTestCase):
    def test_event_mode_is_opt_in_and_interval_is_bounded(self):
        self.assertFalse(tw_p50_event_driven_enabled({}))
        self.assertTrue(tw_p50_event_driven_enabled({"BBOT_TW_P50_EVENT_DRIVEN": "yes"}))
        self.assertFalse(tw_p50_event_driven_enabled({"BBOT_TW_P50_EVENT_DRIVEN": "off"}))
        self.assertEqual(tw_p50_min_interval_sec({}), 0.05)
        self.assertEqual(tw_p50_min_interval_sec({"BBOT_TW_P50_MIN_INTERVAL_MS": "80"}), 0.08)
        self.assertEqual(tw_p50_min_interval_sec({"BBOT_TW_P50_MIN_INTERVAL_MS": "bad"}), 0.05)
        self.assertEqual(tw_p50_min_interval_sec({"BBOT_TW_P50_MIN_INTERVAL_MS": "0"}), 0.05)

    def test_event_mode_refuses_live_or_non_canary_contours(self):
        assert_shadow_canary_context(
            profile="gear22_would_send", mode="policy", broker_is_stub=True,
            env={"LIVE_ORDERS": "0", "BBOT_THETA_LIVE_SEND": "0"},
        )
        cases = (
            {"profile": "gear22_live_canary", "mode": "policy", "broker_is_stub": True,
             "env": {"LIVE_ORDERS": "0", "BBOT_THETA_LIVE_SEND": "0"}},
            {"profile": "gear22_would_send", "mode": "probe", "broker_is_stub": True,
             "env": {}},
            {"profile": "gear22_would_send", "mode": "policy", "broker_is_stub": False,
             "env": {}},
            {"profile": "gear22_would_send", "mode": "policy", "broker_is_stub": True,
             "env": {"LIVE_ORDERS": "1"}},
            {"profile": "gear22_would_send", "mode": "policy", "broker_is_stub": True,
             "env": {"BBOT_THETA_LIVE_SEND": "true"}},
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                assert_shadow_canary_context(**case)

    async def test_multiple_events_before_consume_keep_first_arrival_and_one_wakeup(self):
        trigger = CoalescedTrigger()
        trigger.signal(100)
        trigger.signal(200)
        trigger.signal(300)

        self.assertTrue(trigger.event.is_set())
        self.assertEqual(trigger.consume(), 100)
        self.assertFalse(trigger.event.is_set())
        self.assertIsNone(trigger.consume())

    async def test_event_during_compute_is_kept_for_next_cycle(self):
        trigger = CoalescedTrigger()
        trigger.signal(100)
        self.assertEqual(trigger.consume(), 100)

        # The worker is computing now. A new input must queue exactly one next cycle.
        trigger.signal(200)
        trigger.signal(300)
        self.assertTrue(trigger.event.is_set())
        self.assertEqual(trigger.consume(), 200)

    async def test_wait_exits_for_event_stop_or_timeout(self):
        trigger = CoalescedTrigger()
        stop = asyncio.Event()
        task = asyncio.create_task(wait_for_trigger(trigger, stop, timeout=1))
        await asyncio.sleep(0)
        trigger.signal(100)
        self.assertEqual(await task, "event")

        task = asyncio.create_task(wait_for_trigger(trigger, stop, timeout=1))
        await asyncio.sleep(0)
        stop.set()
        self.assertEqual(await task, "stopped")

        trigger.consume()
        stop.clear()
        self.assertEqual(await wait_for_trigger(trigger, stop, timeout=0), "timeout")

    async def test_default_periodic_wait_has_same_timeout_and_stop_contract(self):
        stop = asyncio.Event()
        self.assertEqual(await wait_for_trigger(None, stop, timeout=0), "timeout")
        stop.set()
        self.assertEqual(await wait_for_trigger(None, stop, timeout=1), "stopped")

    async def test_timeout_fallback_also_waits_for_the_minimum_interval(self):
        stop = asyncio.Event()
        captured = []

        async def timeout_waiter(awaitable, *, timeout):
            awaitable.close()
            captured.append(timeout)
            raise asyncio.TimeoutError()

        with patch(
            "app.bot.tw_p50_trigger.asyncio.wait_for", side_effect=timeout_waiter
        ):
            stopped = await wait_until_allowed(
                stop, 100_000_000, clock_ns=lambda: 80_000_000
            )
        self.assertFalse(stopped)
        self.assertEqual(captured, [0.02])


if __name__ == "__main__":
    unittest.main()
