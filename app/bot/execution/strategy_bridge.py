"""Pure Gear 2.2 → execution-v2 strategy bridge. Stdlib plus frozen contracts.

Sibling kernel: calls ``decide_theta_k1`` at the existing decision point and
emits immutable ``TradeIntent`` objects. Does not reuse ``execute_decision``
or ``_execute_live_send``. Import and construction perform no filesystem,
network, database, Sentry, journal, socket, or engine submit
operations. Occupancy is derived only from execution proof plus an explicit
sidecar context; ACK / ARMED / DISPATCHING never create a position.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Sequence

from app.bot.execution.contracts import (
    SCHEMA_VERSION as CONTRACT_SCHEMA_VERSION,
    ContractValidationError,
    IntentAction,
    SpreadDirection,
    SpreadState,
    SpreadStatus,
    TradeIntent,
    canonical_decimal,
    decimal_to_canonical,
)
from app.bot.execution.state_machine import is_proven_flat, opens_allowed
from app.bot.theta_screener import ThetaSnapshot
from app.bot.theta_trade_manager import (
    DEFAULT_BOOK_DEPTH,
    DEFAULT_LIVE_CANARY_NOTIONAL_USDT,
    DEFAULT_THETA_THR,
    GEAR22_HTML_TOP30,
    POLICY_ID,
    OpenPosition,
    SlotState,
    ThetaDecision,
    build_feature_snapshot,
    decide_theta_k1,
    spread_for_side,
)
from research.gear22_backtest.params_frozen import DEFAULT_OBSERVE_PARAMS
from research.gear22_backtest.policy import PolicyParams

SCHEMA_VERSION = "bbot.execution.strategy_bridge.v1"
CONTEXT_SCHEMA_VERSION = "bbot.execution.strategy_context.v1"
PARITY_SCHEMA_VERSION = "bbot.execution.parity.v1"
INTENT_TTL_NS = 1_000_000_000
DEFAULT_NOTIONAL_USDT = Decimal(str(int(DEFAULT_LIVE_CANARY_NOTIONAL_USDT)))
CANARY_STAGE = "gear22_live_canary"
RISK_POLICY_REVISION = "risk.v1"

_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "api_key",
        "api_secret",
        "secret",
        "passphrase",
        "password",
        "authorization",
        "cookie",
        "set_cookie",
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

_BRIDGE_ERROR_CODES = frozenset(
    {
        "unclassified_non_match",
        "identity_mismatch",
        "ack_not_open",
        "missing_fill_spread",
        "invalid_decision",
        "invalid_spread_state",
        "intent_not_eligible",
        "forbidden_field",
        "invalid_context",
        "invalid_config",
        "invalid_clocks",
        "invalid_ids",
    }
)

_ACK_PENDING_STATUSES = frozenset({SpreadStatus.ARMED, SpreadStatus.DISPATCHING})


class StrategyBridgeError(ValueError):
    """Fail-closed bridge construction or classification error."""

    def __init__(self, reason_code: str) -> None:
        if reason_code not in _BRIDGE_ERROR_CODES:
            raise ValueError("unclassified_non_match")
        self.reason_code = reason_code
        super().__init__(reason_code)

    def to_public_dict(self) -> dict[str, Any]:
        out = {"schema_version": SCHEMA_VERSION, "reason_code": self.reason_code}
        _assert_public(out)
        return out


class DivergenceClass(str, Enum):
    MATCH = "match"
    ACK_NOT_OPEN = "ack_not_open"
    PENDING_INFLIGHT = "pending_inflight"
    FILL_MODEL = "fill_model"
    MISSING_OPEN_CONTEXT = "missing_open_context"
    ENGINE_GATE = "engine_gate"
    ID_SCHEME = "id_scheme"
    NOTIONAL_POLICY = "notional_policy"
    COIN_ORDER = "coin_order"
    PARAM_OVERRIDE = "param_override"
    SIZE_GATE = "size_gate"
    CLOSE_WHILE_NOT_OPEN = "close_while_not_open"


class SlotKind(str, Enum):
    FREE = "free"
    PENDING = "pending"
    OPEN = "open"
    FAIL_CLOSED = "fail_closed"
    STICKY = "sticky"
    CLOSING_CORRELATION = "closing_correlation"


def _norm_key(name: object) -> str:
    return str(name).strip().lower().replace("-", "_")


def _assert_public(node: object) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if _norm_key(key) in _FORBIDDEN_PUBLIC_KEYS:
                raise StrategyBridgeError("forbidden_field")
            _assert_public(value)
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            _assert_public(item)


def _require_finite_float(value: object, *, field: str) -> float:
    if isinstance(value, bool) or value is None:
        raise StrategyBridgeError("invalid_context")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise StrategyBridgeError("invalid_context") from exc
    if not math.isfinite(out):
        raise StrategyBridgeError("missing_fill_spread" if "spread" in field else "invalid_context")
    return out


def _optional_finite_float(value: object) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise StrategyBridgeError("invalid_context")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise StrategyBridgeError("invalid_context") from exc
    if not math.isfinite(out):
        return None
    return out


def _require_side(value: object) -> str:
    side = str(value).strip().lower()
    if side not in {"long", "short"}:
        raise StrategyBridgeError("invalid_context")
    return side


def _require_coin(value: object) -> str:
    coin = str(value).strip().upper()
    if not coin:
        raise StrategyBridgeError("invalid_context")
    return coin


def _require_id(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise StrategyBridgeError("invalid_context")
    return value


def _require_int(value: object, *, field: str, reason: str = "invalid_context") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StrategyBridgeError(reason)
    if value < 0:
        raise StrategyBridgeError(reason)
    return value


def _side_from_direction(direction: Optional[SpreadDirection]) -> Optional[str]:
    if direction is SpreadDirection.LONG:
        return "long"
    if direction is SpreadDirection.SHORT:
        return "short"
    return None


def _direction_from_side(side: str) -> SpreadDirection:
    normalized = _require_side(side)
    return SpreadDirection.LONG if normalized == "long" else SpreadDirection.SHORT


def _canon_num(value: object) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return "nan"
    return format(Decimal(str(number)).normalize(), "f")


@dataclass(frozen=True)
class BridgeClocks:
    monotonic_ns: Callable[[], int]
    wall_ns: Callable[[], int]


@dataclass(frozen=True)
class BridgeIds:
    new_intent_id: Callable[[], str]


@dataclass(frozen=True)
class ClockSnapshot:
    monotonic_ns: int
    wall_ns: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "monotonic_ns",
            _require_int(self.monotonic_ns, field="monotonic_ns", reason="invalid_clocks"),
        )
        object.__setattr__(
            self,
            "wall_ns",
            _require_int(self.wall_ns, field="wall_ns", reason="invalid_clocks"),
        )


@dataclass(frozen=True)
class BridgeConfig:
    run_id: str
    notional_usdt: Decimal = DEFAULT_NOTIONAL_USDT
    policy_version: str = POLICY_ID
    canary_stage: str = CANARY_STAGE
    risk_policy_revision: str = RISK_POLICY_REVISION
    coin_order: tuple[str, ...] = GEAR22_HTML_TOP30
    policy_params: PolicyParams = DEFAULT_OBSERVE_PARAMS
    intent_ttl_ns: int = INTENT_TTL_NS
    book_depth: int = DEFAULT_BOOK_DEPTH
    theta_thr: float = DEFAULT_THETA_THR

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise StrategyBridgeError("invalid_config")
        object.__setattr__(
            self,
            "notional_usdt",
            canonical_decimal(self.notional_usdt, field="notional_usdt", allow_zero=False),
        )
        order = tuple(str(coin).strip().upper() for coin in self.coin_order)
        if not order:
            raise StrategyBridgeError("invalid_config")
        object.__setattr__(self, "coin_order", order)
        if not isinstance(self.policy_params, PolicyParams):
            raise StrategyBridgeError("invalid_config")
        ttl = _require_int(self.intent_ttl_ns, field="intent_ttl_ns", reason="invalid_config")
        if ttl != INTENT_TTL_NS:
            raise StrategyBridgeError("invalid_config")
        object.__setattr__(self, "intent_ttl_ns", ttl)
        depth = _require_int(self.book_depth, field="book_depth", reason="invalid_config")
        if depth < 1:
            raise StrategyBridgeError("invalid_config")
        object.__setattr__(self, "book_depth", depth)
        if not isinstance(self.policy_version, str) or not self.policy_version:
            raise StrategyBridgeError("invalid_config")
        if not isinstance(self.canary_stage, str) or not self.canary_stage:
            raise StrategyBridgeError("invalid_config")
        if not isinstance(self.risk_policy_revision, str) or not self.risk_policy_revision:
            raise StrategyBridgeError("invalid_config")


@dataclass(frozen=True)
class InflightLatch:
    intent_id: str
    trade_id: str
    action: IntentAction
    coin: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_id", _require_id(self.intent_id, field="intent_id"))
        object.__setattr__(self, "trade_id", _require_id(self.trade_id, field="trade_id"))
        action = self.action if isinstance(self.action, IntentAction) else IntentAction(self.action)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "coin", _require_coin(self.coin))


@dataclass(frozen=True)
class _PendingOpenSignal:
    """In-memory open-signal latch. Survives inflight consume until proven OPEN."""

    intent_id: str
    trade_id: str
    coin: str
    side: str
    open_theta_1m: Optional[float]
    signal_snapshot_ref: str
    signal_mono_ns: int
    signal_wall_ns: int
    open_signal_ts_ms: int
    open_notional: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_id", _require_id(self.intent_id, field="intent_id"))
        object.__setattr__(self, "trade_id", _require_id(self.trade_id, field="trade_id"))
        if self.intent_id != self.trade_id:
            raise StrategyBridgeError("identity_mismatch")
        object.__setattr__(self, "coin", _require_coin(self.coin))
        object.__setattr__(self, "side", _require_side(self.side))
        theta = self.open_theta_1m
        if theta is not None:
            theta = _optional_finite_float(theta)
        object.__setattr__(self, "open_theta_1m", theta)
        object.__setattr__(
            self,
            "signal_snapshot_ref",
            _require_id(self.signal_snapshot_ref, field="signal_snapshot_ref"),
        )
        object.__setattr__(
            self, "signal_mono_ns", _require_int(self.signal_mono_ns, field="signal_mono_ns")
        )
        object.__setattr__(
            self, "signal_wall_ns", _require_int(self.signal_wall_ns, field="signal_wall_ns")
        )
        object.__setattr__(
            self,
            "open_signal_ts_ms",
            _require_int(self.open_signal_ts_ms, field="open_signal_ts_ms"),
        )
        object.__setattr__(
            self,
            "open_notional",
            canonical_decimal(self.open_notional, field="open_notional", allow_zero=False),
        )


@dataclass(frozen=True)
class OpenTradeContext:
    schema_version: str
    trade_id: str
    open_intent_id: str
    coin: str
    side: str
    open_signal_ts_ms: int
    open_fill_ts_ms: int
    fill_spread_pp: float
    open_theta_1m: Optional[float]
    open_notional: Decimal
    signal_snapshot_ref: str
    signal_mono_ns: int
    signal_wall_ns: int

    def __post_init__(self) -> None:
        if self.schema_version != CONTEXT_SCHEMA_VERSION:
            raise StrategyBridgeError("invalid_context")
        object.__setattr__(self, "trade_id", _require_id(self.trade_id, field="trade_id"))
        object.__setattr__(
            self, "open_intent_id", _require_id(self.open_intent_id, field="open_intent_id")
        )
        if self.trade_id != self.open_intent_id:
            raise StrategyBridgeError("identity_mismatch")
        object.__setattr__(self, "coin", _require_coin(self.coin))
        object.__setattr__(self, "side", _require_side(self.side))
        object.__setattr__(
            self,
            "open_signal_ts_ms",
            _require_int(self.open_signal_ts_ms, field="open_signal_ts_ms"),
        )
        object.__setattr__(
            self, "open_fill_ts_ms", _require_int(self.open_fill_ts_ms, field="open_fill_ts_ms")
        )
        object.__setattr__(
            self,
            "fill_spread_pp",
            _require_finite_float(self.fill_spread_pp, field="fill_spread_pp"),
        )
        theta = self.open_theta_1m
        if theta is not None:
            theta = _optional_finite_float(theta)
        object.__setattr__(self, "open_theta_1m", theta)
        object.__setattr__(
            self,
            "open_notional",
            canonical_decimal(self.open_notional, field="open_notional", allow_zero=False),
        )
        object.__setattr__(
            self,
            "signal_snapshot_ref",
            _require_id(self.signal_snapshot_ref, field="signal_snapshot_ref"),
        )
        object.__setattr__(
            self, "signal_mono_ns", _require_int(self.signal_mono_ns, field="signal_mono_ns")
        )
        object.__setattr__(
            self, "signal_wall_ns", _require_int(self.signal_wall_ns, field="signal_wall_ns")
        )

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": self.schema_version,
            "trade_id": self.trade_id,
            "open_intent_id": self.open_intent_id,
            "coin": self.coin,
            "side": self.side,
            "open_signal_ts_ms": self.open_signal_ts_ms,
            "open_fill_ts_ms": self.open_fill_ts_ms,
            "fill_spread_pp": _canon_num(self.fill_spread_pp),
            "open_theta_1m": _canon_num(self.open_theta_1m),
            "open_notional": decimal_to_canonical(self.open_notional),
            "signal_snapshot_ref": self.signal_snapshot_ref,
            "signal_mono_ns": self.signal_mono_ns,
            "signal_wall_ns": self.signal_wall_ns,
        }
        _assert_public(out)
        return out

    @classmethod
    def from_public_dict(cls, raw: Mapping[str, Any]) -> "OpenTradeContext":
        if not isinstance(raw, Mapping):
            raise StrategyBridgeError("invalid_context")
        try:
            return cls(
                schema_version=str(raw.get("schema_version") or ""),
                trade_id=str(raw.get("trade_id") or ""),
                open_intent_id=str(raw.get("open_intent_id") or ""),
                coin=str(raw.get("coin") or ""),
                side=str(raw.get("side") or ""),
                open_signal_ts_ms=raw["open_signal_ts_ms"],
                open_fill_ts_ms=raw["open_fill_ts_ms"],
                fill_spread_pp=raw["fill_spread_pp"],
                open_theta_1m=raw.get("open_theta_1m"),
                open_notional=raw["open_notional"],
                signal_snapshot_ref=str(raw.get("signal_snapshot_ref") or ""),
                signal_mono_ns=raw["signal_mono_ns"],
                signal_wall_ns=raw["signal_wall_ns"],
            )
        except StrategyBridgeError:
            raise
        except (KeyError, TypeError, ValueError, ContractValidationError) as exc:
            raise StrategyBridgeError("invalid_context") from exc


@dataclass(frozen=True)
class SlotProjection:
    position: Optional[OpenPosition]
    pending: bool
    allow_open: bool
    allow_close: bool
    slot_kind: SlotKind
    trade_id: Optional[str]
    ack_not_open: bool
    fail_closed_missing_context: bool
    engine_gated: bool
    sticky: bool

    def to_public_dict(self) -> dict[str, Any]:
        position = None
        if self.position is not None:
            position = {
                "trade_id": self.position.trade_id,
                "base_coin": self.position.base_coin,
                "side": self.position.side,
                "open_signal_ts_ms": self.position.open_signal_ts_ms,
                "open_fill_ts_ms": self.position.open_fill_ts_ms,
                "fill_spread_pp": _canon_num(self.position.fill_spread_pp),
                "open_notional": _canon_num(self.position.open_notional),
                "open_theta_1m": _canon_num(self.position.open_theta_1m),
            }
        out = {
            "position": position,
            "pending": self.pending,
            "allow_open": self.allow_open,
            "allow_close": self.allow_close,
            "slot_kind": self.slot_kind.value,
            "trade_id": self.trade_id,
            "ack_not_open": self.ack_not_open,
            "fail_closed_missing_context": self.fail_closed_missing_context,
            "engine_gated": self.engine_gated,
            "sticky": self.sticky,
        }
        _assert_public(out)
        return out


@dataclass(frozen=True)
class DivergenceFacts:
    open_without_context: bool = False
    ack_not_open: bool = False
    pending_inflight: bool = False
    close_while_not_open: bool = False
    engine_gated: bool = False
    size_gated: bool = False
    coin_order_diverged: bool = False
    param_override: bool = False
    notional_diverged: bool = False
    fill_model_diverged: bool = False
    id_scheme_diverged: bool = False
    decisions_match: bool = True


@dataclass(frozen=True)
class ParityRow:
    schema_version: str
    run_id: str
    trade_id: Optional[str]
    intent_id: Optional[str]
    action: str
    coin: str
    side: str
    signal_mono_ns: int
    signal_wall_ns: int
    signal_snapshot_ref: str
    policy_version: str
    risk_policy_revision: str
    canary_stage: str
    coin_rank: int
    slot_kind: str
    decision_action: str
    decision_reason: str
    size_ok: Optional[bool]
    notional_usdt: str
    spread_status: str
    divergence: str

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "trade_id": self.trade_id,
            "intent_id": self.intent_id,
            "action": self.action,
            "coin": self.coin,
            "side": self.side,
            "signal_mono_ns": self.signal_mono_ns,
            "signal_wall_ns": self.signal_wall_ns,
            "signal_snapshot_ref": self.signal_snapshot_ref,
            "policy_version": self.policy_version,
            "risk_policy_revision": self.risk_policy_revision,
            "canary_stage": self.canary_stage,
            "coin_rank": self.coin_rank,
            "slot_kind": self.slot_kind,
            "decision_action": self.decision_action,
            "decision_reason": self.decision_reason,
            "size_ok": self.size_ok,
            "notional_usdt": self.notional_usdt,
            "spread_status": self.spread_status,
            "divergence": self.divergence,
        }
        _assert_public(out)
        return out


@dataclass(frozen=True)
class BridgeTick:
    decision: ThetaDecision
    intent: Optional[TradeIntent]
    trade_id: Optional[str]
    parity: ParityRow
    divergence: DivergenceClass
    projection: SlotProjection
    snapshot_ref: str

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": SCHEMA_VERSION,
            "decision": {
                "action": self.decision.action,
                "base_coin": self.decision.base_coin,
                "side": self.decision.side,
                "reason": self.decision.reason,
                "reject_reason": self.decision.reject_reason,
            },
            "intent": None if self.intent is None else self.intent.to_public_dict(),
            "trade_id": self.trade_id,
            "parity": self.parity.to_public_dict(),
            "divergence": self.divergence.value,
            "projection": self.projection.to_public_dict(),
            "snapshot_ref": self.snapshot_ref,
        }
        _assert_public(out)
        return out


@dataclass(frozen=True)
class ContextRestoreResult:
    restored: bool
    context: Optional[OpenTradeContext]
    divergence: DivergenceClass
    reason: str

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": CONTEXT_SCHEMA_VERSION,
            "restored": self.restored,
            "context": None if self.context is None else self.context.to_public_dict(),
            "divergence": self.divergence.value,
            "reason": self.reason,
        }
        _assert_public(out)
        return out


def _stdlib_clocks() -> BridgeClocks:
    return BridgeClocks(monotonic_ns=time.monotonic_ns, wall_ns=time.time_ns)


def _stdlib_ids() -> BridgeIds:
    return BridgeIds(new_intent_id=lambda: str(uuid.uuid4()))


def _sample_clocks(clocks: BridgeClocks) -> ClockSnapshot:
    if not isinstance(clocks, BridgeClocks):
        raise StrategyBridgeError("invalid_clocks")
    return ClockSnapshot(monotonic_ns=int(clocks.monotonic_ns()), wall_ns=int(clocks.wall_ns()))


def _coin_rank(coin: str) -> int:
    try:
        return GEAR22_HTML_TOP30.index(str(coin).strip().upper()) + 1
    except ValueError:
        return 0


def _context_matches_open(spread_state: SpreadState, context: Optional[OpenTradeContext]) -> bool:
    if context is None or spread_state.status is not SpreadStatus.OPEN:
        return False
    if context.trade_id != spread_state.open_intent_id:
        return False
    if context.open_intent_id != spread_state.open_intent_id:
        return False
    if context.coin != spread_state.coin:
        return False
    return _side_from_direction(spread_state.direction) == context.side


def _position_from_context(context: OpenTradeContext) -> OpenPosition:
    return OpenPosition(
        trade_id=context.trade_id,
        base_coin=context.coin,
        side=context.side,
        open_signal_ts_ms=context.open_signal_ts_ms,
        open_fill_ts_ms=context.open_fill_ts_ms,
        open_fill_spread=context.fill_spread_pp,
        open_notional=float(context.open_notional),
        open_theta_1m=context.open_theta_1m,
        fill_spread_pp=context.fill_spread_pp,
    )


def _idle_or_flat(status: SpreadStatus) -> bool:
    return status in {SpreadStatus.IDLE, SpreadStatus.FLAT}


def should_clear_context(spread_state: SpreadState) -> bool:
    if spread_state.status is SpreadStatus.IDLE:
        return True
    return spread_state.status is SpreadStatus.FLAT and is_proven_flat(spread_state)


def fill_spread_from_quotes(
    quotes: Mapping[str, Mapping[str, Mapping[str, Any]]],
    coin: str,
    side: str,
) -> Optional[float]:
    """Public-book spread for the opened side. Never an ACK field."""
    books = quotes.get(str(coin).strip().upper()) or {}
    return spread_for_side(books.get("okx") or {}, books.get("bybit") or {}, side)


def project_slot(
    spread_state: SpreadState,
    context: Optional[OpenTradeContext],
    inflight: Optional[InflightLatch],
) -> SlotProjection:
    if not isinstance(spread_state, SpreadState):
        raise StrategyBridgeError("invalid_spread_state")
    status = spread_state.status
    inflight_pending = inflight is not None
    correlation_id = None
    if context is not None:
        correlation_id = context.trade_id
    elif spread_state.open_intent_id:
        correlation_id = spread_state.open_intent_id
    elif inflight is not None:
        correlation_id = inflight.trade_id

    if status is SpreadStatus.OPEN:
        if _context_matches_open(spread_state, context):
            assert context is not None
            close_inflight = (
                inflight is not None
                and inflight.action is IntentAction.CLOSE
                and inflight.trade_id == context.trade_id
            )
            if close_inflight:
                return SlotProjection(
                    position=_position_from_context(context),
                    pending=True,
                    allow_open=False,
                    allow_close=False,
                    slot_kind=SlotKind.PENDING,
                    trade_id=context.trade_id,
                    ack_not_open=False,
                    fail_closed_missing_context=False,
                    engine_gated=False,
                    sticky=False,
                )
            return SlotProjection(
                position=_position_from_context(context),
                pending=False,
                allow_open=False,
                allow_close=True,
                slot_kind=SlotKind.OPEN,
                trade_id=context.trade_id,
                ack_not_open=False,
                fail_closed_missing_context=False,
                engine_gated=False,
                sticky=False,
            )
        return SlotProjection(
            position=None,
            pending=True,
            allow_open=False,
            allow_close=False,
            slot_kind=SlotKind.FAIL_CLOSED,
            trade_id=spread_state.open_intent_id,
            ack_not_open=False,
            fail_closed_missing_context=True,
            engine_gated=False,
            sticky=False,
        )

    if status in _ACK_PENDING_STATUSES:
        return SlotProjection(
            position=None,
            pending=True,
            allow_open=False,
            allow_close=False,
            slot_kind=SlotKind.PENDING,
            trade_id=correlation_id,
            ack_not_open=True,
            fail_closed_missing_context=False,
            engine_gated=False,
            sticky=False,
        )

    if status is SpreadStatus.CLOSING:
        return SlotProjection(
            position=None,
            pending=True,
            allow_open=False,
            allow_close=False,
            slot_kind=SlotKind.CLOSING_CORRELATION,
            trade_id=correlation_id,
            ack_not_open=False,
            fail_closed_missing_context=False,
            engine_gated=True,
            sticky=False,
        )

    if status is SpreadStatus.HALTED:
        return SlotProjection(
            position=None,
            pending=True,
            allow_open=False,
            allow_close=False,
            slot_kind=SlotKind.STICKY,
            trade_id=correlation_id,
            ack_not_open=False,
            fail_closed_missing_context=False,
            engine_gated=True,
            sticky=True,
        )

    if status in {SpreadStatus.EXPOSURE_UNKNOWN, SpreadStatus.RECOVERING}:
        return SlotProjection(
            position=None,
            pending=True,
            allow_open=False,
            allow_close=False,
            slot_kind=SlotKind.PENDING,
            trade_id=correlation_id,
            ack_not_open=False,
            fail_closed_missing_context=False,
            engine_gated=True,
            sticky=False,
        )

    engine_blocked = not opens_allowed(spread_state)
    pending = bool(inflight_pending or engine_blocked)
    allow_open = bool(_idle_or_flat(status) and not pending and opens_allowed(spread_state))
    if status is SpreadStatus.FLAT and not is_proven_flat(spread_state):
        allow_open = False
        pending = True
        engine_blocked = True
    kind = SlotKind.PENDING if pending else SlotKind.FREE
    return SlotProjection(
        position=None,
        pending=pending,
        allow_open=allow_open,
        allow_close=False,
        slot_kind=kind,
        trade_id=inflight.trade_id if inflight is not None else None,
        ack_not_open=False,
        fail_closed_missing_context=False,
        engine_gated=engine_blocked,
        sticky=False,
    )


def commit_open_context(
    spread_state: SpreadState,
    candidate: OpenTradeContext,
) -> OpenTradeContext:
    if not isinstance(spread_state, SpreadState):
        raise StrategyBridgeError("invalid_spread_state")
    if not isinstance(candidate, OpenTradeContext):
        raise StrategyBridgeError("invalid_context")
    if spread_state.status is not SpreadStatus.OPEN:
        raise StrategyBridgeError("ack_not_open")
    if not _context_matches_open(spread_state, candidate):
        raise StrategyBridgeError("identity_mismatch")
    return candidate


def restore_context(
    spread_state: SpreadState,
    records: Sequence[OpenTradeContext | Mapping[str, Any]],
) -> ContextRestoreResult:
    if not isinstance(spread_state, SpreadState):
        raise StrategyBridgeError("invalid_spread_state")
    if spread_state.status is not SpreadStatus.OPEN:
        raise StrategyBridgeError("invalid_spread_state")
    parsed: list[OpenTradeContext] = []
    for item in records:
        if isinstance(item, OpenTradeContext):
            parsed.append(item)
        elif isinstance(item, Mapping):
            parsed.append(OpenTradeContext.from_public_dict(item))
        else:
            raise StrategyBridgeError("invalid_context")
    open_id = spread_state.open_intent_id
    matched = [item for item in parsed if item.trade_id == open_id]
    if not matched:
        return ContextRestoreResult(
            restored=False,
            context=None,
            divergence=DivergenceClass.MISSING_OPEN_CONTEXT,
            reason="missing_open_context",
        )
    context = matched[0]
    for other in matched[1:]:
        if other != context:
            raise StrategyBridgeError("invalid_context")
    if not _context_matches_open(spread_state, context):
        raise StrategyBridgeError("identity_mismatch")
    return ContextRestoreResult(
        restored=True,
        context=context,
        divergence=DivergenceClass.MATCH,
        reason="restored",
    )


def redacted_snapshot_hash(
    *,
    snapshots: Sequence[ThetaSnapshot],
    quotes: Mapping[str, Mapping[str, Mapping[str, Any]]],
    projection: SlotProjection,
    config: BridgeConfig,
    clocks: ClockSnapshot,
) -> str:
    ts_s = clocks.wall_ns // 1_000_000_000
    coins: list[dict[str, Any]] = []
    for coin in config.coin_order:
        feat = build_feature_snapshot(
            coin=coin,
            ts_s=ts_s,
            snapshots=snapshots,
            quotes=quotes,
        )
        if feat is None:
            coins.append({"coin": coin, "usable": False})
            continue
        coins.append(
            {
                "coin": feat.coin,
                "usable_long": feat.usable_long,
                "usable_short": feat.usable_short,
                "p50_1m_long": _canon_num(feat.p50_1m_long),
                "p50_1m_short": _canon_num(feat.p50_1m_short),
                "floor_long": _canon_num(feat.floor_long),
                "floor_short": _canon_num(feat.floor_short),
                "theta_1m_long": _canon_num(feat.theta_1m_long),
                "theta_1m_short": _canon_num(feat.theta_1m_short),
                "spread_last_long": _canon_num(feat.spread_last_long),
                "spread_last_short": _canon_num(feat.spread_last_short),
            }
        )
    params = config.policy_params
    payload = {
        "policy_id": config.policy_version,
        "coin_order": list(config.coin_order),
        "slot_kind": projection.slot_kind.value,
        "pending": projection.pending,
        "trade_id": projection.trade_id,
        "notional_usdt": decimal_to_canonical(config.notional_usdt),
        "params": {
            "theta_open": _canon_num(params.theta_open),
            "p50_open": _canon_num(params.p50_open),
            "min_profit_pp": _canon_num(params.min_profit_pp),
            "fee_round_trip_pp": _canon_num(params.fee_round_trip_pp),
            "min_spread_open": _canon_num(params.min_spread_open),
            "min_theta_close": _canon_num(params.min_theta_close),
        },
        "coins": coins,
    }
    _assert_public(payload)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "h" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def classify_divergence(facts: DivergenceFacts) -> DivergenceClass:
    ordered = (
        (facts.open_without_context, DivergenceClass.MISSING_OPEN_CONTEXT),
        (facts.ack_not_open, DivergenceClass.ACK_NOT_OPEN),
        (facts.pending_inflight, DivergenceClass.PENDING_INFLIGHT),
        (facts.close_while_not_open, DivergenceClass.CLOSE_WHILE_NOT_OPEN),
        (facts.engine_gated, DivergenceClass.ENGINE_GATE),
        (facts.size_gated, DivergenceClass.SIZE_GATE),
        (facts.coin_order_diverged, DivergenceClass.COIN_ORDER),
        (facts.param_override, DivergenceClass.PARAM_OVERRIDE),
        (facts.notional_diverged, DivergenceClass.NOTIONAL_POLICY),
        (facts.fill_model_diverged, DivergenceClass.FILL_MODEL),
        (facts.id_scheme_diverged, DivergenceClass.ID_SCHEME),
    )
    hits = tuple(code for flag, code in ordered if flag)
    if len(hits) > 1:
        raise StrategyBridgeError("unclassified_non_match")
    if len(hits) == 1:
        return hits[0]
    if facts.decisions_match:
        return DivergenceClass.MATCH
    raise StrategyBridgeError("unclassified_non_match")


def _hard_intent_blocked(config: BridgeConfig, fill_model_diverged: bool) -> bool:
    coin_order, params, notional = _config_divergence_flags(config)
    return bool(coin_order or params or notional or fill_model_diverged)


def _open_signal_ts_ms(
    snapshots: Sequence[ThetaSnapshot],
    coin: str,
    side: str,
) -> int:
    want_coin = _require_coin(coin)
    want_side = _require_side(side)
    found: Optional[ThetaSnapshot] = None
    for snap in snapshots:
        snap_coin = str(getattr(snap, "base_coin", "") or "").strip().upper()
        snap_side = str(getattr(snap, "side", "") or "").strip().lower()
        if snap_coin == want_coin and snap_side == want_side:
            found = snap
    if found is None:
        raise StrategyBridgeError("invalid_decision")
    return _require_int(found.ts_ms, field="open_signal_ts_ms")


def _config_divergence_flags(config: BridgeConfig) -> tuple[bool, bool, bool]:
    coin_order = tuple(config.coin_order) != GEAR22_HTML_TOP30
    params = config.policy_params != DEFAULT_OBSERVE_PARAMS
    notional = config.notional_usdt != DEFAULT_NOTIONAL_USDT
    return coin_order, params, notional


def _size_ok(decision: ThetaDecision) -> Optional[bool]:
    info = decision.size_info
    if not isinstance(info, Mapping):
        return None
    flag = info.get("size_ok")
    return bool(flag) if isinstance(flag, bool) else None


def _facts_for_tick(
    *,
    projection: SlotProjection,
    decision: ThetaDecision,
    intent: Optional[TradeIntent],
    trade_id: Optional[str],
    config: BridgeConfig,
    inflight_blocked: bool,
    fill_model_diverged: bool = False,
) -> DivergenceFacts:
    coin_order, params, notional = _config_divergence_flags(config)
    id_scheme = False
    if intent is not None:
        if intent.action is IntentAction.OPEN and trade_id != intent.intent_id:
            id_scheme = True
        if intent.action is IntentAction.CLOSE and trade_id == intent.intent_id:
            id_scheme = True
    close_blocked = (
        decision.action == "close"
        and not projection.allow_close
        and not projection.fail_closed_missing_context
        and not projection.ack_not_open
        and not projection.engine_gated
        and not inflight_blocked
    )
    return DivergenceFacts(
        open_without_context=projection.fail_closed_missing_context,
        ack_not_open=projection.ack_not_open,
        pending_inflight=inflight_blocked,
        close_while_not_open=close_blocked,
        engine_gated=projection.engine_gated and not projection.fail_closed_missing_context,
        size_gated=decision.reject_reason == "insufficient_size",
        coin_order_diverged=coin_order,
        param_override=params,
        notional_diverged=notional,
        fill_model_diverged=fill_model_diverged,
        id_scheme_diverged=id_scheme,
        decisions_match=True,
    )


def build_trade_intent(
    decision: ThetaDecision,
    projection: SlotProjection,
    clocks: BridgeClocks | ClockSnapshot,
    ids: BridgeIds,
    config: BridgeConfig,
    *,
    snapshot_ref: Optional[str] = None,
) -> TradeIntent:
    if not isinstance(decision, ThetaDecision):
        raise StrategyBridgeError("invalid_decision")
    if decision.action not in {"open", "close"}:
        raise StrategyBridgeError("invalid_decision")
    if not isinstance(ids, BridgeIds):
        raise StrategyBridgeError("invalid_ids")
    if not isinstance(config, BridgeConfig):
        raise StrategyBridgeError("invalid_config")
    sampled = clocks if isinstance(clocks, ClockSnapshot) else _sample_clocks(clocks)
    if decision.action == "open":
        if not projection.allow_open:
            raise StrategyBridgeError("intent_not_eligible")
        intent_id = ids.new_intent_id()
        direction = _direction_from_side(decision.side)
        coin = _require_coin(decision.base_coin)
    else:
        if not projection.allow_close or projection.position is None:
            raise StrategyBridgeError("intent_not_eligible")
        intent_id = ids.new_intent_id()
        direction = _direction_from_side(projection.position.side)
        coin = _require_coin(projection.position.base_coin)
        if _require_coin(decision.base_coin) != coin:
            raise StrategyBridgeError("identity_mismatch")
        if _require_side(decision.side) != projection.position.side:
            raise StrategyBridgeError("identity_mismatch")
    ref = snapshot_ref or intent_id
    try:
        return TradeIntent(
            schema_version=CONTRACT_SCHEMA_VERSION,
            intent_id=intent_id,
            run_id=config.run_id,
            policy_version=config.policy_version,
            action=IntentAction.OPEN if decision.action == "open" else IntentAction.CLOSE,
            spread_direction=direction,
            coin=coin,
            notional_usdt=config.notional_usdt,
            signal_mono_ns=sampled.monotonic_ns,
            signal_wall_ns=sampled.wall_ns,
            expiry_mono_ns=sampled.monotonic_ns + config.intent_ttl_ns,
            signal_snapshot_ref=ref,
            canary_stage=config.canary_stage,
            risk_policy_revision=config.risk_policy_revision,
        )
    except ContractValidationError as exc:
        raise StrategyBridgeError("invalid_decision") from exc


def _parity_row(
    *,
    config: BridgeConfig,
    decision: ThetaDecision,
    intent: Optional[TradeIntent],
    trade_id: Optional[str],
    clocks: ClockSnapshot,
    snapshot_ref: str,
    projection: SlotProjection,
    spread_state: SpreadState,
    divergence: DivergenceClass,
) -> ParityRow:
    action = "skip"
    coin = str(decision.base_coin or "")
    side = str(decision.side or "")
    intent_id = None
    if intent is not None:
        action = intent.action.value
        coin = intent.coin
        side = intent.spread_direction.value
        intent_id = intent.intent_id
    elif decision.action in {"open", "close"}:
        action = decision.action
        coin = str(decision.base_coin or "")
        side = str(decision.side or "")
    return ParityRow(
        schema_version=PARITY_SCHEMA_VERSION,
        run_id=config.run_id,
        trade_id=trade_id,
        intent_id=intent_id,
        action=action,
        coin=coin,
        side=side,
        signal_mono_ns=clocks.monotonic_ns,
        signal_wall_ns=clocks.wall_ns,
        signal_snapshot_ref=snapshot_ref,
        policy_version=config.policy_version,
        risk_policy_revision=config.risk_policy_revision,
        canary_stage=config.canary_stage,
        coin_rank=_coin_rank(coin),
        slot_kind=projection.slot_kind.value,
        decision_action=decision.action,
        decision_reason=decision.reason,
        size_ok=_size_ok(decision),
        notional_usdt=decimal_to_canonical(config.notional_usdt),
        spread_status=spread_state.status.value,
        divergence=divergence.value,
    )


class Gear22StrategyBridge:
    """In-memory Gear 2.2 bridge. No I/O, no submit, no runtime wiring."""

    def __init__(
        self,
        *,
        run_id: str,
        config: Optional[BridgeConfig] = None,
        clocks: Optional[BridgeClocks] = None,
        ids: Optional[BridgeIds] = None,
    ) -> None:
        self._config = config or BridgeConfig(run_id=run_id)
        if self._config.run_id != run_id:
            raise StrategyBridgeError("invalid_config")
        self._clocks = clocks or _stdlib_clocks()
        self._ids = ids or _stdlib_ids()
        self._context: Optional[OpenTradeContext] = None
        self._inflight: Optional[InflightLatch] = None
        self._pending_open: Optional[_PendingOpenSignal] = None

    @property
    def config(self) -> BridgeConfig:
        return self._config

    @property
    def context(self) -> Optional[OpenTradeContext]:
        return self._context

    @property
    def inflight(self) -> Optional[InflightLatch]:
        return self._inflight

    def project_slot(self, spread_state: SpreadState) -> SlotProjection:
        return project_slot(spread_state, self._context, self._inflight)

    def mark_inflight(self, intent: TradeIntent, trade_id: str) -> InflightLatch:
        latch = InflightLatch(
            intent_id=intent.intent_id,
            trade_id=trade_id,
            action=intent.action,
            coin=intent.coin,
        )
        self._inflight = latch
        return latch

    def clear_inflight_on_reject(self, intent_id: str) -> None:
        if self._inflight is not None and self._inflight.intent_id == intent_id:
            self._inflight = None
        if self._pending_open is not None and self._pending_open.intent_id == intent_id:
            self._pending_open = None

    def consume_inflight_on_transition(self, spread_state: SpreadState) -> None:
        if self._inflight is None:
            return
        if self._inflight.intent_id in spread_state.accepted_intent_ids:
            self._inflight = None

    def commit_open_context(
        self, spread_state: SpreadState, candidate: OpenTradeContext
    ) -> OpenTradeContext:
        self._assert_run_id(spread_state)
        committed = commit_open_context(spread_state, candidate)
        self._context = committed
        if self._inflight is not None and self._inflight.trade_id == committed.trade_id:
            self._inflight = None
        if self._pending_open is not None and self._pending_open.trade_id == committed.trade_id:
            self._pending_open = None
        return committed

    def commit_proven_open(
        self,
        spread_state: SpreadState,
        quotes: Mapping[str, Mapping[str, Mapping[str, Any]]],
    ) -> OpenTradeContext:
        """Commit sidecar context from preserved open-signal fields after proven OPEN.

        Fill spread is the public-book observation at OPEN proof. ACK / ARMED /
        DISPATCHING fail closed. Signal fields come from the original open tick,
        never from current books or invented fixtures.
        """
        if not isinstance(spread_state, SpreadState):
            raise StrategyBridgeError("invalid_spread_state")
        self._assert_run_id(spread_state)
        if spread_state.status is not SpreadStatus.OPEN:
            raise StrategyBridgeError("ack_not_open")
        pending = self._pending_open
        if pending is None:
            raise StrategyBridgeError("invalid_context")
        candidate = OpenTradeContext(
            schema_version=CONTEXT_SCHEMA_VERSION,
            trade_id=pending.trade_id,
            open_intent_id=pending.intent_id,
            coin=pending.coin,
            side=pending.side,
            open_signal_ts_ms=pending.open_signal_ts_ms,
            open_fill_ts_ms=_sample_clocks(self._clocks).wall_ns // 1_000_000,
            fill_spread_pp=self._fill_spread_for_pending(quotes, pending),
            open_theta_1m=pending.open_theta_1m,
            open_notional=pending.open_notional,
            signal_snapshot_ref=pending.signal_snapshot_ref,
            signal_mono_ns=pending.signal_mono_ns,
            signal_wall_ns=pending.signal_wall_ns,
        )
        return self.commit_open_context(spread_state, candidate)

    def restore_context(
        self,
        spread_state: SpreadState,
        records: Sequence[OpenTradeContext | Mapping[str, Any]],
    ) -> ContextRestoreResult:
        if not isinstance(spread_state, SpreadState):
            raise StrategyBridgeError("invalid_spread_state")
        self._assert_run_id(spread_state)
        result = restore_context(spread_state, records)
        if result.restored:
            self._context = result.context
        return result

    def _assert_run_id(self, spread_state: SpreadState) -> None:
        if spread_state.run_id != self._config.run_id:
            raise StrategyBridgeError("invalid_spread_state")

    def _fill_spread_for_pending(
        self,
        quotes: Mapping[str, Mapping[str, Mapping[str, Any]]],
        pending: _PendingOpenSignal,
    ) -> float:
        fill = fill_spread_from_quotes(quotes, pending.coin, pending.side)
        if fill is None:
            raise StrategyBridgeError("missing_fill_spread")
        return fill

    def build_trade_intent(
        self,
        decision: ThetaDecision,
        projection: SlotProjection,
        clocks: BridgeClocks | ClockSnapshot,
        ids: Optional[BridgeIds] = None,
        config: Optional[BridgeConfig] = None,
    ) -> TradeIntent:
        return build_trade_intent(
            decision,
            projection,
            clocks,
            ids or self._ids,
            config or self._config,
        )

    def observe(
        self,
        snapshots: Sequence[ThetaSnapshot],
        quotes: Mapping[str, Mapping[str, Mapping[str, Any]]],
        spread_state: SpreadState,
        *,
        fill_model_diverged: bool = False,
    ) -> BridgeTick:
        if not isinstance(spread_state, SpreadState):
            raise StrategyBridgeError("invalid_spread_state")
        self._assert_run_id(spread_state)
        self.consume_inflight_on_transition(spread_state)
        if should_clear_context(spread_state):
            self._context = None
            if spread_state.status is SpreadStatus.FLAT:
                self._pending_open = None
        had_inflight = self._inflight is not None
        projection = project_slot(spread_state, self._context, self._inflight)
        clocks = _sample_clocks(self._clocks)
        snapshot_ref = redacted_snapshot_hash(
            snapshots=snapshots,
            quotes=quotes,
            projection=projection,
            config=self._config,
            clocks=clocks,
        )
        decision = decide_theta_k1(
            snapshots,
            slot=SlotState(k=1, position=projection.position, pending=projection.pending),
            thr=self._config.theta_thr,
            quotes=quotes,
            notional_usdt=float(self._config.notional_usdt),
            book_depth=self._config.book_depth,
            coin_order=self._config.coin_order,
            policy_params=self._config.policy_params,
        )
        inflight_blocked = bool(
            had_inflight
            and projection.pending
            and not projection.engine_gated
            and not projection.ack_not_open
            and not projection.fail_closed_missing_context
        )
        hard_blocked = _hard_intent_blocked(self._config, fill_model_diverged)
        intent: Optional[TradeIntent] = None
        trade_id = projection.trade_id
        if (
            not hard_blocked
            and decision.action == "open"
            and projection.allow_open
        ):
            intent = build_trade_intent(
                decision,
                projection,
                clocks,
                self._ids,
                self._config,
                snapshot_ref=snapshot_ref,
            )
            trade_id = intent.intent_id
            self.mark_inflight(intent, trade_id)
            self._pending_open = _PendingOpenSignal(
                intent_id=intent.intent_id,
                trade_id=trade_id,
                coin=intent.coin,
                side=decision.side,
                open_theta_1m=decision.theta_1m,
                signal_snapshot_ref=snapshot_ref,
                signal_mono_ns=clocks.monotonic_ns,
                signal_wall_ns=clocks.wall_ns,
                open_signal_ts_ms=_open_signal_ts_ms(
                    snapshots, intent.coin, decision.side
                ),
                open_notional=self._config.notional_usdt,
            )
        elif (
            not hard_blocked
            and decision.action == "close"
            and projection.allow_close
        ):
            intent = build_trade_intent(
                decision,
                projection,
                clocks,
                self._ids,
                self._config,
                snapshot_ref=snapshot_ref,
            )
            trade_id = projection.position.trade_id if projection.position else projection.trade_id
            if trade_id is None:
                raise StrategyBridgeError("identity_mismatch")
            self.mark_inflight(intent, trade_id)
        facts = _facts_for_tick(
            projection=projection,
            decision=decision,
            intent=intent,
            trade_id=trade_id,
            config=self._config,
            inflight_blocked=inflight_blocked,
            fill_model_diverged=fill_model_diverged,
        )
        divergence = classify_divergence(facts)
        parity = _parity_row(
            config=self._config,
            decision=decision,
            intent=intent,
            trade_id=trade_id,
            clocks=clocks,
            snapshot_ref=snapshot_ref,
            projection=projection,
            spread_state=spread_state,
            divergence=divergence,
        )
        return BridgeTick(
            decision=decision,
            intent=intent,
            trade_id=trade_id,
            parity=parity,
            divergence=divergence,
            projection=projection,
            snapshot_ref=snapshot_ref,
        )


__all__ = [
    "CANARY_STAGE",
    "CONTEXT_SCHEMA_VERSION",
    "DEFAULT_NOTIONAL_USDT",
    "INTENT_TTL_NS",
    "PARITY_SCHEMA_VERSION",
    "RISK_POLICY_REVISION",
    "SCHEMA_VERSION",
    "BridgeClocks",
    "BridgeConfig",
    "BridgeIds",
    "BridgeTick",
    "ClockSnapshot",
    "ContextRestoreResult",
    "DivergenceClass",
    "DivergenceFacts",
    "Gear22StrategyBridge",
    "InflightLatch",
    "OpenTradeContext",
    "ParityRow",
    "SlotKind",
    "SlotProjection",
    "StrategyBridgeError",
    "build_trade_intent",
    "classify_divergence",
    "commit_open_context",
    "fill_spread_from_quotes",
    "project_slot",
    "redacted_snapshot_hash",
    "restore_context",
    "should_clear_context",
]
