"""EV2-09A local shadow parity and no-order latency harness tests."""

from __future__ import annotations

import ast
import asyncio
import builtins
import inspect
import socket
import time
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app.bot.execution.contracts import (
    SCHEMA_VERSION as CONTRACT_SCHEMA_VERSION,
    IntentAction,
    SpreadDirection,
    SpreadStatus,
    TradeIntent,
    Venue,
)
from app.bot.execution.shadow import (
    DEFAULT_COUNTED_N,
    DEFAULT_WARMUP_N,
    HEALTH_SCHEMA_VERSION,
    LATENCY_SERIES,
    LatencyHistogram,
    NullTradeSink,
    ProbeSample,
    ShadowError,
    ShadowHealth,
    ShadowHotPath,
    ShadowParityLane,
    canonical_bridge_config,
    classify_dispatch,
    config_is_canonical,
    config_is_shadow_supported,
    config_is_synthetic_canary,
)
from app.bot.execution.state_machine import initial_spread_state
from app.bot.execution.strategy_bridge import (
    BridgeClocks,
    BridgeConfig,
    BridgeIds,
    DivergenceClass,
    Gear22StrategyBridge,
)
from app.bot.execution.transport import (
    SCHEMA_VERSION as TRANSPORT_SCHEMA_VERSION,
    DispatchResult,
    DispatchStatus,
    VenueWriteEvidence,
    WriteOutcome,
)
from app.bot.theta_screener import ThetaSnapshot
from app.bot.theta_trade_manager import GEAR22_HTML_TOP30, POLICY_ID
from research.gear22_backtest.params_frozen import DEFAULT_OBSERVE_PARAMS, PREVIOUS
from research.gear22_backtest.policy import (
    SYNTHETIC_OPEN_ROLL,
    SYNTHETIC_ROLL_POLICY_ID,
    synthetic_roll,
)

RUN_ID = "run_shadow_09a"


class SeqIds:
    def __init__(self) -> None:
        self.value = 0

    def next(self) -> str:
        self.value += 1
        return f"shadow{self.value:026d}"


class ScriptedClock:
    def __init__(self, values: list[int]) -> None:
        self.values = list(values)
        self.calls = 0

    def __call__(self) -> int:
        if not self.values:
            raise AssertionError("scripted clock exhausted")
        self.calls += 1
        return self.values.pop(0)


def _snap(coin: str, side: str, theta: float, *, ts_ms: int = 1_700_000_000_000) -> ThetaSnapshot:
    floor = 0.20
    p50 = floor + theta
    return ThetaSnapshot(
        base_coin=coin,
        side=side,
        ts_ms=ts_ms,
        p50_1m=p50,
        p50_5m=p50,
        floor_tf_select_a25=floor,
        theta_1m=theta,
        theta_5m=theta,
        computed_at_ms=ts_ms + 1,
    )


def _qualify(coin: str = "KAITO") -> list[ThetaSnapshot]:
    return [_snap(coin, "long", 0.60), _snap(coin, "short", 0.01)]


def _books() -> dict[str, dict[str, float]]:
    return {
        "okx": {
            "bid_price": 99.0,
            "ask_price": 100.0,
            "bid_size": 10.0,
            "ask_size": 10.0,
            "local_recv_ts_ms": 1.0,
        },
        "bybit": {
            "bid_price": 100.2,
            "ask_price": 100.5,
            "bid_size": 10.0,
            "ask_size": 10.0,
            "local_recv_ts_ms": 1.0,
        },
    }


def _lane(*, bridge: Gear22StrategyBridge | None = None) -> ShadowParityLane:
    seq = SeqIds()
    return ShadowParityLane(
        run_id=RUN_ID,
        bridge=bridge,
        clocks=BridgeClocks(monotonic_ns=time.monotonic_ns, wall_ns=time.time_ns),
        ids=BridgeIds(new_intent_id=seq.next),
    )


def _sample(value: int, *, warmup: bool = False) -> ProbeSample:
    return ProbeSample(
        intent_id="sample000000000000000000000001",
        warmup=warmup,
        rejected_before_write=False,
        invalid_clock=False,
        missing=False,
        valid=not warmup,
        signal_to_first_write_ns=value,
        bybit_write_latency_ns=value,
        okx_write_latency_ns=value,
        dual_leg_write_ns=value,
        dispatch_status=DispatchStatus.BOTH_COMPLETED.value,
        reason_code=None,
    )


