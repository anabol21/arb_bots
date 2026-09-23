"""Exclusive no-order audit lane with durable WAL evidence.

The caller supplies actual policy intents and an independently sampled private
readiness source. This lane owns no venue sockets and cannot dispatch orders.
It does not substitute for a live execution adapter.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable, Optional

from app.bot.execution.contracts import ExecutionEventType, IntentAction, SpreadStatus, TradeIntent
from app.bot.execution.engine import ExecutionEngine, NoOrderAuditResult, ReadinessSnapshot
from app.bot.execution.wal import ExecutionWal

ReadinessSource = Callable[[], ReadinessSnapshot]


@dataclass(frozen=True)
class DurableNoOrderAudit:
    intent_id: str
    result: Optional[NoOrderAuditResult]
    wal_durable: bool
    replay_proven: bool
    halted: bool
    reason_code: Optional[str]

    @property
    def passed(self) -> bool:
        return bool(
            not self.halted
            and self.wal_durable
            and self.replay_proven
            and self.result is not None
            and self.result.reason_code is None
            and self.result.prewrite is not None
            and self.result.prewrite.ready
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "intent_id": self.intent_id,
            "audit": None if self.result is None else self.result.to_public_dict(),
            "wal_durable": self.wal_durable,
            "replay_proven": self.replay_proven,
            "halted": self.halted,
            "passed": self.passed,
            "reason_code": self.reason_code,
            "orders_sent": 0,
            "trade_socket_bound": False,
        }


class NoOrderAuditLane:
    """Serialize audit → fsync → replay; latch shut on durability uncertainty."""

    def __init__(
        self,
        engine: ExecutionEngine,
        wal: ExecutionWal,
        readiness_source: ReadinessSource,
    ) -> None:
        if not isinstance(engine, ExecutionEngine) or not isinstance(wal, ExecutionWal):
            raise TypeError("invalid_no_order_audit_lane")
        if not callable(readiness_source):
            raise TypeError("invalid_readiness_source")
        self.engine = engine
        self.wal = wal
        self.readiness_source = readiness_source
        self._lock = asyncio.Lock()
        self._halted = False

    async def audit(self, intent: TradeIntent) -> DurableNoOrderAudit:
        if not isinstance(intent, TradeIntent):
            raise TypeError("invalid_intent")
        async with self._lock:
            if self._halted:
                return DurableNoOrderAudit(intent.intent_id, None, False, False, True, "lane_halted")
            # This source is sampled for each attempt, never cached from a
            # heartbeat or from the previous policy decision.
            try:
                readiness = self.readiness_source()
            except Exception:
                self._halted = True
                return DurableNoOrderAudit(intent.intent_id, None, False, False, True, "readiness_source_failed")
            if not isinstance(readiness, ReadinessSnapshot):
                self._halted = True
                return DurableNoOrderAudit(intent.intent_id, None, False, False, True, "readiness_source_failed")
            if readiness.bybit_trade_ready or readiness.okx_trade_ready:
                self._halted = True
                return DurableNoOrderAudit(intent.intent_id, None, False, False, True, "trade_socket_bound")
            self.engine.publish_readiness(readiness)
            result = await self.engine.audit_intent(intent)
            if not result.wal_accepted:
                return DurableNoOrderAudit(intent.intent_id, result, False, False, False, result.reason_code)
            try:
                await asyncio.to_thread(self.wal.drain_all)
                replay = await asyncio.to_thread(self.wal.replay)
            except Exception:
                self._halted = True
                return DurableNoOrderAudit(intent.intent_id, result, False, False, True, "wal_not_durable")
            pair = [
                record.event for record in replay.records
                if record.event.intent_id == intent.intent_id
                and record.event.event_type in {
                    ExecutionEventType.INTENT_ACCEPTED,
                    ExecutionEventType.INTENT_REJECTED,
                }
            ]
            proven = bool(
                replay.integrity_ok
                and not replay.torn_tail
                and replay.state.status is SpreadStatus.IDLE
                and intent.action is IntentAction.OPEN
                and len(pair) == 2
                and pair[0].event_type is ExecutionEventType.INTENT_ACCEPTED
                and pair[1].event_type is ExecutionEventType.INTENT_REJECTED
                and pair[1].payload.get("audit_mode") == "no_order_prewrite"
                and not any(
                    record.event.event_type is ExecutionEventType.REQUEST_SENT
                    for record in replay.records
                )
            )
            if not proven:
                self._halted = True
                return DurableNoOrderAudit(intent.intent_id, result, True, False, True, "replay_not_proven")
            return DurableNoOrderAudit(intent.intent_id, result, True, True, False, result.reason_code)
