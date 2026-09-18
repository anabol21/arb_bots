"""Non-owning execution-v2 dual-venue transport kernel.

Stdlib plus EV2 contracts only. No I/O, secrets, REST, env, file reads,
ACK/fill wait, retries, or live broker wiring.

The kernel dispatches one prepared Bybit write and one prepared OKX write
on the asyncio loop that already owns both warm trade sockets. Local
``asend`` completion is ``write_completed``, never venue acceptance.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from app.bot.execution.contracts import (
    LegPlan,
    TradeIntent,
    Venue,
    decimal_to_canonical,
    derive_client_id,
)

SCHEMA_VERSION = "bbot.execution.transport.v1"

TRANSPORT_REASON_CODES = frozenset(
    {
        "cancelled",
        "client_id_mismatch",
        "clock_regression",
        "duplicate_venue",
        "foreign_loop",
        "intent_id_mismatch",
        "invalid_inst_id_code",
        "invalid_leg_set",
        "invalid_metadata",
        "invalid_socket",
        "loop_not_running",
        "missing_metadata",
        "missing_venue",
        "mixed_loop_ownership",
        "rejected_before_write",
        "stale_metadata",
        "unknown_loop_ownership",
        "write_failed",
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
        "text",
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
        "x-bapi-sign",
        "x-bapi-api-key",
    }
)


class TransportError(ValueError):
    """Fail-closed transport violation. Public view is redacted."""

    def __init__(self, reason_code: str, *, intent_id: Optional[str] = None) -> None:
        if reason_code not in TRANSPORT_REASON_CODES:
            reason_code = "rejected_before_write"
        self.reason_code = reason_code
        self.intent_id = intent_id
        super().__init__(reason_code)

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": SCHEMA_VERSION,
            "reason_code": self.reason_code,
            "intent_id": self.intent_id,
        }
        _assert_public(out)
        return out


class DispatchStatus(str, Enum):
    BOTH_COMPLETED = "both_completed"
    PARTIAL = "partial"
    BOTH_FAILED = "both_failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class WriteOutcome(str, Enum):
    WRITE_COMPLETED = "write_completed"
    WRITE_FAILED = "write_failed"
    CANCELLED = "cancelled"
    NOT_ATTEMPTED = "not_attempted"


class LoopOwnedTradeSocket(Protocol):
    """Narrow send boundary compatible with ``LoopOwnedSocket.asend(text)``.

    The transport never connects, authenticates, subscribes, reconnects,
    receives or closes. Ownership is declared via ``owner_loop`` or the
    existing warm-socket ``_owner.loop`` duck-type.
    """

    async def asend(self, text: str) -> None:
        ...


FrameFinalizer = Callable[..., str]


def declared_owner_loop(socket: object) -> Optional[asyncio.AbstractEventLoop]:
    """Return the loop the socket declares as owner, else None (unknown)."""
    if socket is None:
        return None
    loop = getattr(socket, "owner_loop", None)
    if isinstance(loop, asyncio.AbstractEventLoop):
        return loop
    owner = getattr(socket, "_owner", None)
    nested = getattr(owner, "loop", None) if owner is not None else None
    if isinstance(nested, asyncio.AbstractEventLoop):
        return nested
    return None


def _norm_key(name: object) -> str:
    return str(name).strip().lower().replace("-", "_")


def _assert_public(node: object, *, path: str = "$") -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            nk = _norm_key(key)
            if nk in _FORBIDDEN_PUBLIC_KEYS:
                raise TransportError("rejected_before_write")
            _assert_public(value, path=f"{path}.{nk}")
        return
    if isinstance(node, (list, tuple)):
        for i, item in enumerate(node):
            _assert_public(item, path=f"{path}[{i}]")


def _require_reason(reason_code: Optional[str]) -> Optional[str]:
    if reason_code is None:
        return None
    if reason_code not in TRANSPORT_REASON_CODES:
        raise TransportError("rejected_before_write")
    return reason_code


def _require_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TransportError("rejected_before_write")
    if value < 0:
        raise TransportError("clock_regression")
    return value


def _okx_inst_id_code(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TransportError("invalid_inst_id_code")
    return value


@dataclass(frozen=True)
class CachedInstrument:
    """Immutable instrument snapshot frozen outside the signal path."""

    venue: Venue
    instrument: str
    captured_mono_ns: int
    fresh_until_mono_ns: int
    inst_id_code: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.venue, Venue):
            raise TransportError("invalid_metadata")
        if not isinstance(self.instrument, str) or not self.instrument:
            raise TransportError("invalid_metadata")
        object.__setattr__(
            self, "captured_mono_ns", _require_int(self.captured_mono_ns, field="captured_mono_ns")
        )
        object.__setattr__(
            self,
            "fresh_until_mono_ns",
            _require_int(self.fresh_until_mono_ns, field="fresh_until_mono_ns"),
        )
        if self.fresh_until_mono_ns <= self.captured_mono_ns:
            raise TransportError("invalid_metadata")
        if self.venue is Venue.OKX:
            object.__setattr__(self, "inst_id_code", _okx_inst_id_code(self.inst_id_code))
        elif self.inst_id_code is not None:
            if isinstance(self.inst_id_code, bool) or not isinstance(self.inst_id_code, int):
                raise TransportError("invalid_metadata")
            if self.inst_id_code <= 0:
                raise TransportError("invalid_metadata")

    def assert_fresh(self, *, now_mono_ns: int) -> None:
        now = _require_int(now_mono_ns, field="now_mono_ns")
        if now < self.captured_mono_ns:
            raise TransportError("clock_regression")
        if now > self.fresh_until_mono_ns:
            raise TransportError("stale_metadata")


@dataclass(frozen=True)
class InstrumentCache:
    """Pre-resolved instrument table. dispatch() does not look up metadata."""

    entries: Mapping[tuple[str, str], CachedInstrument]

    def __post_init__(self) -> None:
        raw = self.entries
        if isinstance(raw, Mapping) and not isinstance(raw, (str, bytes)):
            frozen: dict[tuple[str, str], CachedInstrument] = {}
            for key, value in raw.items():
                if not isinstance(key, tuple) or len(key) != 2:
                    raise TransportError("invalid_metadata")
                venue_key, instrument = key
                if not isinstance(venue_key, str) or not isinstance(instrument, str):
                    raise TransportError("invalid_metadata")
                if not isinstance(value, CachedInstrument):
                    raise TransportError("invalid_metadata")
                if value.venue.value != venue_key or value.instrument != instrument:
                    raise TransportError("invalid_metadata")
                frozen[(venue_key, instrument)] = value
            object.__setattr__(self, "entries", MappingProxyType(frozen))
            return
        raise TransportError("invalid_metadata")

    @classmethod
    def from_snapshots(cls, snapshots: Sequence[CachedInstrument]) -> "InstrumentCache":
        table: dict[tuple[str, str], CachedInstrument] = {}
        for item in snapshots:
            if not isinstance(item, CachedInstrument):
                raise TransportError("invalid_metadata")
            table[(item.venue.value, item.instrument)] = item
        return cls(entries=table)

    def get(self, venue: Venue, instrument: str) -> CachedInstrument:
        key = (venue.value, instrument)
        item = self.entries.get(key)
        if item is None:
            raise TransportError("missing_metadata")
        return item


@dataclass(frozen=True)
class FrozenStaticFrame:
    """Static order fields frozen before dispatch. No timestamp or signature."""

    venue: Venue
    leg_id: str
    instrument: str
    side: str
    quantity: str
    reduce_only: bool
    client_id: str
    inst_id_code: Optional[int]

    def __repr__(self) -> str:
        return (
            f"FrozenStaticFrame(venue={self.venue.value!r}, leg_id={self.leg_id!r}, "
            f"client_id={self.client_id!r})"
        )


@dataclass(frozen=True)
class PreparedDualLeg:
    """Intent plus two frozen static frames. No signed text is stored."""

    intent: TradeIntent
    bybit: FrozenStaticFrame
    okx: FrozenStaticFrame
    fresh_until_mono_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.intent, TradeIntent):
            raise TransportError("invalid_leg_set")
        if self.bybit.venue is not Venue.BYBIT or self.okx.venue is not Venue.OKX:
            raise TransportError("invalid_leg_set")
        if self.bybit.client_id != derive_client_id(
            self.intent.intent_id, Venue.BYBIT, reduce_only=self.bybit.reduce_only
        ):
            raise TransportError("client_id_mismatch", intent_id=self.intent.intent_id)
        if self.okx.client_id != derive_client_id(
            self.intent.intent_id, Venue.OKX, reduce_only=self.okx.reduce_only
        ):
            raise TransportError("client_id_mismatch", intent_id=self.intent.intent_id)
        object.__setattr__(
            self,
            "fresh_until_mono_ns",
            _require_int(self.fresh_until_mono_ns, field="fresh_until_mono_ns"),
        )

    def frame_for(self, venue: Venue) -> FrozenStaticFrame:
        if venue is Venue.BYBIT:
            return self.bybit
        if venue is Venue.OKX:
            return self.okx
        raise TransportError("invalid_leg_set", intent_id=self.intent.intent_id)

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": SCHEMA_VERSION,
            "intent_id": self.intent.intent_id,
            "run_id": self.intent.run_id,
            "venues": [Venue.BYBIT.value, Venue.OKX.value],
            "bybit_leg_id": self.bybit.leg_id,
            "okx_leg_id": self.okx.leg_id,
            "bybit_client_id": self.bybit.client_id,
            "okx_client_id": self.okx.client_id,
            "fresh_until_mono_ns": self.fresh_until_mono_ns,
        }
        _assert_public(out)
        return out

    def __repr__(self) -> str:
        return (
            f"PreparedDualLeg(intent_id={self.intent.intent_id!r}, "
            f"bybit_leg_id={self.bybit.leg_id!r}, okx_leg_id={self.okx.leg_id!r})"
        )


def _split_plans(plans: Sequence[LegPlan], intent: TradeIntent) -> tuple[LegPlan, LegPlan]:
    if not isinstance(intent, TradeIntent):
        raise TransportError("invalid_leg_set")
    if not isinstance(plans, Sequence) or isinstance(plans, (str, bytes)):
        raise TransportError("invalid_leg_set", intent_id=intent.intent_id)
    items = list(plans)
    if len(items) != 2:
        raise TransportError("invalid_leg_set", intent_id=intent.intent_id)
    bybit: Optional[LegPlan] = None
    okx: Optional[LegPlan] = None
    seen: set[Venue] = set()
    for plan in items:
        if not isinstance(plan, LegPlan):
            raise TransportError("invalid_leg_set", intent_id=intent.intent_id)
        if plan.intent_id != intent.intent_id:
            raise TransportError("intent_id_mismatch", intent_id=intent.intent_id)
        if plan.venue in seen:
            raise TransportError("duplicate_venue", intent_id=intent.intent_id)
        seen.add(plan.venue)
        expected = derive_client_id(plan.intent_id, plan.venue, reduce_only=plan.reduce_only)
        if plan.client_id != expected:
            raise TransportError("client_id_mismatch", intent_id=intent.intent_id)
        if plan.venue is Venue.BYBIT:
            bybit = plan
        elif plan.venue is Venue.OKX:
            okx = plan
        else:
            raise TransportError("invalid_leg_set", intent_id=intent.intent_id)
    if bybit is None or okx is None:
        raise TransportError("missing_venue", intent_id=intent.intent_id)
    return bybit, okx


def _freeze_plan(plan: LegPlan, snapshot: CachedInstrument) -> FrozenStaticFrame:
    if snapshot.venue is not plan.venue or snapshot.instrument != plan.instrument:
        raise TransportError("invalid_metadata", intent_id=plan.intent_id)
    inst_code: Optional[int]
    if plan.venue is Venue.OKX:
        inst_code = _okx_inst_id_code(snapshot.inst_id_code)
    else:
        inst_code = snapshot.inst_id_code
    return FrozenStaticFrame(
        venue=plan.venue,
        leg_id=plan.leg_id,
        instrument=plan.instrument,
        side=plan.side,
        quantity=decimal_to_canonical(plan.quantity),
        reduce_only=plan.reduce_only,
        client_id=plan.client_id,
        inst_id_code=inst_code,
    )


def prepare_dual_leg(
    intent: TradeIntent,
    plans: Sequence[LegPlan],
    cache: InstrumentCache,
    *,
    now_mono_ns: int,
) -> PreparedDualLeg:
    """Resolve cache + freeze static fields. No I/O. Call outside dispatch."""
    if not isinstance(cache, InstrumentCache):
        raise TransportError("invalid_metadata")
    now = _require_int(now_mono_ns, field="now_mono_ns")
    bybit_plan, okx_plan = _split_plans(plans, intent)
    bybit_meta = cache.get(Venue.BYBIT, bybit_plan.instrument)
    okx_meta = cache.get(Venue.OKX, okx_plan.instrument)
    bybit_meta.assert_fresh(now_mono_ns=now)
    okx_meta.assert_fresh(now_mono_ns=now)
    fresh_until = min(bybit_meta.fresh_until_mono_ns, okx_meta.fresh_until_mono_ns)
    return PreparedDualLeg(
        intent=intent,
        bybit=_freeze_plan(bybit_plan, bybit_meta),
        okx=_freeze_plan(okx_plan, okx_meta),
        fresh_until_mono_ns=fresh_until,
    )


def unsigned_frame_finalizer(
    static: FrozenStaticFrame,
    *,
    timestamp_ms: int,
    request_id: str,
    client_id: str,
) -> str:
    """Test/injected builder: serialize static fields + patched ids/timestamp.

    Does not read secrets, env, or disk. A production signer is injected later
    and may add a venue signature at this same boundary.
    """
    if not isinstance(static, FrozenStaticFrame):
        raise TransportError("rejected_before_write")
    if not isinstance(timestamp_ms, int) or isinstance(timestamp_ms, bool) or timestamp_ms < 0:
        raise TransportError("clock_regression")
    if request_id != client_id or client_id != static.client_id:
        raise TransportError("client_id_mismatch")
    if static.venue is Venue.OKX:
        args: dict[str, Any] = {
            "instId": static.instrument,
            "instIdCode": static.inst_id_code,
            "tdMode": "cross",
            "side": static.side,
            "sz": static.quantity,
            "clOrdId": client_id,
            "ordType": "market",
        }
        if static.reduce_only:
            args["reduceOnly"] = True
        body: dict[str, Any] = {
            "id": request_id,
            "op": "order",
            "args": [args],
            "ts": timestamp_ms,
        }
    elif static.venue is Venue.BYBIT:
        args = {
            "category": "linear",
            "symbol": static.instrument,
            "side": "Buy" if static.side == "buy" else "Sell",
            "qty": static.quantity,
            "orderLinkId": client_id,
            "orderType": "Market",
            "timeInForce": "IOC",
        }
        if static.reduce_only:
            args["reduceOnly"] = True
        body = {
            "reqId": request_id,
            "op": "order.create",
            "args": [args],
            "ts": timestamp_ms,
        }
    else:
        raise TransportError("invalid_leg_set")
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False)


def _payload_bytes(text: str) -> int:
    if not isinstance(text, str):
        raise TransportError("rejected_before_write")
    return len(text.encode("utf-8"))


@dataclass(frozen=True)
class VenueWriteEvidence:
    venue: Venue
    outcome: WriteOutcome
    leg_id: str
    client_id: str
    payload_bytes: int
    asend_start_mono_ns: Optional[int]
    asend_done_mono_ns: Optional[int]
    write_latency_ns: Optional[int]
    reason_code: Optional[str]

    def __post_init__(self) -> None:
        if not isinstance(self.venue, Venue):
            raise TransportError("rejected_before_write")
        object.__setattr__(
            self, "outcome", self.outcome if isinstance(self.outcome, WriteOutcome) else WriteOutcome(self.outcome)
        )
        object.__setattr__(self, "reason_code", _require_reason(self.reason_code))
        if self.payload_bytes < 0:
            raise TransportError("rejected_before_write")
        start = self.asend_start_mono_ns
        done = self.asend_done_mono_ns
        latency = self.write_latency_ns
        if start is not None:
            _require_int(start, field="asend_start_mono_ns")
        if done is not None:
            _require_int(done, field="asend_done_mono_ns")
        if start is not None and done is not None and done < start:
            raise TransportError("clock_regression")
        if latency is not None:
            if latency < 0:
                raise TransportError("clock_regression")
            if start is not None and done is not None and latency != done - start:
                raise TransportError("clock_regression")

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "venue": self.venue.value,
            "outcome": self.outcome.value,
            "leg_id": self.leg_id,
            "client_id": self.client_id,
            "payload_bytes": self.payload_bytes,
            "asend_start_mono_ns": self.asend_start_mono_ns,
            "asend_done_mono_ns": self.asend_done_mono_ns,
            "write_latency_ns": self.write_latency_ns,
            "reason_code": self.reason_code,
        }
        _assert_public(out)
        return out


@dataclass(frozen=True)
class DispatchResult:
    schema_version: str
    status: DispatchStatus
    intent_id: str
    run_id: str
    dispatch_entry_mono_ns: Optional[int]
    signal_mono_ns: int
    signal_to_first_write_ns: Optional[int]
    dual_leg_write_ns: Optional[int]
    bybit: VenueWriteEvidence
    okx: VenueWriteEvidence
    reason_code: Optional[str]

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise TransportError("rejected_before_write")
        object.__setattr__(
            self, "status", self.status if isinstance(self.status, DispatchStatus) else DispatchStatus(self.status)
        )
        object.__setattr__(self, "reason_code", _require_reason(self.reason_code))
        if self.dispatch_entry_mono_ns is not None:
            _require_int(self.dispatch_entry_mono_ns, field="dispatch_entry_mono_ns")
        _require_int(self.signal_mono_ns, field="signal_mono_ns")
        if self.signal_to_first_write_ns is not None and self.signal_to_first_write_ns < 0:
            raise TransportError("clock_regression")
        if self.dual_leg_write_ns is not None and self.dual_leg_write_ns < 0:
            raise TransportError("clock_regression")
        if self.bybit.venue is not Venue.BYBIT or self.okx.venue is not Venue.OKX:
            raise TransportError("invalid_leg_set", intent_id=self.intent_id)

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "intent_id": self.intent_id,
            "run_id": self.run_id,
            "dispatch_entry_mono_ns": self.dispatch_entry_mono_ns,
            "signal_mono_ns": self.signal_mono_ns,
            "signal_to_first_write_ns": self.signal_to_first_write_ns,
            "dual_leg_write_ns": self.dual_leg_write_ns,
            "bybit": self.bybit.to_public_dict(),
            "okx": self.okx.to_public_dict(),
            "reason_code": self.reason_code,
        }
        _assert_public(out)
        return out

    def __repr__(self) -> str:
        return (
            f"DispatchResult(status={self.status.value!r}, intent_id={self.intent_id!r}, "
            f"reason_code={self.reason_code!r})"
        )


def _not_attempted(frame: FrozenStaticFrame, *, reason_code: str) -> VenueWriteEvidence:
    return VenueWriteEvidence(
        venue=frame.venue,
        outcome=WriteOutcome.NOT_ATTEMPTED,
        leg_id=frame.leg_id,
        client_id=frame.client_id,
        payload_bytes=0,
        asend_start_mono_ns=None,
        asend_done_mono_ns=None,
        write_latency_ns=None,
        reason_code=reason_code,
    )


def _chronometry(
    *,
    signal_mono_ns: int,
    entry_ns: int,
    bybit: VenueWriteEvidence,
    okx: VenueWriteEvidence,
) -> tuple[Optional[int], Optional[int]]:
    """Aggregate write clocks. Post-write regression yields ``(None, None)``."""
    try:
        starts = [
            ns
            for ns in (bybit.asend_start_mono_ns, okx.asend_start_mono_ns)
            if ns is not None
        ]
        signal_to_first: Optional[int] = None
        if starts:
            first = min(starts)
            if first < entry_ns or first < signal_mono_ns:
                return None, None
            delta = first - signal_mono_ns
            if delta < 0:
                return None, None
            signal_to_first = delta
        latencies = [
            ns for ns in (bybit.write_latency_ns, okx.write_latency_ns) if ns is not None
        ]
        if any(ns < 0 for ns in latencies):
            return None, None
        slower: Optional[int] = max(latencies) if latencies else None
        return signal_to_first, slower
    except (TypeError, ValueError, TransportError):
        return None, None


def _result_status(bybit: VenueWriteEvidence, okx: VenueWriteEvidence) -> DispatchStatus:
    outcomes = {bybit.outcome, okx.outcome}
    if WriteOutcome.CANCELLED in outcomes:
        return DispatchStatus.CANCELLED
    if outcomes == {WriteOutcome.WRITE_COMPLETED}:
        return DispatchStatus.BOTH_COMPLETED
    if outcomes == {WriteOutcome.WRITE_FAILED}:
        return DispatchStatus.BOTH_FAILED
    if WriteOutcome.NOT_ATTEMPTED in outcomes and WriteOutcome.WRITE_COMPLETED not in outcomes:
        if WriteOutcome.WRITE_FAILED in outcomes:
            return DispatchStatus.BOTH_FAILED
        return DispatchStatus.REJECTED
    if WriteOutcome.WRITE_COMPLETED in outcomes and WriteOutcome.WRITE_FAILED in outcomes:
        return DispatchStatus.PARTIAL
    return DispatchStatus.REJECTED


class ExecutionTransport:
    """Same-loop dual-venue write kernel. Does not own socket lifecycle."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        bybit_socket: LoopOwnedTradeSocket,
        okx_socket: LoopOwnedTradeSocket,
        finalize_frame: FrameFinalizer,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        wall_ms: Optional[Callable[[], int]] = None,
    ) -> None:
        if not isinstance(loop, asyncio.AbstractEventLoop):
            raise TransportError("unknown_loop_ownership")
        if bybit_socket is None or okx_socket is None:
            raise TransportError("invalid_socket")
        if not callable(getattr(bybit_socket, "asend", None)) or not callable(
            getattr(okx_socket, "asend", None)
        ):
            raise TransportError("invalid_socket")
        if not callable(finalize_frame):
            raise TransportError("rejected_before_write")
        bybit_loop = declared_owner_loop(bybit_socket)
        okx_loop = declared_owner_loop(okx_socket)
        if bybit_loop is None or okx_loop is None:
            raise TransportError("unknown_loop_ownership")
        if bybit_loop is not okx_loop:
            raise TransportError("mixed_loop_ownership")
        if bybit_loop is not loop or okx_loop is not loop:
            raise TransportError("mixed_loop_ownership")
        self._loop = loop
        self._bybit_socket = bybit_socket
        self._okx_socket = okx_socket
        self._finalize_frame = finalize_frame
        self._monotonic_ns = monotonic_ns
        self._wall_ms = wall_ms if wall_ms is not None else _default_wall_ms

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    def _mono(self) -> int:
        value = self._monotonic_ns()
        return _require_int(value, field="monotonic_ns")

    def _try_mono(self) -> tuple[Optional[int], bool]:
        try:
            return self._mono(), False
        except asyncio.CancelledError:
            raise
        except TransportError:
            return None, True
        except Exception:
            return None, True

    def _finish_write(
        self,
        *,
        venue: Venue,
        frame: FrozenStaticFrame,
        payload_bytes: int,
        outcome: WriteOutcome,
        start: Optional[int],
        clock_bad: bool,
        reason_code: Optional[str],
    ) -> VenueWriteEvidence:
        done, done_bad = self._try_mono()
        clock_bad = clock_bad or done_bad
        latency: Optional[int] = None
        if start is not None and done is not None:
            if done < start:
                clock_bad = True
                done = None
            else:
                latency = done - start
        if start is None:
            clock_bad = True
        if clock_bad:
            reason_code = "clock_regression"
        try:
            return VenueWriteEvidence(
                venue=venue,
                outcome=outcome,
                leg_id=frame.leg_id,
                client_id=frame.client_id,
                payload_bytes=payload_bytes,
                asend_start_mono_ns=start,
                asend_done_mono_ns=done,
                write_latency_ns=latency,
                reason_code=reason_code,
            )
        except TransportError:
            return VenueWriteEvidence(
                venue=venue,
                outcome=outcome,
                leg_id=frame.leg_id,
                client_id=frame.client_id,
                payload_bytes=payload_bytes if isinstance(payload_bytes, int) and payload_bytes >= 0 else 0,
                asend_start_mono_ns=None,
                asend_done_mono_ns=None,
                write_latency_ns=None,
                reason_code="clock_regression",
            )

    def _reject(
        self,
        prepared: PreparedDualLeg,
        reason_code: str,
        *,
        entry_ns: Optional[int] = None,
    ) -> DispatchResult:
        bybit = _not_attempted(prepared.bybit, reason_code=reason_code)
        okx = _not_attempted(prepared.okx, reason_code=reason_code)
        return DispatchResult(
            schema_version=SCHEMA_VERSION,
            status=DispatchStatus.REJECTED,
            intent_id=prepared.intent.intent_id,
            run_id=prepared.intent.run_id,
            dispatch_entry_mono_ns=entry_ns,
            signal_mono_ns=prepared.intent.signal_mono_ns,
            signal_to_first_write_ns=None,
            dual_leg_write_ns=None,
            bybit=bybit,
            okx=okx,
            reason_code=reason_code,
        )

    def _preflight(self, prepared: PreparedDualLeg) -> Optional[DispatchResult]:
        if not isinstance(prepared, PreparedDualLeg):
            raise TransportError("invalid_leg_set")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return self._reject(prepared, "loop_not_running")
        if running is not self._loop:
            return self._reject(prepared, "foreign_loop")
        if not self._loop.is_running():
            return self._reject(prepared, "loop_not_running")
        bybit_loop = declared_owner_loop(self._bybit_socket)
        okx_loop = declared_owner_loop(self._okx_socket)
        if bybit_loop is None or okx_loop is None:
            return self._reject(prepared, "unknown_loop_ownership")
        if bybit_loop is not self._loop or okx_loop is not self._loop:
            return self._reject(prepared, "mixed_loop_ownership")
        if bybit_loop is not okx_loop:
            return self._reject(prepared, "mixed_loop_ownership")
        return None

    def _finalize(self, prepared: PreparedDualLeg, timestamp_ms: int) -> tuple[str, str, int, int]:
        bybit_text = self._finalize_frame(
            prepared.bybit,
            timestamp_ms=timestamp_ms,
            request_id=prepared.bybit.client_id,
            client_id=prepared.bybit.client_id,
        )
        okx_text = self._finalize_frame(
            prepared.okx,
            timestamp_ms=timestamp_ms,
            request_id=prepared.okx.client_id,
            client_id=prepared.okx.client_id,
        )
        if not isinstance(bybit_text, str) or not isinstance(okx_text, str):
            raise TransportError("rejected_before_write", intent_id=prepared.intent.intent_id)
        return bybit_text, okx_text, _payload_bytes(bybit_text), _payload_bytes(okx_text)

    async def _write_one(
        self,
        *,
        venue: Venue,
        socket: LoopOwnedTradeSocket,
        text: str,
        frame: FrozenStaticFrame,
        payload_bytes: int,
    ) -> VenueWriteEvidence:
        start, clock_bad = self._try_mono()
        try:
            await socket.asend(text)
        except asyncio.CancelledError:
            return self._finish_write(
                venue=venue,
                frame=frame,
                payload_bytes=payload_bytes,
                outcome=WriteOutcome.CANCELLED,
                start=start,
                clock_bad=clock_bad,
                reason_code="cancelled",
            )
        except Exception:
            return self._finish_write(
                venue=venue,
                frame=frame,
                payload_bytes=payload_bytes,
                outcome=WriteOutcome.WRITE_FAILED,
                start=start,
                clock_bad=clock_bad,
                reason_code="write_failed",
            )
        return self._finish_write(
            venue=venue,
            frame=frame,
            payload_bytes=payload_bytes,
            outcome=WriteOutcome.WRITE_COMPLETED,
            start=start,
            clock_bad=clock_bad,
            reason_code=None,
        )

    def _evidence_from_task(
        self,
        task: "asyncio.Task[VenueWriteEvidence]",
        frame: FrozenStaticFrame,
        payload_bytes: int,
        *,
        reason_code: str,
    ) -> VenueWriteEvidence:
        fallback_reason = reason_code if reason_code in TRANSPORT_REASON_CODES else "write_failed"
        fallback_outcome = (
            WriteOutcome.CANCELLED if fallback_reason == "cancelled" else WriteOutcome.WRITE_FAILED
        )
        fallback = VenueWriteEvidence(
            venue=frame.venue,
            outcome=fallback_outcome,
            leg_id=frame.leg_id,
            client_id=frame.client_id,
            payload_bytes=payload_bytes,
            asend_start_mono_ns=None,
            asend_done_mono_ns=None,
            write_latency_ns=None,
            reason_code=fallback_reason,
        )
        if not task.done():
            return fallback
        try:
            if task.cancelled():
                return VenueWriteEvidence(
                    venue=frame.venue,
                    outcome=WriteOutcome.CANCELLED,
                    leg_id=frame.leg_id,
                    client_id=frame.client_id,
                    payload_bytes=payload_bytes,
                    asend_start_mono_ns=None,
                    asend_done_mono_ns=None,
                    write_latency_ns=None,
                    reason_code="cancelled",
                )
            exc = task.exception()
            if exc is not None:
                mapped = "write_failed"
                if isinstance(exc, TransportError) and exc.reason_code in TRANSPORT_REASON_CODES:
                    mapped = exc.reason_code
                return VenueWriteEvidence(
                    venue=frame.venue,
                    outcome=WriteOutcome.WRITE_FAILED,
                    leg_id=frame.leg_id,
                    client_id=frame.client_id,
                    payload_bytes=payload_bytes,
                    asend_start_mono_ns=None,
                    asend_done_mono_ns=None,
                    write_latency_ns=None,
                    reason_code=mapped,
                )
            result = task.result()
        except asyncio.CancelledError:
            return VenueWriteEvidence(
                venue=frame.venue,
                outcome=WriteOutcome.CANCELLED,
                leg_id=frame.leg_id,
                client_id=frame.client_id,
                payload_bytes=payload_bytes,
                asend_start_mono_ns=None,
                asend_done_mono_ns=None,
                write_latency_ns=None,
                reason_code="cancelled",
            )
        except TransportError as exc:
            mapped = exc.reason_code if exc.reason_code in TRANSPORT_REASON_CODES else "write_failed"
            return VenueWriteEvidence(
                venue=frame.venue,
                outcome=WriteOutcome.WRITE_FAILED,
                leg_id=frame.leg_id,
                client_id=frame.client_id,
                payload_bytes=payload_bytes,
                asend_start_mono_ns=None,
                asend_done_mono_ns=None,
                write_latency_ns=None,
                reason_code=mapped,
            )
        except Exception:
            return fallback
        if isinstance(result, VenueWriteEvidence):
            return result
        return fallback

    async def dispatch(self, prepared: PreparedDualLeg) -> DispatchResult:
        """Schedule both ``asend`` calls before waiting for either result.

        Never receives, never waits for ACK, never retries.
        """
        rejected = self._preflight(prepared)
        if rejected is not None:
            return rejected
        entry_ns = self._mono()
        if entry_ns > prepared.fresh_until_mono_ns:
            return self._reject(prepared, "stale_metadata", entry_ns=entry_ns)
        try:
            timestamp_ms = self._wall_ms()
            if not isinstance(timestamp_ms, int) or isinstance(timestamp_ms, bool) or timestamp_ms < 0:
                return self._reject(prepared, "clock_regression", entry_ns=entry_ns)
            bybit_text, okx_text, bybit_bytes, okx_bytes = self._finalize(prepared, timestamp_ms)
        except TransportError as exc:
            return self._reject(prepared, exc.reason_code, entry_ns=entry_ns)
        except Exception:
            return self._reject(prepared, "rejected_before_write", entry_ns=entry_ns)

        bybit_task = self._loop.create_task(
            self._write_one(
                venue=Venue.BYBIT,
                socket=self._bybit_socket,
                text=bybit_text,
                frame=prepared.bybit,
                payload_bytes=bybit_bytes,
            ),
            name="ev2-bybit-asend",
        )
        okx_task = self._loop.create_task(
            self._write_one(
                venue=Venue.OKX,
                socket=self._okx_socket,
                text=okx_text,
                frame=prepared.okx,
                payload_bytes=okx_bytes,
            ),
            name="ev2-okx-asend",
        )
        cancelled_after_schedule = False
        while True:
            pending = [task for task in (bybit_task, okx_task) if not task.done()]
            if not pending:
                break
            try:
                await asyncio.wait(pending, return_when=asyncio.ALL_COMPLETED)
            except asyncio.CancelledError:
                cancelled_after_schedule = True
                for task in (bybit_task, okx_task):
                    if not task.done():
                        task.cancel()
        fallback_reason = "cancelled" if cancelled_after_schedule else "write_failed"
        bybit_ev = self._evidence_from_task(
            bybit_task,
            prepared.bybit,
            bybit_bytes,
            reason_code=fallback_reason,
        )
        okx_ev = self._evidence_from_task(
            okx_task,
            prepared.okx,
            okx_bytes,
            reason_code=fallback_reason,
        )
        signal_to_first, slower = _chronometry(
            signal_mono_ns=prepared.intent.signal_mono_ns,
            entry_ns=entry_ns,
            bybit=bybit_ev,
            okx=okx_ev,
        )
        starts_present = (
            bybit_ev.asend_start_mono_ns is not None or okx_ev.asend_start_mono_ns is not None
        )
        clock_regressed = (
            bybit_ev.reason_code == "clock_regression"
            or okx_ev.reason_code == "clock_regression"
            or (starts_present and signal_to_first is None)
        )
        if clock_regressed:
            signal_to_first, slower = None, None
        status = _result_status(bybit_ev, okx_ev)
        if cancelled_after_schedule:
            status = DispatchStatus.CANCELLED
        reason: Optional[str]
        if status is DispatchStatus.CANCELLED:
            reason = "cancelled"
        elif clock_regressed:
            reason = "clock_regression"
        elif status is DispatchStatus.BOTH_FAILED:
            reason = "write_failed"
        elif status is DispatchStatus.PARTIAL:
            reason = "write_failed"
        elif status is DispatchStatus.REJECTED:
            reason = "rejected_before_write"
        else:
            reason = None
        try:
            return DispatchResult(
                schema_version=SCHEMA_VERSION,
                status=status,
                intent_id=prepared.intent.intent_id,
                run_id=prepared.intent.run_id,
                dispatch_entry_mono_ns=entry_ns,
                signal_mono_ns=prepared.intent.signal_mono_ns,
                signal_to_first_write_ns=signal_to_first,
                dual_leg_write_ns=slower,
                bybit=bybit_ev,
                okx=okx_ev,
                reason_code=reason,
            )
        except TransportError:
            return DispatchResult(
                schema_version=SCHEMA_VERSION,
                status=status,
                intent_id=prepared.intent.intent_id,
                run_id=prepared.intent.run_id,
                dispatch_entry_mono_ns=entry_ns if entry_ns >= 0 else None,
                signal_mono_ns=prepared.intent.signal_mono_ns,
                signal_to_first_write_ns=None,
                dual_leg_write_ns=None,
                bybit=bybit_ev,
                okx=okx_ev,
                reason_code="cancelled" if status is DispatchStatus.CANCELLED else "clock_regression",
            )


def _default_wall_ms() -> int:
    return int(time.time() * 1000)