def _ts_for_roll(target: int, *, seed: int = 7) -> int:
    return next(ts for ts in range(1_000_000, 1_100_000) if synthetic_roll(ts, seed) == target)


def _intent(
    *,
    signal_mono_ns: int = 900,
    expiry_mono_ns: int = 1_000_000_900,
) -> TradeIntent:
    return TradeIntent(
        schema_version=CONTRACT_SCHEMA_VERSION,
        intent_id="shadow000000000000000000000001",
        run_id=RUN_ID,
        policy_version=POLICY_ID,
        action=IntentAction.OPEN,
        spread_direction=SpreadDirection.LONG,
        coin="KAITO",
        notional_usdt=Decimal("20"),
        signal_mono_ns=signal_mono_ns,
        signal_wall_ns=1_700_000_000_000_000_000,
        expiry_mono_ns=expiry_mono_ns,
        signal_snapshot_ref="h" + ("ab" * 32),
        canary_stage="gear22_live_canary",
        risk_policy_revision="risk.v1",
    )


def _evidence(
    venue: Venue,
    outcome: WriteOutcome,
    *,
    start: int | None,
    done: int | None,
    reason: str | None = None,
) -> VenueWriteEvidence:
    latency = None if start is None or done is None else done - start
    return VenueWriteEvidence(
        venue=venue,
        outcome=outcome,
        leg_id=f"leg_{venue.value}",
        client_id=f"client_{venue.value}",
        payload_bytes=10 if start is not None else 0,
        asend_start_mono_ns=start,
        asend_done_mono_ns=done,
        write_latency_ns=latency,
        reason_code=reason,
    )


def _dispatch(
    *,
    status: DispatchStatus,
    bybit: VenueWriteEvidence,
    okx: VenueWriteEvidence,
    signal_to_first: int | None,
    dual: int | None,
    reason: str | None = None,
) -> DispatchResult:
    return DispatchResult(
        schema_version=TRANSPORT_SCHEMA_VERSION,
        status=status,
        intent_id="sample000000000000000000000001",
        run_id=RUN_ID,
        dispatch_entry_mono_ns=1_000,
        signal_mono_ns=900,
        signal_to_first_write_ns=signal_to_first,
        dual_leg_write_ns=dual,
        bybit=bybit,
        okx=okx,
        reason_code=reason,
    )


