"""EV2-09B target-runtime shadow wiring.

The observer is deliberately a sibling of the legacy ``would_sent`` manager.
It receives the exact same theta snapshots and books, compares the frozen
decision with the execution-v2 strategy bridge, and exercises only the
network-incapable :class:`~app.bot.execution.shadow.NullTradeSink` transport.

This module has no venue credential loader and no private/trade socket import.
Private read-only websocket qualification is run by the existing
``app.bot.private --ws-readonly`` entrypoint with ``LIVE_ORDERS=0``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from app.bot.execution.contracts import (
    SCHEMA_VERSION as CONTRACT_SCHEMA_VERSION,
    ExecutionEvent,
    ExecutionEventType,
    IntentAction,
    SpreadDirection,
    SpreadState,
    TradeIntent,
    Venue,
    derive_client_id,
)
from app.bot.execution.shadow import (
    DEFAULT_COUNTED_N,
    DEFAULT_WARMUP_N,
    LatencyHistogram,
    ShadowHealth,
    ShadowHotPath,
    ShadowParityLane,
    ShadowParityTick,
)
from app.bot.execution.state_machine import apply_events, initial_spread_state
from app.bot.execution.strategy_bridge import (
    CANARY_STAGE,
    CONTEXT_SCHEMA_VERSION,
    DEFAULT_NOTIONAL_USDT,
    INTENT_TTL_NS,
    RISK_POLICY_REVISION,
    BridgeConfig,
    OpenTradeContext,
)
from app.bot.theta_trade_manager import (
    POLICY_ID,
    OpenPosition,
    SlotState,
    ThetaTradeConfig,
    assert_synthetic_policy_gates,
)

SCHEMA_VERSION = "bbot.execution.shadow_runtime.v1"
ENV_ENABLED = "BBOT_EV2_SHADOW"
ENV_TARGET_VPS = "BBOT_EV2_TARGET_VPS"
ENV_PROBE_DELAY_SEC = "BBOT_EV2_PROBE_DELAY_SEC"
ENV_COUNTED_N = "BBOT_EV2_COUNTED_N"
ENV_WARMUP_N = "BBOT_EV2_WARMUP_N"
ENV_AUDIT_ENABLED = "BBOT_EV2_AUDIT"
DEFAULT_PROBE_DELAY_SEC = 60.0
QUEUE_MAX = 4096


class ShadowRuntimeGateError(RuntimeError):
    """Fail-closed configuration error raised before runtime tasks start."""


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def shadow_runtime_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    return _truthy((env or os.environ).get(ENV_ENABLED))


def assert_shadow_runtime_gates(
    profile: str,
    env: Optional[Mapping[str, str]] = None,
) -> None:
    """Require a stub, read-only Gear 2.2 profile before any broker is built."""

    e = dict(env or os.environ)
    if not shadow_runtime_enabled(e):
        return
    normalized = str(profile or "").strip().lower()
    if normalized not in {"gear22_would_send", "gear22"}:
        raise ShadowRuntimeGateError("EV2 shadow requires gear22_would_send profile")
    broker = str(e.get("BBOT_BROKER") or "stub").strip().lower()
    if broker not in {"stub", ""}:
        raise ShadowRuntimeGateError("EV2 shadow requires BBOT_BROKER=stub")
    if _truthy(e.get("LIVE_ORDERS")):
        raise ShadowRuntimeGateError("EV2 shadow requires LIVE_ORDERS=0")
    if _truthy(e.get("BBOT_THETA_LIVE_SEND")):
        raise ShadowRuntimeGateError("EV2 shadow requires BBOT_THETA_LIVE_SEND=0")
    if _truthy(e.get(ENV_AUDIT_ENABLED)):
        if normalized != "gear22_would_send":
            raise ShadowRuntimeGateError("no-order audit requires gear22_would_send")
        if not shadow_runtime_enabled(e):
            raise ShadowRuntimeGateError("no-order audit requires EV2 shadow")
        if str(e.get("BBOT_POLICY_MODE") or "").strip().lower() != "synthetic_roll_v1":
            raise ShadowRuntimeGateError("no-order audit requires synthetic policy")
        if not str(e.get("BBOT_PRIVATE_STATUS_DIR") or "").strip():
            raise ShadowRuntimeGateError("no-order audit requires private status directory")
        try:
            assert_synthetic_policy_gates(e)
        except Exception as exc:
            raise ShadowRuntimeGateError("no-order audit policy gate rejected") from exc


class _JsonlQueue:
    """Bounded async journal; filesystem work is kept off the event loop."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.queue: asyncio.Queue[Optional[dict[str, Any]]] = asyncio.Queue(
            maxsize=QUEUE_MAX
        )
        self.task: Optional[asyncio.Task[None]] = None
        self.dropped = 0

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self._run(), name="ev2-shadow-jsonl")

    def emit(self, record: Mapping[str, Any]) -> None:
        try:
            self.queue.put_nowait(dict(record))
        except asyncio.QueueFull:
            self.dropped += 1

    async def close(self) -> None:
        if self.task is None:
            return
        await self.queue.put(None)
        await self.task
        self.task = None

    async def _run(self) -> None:
        while True:
            item = await self.queue.get()
            try:
                if item is None:
                    return
                await asyncio.to_thread(self._append, item)
            finally:
                self.queue.task_done()

    def _append(self, record: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(
            dict(record), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(raw + "\n")
            fh.flush()


@dataclass(frozen=True)
class PendingShadowTick:
    tick: ShadowParityTick
    sample: Optional[dict[str, Any]]


class _EventFactory:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._event_n = 0
        self._last_mono = 0

    def batch(
        self,
        intent: TradeIntent,
        *,
        close: bool,
        start_mono: int,
    ) -> list[ExecutionEvent]:
        self._last_mono = max(self._last_mono, int(start_mono))
        seq = 0

        def event(
            kind: ExecutionEventType,
            *,
            venue: Optional[Venue] = None,
            leg_id: Optional[str] = None,
            payload: Optional[dict[str, Any]] = None,
        ) -> ExecutionEvent:
            nonlocal seq
            seq += 1
            self._event_n += 1
            self._last_mono += 1
            return ExecutionEvent(
                schema_version=CONTRACT_SCHEMA_VERSION,
                event_id=f"shadowevt{self._event_n:024d}",
                event_type=kind,
                intent_id=intent.intent_id,
                run_id=self.run_id,
                sequence=seq,
                monotonic_ns=self._last_mono,
                venue=venue,
                leg_id=leg_id,
                payload=payload or {},
            )

        action = "close" if close else "open"
        rows = [
            event(
                ExecutionEventType.INTENT_ACCEPTED,
                payload={
                    "action": action,
                    "coin": intent.coin,
                    "spread_direction": intent.spread_direction.value,
                    "lot_tolerance": "0",
                },
            )
        ]
        for venue, leg_id in ((Venue.OKX, "leg_okx"), (Venue.BYBIT, "leg_bybit")):
            if intent.spread_direction is SpreadDirection.LONG:
                side = "buy" if venue is Venue.OKX else "sell"
            else:
                side = "sell" if venue is Venue.OKX else "buy"
            if close:
                side = "sell" if side == "buy" else "buy"
            instrument = (
                f"{intent.coin}-USDT-SWAP"
                if venue is Venue.OKX
                else f"{intent.coin}USDT"
            )
            rows.append(
                event(
                    ExecutionEventType.REQUEST_SENT,
                    venue=venue,
                    leg_id=leg_id,
                    payload={
                        "quantity": "1",
                        "reduce_only": close,
                        "instrument": instrument,
                        "side": side,
                        "client_id": derive_client_id(
                            intent.intent_id, venue, reduce_only=close
                        ),
                    },
                )
            )
        for venue, leg_id in ((Venue.OKX, "leg_okx"), (Venue.BYBIT, "leg_bybit")):
            rows.append(
                event(
                    ExecutionEventType.ACK_ACCEPTED,
                    venue=venue,
                    leg_id=leg_id,
                )
            )
        for venue, leg_id in ((Venue.OKX, "leg_okx"), (Venue.BYBIT, "leg_bybit")):
            rows.append(
                event(
                    ExecutionEventType.FILL,
                    venue=venue,
                    leg_id=leg_id,
                    payload={"quantity": "1"},
                )
            )
        if close:
            for venue, leg_id in ((Venue.OKX, "leg_okx"), (Venue.BYBIT, "leg_bybit")):
                rows.extend(
                    [
                        event(
                            ExecutionEventType.POSITION_OBSERVED,
                            venue=venue,
                            leg_id=leg_id,
                            payload={"quantity": "0"},
                        ),
                        event(
                            ExecutionEventType.OPEN_ORDERS_OBSERVED,
                            venue=venue,
                            leg_id=leg_id,
                            payload={"open_order_count": 0},
                        ),
                    ]
                )
            rows.append(
                event(
                    ExecutionEventType.FLATNESS_PROVEN,
                    payload={"positions_flat": True, "open_orders_flat": True},
                )
            )
        return rows


class ExecutionShadowRuntime:
    """Live-feed EV2 parity observer with structurally no-order transport."""

    def __init__(
        self,
        *,
        data_root: Path,
        log: Callable[[str], None],
        env: Optional[Mapping[str, str]] = None,
        initial_position: Optional[OpenPosition] = None,
        audit_bridge_factory: Optional[Callable[[asyncio.AbstractEventLoop, str], Any]] = None,
    ) -> None:
        self.env = dict(env or os.environ)
        self.target_vps = _truthy(self.env.get(ENV_TARGET_VPS))
        self.run_id = f"shadow_{uuid.uuid4().hex}"
        self.log = log
        self.writer = _JsonlQueue(Path(data_root) / "execution-v2-shadow.jsonl")
        policy_config = ThetaTradeConfig.from_env(self.env)
        bridge_config = BridgeConfig(
            run_id=self.run_id,
            policy_version=policy_config.policy_id,
            policy_params=policy_config.policy_params,
        )
        self.lane = ShadowParityLane(run_id=self.run_id, config=bridge_config)
        self.policy_id = policy_config.policy_id
        self.state: SpreadState = initial_spread_state(run_id=self.run_id)
        self.events = _EventFactory(self.run_id)
        self.restored_trade_id: Optional[str] = None
        if initial_position is not None:
            self._restore_open_position(initial_position)
        self.hot_path: Optional[ShadowHotPath] = None
        self.health = ShadowHealth()
        self._tasks: list[asyncio.Task[Any]] = []
        self._started = False
        self._parity_ticks = 0
        self._divergences = 0
        self._lifecycle_drops = 0
        self._last_loop_target_ns = 0
        self._audit_bridge_factory = audit_bridge_factory
        self._audit_bridge: Any = None
        self._audit_attempts = 0
        self._audit_passes = 0
        self._audit_rejections: dict[str, int] = {}

    @property
    def summary(self) -> dict[str, Any]:
        histogram = None
        if self.hot_path is not None:
            histogram = self._qualified_gate()
        return {
            "schema_version": SCHEMA_VERSION,
            "event": "summary",
            "run_id": self.run_id,
            "parity_ticks": self._parity_ticks,
            "divergences": self._divergences,
            "lifecycle_drops": self._lifecycle_drops,
            "journal_drops": self.writer.dropped,
            "spread_status": self.state.status.value,
            "histogram": histogram,
            "orders_sent": 0,
            "trade_socket_bound": False,
            "policy_id": self.policy_id,
            "target_vps_gate_eligible": self.target_vps,
            "restored_trade_id": self.restored_trade_id,
            "no_order_audit": {
                "attempts": self._audit_attempts,
                "passes": self._audit_passes,
                "rejections": dict(self._audit_rejections),
            },
        }

    def _restore_open_position(self, position: OpenPosition) -> None:
        """Rebuild shadow FSM + bridge context from durable manager history."""

        if not isinstance(position, OpenPosition):
            raise ShadowRuntimeGateError("invalid restored position")
        if position.fill_spread_pp is None:
            raise ShadowRuntimeGateError("restored position missing fill spread")
        direction = (
            SpreadDirection.LONG if position.side == "long" else SpreadDirection.SHORT
        )
        signal_mono_ns = max(1, int(position.open_signal_ts_ms) * 1_000_000)
        signal_wall_ns = max(1, int(position.open_signal_ts_ms) * 1_000_000)
        snapshot_ref = "h" + hashlib.sha256(
            f"theta-replay:{position.trade_id}".encode("utf-8")
        ).hexdigest()
        intent = TradeIntent(
            schema_version=CONTRACT_SCHEMA_VERSION,
            intent_id=position.trade_id,
            run_id=self.run_id,
            policy_version=self.policy_id,
            action=IntentAction.OPEN,
            spread_direction=direction,
            coin=position.base_coin,
            notional_usdt=Decimal(str(position.open_notional)),
            signal_mono_ns=signal_mono_ns,
            signal_wall_ns=signal_wall_ns,
            expiry_mono_ns=signal_mono_ns + INTENT_TTL_NS,
            signal_snapshot_ref=snapshot_ref,
            canary_stage=CANARY_STAGE,
            risk_policy_revision=RISK_POLICY_REVISION,
        )
        events = self.events.batch(intent, close=False, start_mono=signal_mono_ns)
        self.state = apply_events(self.state, events)
        restored = self.lane.bridge.restore_context(
            self.state,
            [
                OpenTradeContext(
                    schema_version=CONTEXT_SCHEMA_VERSION,
                    trade_id=position.trade_id,
                    open_intent_id=position.trade_id,
                    coin=position.base_coin,
                    side=position.side,
                    open_signal_ts_ms=position.open_signal_ts_ms,
                    open_fill_ts_ms=position.open_fill_ts_ms,
                    fill_spread_pp=float(position.fill_spread_pp),
                    open_theta_1m=position.open_theta_1m,
                    open_notional=Decimal(str(position.open_notional)),
                    signal_snapshot_ref=snapshot_ref,
                    signal_mono_ns=signal_mono_ns,
                    signal_wall_ns=signal_wall_ns,
                )
            ],
        )
        if not restored.restored:
            raise ShadowRuntimeGateError("restored position context mismatch")
        self.restored_trade_id = position.trade_id

    def _qualified_gate(self) -> dict[str, Any]:
        if self.hot_path is None:
            raise ShadowRuntimeGateError("EV2 shadow hot path not started")
        gate = self.hot_path.histogram.gate().to_public_dict()
        gate["local_descriptive_only"] = not self.target_vps
        gate["target_vps_gate_eligible"] = self.target_vps
        return gate

    async def start(self) -> None:
        if self._started:
            return
        loop = asyncio.get_running_loop()
        if self._audit_bridge_factory is not None:
            self._audit_bridge = self._audit_bridge_factory(loop, self.run_id)
        warmup_n = max(0, int(self.env.get(ENV_WARMUP_N) or DEFAULT_WARMUP_N))
        counted_n = max(1, int(self.env.get(ENV_COUNTED_N) or DEFAULT_COUNTED_N))
        self.hot_path = ShadowHotPath(
            loop,
            clock=time.monotonic_ns,
            wall_ms=lambda: time.time_ns() // 1_000_000,
            warmup_n=warmup_n,
            histogram=LatencyHistogram(
                required_valid_n=counted_n,
                required_warmup_n=warmup_n,
            ),
        )
        self.writer.start()
        self._started = True
        self.writer.emit(
            {
                "schema_version": SCHEMA_VERSION,
                "event": "start",
                "run_id": self.run_id,
                "ts_ns": time.time_ns(),
                "orders_sent": 0,
                "trade_socket_bound": False,
                "live_orders": False,
                "policy_id": self.policy_id,
                "target_vps_gate_eligible": self.target_vps,
                "restored_trade_id": self.restored_trade_id,
            }
        )
        self._tasks = [
            asyncio.create_task(self._probe_loop(), name="ev2-shadow-probes"),
            asyncio.create_task(self._health_loop(), name="ev2-shadow-health"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.writer.emit(self.summary)
        await self.writer.close()
        if self._audit_bridge is not None:
            self._audit_bridge.close()
            self._audit_bridge = None
        self._started = False

    async def before_trade(
        self,
        snapshots: Sequence[Any],
        quotes: Mapping[str, Mapping[str, Mapping[str, Any]]],
        slot: SlotState,
        *,
        decision_ts_s: Optional[int] = None,
    ) -> PendingShadowTick:
        if not self._started or self.hot_path is None:
            raise ShadowRuntimeGateError("EV2 shadow runtime not started")
        tick = self.lane.tick(
            snapshots,
            quotes,
            self.state,
            slot=slot,
            decision_ts_s=decision_ts_s,
        )
        self._parity_ticks += 1
        if tick.divergence.value != "match":
            self._divergences += 1
        sample = None
        if tick.intent is not None:
            sample = (await self.hot_path.probe(tick.intent)).to_public_dict()
            if (
                self._audit_bridge is not None
                and tick.intent.action is IntentAction.OPEN
            ):
                audit = await self._audit_bridge.audit(tick.intent, quotes)
                self._audit_attempts += 1
                if audit.passed:
                    self._audit_passes += 1
                else:
                    code = audit.reason_code or "audit_not_passed"
                    self._audit_rejections[code] = self._audit_rejections.get(code, 0) + 1
                self.writer.emit({
                    "schema_version": SCHEMA_VERSION,
                    "event": "no_order_prewrite_audit",
                    "run_id": self.run_id,
                    "audit": audit.to_public_dict(),
                    "orders_sent": 0,
                    "trade_socket_bound": False,
                })
        self.writer.emit(
            {
                "schema_version": SCHEMA_VERSION,
                "event": "parity_tick",
                "run_id": self.run_id,
                "ts_ns": time.time_ns(),
                "parity": tick.to_public_dict(),
                "probe": sample,
                "orders_sent": 0,
            }
        )
        return PendingShadowTick(tick=tick, sample=sample)

    async def after_trade(
        self,
        pending: PendingShadowTick,
        rows: Sequence[Mapping[str, Any]],
        quotes: Mapping[str, Mapping[str, Mapping[str, Any]]],
    ) -> None:
        intent = pending.tick.intent
        if intent is None:
            return
        event_name = intent.action.value
        matching_rows = [
            row for row in rows if str(row.get("event") or "") == event_name
        ]
        attempt_rows = [
            row for row in rows if str(row.get("event") or "") == f"{event_name}_attempt"
        ]
        matched = any(
            row.get("lifecycle_committed") is not False for row in matching_rows
        )
        if not matched:
            self.lane.bridge.clear_inflight_on_reject(intent.intent_id)
            unfilled = any(
                row.get("lifecycle_committed") is False for row in attempt_rows
            )
            if not unfilled:
                self._lifecycle_drops += 1
            self.writer.emit(
                {
                    "schema_version": SCHEMA_VERSION,
                    "event": "mirror_reject",
                    "run_id": self.run_id,
                    "intent_id": intent.intent_id,
                    "reason": (
                        "synthetic_fill_unavailable"
                        if unfilled
                        else "would_sent_row_missing"
                    ),
                    "orders_sent": 0,
                }
            )
            return
        close = intent.action is IntentAction.CLOSE
        events = self.events.batch(
            intent,
            close=close,
            start_mono=max(time.monotonic_ns(), self.state.last_monotonic_ns),
        )
        self.state = apply_events(self.state, events)
        if not close:
            self.lane.bridge.commit_proven_open(self.state, quotes)
        self.writer.emit(
            {
                "schema_version": SCHEMA_VERSION,
                "event": "mirror_transition",
                "run_id": self.run_id,
                "intent_id": intent.intent_id,
                "action": event_name,
                "spread_status": self.state.status.value,
                "state_source": "would_sent_shadow_fill_model",
                "orders_sent": 0,
            }
        )

    async def after_error(self, pending: Optional[PendingShadowTick]) -> None:
        if pending is None or pending.tick.intent is None:
            return
        self.lane.bridge.clear_inflight_on_reject(pending.tick.intent.intent_id)
        self._lifecycle_drops += 1

    def _synthetic_intent(self, index: int) -> TradeIntent:
        mono = time.monotonic_ns()
        return TradeIntent(
            schema_version=CONTRACT_SCHEMA_VERSION,
            intent_id=f"shadowprobe{index:022d}",
            run_id=self.run_id,
            policy_version=self.policy_id,
            action=IntentAction.OPEN,
            spread_direction=SpreadDirection.LONG,
            coin="KAITO",
            notional_usdt=DEFAULT_NOTIONAL_USDT,
            signal_mono_ns=mono,
            signal_wall_ns=time.time_ns(),
            expiry_mono_ns=mono + INTENT_TTL_NS,
            signal_snapshot_ref="h" + ("00" * 32),
            canary_stage=CANARY_STAGE,
            risk_policy_revision=RISK_POLICY_REVISION,
        )

    async def _probe_loop(self) -> None:
        assert self.hot_path is not None
        delay = max(0.0, float(self.env.get(ENV_PROBE_DELAY_SEC) or DEFAULT_PROBE_DELAY_SEC))
        counted = max(1, int(self.env.get(ENV_COUNTED_N) or DEFAULT_COUNTED_N))
        warmup = max(0, int(self.env.get(ENV_WARMUP_N) or DEFAULT_WARMUP_N))
        await asyncio.sleep(delay)
        for index in range(1, warmup + counted + 1):
            await self.hot_path.probe(self._synthetic_intent(index))
            if index % 250 == 0:
                await asyncio.sleep(0)
        self.writer.emit(
            {
                "schema_version": SCHEMA_VERSION,
                "event": "latency_report",
                "run_id": self.run_id,
                "report": self._qualified_gate(),
                "orders_sent": 0,
            }
        )
        self.log(
            "ev2_shadow_latency_complete | run_id=%s | warmup=%s | counted=%s"
            % (self.run_id, warmup, counted)
        )

    async def _health_loop(self) -> None:
        assert self.hot_path is not None
        loop = asyncio.get_running_loop()
        interval_ns = 30_000_000_000
        self._last_loop_target_ns = time.monotonic_ns() + interval_ns
        while True:
            await asyncio.sleep(30.0)
            now = time.monotonic_ns()
            lag = max(0, now - self._last_loop_target_ns)
            self._last_loop_target_ns = now + interval_ns
            health = self.health.sample(
                hot_path=self.hot_path,
                loop=loop,
                event_loop_lag_ns=lag,
                unknown_state_count=0,
            ).to_public_dict()
            health.update(
                {
                    "schema_version": SCHEMA_VERSION,
                    "event": "health",
                    "run_id": self.run_id,
                    "ts_ns": time.time_ns(),
                    "journal_queue_depth": self.writer.queue.qsize(),
                    "journal_drops": self.writer.dropped,
                    "orders_sent": 0,
                    "local_descriptive_only": not self.target_vps,
                    "target_vps_gate_eligible": self.target_vps,
                }
            )
            self.writer.emit(health)


__all__ = [
    "ENV_ENABLED",
    "ENV_TARGET_VPS",
    "ExecutionShadowRuntime",
    "PendingShadowTick",
    "ShadowRuntimeGateError",
    "assert_shadow_runtime_gates",
    "shadow_runtime_enabled",
]
