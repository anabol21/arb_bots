"""Pure execution-v2 recovery planner. Stdlib plus frozen EV2 contracts.

No sockets, secrets, REST, WAL drain, or live send. Recovery is not a
TradeIntent action. The planner emits one finite venue decision at a time.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Sequence

from app.bot.execution.contracts import (
    LegPlan,
    LegState,
    LegStatus,
    SpreadState,
    SpreadStatus,
    Venue,
    ack_timeout_is_unresolved,
    canonical_decimal,
    flat_evidence_is_complete,
    leg_open_qty,
    leg_quantity_exceeds_plan,
    observed_position_contradicts_fill,
)
from app.bot.execution.state_machine import (
    is_proven_flat,
    needs_reconciliation,
    opens_allowed,
)
from app.bot.execution.transport import (
    FrozenStaticFrame,
    VenueWriteEvidence,
)

SCHEMA_VERSION = "bbot.execution.recovery.v1"
MAX_RECOVERY_STEPS = 8
RECOVERY_WORST_CASE_EVENTS = 4

RECOVERY_REASON_CODES = frozenset(
    {
        "wait_reseed",
        "peer_working",
        "timeout_unresolved",
        "flatten_required",
        "prove_flat",
        "flatten_failed",
        "cancel_failed",
        "reseed_failed",
        "restart_unproven",
        "ambiguous_exposure",
        "qty_conflict",
        "stream_blocked",
        "trade_socket_not_ready",
        "wal_capacity",
        "wal_unhealthy",
        "ownership_not_held",
        "halt",
        "nothing_to_do",
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


def _norm_key(name: object) -> str:
    return str(name).strip().lower().replace("-", "_")


def _assert_public(node: object) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if _norm_key(key) in _FORBIDDEN_PUBLIC_KEYS:
                raise RecoveryError("forbidden_field")
            _assert_public(value)
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            _assert_public(item)


def _require_reason(code: Optional[str]) -> str:
    if code in RECOVERY_REASON_CODES:
        return code
    return "ambiguous_exposure"


class RecoveryError(ValueError):
    """Fail-closed recovery construction error. Public view is redacted."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = _require_reason(reason_code)
        super().__init__(self.reason_code)

    def to_public_dict(self) -> dict[str, Any]:
        out = {"schema_version": SCHEMA_VERSION, "reason_code": self.reason_code}
        _assert_public(out)
        return out

    def __repr__(self) -> str:
        return f"RecoveryError(reason_code={self.reason_code!r})"


class RecoveryActionKind(str, Enum):
    WAIT_RESEED = "wait_reseed"
    CANCEL_PEER = "cancel_peer"
    FLATTEN_FILLED = "flatten_filled"
    PROVE_FLAT = "prove_flat"
    HALT = "halt"
    NOTHING = "nothing"


class VenueActionKind(str, Enum):
    PLACE = "place"
    CANCEL = "cancel"


class RecoveryStatus(str, Enum):
    PLANNED = "planned"
    APPLIED = "applied"
    BLOCKED = "blocked"
    HALTED = "halted"


@dataclass(frozen=True)
class PreparedVenueAction:
    intent_id: str
    run_id: str
    kind: VenueActionKind
    venue: Venue
    frame: FrozenStaticFrame
    fresh_until_mono_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, VenueActionKind):
            object.__setattr__(self, "kind", VenueActionKind(self.kind))
        if not isinstance(self.venue, Venue):
            raise RecoveryError("ambiguous_exposure")
        if not isinstance(self.frame, FrozenStaticFrame):
            raise RecoveryError("ambiguous_exposure")
        if self.frame.venue is not self.venue:
            raise RecoveryError("ambiguous_exposure")
        if self.kind is VenueActionKind.PLACE and not self.frame.reduce_only:
            raise RecoveryError("flatten_failed")
        if self.kind is VenueActionKind.CANCEL and self.frame.reduce_only:
            raise RecoveryError("cancel_failed")
        if isinstance(self.fresh_until_mono_ns, bool) or not isinstance(
            self.fresh_until_mono_ns, int
        ):
            raise RecoveryError("ambiguous_exposure")
        if self.fresh_until_mono_ns < 0:
            raise RecoveryError("ambiguous_exposure")

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": SCHEMA_VERSION,
            "intent_id": self.intent_id,
            "run_id": self.run_id,
            "kind": self.kind.value,
            "venue": self.venue.value,
            "leg_id": self.frame.leg_id,
            "client_id": self.frame.client_id,
            "reduce_only": self.frame.reduce_only,
            "fresh_until_mono_ns": self.fresh_until_mono_ns,
        }
        _assert_public(out)
        return out