class CanonicalParityTests(unittest.TestCase):
    def test_synthetic_policy_is_explicitly_supported_and_matches(self) -> None:
        params = replace(DEFAULT_OBSERVE_PARAMS, synthetic_roll_seed=7)
        config = BridgeConfig(
            run_id=RUN_ID,
            policy_version=SYNTHETIC_ROLL_POLICY_ID,
            policy_params=params,
        )
        self.assertFalse(config_is_canonical(config))
        self.assertTrue(config_is_synthetic_canary(config))
        self.assertTrue(config_is_shadow_supported(config))
        seq = SeqIds()
        lane = ShadowParityLane(
            run_id=RUN_ID,
            config=config,
            clocks=BridgeClocks(monotonic_ns=time.monotonic_ns, wall_ns=time.time_ns),
            ids=BridgeIds(new_intent_id=seq.next),
        )
        tick = lane.tick(
            _qualify(),
            {"KAITO": _books()},
            initial_spread_state(run_id=RUN_ID),
            decision_ts_s=_ts_for_roll(SYNTHETIC_OPEN_ROLL),
        )
        self.assertEqual(tick.divergence, DivergenceClass.MATCH)
        self.assertEqual(tick.bridge.decision.reason, "synthetic_open_17")
        self.assertIsNotNone(tick.intent)
        assert tick.intent is not None
        self.assertEqual(tick.intent.policy_version, SYNTHETIC_ROLL_POLICY_ID)

    def test_config_pins_all_shadow_inputs(self) -> None:
        config = canonical_bridge_config(RUN_ID)
        self.assertTrue(config_is_canonical(config))
        self.assertEqual(config.coin_order, GEAR22_HTML_TOP30)
        self.assertEqual(config.policy_params, DEFAULT_OBSERVE_PARAMS)
        self.assertEqual(config.notional_usdt, Decimal("20"))
        self.assertEqual(config.policy_version, POLICY_ID)
        self.assertEqual(DEFAULT_WARMUP_N, 100)
        self.assertEqual(DEFAULT_COUNTED_N, 10_000)

        self.assertFalse(
            config_is_canonical(
                BridgeConfig(run_id=RUN_ID, coin_order=tuple(reversed(GEAR22_HTML_TOP30)))
            )
        )
        self.assertFalse(
            config_is_canonical(BridgeConfig(run_id=RUN_ID, policy_params=PREVIOUS))
        )
        self.assertFalse(
            config_is_canonical(
                BridgeConfig(run_id=RUN_ID, notional_usdt=Decimal("19"))
            )
        )

    def test_equivalent_empty_slots_match_and_handoff_clears_latch(self) -> None:
        lane = _lane()
        handed = []
        tick = lane.tick(
            _qualify(),
            {"KAITO": _books()},
            initial_spread_state(run_id=RUN_ID),
            on_unsent_intent=handed.append,
        )
        self.assertEqual(tick.divergence, DivergenceClass.MATCH)
        self.assertEqual(tick.replica.action, tick.bridge.decision.action)
        self.assertEqual(tick.replica.base_coin, tick.bridge.decision.base_coin)
        self.assertEqual(handed, [tick.intent])
        self.assertTrue(tick.inflight_cleared)
        self.assertIsNone(lane.bridge.inflight)

    def test_without_handoff_latch_is_retained_and_blocks_duplicate(self) -> None:
        lane = _lane()
        idle = initial_spread_state(run_id=RUN_ID)
        first = lane.tick(_qualify(), {"KAITO": _books()}, idle)
        second = lane.tick(_qualify(), {"KAITO": _books()}, idle)
        self.assertIsNotNone(first.intent)
        self.assertFalse(first.inflight_cleared)
        self.assertIsNotNone(lane.bridge.inflight)
        self.assertIsNone(second.intent)
        self.assertEqual(second.bridge.divergence, DivergenceClass.PENDING_INFLIGHT)

    def test_noncanonical_bridge_emits_no_probe_intent(self) -> None:
        config = BridgeConfig(
            run_id=RUN_ID,
            coin_order=tuple(reversed(GEAR22_HTML_TOP30)),
        )
        seq = SeqIds()
        bridge = Gear22StrategyBridge(
            run_id=RUN_ID,
            config=config,
            clocks=BridgeClocks(monotonic_ns=time.monotonic_ns, wall_ns=time.time_ns),
            ids=BridgeIds(new_intent_id=seq.next),
        )
        with self.assertRaises(ShadowError) as ctx:
            _lane(bridge=bridge)
        self.assertEqual(ctx.exception.reason_code, "noncanonical_config")


