"""Local EV2-09A shadow parity and latency harness.

Import and construction are inert: no tasks, files, Sentry, or venue I/O.
Parity and latency-probe lanes are separate. The mass probe reuses
``prepare_dual_leg`` and ``ExecutionTransport.dispatch`` into a
structurally network-incapable sink. Intents originate only via
``Gear22StrategyBridge.observe``. This module does not call engine submit,
invent fills, or write the old trade root.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import resource
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping, Optional, Sequence

from app.bot.execution.contracts import (
    IntentAction,
    LegPlan,
    SpreadDirection,
    SpreadState,
    TradeIntent,
    Venue,
)
from app.bot.execution.strategy_bridge import (
    CANARY_STAGE,
    DEFAULT_NOTIONAL_USDT,
    INTENT_TTL_NS,
    RISK_POLICY_REVISION,
    BridgeClocks,
    BridgeConfig,
    BridgeIds,
    BridgeTick,
    DivergenceClass,
    Gear22StrategyBridge,
)
from app.bot.execution.transport import (
    CachedInstrument,
    DispatchResult,
    DispatchStatus,
    ExecutionTransport,
    InstrumentCache,
    TransportError,
    WriteOutcome,
    prepare_dual_leg,
    unsigned_frame_finalizer,
)
from app.bot.theta_screener import ThetaSnapshot
from app.bot.theta_trade_manager import (
    DEFAULT_BOOK_DEPTH,
    DEFAULT_THETA_THR,
    GEAR22_HTML_TOP30,
    POLICY_ID,
    SlotState,
    ThetaDecision,
    decide_theta_k1,
)
from research.gear22_backtest.params_frozen import DEFAULT_OBSERVE_PARAMS

SCHEMA_VERSION = "bbot.execution.shadow.v1"
HEALTH_SCHEMA_VERSION = "bbot.execution.shadow_health.v1"
DEFAULT_WARMUP_N = 100
DEFAULT_COUNTED_N = 10_000
MS_NS = 1_000_000
THRESHOLD_1MS_NS = 1 * MS_NS
THRESHOLD_3MS_NS = 3 * MS_NS
THRESHOLD_10MS_NS = 10 * MS_NS
OKX_INST_ID_CODE_BASE = 10_000
_NON_VENUE_HMAC_KEY = b"ev2-09a-local-non-venue-hmac-key"

LATENCY_SERIES = (
    "signal_to_first_write_ns",
    "bybit_write_latency_ns",
    "okx_write_latency_ns",
    "dual_leg_write_ns",
)

_SHADOW_ERROR_CODES = frozenset(
    {
        "noncanonical_intent",
        "noncanonical_config",
        "unclassified_non_match",
        "invalid_histogram",
        "invalid_slot",
        "invalid_health",
        "invalid_dispatch",
        "invalid_clocks",
        "invalid_intent",
    }
)

_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "api_key",
        "api_secret",
        "secret",
        "passphrase",
        "password",
        "authorization",
        "signature",
        "sign",
        "access_token",
        "refresh_token",
        "bearer_token",
        "private_key",
        "client_secret",
        "raw_payload",
        "raw_frame",
        "frame",
        "request_body",
        "response_body",
        "headers",
        "canonical_request",
        "account_id",
        "uid",
        "member_id",
        "wallet_address",
        "exchange_order_id",
        "order_id",
        "client_order_id",
        "clordid",
        "ordid",
        "balance",
        "available_balance",
        "equity",
        "margin",
        "account_value",
        "fill_price",
    }
)


class ShadowError(ValueError):
    """Fail-closed local shadow harness error."""

    def __init__(self, reason_code: str) -> None:
        if reason_code not in _SHADOW_ERROR_CODES:
            reason_code = "invalid_intent"
        self.reason_code = reason_code
        super().__init__(reason_code)

    def to_public_dict(self) -> dict[str, Any]:
        out = {"schema_version": SCHEMA_VERSION, "reason_code": self.reason_code}
        _assert_public(out)
        return out


def _norm_key(name: object) -> str:
    return str(name).strip().lower().replace("-", "_")


def _assert_public(node: object) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if _norm_key(key) in _FORBIDDEN_PUBLIC_KEYS:
                raise ShadowError("invalid_intent")
            _assert_public(value)
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            _assert_public(item)


def _require_int(value: object, *, reason: str = "invalid_clocks") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ShadowError(reason)
    if value < 0:
        raise ShadowError(reason)
    return value


def canonical_bridge_config(run_id: str) -> BridgeConfig:
    """Exact Gear 2.2 canary pins: top-30 order, frozen params, $20, 1s TTL."""
    return BridgeConfig(
        run_id=run_id,
        notional_usdt=DEFAULT_NOTIONAL_USDT,
        policy_version=POLICY_ID,
        canary_stage=CANARY_STAGE,
        risk_policy_revision=RISK_POLICY_REVISION,
        coin_order=GEAR22_HTML_TOP30,
        policy_params=DEFAULT_OBSERVE_PARAMS,
        intent_ttl_ns=INTENT_TTL_NS,
        book_depth=DEFAULT_BOOK_DEPTH,
        theta_thr=DEFAULT_THETA_THR,
    )


def config_is_canonical(config: BridgeConfig) -> bool:
    if not isinstance(config, BridgeConfig):
        return False
    return (
        tuple(config.coin_order) == GEAR22_HTML_TOP30
        and config.policy_params == DEFAULT_OBSERVE_PARAMS
        and config.notional_usdt == DEFAULT_NOTIONAL_USDT
        and config.policy_version == POLICY_ID
        and config.canary_stage == CANARY_STAGE
        and config.risk_policy_revision == RISK_POLICY_REVISION
        and config.intent_ttl_ns == INTENT_TTL_NS
        and config.book_depth == DEFAULT_BOOK_DEPTH
        and config.theta_thr == DEFAULT_THETA_THR
    )


def _intent_is_canonical(intent: TradeIntent) -> bool:
    if not isinstance(intent, TradeIntent):
        return False
    ttl = intent.expiry_mono_ns - intent.signal_mono_ns
    return (
        intent.policy_version == POLICY_ID
        and intent.notional_usdt == DEFAULT_NOTIONAL_USDT
        and intent.canary_stage == CANARY_STAGE
        and intent.risk_policy_revision == RISK_POLICY_REVISION
        and ttl == INTENT_TTL_NS
        and intent.coin in GEAR22_HTML_TOP30
    )


def _decision_identity(decision: ThetaDecision) -> tuple[str, str, str, str]:
    return (
        str(decision.action),
        str(decision.base_coin or ""),
        str(decision.side or ""),
        str(decision.reason or ""),
    )


def canonical_instrument_cache(*, captured_mono_ns: int = 0, fresh_until_mono_ns: int = 10**18) -> InstrumentCache:
    captured = _require_int(captured_mono_ns)
    fresh_until = _require_int(fresh_until_mono_ns)
    items: list[CachedInstrument] = []
    for index, coin in enumerate(GEAR22_HTML_TOP30):
        items.append(
            CachedInstrument(
                venue=Venue.BYBIT,
                instrument=f"{coin}USDT",
                captured_mono_ns=captured,
                fresh_until_mono_ns=fresh_until,
            )
        )
        items.append(
            CachedInstrument(
                venue=Venue.OKX,
                instrument=f"{coin}-USDT-SWAP",
                captured_mono_ns=captured,
                fresh_until_mono_ns=fresh_until,
                inst_id_code=OKX_INST_ID_CODE_BASE + index,
            )
        )
    return InstrumentCache.from_snapshots(items)


def _deterministic_plans(intent: TradeIntent) -> tuple[LegPlan, LegPlan]:
    reduce_only = intent.action is IntentAction.CLOSE
    if intent.spread_direction is SpreadDirection.LONG:
        bybit_side = "buy" if reduce_only else "sell"
        okx_side = "sell" if reduce_only else "buy"
    else:
        bybit_side = "sell" if reduce_only else "buy"
        okx_side = "buy" if reduce_only else "sell"
    quantity = Decimal("1")
    return (
        LegPlan.build(
            intent_id=intent.intent_id,
            leg_id="leg_bybit",
            venue=Venue.BYBIT,
            instrument=f"{intent.coin}USDT",
            side=bybit_side,
            quantity=quantity,
            reduce_only=reduce_only,
        ),
        LegPlan.build(
            intent_id=intent.intent_id,
            leg_id="leg_okx",
            venue=Venue.OKX,
            instrument=f"{intent.coin}-USDT-SWAP",
            side=okx_side,
            quantity=quantity,
            reduce_only=reduce_only,
        ),
    )


def _shadow_hmac_finalizer(
    static: Any,
    *,
    timestamp_ms: int,
    request_id: str,
    client_id: str,
) -> str:
    """Serialize the frozen frame and MAC it with an in-memory non-venue key."""
    unsigned = unsigned_frame_finalizer(
        static,
        timestamp_ms=timestamp_ms,
        request_id=request_id,
        client_id=client_id,
    )
    digest = hmac.new(_NON_VENUE_HMAC_KEY, unsigned.encode("utf-8"), hashlib.sha256).hexdigest()
    payload = json.loads(unsigned)
    payload["shadow_mac"] = digest
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


class NullTradeSink:
    """In-memory dual-leg write target. Owner loop + ``asend(text)`` only.

    Hashes and counts bytes, then yields once. Cannot connect, resolve a
    host, or hold credentials. Construction starts no task.
    """

    __slots__ = (
        "owner_loop",
        "_clock",
        "payload_bytes",
        "write_count",
        "last_digest",
        "last_write_mono_ns",
        "max_concurrent",
        "_active",
    )

    def __init__(self, loop: asyncio.AbstractEventLoop, clock: Callable[[], int]) -> None:
        if not isinstance(loop, asyncio.AbstractEventLoop):
            raise ShadowError("invalid_clocks")
        if not callable(clock):
            raise ShadowError("invalid_clocks")
        self.owner_loop = loop
        self._clock = clock
        self.payload_bytes = 0
        self.write_count = 0
        self.last_digest: Optional[str] = None
        self.last_write_mono_ns: Optional[int] = None
        self.max_concurrent = 0
        self._active = 0

    async def asend(self, text: str) -> None:
        if not isinstance(text, str):
            raise TypeError("asend requires text")
        self._active += 1
        if self._active > self.max_concurrent:
            self.max_concurrent = self._active
        try:
            encoded = text.encode("utf-8")
            self.payload_bytes += len(encoded)
            self.write_count += 1
            self.last_digest = hashlib.sha256(encoded).hexdigest()
            self.last_write_mono_ns = _require_int(self._clock())
            await asyncio.sleep(0)
        finally:
            self._active -= 1


class WouldSentDecisionReplica:
    """Frozen Gear 2.2 would-send replica. Canonical params only. No manager."""

    def decide(
        self,
        snapshots: Sequence[ThetaSnapshot],
        quotes: Mapping[str, Mapping[str, Mapping[str, Any]]],
        slot: SlotState,
    ) -> ThetaDecision:
        if not isinstance(slot, SlotState):
            raise ShadowError("invalid_slot")
        return decide_theta_k1(
            snapshots,
            slot=slot,
            thr=DEFAULT_THETA_THR,
            quotes=quotes,
            notional_usdt=float(DEFAULT_NOTIONAL_USDT),
            book_depth=DEFAULT_BOOK_DEPTH,
            coin_order=GEAR22_HTML_TOP30,
            policy_params=DEFAULT_OBSERVE_PARAMS,
        )


@dataclass(frozen=True)
class ShadowParityTick:
    replica: ThetaDecision
    bridge: BridgeTick
    divergence: DivergenceClass
    intent: Optional[TradeIntent]
    inflight_cleared: bool

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": SCHEMA_VERSION,
            "replica": {
                "action": self.replica.action,
                "base_coin": self.replica.base_coin,
                "side": self.replica.side,
                "reason": self.replica.reason,
            },
            "bridge": self.bridge.to_public_dict(),
            "divergence": self.divergence.value,
            "intent_id": None if self.intent is None else self.intent.intent_id,
            "inflight_cleared": self.inflight_cleared,
        }
        _assert_public(out)
        return out


class ShadowParityLane:
    """Compares replica ``decide_theta_k1`` with ``Gear22StrategyBridge.observe``."""

    def __init__(
        self,
        *,
        run_id: str,
        bridge: Optional[Gear22StrategyBridge] = None,
        clocks: Optional[BridgeClocks] = None,
        ids: Optional[BridgeIds] = None,
    ) -> None:
        if bridge is not None:
            if not isinstance(bridge, Gear22StrategyBridge):
                raise ShadowError("invalid_intent")
            if not config_is_canonical(bridge.config):
                raise ShadowError("noncanonical_config")
            self._bridge = bridge
        else:
            self._bridge = Gear22StrategyBridge(
                run_id=run_id,
                config=canonical_bridge_config(run_id),
                clocks=clocks,
                ids=ids,
            )
        self._replica = WouldSentDecisionReplica()

    @property
    def bridge(self) -> Gear22StrategyBridge:
        return self._bridge

    def tick(
        self,
        snapshots: Sequence[ThetaSnapshot],
        quotes: Mapping[str, Mapping[str, Mapping[str, Any]]],
        spread_state: SpreadState,
        slot: Optional[SlotState] = None,
        *,
        fill_model_diverged: bool = False,
        on_unsent_intent: Optional[Callable[[TradeIntent], None]] = None,
    ) -> ShadowParityTick:
        replica_slot = SlotState() if slot is None else slot
        replica = self._replica.decide(snapshots, quotes, replica_slot)
        bridge_tick = self._bridge.observe(
            snapshots,
            quotes,
            spread_state,
            fill_model_diverged=fill_model_diverged,
        )
        same = _decision_identity(replica) == _decision_identity(bridge_tick.decision)
        if same:
            divergence = DivergenceClass.MATCH
        elif bridge_tick.divergence is DivergenceClass.MATCH:
            raise ShadowError("unclassified_non_match")
        else:
            divergence = bridge_tick.divergence

        intent = bridge_tick.intent
        inflight_cleared = False
        canonical = config_is_canonical(self._bridge.config)
        if intent is not None and not canonical:
            self._bridge.clear_inflight_on_reject(intent.intent_id)
            inflight_cleared = self._bridge.inflight is None
            intent = None
        elif intent is not None and on_unsent_intent is not None:
            # The callback is the hand-off boundary. If it raises, retain the
            # latch so the same decision cannot be emitted again silently.
            on_unsent_intent(intent)
            self._bridge.clear_inflight_on_reject(intent.intent_id)
            inflight_cleared = self._bridge.inflight is None
        return ShadowParityTick(
            replica=replica,
            bridge=bridge_tick,
            divergence=divergence,
            intent=intent,
            inflight_cleared=inflight_cleared,
        )


@dataclass(frozen=True)
class ProbeSample:
    intent_id: str
    warmup: bool
    rejected_before_write: bool
    invalid_clock: bool
    missing: bool
    valid: bool
    signal_to_first_write_ns: Optional[int]
    bybit_write_latency_ns: Optional[int]
    okx_write_latency_ns: Optional[int]
    dual_leg_write_ns: Optional[int]
    dispatch_status: str
    reason_code: Optional[str]

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": SCHEMA_VERSION,
            "intent_id": self.intent_id,
            "warmup": self.warmup,
            "rejected_before_write": self.rejected_before_write,
            "invalid_clock": self.invalid_clock,
            "missing": self.missing,
            "valid": self.valid,
            "signal_to_first_write_ns": self.signal_to_first_write_ns,
            "bybit_write_latency_ns": self.bybit_write_latency_ns,
            "okx_write_latency_ns": self.okx_write_latency_ns,
            "dual_leg_write_ns": self.dual_leg_write_ns,
            "dispatch_status": self.dispatch_status,
            "reason_code": self.reason_code,
        }
        _assert_public(out)
        return out


def _chronometry_valid(result: DispatchResult) -> bool:
    signal = result.signal_to_first_write_ns
    bybit = result.bybit.write_latency_ns
    okx = result.okx.write_latency_ns
    dual = result.dual_leg_write_ns
    if signal is None or bybit is None or okx is None or dual is None:
        return False
    if signal < 0 or bybit < 0 or okx < 0 or dual < 0:
        return False
    return dual == max(bybit, okx)


def classify_dispatch(result: DispatchResult, *, warmup: bool) -> ProbeSample:
    if not isinstance(result, DispatchResult):
        raise ShadowError("invalid_dispatch")
    both_completed = (
        result.bybit.outcome is WriteOutcome.WRITE_COMPLETED
        and result.okx.outcome is WriteOutcome.WRITE_COMPLETED
    )
    rejected = result.status is DispatchStatus.REJECTED or (
        result.bybit.outcome is WriteOutcome.NOT_ATTEMPTED
        and result.okx.outcome is WriteOutcome.NOT_ATTEMPTED
    )
    clock_bad = (
        result.reason_code == "clock_regression"
        or result.bybit.reason_code == "clock_regression"
        or result.okx.reason_code == "clock_regression"
    )
    fields_ok = _chronometry_valid(result)
    rejected_before_write = bool(rejected and not both_completed)
    invalid_clock = bool(clock_bad and not rejected_before_write)
    missing = bool(
        not warmup
        and not rejected_before_write
        and not clock_bad
        and (not both_completed or not fields_ok)
    )
    valid = bool(both_completed and fields_ok and not clock_bad and not warmup)
    return ProbeSample(
        intent_id=result.intent_id,
        warmup=warmup,
        rejected_before_write=rejected_before_write,
        invalid_clock=invalid_clock,
        missing=missing,
        valid=valid,
        signal_to_first_write_ns=result.signal_to_first_write_ns,
        bybit_write_latency_ns=result.bybit.write_latency_ns,
        okx_write_latency_ns=result.okx.write_latency_ns,
        dual_leg_write_ns=result.dual_leg_write_ns,
        dispatch_status=result.status.value,
        reason_code=result.reason_code,
    )


@dataclass(frozen=True)
class SeriesCounts:
    name: str
    n: int
    le_1ms: int
    le_3ms: int
    le_10ms: int
    p50_pass: bool
    p99_pass: bool
    p999_alert: bool

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "name": self.name,
            "n": self.n,
            "le_1ms": self.le_1ms,
            "le_3ms": self.le_3ms,
            "le_10ms": self.le_10ms,
            "p50_pass": self.p50_pass,
            "p99_pass": self.p99_pass,
            "p999_alert": self.p999_alert,
        }
        _assert_public(out)
        return out


@dataclass(frozen=True)
class HistogramGate:
    valid_n: int
    warmup_n: int
    invalid_clock_n: int
    missing_n: int
    rejected_before_write_n: int
    required_valid_n: int
    required_warmup_n: int
    valid_count_complete: bool
    warmup_complete: bool
    series: tuple[SeriesCounts, ...]
    p50_pass: bool
    p99_pass: bool
    p999_alert: bool
    report_failed: bool
    local_descriptive_only: bool
    target_vps_gate_eligible: bool

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": SCHEMA_VERSION,
            "valid_n": self.valid_n,
            "warmup_n": self.warmup_n,
            "invalid_clock_n": self.invalid_clock_n,
            "missing_n": self.missing_n,
            "rejected_before_write_n": self.rejected_before_write_n,
            "required_valid_n": self.required_valid_n,
            "required_warmup_n": self.required_warmup_n,
            "valid_count_complete": self.valid_count_complete,
            "warmup_complete": self.warmup_complete,
            "series": [item.to_public_dict() for item in self.series],
            "p50_pass": self.p50_pass,
            "p99_pass": self.p99_pass,
            "p999_alert": self.p999_alert,
            "report_failed": self.report_failed,
            "local_descriptive_only": self.local_descriptive_only,
            "target_vps_gate_eligible": self.target_vps_gate_eligible,
        }
        _assert_public(out)
        return out


def _nearest_rank_counts(raw: Sequence[int], n: int) -> dict[str, Any]:
    le_1 = sum(1 for item in raw if item <= THRESHOLD_1MS_NS)
    le_3 = sum(1 for item in raw if item <= THRESHOLD_3MS_NS)
    le_10 = sum(1 for item in raw if item <= THRESHOLD_10MS_NS)
    if n <= 0:
        return {
            "le_1ms": le_1,
            "le_3ms": le_3,
            "le_10ms": le_10,
            "p50_pass": False,
            "p99_pass": False,
            "p999_alert": True,
        }
    p50_need = math.ceil(0.50 * n)
    p99_need = math.ceil(0.99 * n)
    p999_need = math.ceil(0.999 * n)
    return {
        "le_1ms": le_1,
        "le_3ms": le_3,
        "le_10ms": le_10,
        "p50_pass": le_1 >= p50_need,
        "p99_pass": le_3 >= p99_need,
        "p999_alert": le_10 < p999_need,
    }


class LatencyHistogram:
    """Raw-sample histogram. Merge concatenates samples and recounts. Never percentiles."""

    def __init__(
        self,
        *,
        required_valid_n: int = DEFAULT_COUNTED_N,
        required_warmup_n: int = DEFAULT_WARMUP_N,
    ) -> None:
        self.required_valid_n = _require_int(
            required_valid_n, reason="invalid_histogram"
        )
        self.required_warmup_n = _require_int(
            required_warmup_n, reason="invalid_histogram"
        )
        self.valid_n = 0
        self.warmup_n = 0
        self.invalid_clock_n = 0
        self.missing_n = 0
        self.rejected_before_write_n = 0
        self._raw: dict[str, list[int]] = {name: [] for name in LATENCY_SERIES}

    @property
    def raw(self) -> dict[str, tuple[int, ...]]:
        return {name: tuple(values) for name, values in self._raw.items()}

    def add(self, sample: ProbeSample) -> None:
        if not isinstance(sample, ProbeSample):
            raise ShadowError("invalid_histogram")
        if sample.warmup:
            self.warmup_n += 1
            return
        if sample.rejected_before_write:
            self.rejected_before_write_n += 1
            return
        if sample.invalid_clock:
            self.invalid_clock_n += 1
            return
        if sample.missing:
            self.missing_n += 1
            return
        if not sample.valid:
            raise ShadowError("invalid_histogram")
        fields = (
            sample.signal_to_first_write_ns,
            sample.bybit_write_latency_ns,
            sample.okx_write_latency_ns,
            sample.dual_leg_write_ns,
        )
        signal, bybit, okx, dual = fields
        if signal is None or bybit is None or okx is None or dual is None:
            self.missing_n += 1
            return
        if signal < 0 or bybit < 0 or okx < 0 or dual < 0:
            self.missing_n += 1
            return
        self.valid_n += 1
        self._raw["signal_to_first_write_ns"].append(signal)
        self._raw["bybit_write_latency_ns"].append(bybit)
        self._raw["okx_write_latency_ns"].append(okx)
        self._raw["dual_leg_write_ns"].append(dual)

    def merge(self, other: "LatencyHistogram") -> "LatencyHistogram":
        if not isinstance(other, LatencyHistogram):
            raise ShadowError("invalid_histogram")
        if (
            self.required_valid_n != other.required_valid_n
            or self.required_warmup_n != other.required_warmup_n
        ):
            raise ShadowError("invalid_histogram")
        out = LatencyHistogram(
            required_valid_n=self.required_valid_n,
            required_warmup_n=self.required_warmup_n,
        )
        out.valid_n = self.valid_n + other.valid_n
        out.warmup_n = self.warmup_n + other.warmup_n
        out.invalid_clock_n = self.invalid_clock_n + other.invalid_clock_n
        out.missing_n = self.missing_n + other.missing_n
        out.rejected_before_write_n = (
            self.rejected_before_write_n + other.rejected_before_write_n
        )
        for name in LATENCY_SERIES:
            out._raw[name].extend(self._raw[name])
            out._raw[name].extend(other._raw[name])
        return out

    def gate(self) -> HistogramGate:
        series: list[SeriesCounts] = []
        for name in LATENCY_SERIES:
            raw = self._raw[name]
            counted = _nearest_rank_counts(raw, self.valid_n)
            series.append(
                SeriesCounts(
                    name=name,
                    n=self.valid_n,
                    le_1ms=counted["le_1ms"],
                    le_3ms=counted["le_3ms"],
                    le_10ms=counted["le_10ms"],
                    p50_pass=counted["p50_pass"],
                    p99_pass=counted["p99_pass"],
                    p999_alert=counted["p999_alert"],
                )
            )
        p50 = all(item.p50_pass for item in series) if series else False
        p99 = all(item.p99_pass for item in series) if series else False
        p999_alert = any(item.p999_alert for item in series)
        valid_count_complete = self.valid_n >= self.required_valid_n
        warmup_complete = self.warmup_n >= self.required_warmup_n
        report_failed = (
            self.invalid_clock_n != 0
            or self.missing_n != 0
            or self.valid_n == 0
            or not valid_count_complete
            or not warmup_complete
            or not p50
            or not p99
        )
        return HistogramGate(
            valid_n=self.valid_n,
            warmup_n=self.warmup_n,
            invalid_clock_n=self.invalid_clock_n,
            missing_n=self.missing_n,
            rejected_before_write_n=self.rejected_before_write_n,
            required_valid_n=self.required_valid_n,
            required_warmup_n=self.required_warmup_n,
            valid_count_complete=valid_count_complete,
            warmup_complete=warmup_complete,
            series=tuple(series),
            p50_pass=p50,
            p99_pass=p99,
            p999_alert=p999_alert,
            report_failed=report_failed,
            local_descriptive_only=True,
            target_vps_gate_eligible=False,
        )


class ShadowHotPath:
    """Transport hot-path probe. Never calls engine submit or mutates WAL/FSM."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        clock: Callable[[], int],
        wall_ms: Optional[Callable[[], int]] = None,
        warmup_n: int = DEFAULT_WARMUP_N,
        histogram: Optional[LatencyHistogram] = None,
        cache: Optional[InstrumentCache] = None,
        bybit_sink: Optional[NullTradeSink] = None,
        okx_sink: Optional[NullTradeSink] = None,
    ) -> None:
        if not isinstance(loop, asyncio.AbstractEventLoop):
            raise ShadowError("invalid_clocks")
        if not callable(clock):
            raise ShadowError("invalid_clocks")
        warmup = _require_int(warmup_n, reason="invalid_intent")
        self._loop = loop
        self._clock = clock
        self._warmup_n = warmup
        self._attempt = 0
        self._probe_queue_depth = 0
        self._lifecycle_drops = 0
        self._histogram = histogram if histogram is not None else LatencyHistogram()
        self._cache = cache if cache is not None else canonical_instrument_cache()
        self._bybit = bybit_sink if bybit_sink is not None else NullTradeSink(loop, clock)
        self._okx = okx_sink if okx_sink is not None else NullTradeSink(loop, clock)
        self._transport = ExecutionTransport(
            loop,
            bybit_socket=self._bybit,
            okx_socket=self._okx,
            finalize_frame=_shadow_hmac_finalizer,
            monotonic_ns=clock,
            wall_ms=wall_ms if wall_ms is not None else (lambda: 1_700_000_000_000),
        )

    @property
    def histogram(self) -> LatencyHistogram:
        return self._histogram

    @property
    def bybit_sink(self) -> NullTradeSink:
        return self._bybit

    @property
    def okx_sink(self) -> NullTradeSink:
        return self._okx

    @property
    def probe_queue_depth(self) -> int:
        return self._probe_queue_depth

    @property
    def lifecycle_drops(self) -> int:
        return self._lifecycle_drops

    async def probe(self, intent: TradeIntent) -> ProbeSample:
        if not _intent_is_canonical(intent):
            raise ShadowError("noncanonical_intent")
        self._attempt += 1
        warmup = self._attempt <= self._warmup_n
        self._probe_queue_depth += 1
        try:
            try:
                now = _require_int(self._clock())
                plans = _deterministic_plans(intent)
                prepared = prepare_dual_leg(intent, plans, self._cache, now_mono_ns=now)
            except TransportError as exc:
                sample = _rejected_or_clock_sample(intent, exc.reason_code, warmup=warmup)
                self._histogram.add(sample)
                if not warmup and (sample.rejected_before_write or sample.invalid_clock):
                    self._lifecycle_drops += 1
                return sample
            except ShadowError:
                raise
            try:
                result = await self._transport.dispatch(prepared)
            except TransportError as exc:
                sample = _rejected_or_clock_sample(
                    intent, exc.reason_code, warmup=warmup
                )
                self._histogram.add(sample)
                if not warmup and (sample.invalid_clock or sample.missing):
                    self._lifecycle_drops += 1
                return sample
            sample = classify_dispatch(result, warmup=warmup)
            both_completed = (
                result.bybit.outcome is WriteOutcome.WRITE_COMPLETED
                and result.okx.outcome is WriteOutcome.WRITE_COMPLETED
            )
            if (
                not warmup
                and not both_completed
                and not sample.rejected_before_write
            ):
                self._lifecycle_drops += 1
            self._histogram.add(sample)
            return sample
        finally:
            self._probe_queue_depth -= 1