@dataclass(frozen=True)
class VenueActionResult:
    schema_version: str
    kind: VenueActionKind
    venue: Venue
    evidence: VenueWriteEvidence

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise RecoveryError("ambiguous_exposure")
        object.__setattr__(
            self,
            "kind",
            self.kind if isinstance(self.kind, VenueActionKind) else VenueActionKind(self.kind),
        )
        if not isinstance(self.evidence, VenueWriteEvidence):
            raise RecoveryError("ambiguous_exposure")
        if self.evidence.venue is not self.venue:
            raise RecoveryError("ambiguous_exposure")

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "venue": self.venue.value,
            "evidence": self.evidence.to_public_dict(),
        }
        _assert_public(out)
        return out


@dataclass(frozen=True)
class RecoveryPlan:
    kind: RecoveryActionKind
    venue: Optional[Venue]
    reason_code: str
    flatten: Optional[LegPlan] = None
    cancel_client_id: Optional[str] = None
    cancel_instrument: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "kind",
            self.kind if isinstance(self.kind, RecoveryActionKind) else RecoveryActionKind(self.kind),
        )
        object.__setattr__(self, "reason_code", _require_reason(self.reason_code))
        if self.venue is not None and not isinstance(self.venue, Venue):
            raise RecoveryError("ambiguous_exposure")
        if self.flatten is not None and not isinstance(self.flatten, LegPlan):
            raise RecoveryError("flatten_failed")
        if self.kind is RecoveryActionKind.FLATTEN_FILLED and self.venue is None:
            raise RecoveryError("flatten_failed")
        if self.kind is RecoveryActionKind.CANCEL_PEER and self.venue is None:
            raise RecoveryError("cancel_failed")
        if self.kind is RecoveryActionKind.NOTHING and self.venue is not None:
            raise RecoveryError("nothing_to_do")
        if self.flatten is not None:
            if not self.flatten.reduce_only:
                raise RecoveryError("flatten_failed")
            if self.venue is not None and self.flatten.venue is not self.venue:
                raise RecoveryError("flatten_failed")

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": SCHEMA_VERSION,
            "kind": self.kind.value,
            "venue": None if self.venue is None else self.venue.value,
            "reason_code": self.reason_code,
            "flatten": None if self.flatten is None else self.flatten.to_public_dict(),
            "cancel_client_id": self.cancel_client_id,
            "cancel_instrument": self.cancel_instrument,
        }
        _assert_public(out)
        return out


@dataclass(frozen=True)
class RecoveryResult:
    status: RecoveryStatus
    reason_code: str
    recovery_required: bool
    halted: bool
    action: RecoveryActionKind
    dispatch: Optional[VenueActionResult] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            self.status if isinstance(self.status, RecoveryStatus) else RecoveryStatus(self.status),
        )
        object.__setattr__(
            self,
            "action",
            self.action
            if isinstance(self.action, RecoveryActionKind)
            else RecoveryActionKind(self.action),
        )
        object.__setattr__(self, "reason_code", _require_reason(self.reason_code))
        if not isinstance(self.recovery_required, bool) or not isinstance(self.halted, bool):
            raise RecoveryError("ambiguous_exposure")
        if self.dispatch is not None and not isinstance(self.dispatch, VenueActionResult):
            raise RecoveryError("ambiguous_exposure")

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": SCHEMA_VERSION,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "recovery_required": self.recovery_required,
            "halted": self.halted,
            "action": self.action.value,
            "dispatch": None if self.dispatch is None else self.dispatch.to_public_dict(),
        }
        _assert_public(out)
        return out

    def __repr__(self) -> str:
        return (
            "RecoveryResult("
            f"status={self.status.value!r}, action={self.action.value!r}, "
            f"reason_code={self.reason_code!r})"
        )


