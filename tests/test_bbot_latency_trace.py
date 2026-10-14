"""Emit-cycle await diagnostics, without tick/decision provenance claims."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.bot.runtime import BotRuntime
from app.bot.theta_screener import ThetaSnapshot
from app.bot.tw_p50_watcher import TwP50Snapshot


STAGES = (
    "tw_compute_await", "tw_journal_await", "theta_compute_await",
    "theta_journal_await", "manager_invocation_await",
)
DELAYS = dict(zip(STAGES, (11, 23, 37, 41, 53)))


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        return self.now


class LatencyTraceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, *, enabled=True, error_stage=None, cancel_stage=None, delays=None):
        from app.bot.latency_trace import EmitLatencyTrace

        clock = FakeClock()
        runtime = object.__new__(BotRuntime)
        runtime._latency_trace = EmitLatencyTrace(clock=clock) if enabled else None
        runtime.stop_event = asyncio.Event()
        runtime.log = Mock()
        runtime._tw_p50_flush_warned = False
        runtime._theta_flush_warned = False
        runtime.theta_enabled = True
        runtime.theta_trade_enabled = True
        runtime.theta_trade = object()
        events = []
        tw = [TwP50Snapshot("BTC", "long", 60_000, 0.25, 1, 1.0, 60_001)]
        theta = [ThetaSnapshot("BTC", "long", 60_000, 0.25, 0.125, 0.125, 60_002)]

        def endpoint(stage, payload=None, result=None):
            events.append((stage, payload))
            clock.now += (delays or DELAYS)[stage]
            if stage == error_stage:
                raise RuntimeError("injected endpoint failure")
            if stage == cancel_stage:
                raise asyncio.CancelledError()
            return result

        runtime.tw_p50_observer = SimpleNamespace(
            compute_snapshots=lambda: endpoint(STAGES[0], result=tw)
        )
        runtime.tw_p50_journal = SimpleNamespace(
            append_rows=lambda rows: endpoint(STAGES[1], rows)
        )
        runtime.theta_screener = SimpleNamespace(
            compute_from_tw_snapshots=lambda snapshots: endpoint(STAGES[2], snapshots, theta)
        )
        runtime.theta_journal = SimpleNamespace(
            append_rows=lambda rows: endpoint(STAGES[3], rows)
        )

        async def manager(snapshots):
            return endpoint(STAGES[4], snapshots, "unchanged decision")

        runtime._run_theta_trade = manager
        return runtime, clock, events

    async def run_one_cycle(self, runtime, *, real_threads=False):
        cadence_calls = 0

        async def cadence(waiter, *, timeout):
            nonlocal cadence_calls
            waiter.close()
            cadence_calls += 1
            if cadence_calls == 1:
                raise asyncio.TimeoutError()
            return True

        async def inline_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch("app.bot.runtime.asyncio.wait_for", side_effect=cadence):
            if real_threads:
                await runtime._tw_p50_emit_loop()
            else:
                with patch("app.bot.runtime.asyncio.to_thread", side_effect=inline_thread):
                    await runtime._tw_p50_emit_loop()

    async def test_runtime_exposes_default_off_trace_snapshot(self):
        runtime = object.__new__(BotRuntime)
        runtime._latency_trace = None
        accessor = getattr(runtime, "latency_trace_snapshot", None)
        self.assertTrue(callable(accessor), "P01 needs an immutable snapshot accessor")
        self.assertIsNone(accessor())

    async def test_stage_order_durations_and_terminal_completion(self):
        runtime, clock, events = self.runtime()
        await self.run_one_cycle(runtime)
        snapshot = runtime.latency_trace_snapshot()
        self.assertEqual(snapshot.dropped_cycles, 0)
        self.assertEqual(len(snapshot.cycles), 1)
        cycle = snapshot.cycles[0]
        self.assertEqual(cycle.cycle_id, 1)
        self.assertEqual(cycle.outcome, "completed")
        self.assertEqual([span.stage for span in cycle.spans], list(STAGES))
        self.assertEqual([name for name, _ in events], list(STAGES))
        self.assertEqual(
            [span.ended_ns - span.started_ns for span in cycle.spans], list(DELAYS.values())
        )
        self.assertEqual(cycle.ended_ns - cycle.started_ns, sum(DELAYS.values()))
        self.assertEqual(clock.calls, 12)
        self.assertEqual(cycle.started_ns, cycle.spans[0].started_ns)
        self.assertEqual(cycle.ended_ns, cycle.spans[-1].ended_ns)
        for earlier, later in zip(cycle.spans, cycle.spans[1:]):
            self.assertEqual(earlier.ended_ns, later.started_ns)
        with self.assertRaises(FrozenInstanceError):
            cycle.outcome = "error"
        with self.assertRaises(FrozenInstanceError):
            cycle.spans[0].ended_ns = 0
        self.assertIsInstance(snapshot.cycles, tuple)
        self.assertIsInstance(cycle.spans, tuple)

    async def test_journal_delay_is_isolated_to_its_await(self):
        measured = []
        for extra in (0, 10_000):
            delays = dict(DELAYS)
            delays["tw_journal_await"] += extra
            runtime, _, _ = self.runtime(delays=delays)
            await self.run_one_cycle(runtime)
            cycle = runtime.latency_trace_snapshot().cycles[0]
            measured.append([s.ended_ns - s.started_ns for s in cycle.spans])
        self.assertEqual(measured[1], [11, 10_023, 37, 41, 53])
        self.assertEqual(measured[0], list(DELAYS.values()))

    async def test_disabled_has_no_trace_allocations_or_clock_calls_and_same_outputs(self):
        enabled, _, expected = self.runtime()
        await self.run_one_cycle(enabled)
        disabled, clock, actual = self.runtime(enabled=False)
        with patch("app.bot.latency_trace.EmitLatencyCycle", side_effect=AssertionError("allocated")), \
             patch("app.bot.latency_trace.EmitAwaitSpan", side_effect=AssertionError("allocated")), \
             patch("app.bot.latency_trace.time.monotonic_ns", side_effect=AssertionError("clock")):
            await self.run_one_cycle(disabled)
        self.assertEqual(clock.calls, 0)
        self.assertIsNone(disabled.latency_trace_snapshot())
        self.assertEqual(actual, expected)  # Includes both JSON rows and manager inputs.

    async def test_compute_errors_have_terminal_error_and_preserve_short_circuit(self):
        for stage, attempted in ((STAGES[0], 1), (STAGES[2], 3)):
            with self.subTest(stage=stage):
                runtime, _, events = self.runtime(error_stage=stage)
                await self.run_one_cycle(runtime)
                cycle = runtime.latency_trace_snapshot().cycles[0]
                self.assertEqual(cycle.outcome, "error")
                self.assertEqual(cycle.spans[-1].outcome, "error")
                self.assertEqual([name for name, _ in events], list(STAGES[:attempted]))

    async def test_journal_helper_return_is_not_durable_success(self):
        for stage in (STAGES[1], STAGES[3]):
            with self.subTest(stage=stage):
                runtime, _, events = self.runtime(error_stage=stage)
                await self.run_one_cycle(runtime)
                cycle = runtime.latency_trace_snapshot().cycles[0]
                self.assertEqual(cycle.outcome, "completed")
                self.assertEqual([name for name, _ in events], list(STAGES))
                self.assertTrue(runtime.log.warning.called)
                self.assertEqual(cycle.spans[STAGES.index(stage)].outcome, "completed")

    async def test_propagated_manager_error_is_observed_and_still_propagates(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                runtime, _, _ = self.runtime(enabled=enabled, error_stage=STAGES[4])
                with self.assertRaisesRegex(RuntimeError, "injected endpoint failure"):
                    await self.run_one_cycle(runtime)
                if enabled:
                    cycle = runtime.latency_trace_snapshot().cycles[0]
                    self.assertEqual(cycle.outcome, "error")
                    self.assertEqual(cycle.spans[-1].outcome, "error")

    async def test_cancellation_at_each_await_remains_cancellation(self):
        for index, stage in enumerate(STAGES):
            with self.subTest(stage=stage):
                runtime, _, _ = self.runtime(cancel_stage=stage)
                with self.assertRaises(asyncio.CancelledError):
                    await self.run_one_cycle(runtime)
                cycle = runtime.latency_trace_snapshot().cycles[0]
                self.assertEqual(cycle.outcome, "cancelled")
                self.assertEqual(cycle.spans[-1].outcome, "cancelled")
                self.assertEqual([s.stage for s in cycle.spans], list(STAGES[:index + 1]))

    async def test_cancelled_to_thread_await_does_not_claim_worker_stopped(self):
        runtime, _, _ = self.runtime()
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def worker():
            started.set()
            release.wait(timeout=5)
            finished.set()
            return []

        runtime.tw_p50_observer.compute_snapshots = worker
        task = asyncio.create_task(self.run_one_cycle(runtime, real_threads=True))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            snapshot = runtime.latency_trace_snapshot()
            self.assertEqual(snapshot.cycles[0].spans[0].outcome, "cancelled")
            self.assertFalse(finished.is_set())
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(await asyncio.to_thread(finished.wait, 1))
        self.assertEqual(runtime.latency_trace_snapshot(), snapshot)

    async def test_inactive_sides_and_absent_theta_do_not_invent_stages(self):
        runtime, _, events = self.runtime()
        runtime.theta_enabled = False
        runtime.tw_p50_observer.compute_snapshots = lambda: [
            TwP50Snapshot("BTC", "long", 60_000, None, 0, 0.0, 60_001)
        ]
        await self.run_one_cycle(runtime)
        cycle = runtime.latency_trace_snapshot().cycles[0]
        self.assertEqual([span.stage for span in cycle.spans], [STAGES[0]])
        self.assertEqual(events, [])

    async def test_event_driven_mode_coalesces_policy_work_without_metric_journaling(self):
        from app.bot.tw_p50_trigger import CoalescedTrigger

        runtime, _, _ = self.runtime()
        runtime._tw_p50_trigger = CoalescedTrigger()
        runtime._tw_p50_min_interval_sec = 0.05
        runtime.tw_p50_observer.compute_snapshots = lambda: [
            TwP50Snapshot("BTC", "long", 60_000, 0.25, 1, 1.0, 60_001)
        ]
        journal = Mock()
        runtime.tw_p50_journal = SimpleNamespace(append_rows=journal)
        observed_starts = []

        async def emit_theta(snapshots, *, _latency_cycle, journal_metrics):
            observed_starts.append(runtime._tw_p50_current_event_started_ns)
            self.assertFalse(journal_metrics)
            self.assertEqual(len(snapshots), 1)
            runtime.stop_event.set()

        runtime._emit_theta_from_tw = emit_theta
        runtime._tw_p50_trigger.signal(123_456)
        with patch(
            "app.bot.runtime.asyncio.to_thread",
            side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
        ):
            await runtime._tw_p50_emit_loop()

        self.assertEqual(observed_starts, [123_456])
        journal.assert_not_called()
        self.assertIsNone(runtime._tw_p50_current_event_started_ns)

    async def test_timeout_fallback_consumes_event_arriving_during_rate_limit_wait(self):
        from app.bot.tw_p50_trigger import CoalescedTrigger

        runtime, _, _ = self.runtime()
        runtime._tw_p50_trigger = CoalescedTrigger()
        runtime._tw_p50_min_interval_sec = 0.05
        runtime.tw_p50_observer.compute_snapshots = lambda: [
            TwP50Snapshot("BTC", "long", 60_000, 0.25, 1, 1.0, 60_001)
        ]
        observed_starts = []

        async def wait_once(_trigger, _stop, *, timeout):
            return "timeout"

        async def rate_limit(stop_event, _next_allowed_ns):
            runtime._tw_p50_trigger.signal(654_321)
            return False

        async def emit_theta(_snapshots, *, _latency_cycle, journal_metrics):
            observed_starts.append(runtime._tw_p50_current_event_started_ns)
            runtime.stop_event.set()

        runtime._emit_theta_from_tw = emit_theta
        calls = 0

        async def wait_then_stop(trigger, stop_event, *, timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                return await wait_once(trigger, stop_event, timeout=timeout)
            return "stopped"

        with patch("app.bot.runtime.wait_for_trigger", side_effect=wait_then_stop), \
             patch("app.bot.runtime.wait_until_allowed", side_effect=rate_limit), \
             patch("app.bot.runtime.asyncio.to_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)):
            await runtime._tw_p50_emit_loop()

        self.assertEqual(observed_starts, [654_321])
        self.assertFalse(runtime._tw_p50_trigger.event.is_set())


class LatencyTraceStorageTests(unittest.TestCase):
    def test_bounded_immutable_snapshot_and_drop_counter(self):
        from app.bot.latency_trace import EmitLatencyTrace

        clock = FakeClock()
        trace = EmitLatencyTrace(capacity=2, clock=clock)
        for _ in range(5):
            with trace.cycle() as cycle:
                with cycle.span(STAGES[0]):
                    clock.now += 10
        snapshot = trace.snapshot()
        self.assertEqual([c.cycle_id for c in snapshot.cycles], [4, 5])
        self.assertEqual(snapshot.dropped_cycles, 3)
        with trace.cycle():
            clock.now += 1
        self.assertEqual(snapshot.dropped_cycles, 3)
        self.assertEqual(trace.snapshot().dropped_cycles, 4)
        with self.assertRaises(FrozenInstanceError):
            snapshot.dropped_cycles = 0
        with self.assertRaises(ValueError):
            EmitLatencyTrace(capacity=0)

    def test_raw_to_policy_latency_samples_are_bounded_and_do_not_retain_policy_payload(self):
        from app.bot.latency_trace import EmitLatencyTrace

        trace = EmitLatencyTrace(capacity=2, clock=FakeClock())
        trace.record_raw_to_policy(100, 150)
        trace.record_raw_to_policy(200, 275)
        trace.record_raw_to_policy(300, 420)
        snapshot = trace.snapshot()
        self.assertEqual(snapshot.dropped_raw_to_policy, 1)
        self.assertEqual(
            [(sample.started_ns, sample.ended_ns, sample.elapsed_ns)
             for sample in snapshot.raw_to_policy],
            [(200, 275, 75), (300, 420, 120)],
        )
        self.assertFalse(hasattr(snapshot.raw_to_policy[0], "decision"))
        with self.assertRaises(FrozenInstanceError):
            snapshot.raw_to_policy[0].elapsed_ns = 0

    def test_heartbeat_fields_expose_only_bounded_latency_percentiles(self):
        from app.bot.latency_trace import EmitLatencyTrace

        runtime = object.__new__(BotRuntime)
        runtime._latency_trace = EmitLatencyTrace(capacity=3, clock=FakeClock())
        for duration in (10_000_000, 20_000_000, 40_000_000, 90_000_000):
            runtime._latency_trace.record_raw_to_policy(1, 1 + duration)

        fields = runtime._latency_trace_log_fields()
        self.assertIn("raw_policy_n=3", fields)
        self.assertIn("raw_policy_p50_ms=40.000", fields)
        self.assertIn("raw_policy_p95_ms=90.000", fields)
        self.assertIn("raw_policy_dropped=1", fields)

    def test_interleaved_cycles_do_not_share_current_record(self):
        from app.bot.latency_trace import EmitLatencyTrace

        trace = EmitLatencyTrace(clock=FakeClock())
        with trace.cycle() as first:
            with trace.cycle() as second:
                with first.span(STAGES[0]):
                    pass
                with second.span(STAGES[2]):
                    pass
        second_record, first_record = trace.snapshot().cycles
        self.assertEqual(first_record.cycle_id, 1)
        self.assertEqual(second_record.cycle_id, 2)
        self.assertEqual([s.stage for s in first_record.spans], [STAGES[0]])
        self.assertEqual([s.stage for s in second_record.spans], [STAGES[2]])

    def test_flag_default_off_and_runtime_initialization(self):
        from app.bot.latency_trace import latency_trace_enabled

        self.assertFalse(latency_trace_enabled({}))
        for value in ("", "0", "false", "off", "unknown"):
            self.assertFalse(latency_trace_enabled({"BBOT_LATENCY_TRACE": value}))
        for value in ("1", "true", "yes", "on"):
            self.assertTrue(latency_trace_enabled({"BBOT_LATENCY_TRACE": value}))
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "BBOT_PROFILE": "gear2_would_send", "BBOT_MODE": "policy",
                "BBOT_BROKER": "stub", "BBOT_COINS": "BTC",
                "BBOT_DATA_ROOT": tmp, "BBOT_LOG_PATH": str(Path(tmp) / "bbot.log"),
            }
            for value, enabled in (("0", False), ("1", True)):
                with patch.dict(os.environ, {**env, "BBOT_LATENCY_TRACE": value}), \
                     patch("app.bot.latency_trace.time.monotonic_ns") as clock:
                    runtime = BotRuntime()
                    try:
                        self.assertEqual(runtime.latency_trace_snapshot() is not None, enabled)
                        clock.assert_not_called()
                    finally:
                        for handler in runtime.log.handlers:
                            handler.close()
                        runtime.log.handlers.clear()


if __name__ == "__main__":
    unittest.main()