def _rejected_or_clock_sample(intent: TradeIntent, reason_code: str, *, warmup: bool) -> ProbeSample:
    clock_bad = reason_code == "clock_regression"
    return ProbeSample(
        intent_id=intent.intent_id,
        warmup=warmup,
        rejected_before_write=not clock_bad,
        invalid_clock=clock_bad,
        missing=False,
        valid=False,
        signal_to_first_write_ns=None,
        bybit_write_latency_ns=None,
        okx_write_latency_ns=None,
        dual_leg_write_ns=None,
        dispatch_status=DispatchStatus.REJECTED.value,
        reason_code=reason_code,
    )


def _rss_bytes(usage: resource.struct_rusage) -> int:
    raw = int(usage.ru_maxrss)
    if sys.platform == "darwin":
        return raw
    return raw * 1024


@dataclass(frozen=True)
class ShadowHealthSample:
    schema_version: str
    event_loop_lag_ns: int
    cpu_user_s: float
    cpu_system_s: float
    rss_bytes: int
    probe_queue_depth: int
    lifecycle_drops: int
    unknown_state_count: int
    reconnect_applicable: bool
    reconnect_count: int
    wal_applicable: bool
    durable_lag_ns: Optional[int]
    collector_observed: bool
    collector_in_path: bool
    sampled_outside_write_await: bool
    local_descriptive_only: bool
    target_vps_gate_eligible: bool

    def __post_init__(self) -> None:
        if self.schema_version != HEALTH_SCHEMA_VERSION:
            raise ShadowError("invalid_health")
        object.__setattr__(self, "event_loop_lag_ns", _require_int(self.event_loop_lag_ns, reason="invalid_health"))
        object.__setattr__(self, "probe_queue_depth", _require_int(self.probe_queue_depth, reason="invalid_health"))
        object.__setattr__(self, "lifecycle_drops", _require_int(self.lifecycle_drops, reason="invalid_health"))
        object.__setattr__(self, "unknown_state_count", _require_int(self.unknown_state_count, reason="invalid_health"))
        object.__setattr__(self, "reconnect_count", _require_int(self.reconnect_count, reason="invalid_health"))

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": self.schema_version,
            "event_loop_lag_ns": self.event_loop_lag_ns,
            "cpu_user_s": self.cpu_user_s,
            "cpu_system_s": self.cpu_system_s,
            "rss_bytes": self.rss_bytes,
            "probe_queue_depth": self.probe_queue_depth,
            "lifecycle_drops": self.lifecycle_drops,
            "unknown_state_count": self.unknown_state_count,
            "reconnect": {
                "applicable": self.reconnect_applicable,
                "in_path": False,
                "count": self.reconnect_count,
            },
            "wal": {
                "applicable": self.wal_applicable,
                "in_path": False,
                "durable_lag_ns": self.durable_lag_ns,
            },
            "collector_baseline": {
                "observed": self.collector_observed,
                "in_path": self.collector_in_path,
            },
            "sampled_outside_write_await": self.sampled_outside_write_await,
            "local_descriptive_only": self.local_descriptive_only,
            "target_vps_gate_eligible": self.target_vps_gate_eligible,
        }
        _assert_public(out)
        return out