@dataclass(frozen=True)
class RestartResult:
    replayed_status: SpreadStatus
    opens_allowed: bool
    requires_reseed: bool
    restart_intent_id: str
    reason_code: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "replayed_status",
            self.replayed_status
            if isinstance(self.replayed_status, SpreadStatus)
            else SpreadStatus(self.replayed_status),
        )
        object.__setattr__(self, "reason_code", _require_reason(self.reason_code))
        if self.opens_allowed:
            raise RecoveryError("restart_unproven")
        if not self.requires_reseed:
            raise RecoveryError("restart_unproven")
        if not isinstance(self.restart_intent_id, str) or not self.restart_intent_id:
            raise RecoveryError("restart_unproven")

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": SCHEMA_VERSION,
            "replayed_status": self.replayed_status.value,
            "opens_allowed": False,
            "requires_reseed": True,
            "restart_intent_id": self.restart_intent_id,
            "reason_code": self.reason_code,
        }
        _assert_public(out)
        return out


@dataclass(frozen=True)
class RestartLiveSnapshot:
    complete: bool
    bybit_positions_flat: bool
    okx_positions_flat: bool
    bybit_open_orders_flat: bool
    okx_open_orders_flat: bool

    def __post_init__(self) -> None:
        for name in (
            "complete",
            "bybit_positions_flat",
            "okx_positions_flat",
            "bybit_open_orders_flat",
            "okx_open_orders_flat",
        ):
            if not isinstance(getattr(self, name), bool):
                raise RecoveryError("reseed_failed")

    @property
    def watch_set_flat(self) -> bool:
        return (
            self.complete
            and self.bybit_positions_flat
            and self.okx_positions_flat
            and self.bybit_open_orders_flat
            and self.okx_open_orders_flat
        )

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": SCHEMA_VERSION,
            "complete": self.complete,
            "bybit_positions_flat": self.bybit_positions_flat,
            "okx_positions_flat": self.okx_positions_flat,
            "bybit_open_orders_flat": self.bybit_open_orders_flat,
            "okx_open_orders_flat": self.okx_open_orders_flat,
        }
        _assert_public(out)
        return out


RecoveryLegFactory = Callable[[SpreadState, Venue, VenueActionKind], LegPlan]
WalDrainPort = Callable[[], Sequence[Any]]
SnapshotPort = Callable[[], Optional[RestartLiveSnapshot]]


def restart_correlation_id(state: SpreadState, run_id: str) -> str:
    if isinstance(state, SpreadState) and state.intent_id:
        return state.intent_id
    if not isinstance(run_id, str) or not run_id:
        raise RecoveryError("restart_unproven")
    return f"restart:{run_id}"


def exposure_qty(leg: LegState, tolerance: Decimal) -> Optional[Decimal]:
    """Authoritative flatten quantity, or None when the qty is not safe to send."""
    if not isinstance(leg, LegState):
        return None
    lot = canonical_decimal(tolerance, field="lot_tolerance")
    if observed_position_contradicts_fill(leg, lot):
        return None
    if leg_quantity_exceeds_plan(leg, lot):
        return None
    if leg.status is LegStatus.PARTIAL and not (
        leg.position_observed and leg.position_quantity > 0
    ):
        return None
    if leg.status in {LegStatus.UNKNOWN, LegStatus.RECONCILING} and not (
        leg.position_observed and leg.position_quantity > 0
    ):
        fill = leg.filled_quantity if leg.filled_quantity > 0 else Decimal("0")
        if fill <= 0:
            return None
    fill = leg.filled_quantity if leg.filled_quantity > 0 else Decimal("0")
    pos = (
        leg.position_quantity
        if leg.position_observed and leg.position_quantity > 0
        else Decimal("0")
    )
    qty = fill if fill >= pos else pos
    if qty <= 0:
        return None
    return qty


def _positive_non_reduce_proof(leg: LegState, tolerance: Decimal) -> bool:
    if leg.reduce_only:
        return False
    qty = leg_open_qty(leg, tolerance)
    if qty is not None and qty > 0:
        return True
    exposed = exposure_qty(leg, tolerance)
    return exposed is not None and exposed > 0 and leg.status is LegStatus.FILLED


def _confirmed_unfilled(leg: LegState) -> bool:
    if leg.filled_quantity != 0:
        return False
    if leg.position_observed and leg.position_quantity != 0:
        return False
    if leg.confirmed_unfilled:
        return True
    return leg.status in {LegStatus.ACK_REJECTED, LegStatus.CANCELLED}


