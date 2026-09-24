"""Per-leg EV2 live milestones, without conflating venue and local clocks.

Only adapter-accepted ACK/fill events are recorded. Exchange fill timestamps
remain in the exchange's wall-clock domain; local signal/send/receive stamps
remain monotonic. A chart must not subtract those two clock domains directly.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Optional, Sequence

from app.bot.execution.contracts import ExecutionEvent, ExecutionEventType, LegPlan, TradeIntent, Venue
from app.bot.execution.transport import DispatchResult


@dataclass(frozen=True)
class LegMilestones:
    intent_id: str
    venue: Venue
    client_id: str
    signal_mono_ns: int
    signal_wall_ns: int
    send_start_mono_ns: Optional[int] = None
    send_done_mono_ns: Optional[int] = None
    ack_receive_mono_ns: Optional[int] = None
    first_fill_receive_mono_ns: Optional[int] = None
    first_fill_exchange_ms: Optional[int] = None
    full_fill_receive_mono_ns: Optional[int] = None
    full_fill_exchange_ms: Optional[int] = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "venue": self.venue.value,
            "client_id": self.client_id,
            "signal_mono_ns": self.signal_mono_ns,
            "signal_wall_ns": self.signal_wall_ns,
            "send_start_mono_ns": self.send_start_mono_ns,
            "send_done_mono_ns": self.send_done_mono_ns,
            "ack_receive_mono_ns": self.ack_receive_mono_ns,
            "first_fill_receive_mono_ns": self.first_fill_receive_mono_ns,
            "first_fill_exchange_ms": self.first_fill_exchange_ms,
            "full_fill_receive_mono_ns": self.full_fill_receive_mono_ns,
            "full_fill_exchange_ms": self.full_fill_exchange_ms,
        }


def _exchange_fill_ms(row: Mapping[str, Any], venue: Venue) -> Optional[int]:
    # Never substitute order creation/update time for an exchange fill time.
    keys = ("execTime",) if venue is Venue.BYBIT else ("fillTime", "fillTs")
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.isdigit():
            stamp = int(value)
            if 1_000_000_000_000 <= stamp < 10_000_000_000_000:
                return stamp
        elif isinstance(value, int) and not isinstance(value, bool):
            if 1_000_000_000_000 <= value < 10_000_000_000_000:
                return value
    return None


class LiveChronometry:
    def __init__(self) -> None:
        self._legs: dict[tuple[str, Venue], LegMilestones] = {}

    def bind_dispatch(
        self, intent: TradeIntent, plans: Sequence[LegPlan], dispatch: DispatchResult
    ) -> None:
        if dispatch.intent_id != intent.intent_id or dispatch.run_id != intent.run_id:
            raise ValueError("chronometry_dispatch_mismatch")
        by_venue = {plan.venue: plan for plan in plans}
        if len(plans) != 2 or set(by_venue) != {Venue.BYBIT, Venue.OKX}:
            raise ValueError("chronometry_plan_mismatch")
        for venue, evidence in ((Venue.BYBIT, dispatch.bybit), (Venue.OKX, dispatch.okx)):
            plan = by_venue[venue]
            if evidence.client_id != plan.client_id:
                raise ValueError("chronometry_client_mismatch")
            key = (intent.intent_id, venue)
            if key in self._legs:
                raise ValueError("chronometry_duplicate_dispatch")
            self._legs[key] = LegMilestones(
                intent_id=intent.intent_id,
                venue=venue,
                client_id=plan.client_id,
                signal_mono_ns=intent.signal_mono_ns,
                signal_wall_ns=intent.signal_wall_ns,
                send_start_mono_ns=evidence.asend_start_mono_ns,
                send_done_mono_ns=evidence.asend_done_mono_ns,
            )

    def record_accepted_batch(
        self,
        events: Sequence[ExecutionEvent],
        *,
        payload: Mapping[str, Any],
        venue: Venue,
        receive_mono_ns: int,
    ) -> None:
        rows = payload.get("data")
        for event in events:
            if event.venue is not venue:
                continue
            key = (event.intent_id, venue)
            rec = self._legs.get(key)
            if rec is None:
                continue
            if event.event_type in {ExecutionEventType.ACK_ACCEPTED, ExecutionEventType.ACK_REJECTED}:
                if rec.ack_receive_mono_ns is None:
                    self._legs[key] = replace(rec, ack_receive_mono_ns=receive_mono_ns)
                continue
            if event.event_type not in {ExecutionEventType.PARTIAL_FILL, ExecutionEventType.FILL}:
                continue
            exchange_ms = None
            if isinstance(rows, list):
                client_key = "orderLinkId" if venue is Venue.BYBIT else "clOrdId"
                matched = [
                    row for row in rows
                    if isinstance(row, Mapping) and row.get(client_key) == rec.client_id
                ]
                # Multiple cumulative rows cannot be assigned to one EV2
                # event without a venue execution ID; leave timestamp blank.
                if len(matched) == 1:
                    exchange_ms = _exchange_fill_ms(matched[0], venue)
            if rec.first_fill_receive_mono_ns is None:
                rec = replace(
                    rec,
                    first_fill_receive_mono_ns=receive_mono_ns,
                    first_fill_exchange_ms=exchange_ms,
                )
            if event.event_type is ExecutionEventType.FILL and rec.full_fill_receive_mono_ns is None:
                rec = replace(
                    rec,
                    full_fill_receive_mono_ns=receive_mono_ns,
                    full_fill_exchange_ms=exchange_ms,
                )
            self._legs[key] = rec

    def snapshot(self) -> tuple[LegMilestones, ...]:
        return tuple(self._legs.values())
