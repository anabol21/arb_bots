"""Owner-loop bridge from warm private WS frames to durable EV2 evidence.

The bridge is deliberately not an order arm.  It buffers frames while an
EV2 submit holds the engine lock, then binds the exact intent and WAL sequence
before adapting them.  No private frame is allowed to overtake REQUEST_SENT.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from app.bot.execution.adapters import PrivateEventAdapter
from app.bot.execution.contracts import LegPlan, TradeIntent, Venue
from app.bot.execution.engine import ExecutionEngine
from app.bot.execution.live_chronometry import LiveChronometry
from app.bot.execution.transport import DispatchResult


class LivePrivateBridgeError(RuntimeError):
    """Private evidence could not be durably and unambiguously ingested."""


@dataclass(frozen=True)
class _QueuedFrame:
    venue: Venue
    source: str
    generation: int
    receive_mono_ns: int
    payload: Mapping[str, Any]
    expected_client_id: Optional[str] = None


def _source(payload: Mapping[str, Any], venue: Venue) -> Optional[str]:
    if venue is Venue.BYBIT:
        topic = payload.get("topic")
        if not isinstance(topic, str):
            return None
        head = topic.split(".", 1)[0].lower()
        return head if head in {"order", "execution", "position"} else None
    arg = payload.get("arg")
    if not isinstance(arg, Mapping):
        return None
    channel = arg.get("channel")
    if channel == "orders":
        return "order"
    if channel == "fills":
        return "execution"
    if channel == "positions":
        return "position"
    return None


class LivePrivateEvidenceBridge:
    """One owner-loop, one in-flight EV2 intent, no optimistic fill credit.

    Install ``observe`` on each warm private slot before calling submit.
    Call ``begin_submission`` before submit and ``bind_submitted`` after its
    REQUEST_SENT events are committed.  The caller must not send a successor
    intent until the previous worker is drained and the EV2 FSM proves the
    expected state.  ACKs use the same adapter via ``ingest_trade_ack``.
    """

    def __init__(
        self,
        *,
        engine: ExecutionEngine,
        symbols_by_venue: Mapping[Venue, str],
        max_buffered_frames: int = 256,
    ) -> None:
        if (
            not isinstance(engine, ExecutionEngine)
            or set(symbols_by_venue) != {Venue.BYBIT, Venue.OKX}
            or any(not isinstance(symbol, str) or not symbol for symbol in symbols_by_venue.values())
            or max_buffered_frames < 1
        ):
            raise ValueError("invalid_live_bridge")
        self._engine = engine
        self._symbols = dict(symbols_by_venue)
        self._max = max_buffered_frames
        self._pending: deque[_QueuedFrame] = deque()
        self._adapter: Optional[PrivateEventAdapter] = None
        self._intent_id: Optional[str] = None
        self._draining = False
        self._fatal: Optional[str] = None
        self._capturing = False
        self._expected_clients: dict[Venue, str] = {}
        self.chronometry = LiveChronometry()

    @property
    def fatal_reason(self) -> Optional[str]:
        return self._fatal

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def begin_submission(self, plans: Sequence[LegPlan]) -> None:
        if self._fatal is not None or self._pending or self._draining:
            raise LivePrivateBridgeError("private_evidence_unsettled")
        expected = {plan.venue: plan for plan in plans}
        if (
            len(plans) != 2
            or set(expected) != {Venue.BYBIT, Venue.OKX}
            or any(expected[v].instrument != self._symbols[v] for v in expected)
        ):
            raise LivePrivateBridgeError("invalid_live_plan")
        self._adapter = None
        self._intent_id = None
        self._expected_clients = {venue: plan.client_id for venue, plan in expected.items()}
        self._capturing = True

    def observe(
        self, text: str, receive_mono_ns: int, *, venue: Venue, generation: int
    ) -> None:
        """Non-blocking warm-loop callback; retains receive-time evidence."""
        if self._fatal is not None:
            raise LivePrivateBridgeError(self._fatal)
        try:
            payload = json.loads(text)
        except (TypeError, ValueError) as exc:
            self._fatal = "private_frame_invalid"
            raise LivePrivateBridgeError(self._fatal) from exc
        if not isinstance(payload, dict):
            self._fatal = "private_frame_invalid"
            raise LivePrivateBridgeError(self._fatal)
        source = _source(payload, venue)
        if source is None or not self._capturing:
            return
        rows = payload.get("data")
        if not isinstance(rows, list):
            self._fatal = "private_frame_invalid"
            raise LivePrivateBridgeError(self._fatal)
        symbol_key = "symbol" if venue is Venue.BYBIT else "instId"
        target = self._symbols[venue]
        relevant: list[Mapping[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                self._fatal = "private_frame_invalid"
                raise LivePrivateBridgeError(self._fatal)
            symbol = row.get(symbol_key)
            if not isinstance(symbol, str):
                self._fatal = "private_symbol_missing"
                raise LivePrivateBridgeError(self._fatal)
            if symbol == target:
                relevant.append(row)
        if not relevant:
            return
        payload = {**payload, "data": relevant}
        if len(self._pending) >= self._max:
            self._fatal = "private_frame_overflow"
            raise LivePrivateBridgeError(self._fatal)
        self._pending.append(
            _QueuedFrame(venue, source, generation, receive_mono_ns, payload)
        )

    def observe_trade(
        self, text: str, receive_mono_ns: int, *, venue: Venue, generation: int
    ) -> None:
        """Capture only this EV2 place ACK; the warm queue still owns delivery."""
        if self._fatal is not None:
            raise LivePrivateBridgeError(self._fatal)
        if not self._capturing:
            return
        try:
            payload = json.loads(text)
        except (TypeError, ValueError) as exc:
            self._fatal = "trade_frame_invalid"
            raise LivePrivateBridgeError(self._fatal) from exc
        if not isinstance(payload, dict):
            self._fatal = "trade_frame_invalid"
            raise LivePrivateBridgeError(self._fatal)
        expected = self._expected_clients.get(venue)
        if expected is None:
            raise LivePrivateBridgeError("intent_not_prepared")
        if venue is Venue.BYBIT:
            matching = payload.get("reqId") == expected
        else:
            matching = payload.get("id") == expected or (
                payload.get("event") == "error" and not payload.get("id")
            )
        if not matching:
            return
        if len(self._pending) >= self._max:
            self._fatal = "private_frame_overflow"
            raise LivePrivateBridgeError(self._fatal)
        self._pending.append(
            _QueuedFrame(
                venue, "trade_ack", generation, receive_mono_ns, payload,
                expected_client_id=expected,
            )
        )

    def bind_submitted(
        self,
        intent: TradeIntent,
        plans: Sequence[LegPlan],
        *,
        bybit_generation: int,
        okx_generation: int,
        dispatch: Optional[DispatchResult] = None,
    ) -> None:
        """Bind only after EV2 has committed its local send events."""
        if self._fatal is not None or self._adapter is not None:
            raise LivePrivateBridgeError(self._fatal or "intent_already_bound")
        state = self._engine.state
        expected = {plan.venue: plan.client_id for plan in plans}
        observed = {leg.venue: leg.client_id for leg in state.legs}
        if (
            state.intent_id != intent.intent_id
            or set(expected) != {Venue.BYBIT, Venue.OKX}
            or observed != expected
            or expected != self._expected_clients
        ):
            raise LivePrivateBridgeError("request_not_committed")
        adapter = PrivateEventAdapter(
            expected_generations={
                Venue.BYBIT: bybit_generation,
                Venue.OKX: okx_generation,
            },
            last_sequences={intent.intent_id: state.last_sequence},
            last_monotonic_ns=state.last_monotonic_ns,
        )
        adapter.register(intent, tuple(plans))
        if dispatch is not None:
            self.chronometry.bind_dispatch(intent, plans, dispatch)
        self._adapter = adapter
        self._intent_id = intent.intent_id

    async def drain(self) -> int:
        """Fold private evidence in receive order and fsync each nonempty batch."""
        if self._fatal is not None:
            raise LivePrivateBridgeError(self._fatal)
        adapter = self._adapter
        if adapter is None or self._draining:
            raise LivePrivateBridgeError("intent_not_bound")
        self._draining = True
        count = 0
        try:
            while self._pending:
                frame = self._pending.popleft()
                batch = adapter.adapt(
                    frame.payload,
                    venue=frame.venue,
                    source=frame.source,
                    generation=frame.generation,
                    receive_mono_ns=frame.receive_mono_ns,
                    expected_client_id=frame.expected_client_id,
                )
                result = await self._engine.ingest_adapter_batch_durable(batch)
                if not result.accepted:
                    self._fatal = result.reason_code or "private_ingest_failed"
                    raise LivePrivateBridgeError(self._fatal)
                self.chronometry.record_accepted_batch(
                    batch.events,
                    payload=frame.payload,
                    venue=frame.venue,
                    receive_mono_ns=frame.receive_mono_ns,
                )
                count += result.applied_count
        finally:
            self._draining = False
        return count

    async def ingest_trade_ack(
        self,
        payload: Mapping[str, Any],
        *,
        venue: Venue,
        generation: int,
        receive_mono_ns: int,
        client_id: str,
    ) -> int:
        adapter = self._adapter
        if self._fatal is not None or adapter is None:
            raise LivePrivateBridgeError(self._fatal or "intent_not_bound")
        batch = adapter.adapt(
            payload,
            venue=venue,
            source="trade_ack",
            generation=generation,
            receive_mono_ns=receive_mono_ns,
            expected_client_id=client_id,
        )
        result = await self._engine.ingest_adapter_batch_durable(batch)
        if not result.accepted:
            self._fatal = result.reason_code or "ack_ingest_failed"
            raise LivePrivateBridgeError(self._fatal)
        self.chronometry.record_accepted_batch(
            batch.events,
            payload=payload,
            venue=venue,
            receive_mono_ns=receive_mono_ns,
        )
        return result.applied_count

    async def ingest_complete_rest_snapshot(
        self,
        payload: Mapping[str, Any],
        *,
        venue: Venue,
        source: str,
        generation: int,
        receive_mono_ns: int,
    ) -> int:
        """Durable REST reconciliation; caller must have proved all pages.

        This API never infers completeness from an empty first page.  A live
        caller must reject cursors, missing pages and generation changes
        before setting ``snapshot_complete`` here.
        """
        if source not in {"rest_positions", "rest_open_orders"}:
            raise LivePrivateBridgeError("invalid_rest_source")
        adapter = self._adapter
        if self._fatal is not None or adapter is None or self._pending:
            raise LivePrivateBridgeError(self._fatal or "private_evidence_unsettled")
        batch = adapter.adapt(
            payload,
            venue=venue,
            source=source,
            generation=generation,
            receive_mono_ns=receive_mono_ns,
            snapshot_complete=True,
        )
        result = await self._engine.ingest_adapter_batch_durable(batch)
        if not result.accepted:
            self._fatal = result.reason_code or "rest_ingest_failed"
            raise LivePrivateBridgeError(self._fatal)
        return result.applied_count