def _one_positive_one_unfilled(state: SpreadState) -> bool:
    if len(state.legs) != 2:
        return False
    if any(leg.reduce_only for leg in state.legs):
        return False
    proven: list[LegState] = []
    unfilled: list[LegState] = []
    for leg in state.legs:
        if _positive_non_reduce_proof(leg, state.lot_tolerance):
            proven.append(leg)
        elif _confirmed_unfilled(leg):
            unfilled.append(leg)
    return len(proven) == 1 and len(unfilled) == 1


def _both_confirmed_unfilled_zero(state: SpreadState) -> bool:
    if len(state.legs) != 2:
        return False
    if any(leg.reduce_only for leg in state.legs):
        return False
    return all(
        _confirmed_unfilled(leg)
        and leg.status in {LegStatus.ACK_REJECTED, LegStatus.CANCELLED}
        for leg in state.legs
    )


def _has_qty_conflict(state: SpreadState) -> bool:
    for leg in state.legs:
        if observed_position_contradicts_fill(leg, state.lot_tolerance):
            return True
        if leg_quantity_exceeds_plan(leg, state.lot_tolerance):
            return True
    if len(state.legs) != 2 or any(leg.reduce_only for leg in state.legs):
        return False
    qtys: list[Decimal] = []
    for leg in state.legs:
        if leg.filled_quantity > 0:
            qtys.append(leg.filled_quantity)
        elif leg.position_observed and leg.position_quantity > 0:
            qtys.append(leg.position_quantity)
        else:
            return False
    return abs(qtys[0] - qtys[1]) > state.lot_tolerance


def _unresolved_timeout(state: SpreadState) -> bool:
    return any(ack_timeout_is_unresolved(leg, state.lot_tolerance) for leg in state.legs)


def _fresh_zeros(state: SpreadState) -> bool:
    if len(state.legs) != 2:
        return False
    return flat_evidence_is_complete(
        legs=state.legs,
        positions_flat=all(
            leg.position_observed and leg.position_quantity == 0 for leg in state.legs
        ),
        open_orders_flat=all(
            leg.open_orders_observed and leg.open_order_count == 0 for leg in state.legs
        ),
    )


def _observations_fresh(state: SpreadState) -> bool:
    if len(state.legs) != 2:
        return False
    return all(leg.position_observed and leg.open_orders_observed for leg in state.legs)


def _filled_and_peer(
    state: SpreadState,
) -> tuple[Optional[LegState], Optional[LegState]]:
    filled: Optional[LegState] = None
    for leg in state.legs:
        if _positive_non_reduce_proof(leg, state.lot_tolerance):
            filled = leg
            break
    if filled is None:
        return None, None
    peer = None
    for leg in state.legs:
        if leg.venue is not filled.venue:
            peer = leg
            break
    return filled, peer


def _trade_ready(readiness: Any, venue: Venue) -> bool:
    if readiness is None:
        return False
    if venue is Venue.BYBIT:
        return bool(getattr(readiness, "bybit_trade_ready", False))
    return bool(getattr(readiness, "okx_trade_ready", False))


def _private_ready(readiness: Any, venue: Venue) -> bool:
    if readiness is None:
        return False
    if venue is Venue.BYBIT:
        return bool(getattr(readiness, "bybit_private_ready", False))
    return bool(getattr(readiness, "okx_private_ready", False))


def _plan(
    kind: RecoveryActionKind,
    reason_code: str,
    *,
    venue: Optional[Venue] = None,
    flatten: Optional[LegPlan] = None,
    cancel_client_id: Optional[str] = None,
    cancel_instrument: Optional[str] = None,
) -> RecoveryPlan:
    return RecoveryPlan(
        kind=kind,
        venue=venue,
        reason_code=reason_code,
        flatten=flatten,
        cancel_client_id=cancel_client_id,
        cancel_instrument=cancel_instrument,
    )