class SourceIsolationTests(unittest.TestCase):
    def test_shadow_source_has_no_forbidden_calls_or_imports(self) -> None:
        import app.bot.execution.shadow as shadow

        source = Path(inspect.getsourcefile(shadow)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls: set[str] = set()
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    calls.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    calls.add(node.func.attr)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
            elif isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
        self.assertNotIn("submit", calls)
        self.assertNotIn("build_trade_intent", calls)
        self.assertNotIn("open", calls)
        self.assertFalse(any(name.startswith("app.bot.private") for name in imports))
        self.assertFalse({"socket", "websockets", "aiohttp"} & imports)
        self.assertNotIn("app.bot.runtime", imports)
        self.assertFalse(any("sentry" in name for name in imports))
        self.assertFalse(any("journal" in name for name in imports))


class HistogramTests(unittest.TestCase):
    def test_warmup_does_not_pollute_invalid_or_missing_counts(self) -> None:
        histogram = LatencyHistogram(required_valid_n=1, required_warmup_n=1)
        warm = ProbeSample(
            intent_id="warm00000000000000000000000001",
            warmup=True,
            rejected_before_write=False,
            invalid_clock=True,
            missing=False,
            valid=False,
            signal_to_first_write_ns=None,
            bybit_write_latency_ns=None,
            okx_write_latency_ns=None,
            dual_leg_write_ns=None,
            dispatch_status=DispatchStatus.BOTH_COMPLETED.value,
            reason_code="clock_regression",
        )
        histogram.add(warm)
        self.assertEqual(histogram.warmup_n, 1)
        self.assertEqual(histogram.invalid_clock_n, 0)
        self.assertEqual(histogram.missing_n, 0)

    def test_threshold_math_and_full_count_gate(self) -> None:
        histogram = LatencyHistogram(required_valid_n=100, required_warmup_n=0)
        for index in range(100):
            if index < 50:
                value = 1_000_000
            elif index < 99:
                value = 3_000_000
            else:
                value = 10_000_000
            histogram.add(_sample(value))
        gate = histogram.gate()
        self.assertTrue(gate.valid_count_complete)
        self.assertTrue(gate.warmup_complete)
        self.assertTrue(gate.p50_pass)
        self.assertTrue(gate.p99_pass)
        self.assertFalse(gate.p999_alert)
        self.assertFalse(gate.report_failed)
        self.assertFalse(gate.target_vps_gate_eligible)

    def test_merge_counts_raw_samples_and_rejects_mixed_requirements(self) -> None:
        left = LatencyHistogram(required_valid_n=4, required_warmup_n=0)
        right = LatencyHistogram(required_valid_n=4, required_warmup_n=0)
        for value in (1, 2):
            left.add(_sample(value))
        for value in (3, 4):
            right.add(_sample(value))
        merged = left.merge(right)
        self.assertEqual(merged.valid_n, 4)
        for name in LATENCY_SERIES:
            self.assertEqual(merged.raw[name], (1, 2, 3, 4))
        self.assertFalse(merged.gate().report_failed)
        with self.assertRaises(ShadowError):
            left.merge(LatencyHistogram(required_valid_n=5, required_warmup_n=0))

    def test_missing_and_invalid_clock_fail_report(self) -> None:
        histogram = LatencyHistogram(required_valid_n=1, required_warmup_n=0)
        histogram.add(
            ProbeSample(
                intent_id="missing00000000000000000000001",
                warmup=False,
                rejected_before_write=False,
                invalid_clock=False,
                missing=True,
                valid=False,
                signal_to_first_write_ns=None,
                bybit_write_latency_ns=None,
                okx_write_latency_ns=None,
                dual_leg_write_ns=None,
                dispatch_status=DispatchStatus.PARTIAL.value,
                reason_code="write_failed",
            )
        )
        self.assertEqual(histogram.missing_n, 1)
        self.assertTrue(histogram.gate().report_failed)
        impossible = ProbeSample(
            intent_id="impossible000000000000000000001",
            warmup=False,
            rejected_before_write=False,
            invalid_clock=False,
            missing=False,
            valid=False,
            signal_to_first_write_ns=None,
            bybit_write_latency_ns=None,
            okx_write_latency_ns=None,
            dual_leg_write_ns=None,
            dispatch_status=DispatchStatus.PARTIAL.value,
            reason_code="write_failed",
        )
        with self.assertRaises(ShadowError):
            histogram.add(impossible)

    def test_partial_dispatch_is_missing_not_silently_dropped(self) -> None:
        result = _dispatch(
            status=DispatchStatus.PARTIAL,
            bybit=_evidence(Venue.BYBIT, WriteOutcome.WRITE_COMPLETED, start=1_100, done=1_200),
            okx=_evidence(Venue.OKX, WriteOutcome.WRITE_FAILED, start=1_150, done=1_250, reason="write_failed"),
            signal_to_first=200,
            dual=100,
            reason="write_failed",
        )
        sample = classify_dispatch(result, warmup=False)
        self.assertTrue(sample.missing)
        self.assertFalse(sample.valid)

    def test_nearest_rank_failure_vector_is_not_hidden(self) -> None:
        histogram = LatencyHistogram(required_valid_n=100, required_warmup_n=0)
        for index in range(100):
            if index < 49:
                value = 1_000_000
            elif index < 98:
                value = 3_000_000
            elif index == 98:
                value = 10_000_000
            else:
                value = 11_000_000
            histogram.add(_sample(value))
        gate = histogram.gate()
        self.assertFalse(gate.p50_pass)
        self.assertFalse(gate.p99_pass)
        self.assertTrue(gate.p999_alert)
        self.assertTrue(gate.report_failed)


class ShadowHotPathTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.loop = asyncio.get_running_loop()

    def _intent(self):
        handed = []
        tick = _lane().tick(
            _qualify(),
            {"KAITO": _books()},
            initial_spread_state(run_id=RUN_ID),
            on_unsent_intent=handed.append,
        )
        self.assertEqual(len(handed), 1)
        return handed[0]

    async def test_null_sink_has_no_network_shape_and_probe_uses_no_socket(self) -> None:
        sink = NullTradeSink(self.loop, time.monotonic_ns)
        for name in ("url", "host", "port", "ssl", "credentials", "socket"):
            self.assertFalse(hasattr(sink, name))
        histogram = LatencyHistogram(required_valid_n=1, required_warmup_n=0)
        hot = ShadowHotPath(
            self.loop,
            clock=time.monotonic_ns,
            wall_ms=lambda: int(time.time() * 1000),
            warmup_n=0,
            histogram=histogram,
        )
        with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")):
            sample = await hot.probe(self._intent())
        self.assertTrue(sample.valid)
        self.assertEqual(hot.bybit_sink.write_count, 1)
        self.assertEqual(hot.okx_sink.write_count, 1)
        self.assertEqual(hot.probe_queue_depth, 0)
        self.assertEqual(hot.lifecycle_drops, 0)
        self.assertTrue(histogram.gate().valid_count_complete)

    async def test_scripted_clock_exact_chronometry_and_regression(self) -> None:
        clock = ScriptedClock([1_000, 1_100, 1_200, 1_300, 1_400, 1_500, 1_600, 1_700])
        histogram = LatencyHistogram(required_valid_n=1, required_warmup_n=0)
        hot = ShadowHotPath(
            self.loop,
            clock=clock,
            wall_ms=lambda: 1_700_000_000_000,
            warmup_n=0,
            histogram=histogram,
        )
        sample = await hot.probe(_intent())
        self.assertTrue(sample.valid)
        self.assertEqual(sample.signal_to_first_write_ns, 300)
        self.assertEqual(sample.bybit_write_latency_ns, 400)
        self.assertEqual(sample.okx_write_latency_ns, 300)
        self.assertEqual(sample.dual_leg_write_ns, 400)
        self.assertEqual(clock.calls, 8)

        regressed = ScriptedClock(
            [1_000, 1_100, 1_200, 1_300, 1_400, 1_500, 1_600, 1_700]
        )
        bad_histogram = LatencyHistogram(required_valid_n=1, required_warmup_n=0)
        bad = ShadowHotPath(
            self.loop,
            clock=regressed,
            wall_ms=lambda: 1_700_000_000_000,
            warmup_n=0,
            histogram=bad_histogram,
        )
        bad_sample = await bad.probe(
            _intent(signal_mono_ns=10_000, expiry_mono_ns=1_000_010_000)
        )
        self.assertTrue(bad_sample.invalid_clock)
        self.assertFalse(bad_sample.valid)
        self.assertEqual(bad_histogram.invalid_clock_n, 1)
        self.assertTrue(bad_histogram.gate().report_failed)

    async def test_construction_is_inert(self) -> None:
        before = {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}
        with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")), patch.object(
            builtins, "open", side_effect=AssertionError("file I/O forbidden")
        ), patch.object(
            self.loop, "create_task", side_effect=AssertionError("task start forbidden")
        ):
            hot = ShadowHotPath(
                self.loop,
                clock=time.monotonic_ns,
                warmup_n=0,
                histogram=LatencyHistogram(required_valid_n=1, required_warmup_n=0),
            )
        after = {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}
        self.assertEqual(before, after)
        self.assertEqual(hot.bybit_sink.write_count, 0)
        self.assertEqual(hot.okx_sink.write_count, 0)

    async def test_both_sink_tasks_start_before_release(self) -> None:
        release = asyncio.Event()
        bybit_started = asyncio.Event()
        okx_started = asyncio.Event()

        class BlockingSink(NullTradeSink):
            def __init__(self, loop, clock, started):
                super().__init__(loop, clock)
                self.started = started

            async def asend(self, text: str) -> None:
                self.started.set()
                await release.wait()
                await super().asend(text)

        bybit = BlockingSink(self.loop, time.monotonic_ns, bybit_started)
        okx = BlockingSink(self.loop, time.monotonic_ns, okx_started)
        hot = ShadowHotPath(
            self.loop,
            clock=time.monotonic_ns,
            wall_ms=lambda: int(time.time() * 1000),
            warmup_n=0,
            histogram=LatencyHistogram(required_valid_n=1, required_warmup_n=0),
            bybit_sink=bybit,
            okx_sink=okx,
        )
        task = asyncio.create_task(hot.probe(self._intent()))
        await asyncio.wait_for(
            asyncio.gather(bybit_started.wait(), okx_started.wait()), timeout=1
        )
        self.assertFalse(task.done())
        self.assertEqual(hot.probe_queue_depth, 1)
        release.set()
        sample = await task
        self.assertTrue(sample.valid)

    async def test_repeated_probes_do_not_mutate_execution_state(self) -> None:
        lane = _lane()
        idle = initial_spread_state(run_id=RUN_ID)
        histogram = LatencyHistogram(required_valid_n=5, required_warmup_n=2)
        hot = ShadowHotPath(
            self.loop,
            clock=time.monotonic_ns,
            wall_ms=lambda: int(time.time() * 1000),
            warmup_n=2,
            histogram=histogram,
        )
        for _ in range(7):
            handed = []
            tick = lane.tick(
                _qualify(),
                {"KAITO": _books()},
                idle,
                on_unsent_intent=handed.append,
            )
            self.assertEqual(tick.divergence, DivergenceClass.MATCH)
            self.assertEqual(len(handed), 1)
            await hot.probe(handed[0])
        self.assertEqual(idle.status, SpreadStatus.IDLE)
        self.assertEqual(histogram.warmup_n, 2)
        self.assertEqual(histogram.valid_n, 5)
        self.assertFalse(histogram.gate().report_failed)

    async def test_full_10100_probe_mechanics_have_complete_samples(self) -> None:
        lane = _lane()
        idle = initial_spread_state(run_id=RUN_ID)
        histogram = LatencyHistogram()
        hot = ShadowHotPath(
            self.loop,
            clock=time.monotonic_ns,
            wall_ms=lambda: int(time.time() * 1000),
            histogram=histogram,
        )
        snapshots = _qualify()
        quotes = {"KAITO": _books()}
        for _ in range(DEFAULT_WARMUP_N + DEFAULT_COUNTED_N):
            handed = []
            tick = lane.tick(
                snapshots,
                quotes,
                idle,
                on_unsent_intent=handed.append,
            )
            self.assertEqual(tick.divergence, DivergenceClass.MATCH)
            self.assertEqual(len(handed), 1)
            await hot.probe(handed[0])
        gate = histogram.gate()
        self.assertEqual(histogram.warmup_n, DEFAULT_WARMUP_N)
        self.assertEqual(histogram.valid_n, DEFAULT_COUNTED_N)
        self.assertEqual(histogram.invalid_clock_n, 0)
        self.assertEqual(histogram.missing_n, 0)
        self.assertEqual(hot.lifecycle_drops, 0)
        self.assertTrue(gate.warmup_complete)
        self.assertTrue(gate.valid_count_complete)
        self.assertTrue(gate.local_descriptive_only)
        self.assertFalse(gate.target_vps_gate_eligible)
        self.assertEqual(idle.status, SpreadStatus.IDLE)