class ShadowHealth:
    """Observability snapshot. Sampling is never inside a transport write await."""

    def sample(
        self,
        *,
        hot_path: Optional[ShadowHotPath] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        event_loop_lag_ns: int = 0,
        unknown_state_count: int = 0,
    ) -> ShadowHealthSample:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        lag = _require_int(event_loop_lag_ns, reason="invalid_health")
        if loop is not None and not isinstance(loop, asyncio.AbstractEventLoop):
            raise ShadowError("invalid_health")
        depth = 0
        drops = 0
        if hot_path is not None:
            depth = hot_path.probe_queue_depth
            drops = hot_path.lifecycle_drops
        return ShadowHealthSample(
            schema_version=HEALTH_SCHEMA_VERSION,
            event_loop_lag_ns=lag,
            cpu_user_s=float(usage.ru_utime),
            cpu_system_s=float(usage.ru_stime),
            rss_bytes=_rss_bytes(usage),
            probe_queue_depth=depth,
            lifecycle_drops=drops,
            unknown_state_count=_require_int(unknown_state_count, reason="invalid_health"),
            reconnect_applicable=False,
            reconnect_count=0,
            wal_applicable=False,
            durable_lag_ns=None,
            collector_observed=False,
            collector_in_path=False,
            sampled_outside_write_await=True,
            local_descriptive_only=True,
            target_vps_gate_eligible=False,
        )


__all__ = [
    "DEFAULT_COUNTED_N",
    "DEFAULT_WARMUP_N",
    "HEALTH_SCHEMA_VERSION",
    "LATENCY_SERIES",
    "SCHEMA_VERSION",
    "HistogramGate",
    "LatencyHistogram",
    "NullTradeSink",
    "ProbeSample",
    "SeriesCounts",
    "ShadowError",
    "ShadowHealth",
    "ShadowHealthSample",
    "ShadowHotPath",
    "ShadowParityLane",
    "ShadowParityTick",
    "WouldSentDecisionReplica",
    "canonical_bridge_config",
    "canonical_instrument_cache",
    "classify_dispatch",
    "config_is_canonical",
]