def plan_recovery(
    state: SpreadState,
    readiness: Any,
    *,
    attempts: int,
    wal_blocks_opens: bool = False,
) -> RecoveryPlan:
    """Pure one-step recovery decision. No I/O and no TradeIntent."""
    if not isinstance(state, SpreadState):
        return _plan(RecoveryActionKind.HALT, "ambiguous_exposure")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        return _plan(RecoveryActionKind.HALT, "ambiguous_exposure")

    live_unproven = state.status not in {SpreadStatus.IDLE, SpreadStatus.FLAT} or (
        state.status is SpreadStatus.FLAT and not is_proven_flat(state)
    )
    if attempts >= MAX_RECOVERY_STEPS and live_unproven:
        if state.status is SpreadStatus.HALTED:
            return _plan(RecoveryActionKind.NOTHING, "halt")
        return _plan(RecoveryActionKind.HALT, "halt")

    if state.status is SpreadStatus.OPEN:
        return _plan(RecoveryActionKind.NOTHING, "nothing_to_do")

    if state.status is SpreadStatus.IDLE and not state.legs:
        if opens_allowed(state) and not wal_blocks_opens:
            return _plan(RecoveryActionKind.NOTHING, "nothing_to_do")
        return _plan(RecoveryActionKind.WAIT_RESEED, "wait_reseed")

    if state.status is SpreadStatus.FLAT and is_proven_flat(state):
        if opens_allowed(state) and not wal_blocks_opens:
            return _plan(RecoveryActionKind.NOTHING, "nothing_to_do")
        return _plan(RecoveryActionKind.WAIT_RESEED, "wait_reseed")

    if _has_qty_conflict(state):
        return _plan(RecoveryActionKind.WAIT_RESEED, "qty_conflict")

    if _unresolved_timeout(state):
        return _plan(RecoveryActionKind.WAIT_RESEED, "timeout_unresolved")

    if not state.stream_generation_ok:
        return _plan(RecoveryActionKind.WAIT_RESEED, "stream_blocked")

    if state.status is SpreadStatus.HALTED:
        filled, peer = _filled_and_peer(state)
        if not _observations_fresh(state):
            return _plan(RecoveryActionKind.WAIT_RESEED, "wait_reseed")
        if filled is not None and peer is not None and _confirmed_unfilled(peer):
            if filled.reduce_only:
                return _plan(RecoveryActionKind.NOTHING, "halt")
            if not _trade_ready(readiness, filled.venue):
                return _plan(RecoveryActionKind.WAIT_RESEED, "trade_socket_not_ready")
            return _plan(
                RecoveryActionKind.FLATTEN_FILLED,
                "flatten_required",
                venue=filled.venue,
            )
        return _plan(RecoveryActionKind.NOTHING, "halt")

    if _fresh_zeros(state) and state.stream_generation_ok:
        if not (
            _private_ready(readiness, Venue.BYBIT) and _private_ready(readiness, Venue.OKX)
        ):
            return _plan(RecoveryActionKind.WAIT_RESEED, "stream_blocked")
        if any(
            leg.status in {LegStatus.PARTIAL, LegStatus.UNKNOWN, LegStatus.RECONCILING}
            for leg in state.legs
        ):
            return _plan(RecoveryActionKind.WAIT_RESEED, "wait_reseed")
        return _plan(RecoveryActionKind.PROVE_FLAT, "prove_flat")

    if state.status is SpreadStatus.RECOVERING and _one_positive_one_unfilled(state):
        filled, peer = _filled_and_peer(state)
        if filled is None or peer is None:
            return _plan(RecoveryActionKind.WAIT_RESEED, "wait_reseed")
        if peer.open_orders_observed and peer.open_order_count > 0:
            if not _trade_ready(readiness, peer.venue):
                return _plan(RecoveryActionKind.WAIT_RESEED, "trade_socket_not_ready")
            return _plan(
                RecoveryActionKind.CANCEL_PEER,
                "peer_working",
                venue=peer.venue,
                cancel_client_id=peer.client_id,
            )
        if _confirmed_unfilled(peer) and not filled.reduce_only:
            if exposure_qty(filled, state.lot_tolerance) is None:
                return _plan(RecoveryActionKind.WAIT_RESEED, "ambiguous_exposure")
            if not _trade_ready(readiness, filled.venue):
                return _plan(RecoveryActionKind.WAIT_RESEED, "trade_socket_not_ready")
            return _plan(
                RecoveryActionKind.FLATTEN_FILLED,
                "flatten_required",
                venue=filled.venue,
            )
        return _plan(RecoveryActionKind.WAIT_RESEED, "wait_reseed")

    if _both_confirmed_unfilled_zero(state):
        return _plan(RecoveryActionKind.WAIT_RESEED, "wait_reseed")

    if needs_reconciliation(state) and not _one_positive_one_unfilled(state):
        return _plan(RecoveryActionKind.WAIT_RESEED, "wait_reseed")

    if opens_allowed(state):
        return _plan(RecoveryActionKind.NOTHING, "nothing_to_do")
    return _plan(RecoveryActionKind.WAIT_RESEED, "wait_reseed")