class HealthTests(unittest.TestCase):
    def test_local_health_schema_is_explicit_and_not_vps_eligible(self) -> None:
        sample = ShadowHealth().sample(event_loop_lag_ns=123)
        public = sample.to_public_dict()
        self.assertEqual(
            set(public),
            {
                "schema_version",
                "event_loop_lag_ns",
                "cpu_user_s",
                "cpu_system_s",
                "rss_bytes",
                "probe_queue_depth",
                "lifecycle_drops",
                "unknown_state_count",
                "reconnect",
                "wal",
                "collector_baseline",
                "sampled_outside_write_await",
                "local_descriptive_only",
                "target_vps_gate_eligible",
            },
        )
        self.assertEqual(public["schema_version"], HEALTH_SCHEMA_VERSION)
        self.assertEqual(public["event_loop_lag_ns"], 123)
        self.assertFalse(public["collector_baseline"]["observed"])
        self.assertFalse(public["collector_baseline"]["in_path"])
        self.assertFalse(public["reconnect"]["applicable"])
        self.assertFalse(public["wal"]["applicable"])
        self.assertTrue(public["sampled_outside_write_await"])
        self.assertTrue(public["local_descriptive_only"])
        self.assertFalse(public["target_vps_gate_eligible"])


if __name__ == "__main__":
    unittest.main()
