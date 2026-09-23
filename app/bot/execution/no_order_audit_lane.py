"""Exclusive no-order audit lane with durable WAL evidence.

The caller supplies actual policy intents and an independently sampled private
readiness source. This lane owns no venue sockets and cannot dispatch orders.
It does not substitute for a live execution adapter.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from app.bot.execution.contracts import (
    ExecutionEventType, IntentAction, LegPlan, SpreadDirection, SpreadStatus,
    TradeIntent, Venue,
)
from app.bot.execution.engine import (
    ExecutionEngine, NoOrderAuditResult, ReadinessSnapshot, RiskPolicy,
)
from app.bot.execution.ownership import FileOwnershipFence
from app.bot.execution.readiness import snapshot_from_readonly_companions
from app.bot.execution.transport import (
    CachedInstrument, ExecutionTransport, InstrumentCache, NoOrderTradeSocket,
    unsigned_frame_finalizer,
)
from app.bot.execution.wal import ExecutionWal
from app.bot.stub_broker import InstrumentMeta, signal_price_for_leg, snap_to_lot

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


class NoOrderAuditBridge:
    """Build the no-order engine from frozen universe metadata and companion files."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        data_root: Path,
        run_id: str,
        coins: Sequence[str],
        universe: Mapping[str, InstrumentMeta],
        okx_inst_id_codes: Mapping[str, int],
        status_dir: Path,
        status_max_age_ns: int,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self.run_id = run_id
        self.root = Path(data_root) / "execution-v2-no-order"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._plans: dict[str, tuple[LegPlan, LegPlan]] = {}
        self._universe = {coin.upper(): universe[coin.upper()] for coin in coins}
        self._codes = dict(okx_inst_id_codes)
        self._mono = monotonic_ns
        self._validate_metadata()
        self._fence = FileOwnershipFence(self.root / "owner.lock")
        if status_max_age_ns < 1_000_000_000 or status_max_age_ns > 30_000_000_000:
            raise RuntimeError("no_order_private_status_age_invalid")
        self._fence.acquire()
        try:
            now = self._mono()
            freshness_ns = 3 * 60 * 60 * 1_000_000_000
            cached = []
            for coin, meta in self._universe.items():
                cached.extend((
                    CachedInstrument(
                        Venue.BYBIT, meta.bybit_symbol, now,
                        now + freshness_ns,
                    ),
                    CachedInstrument(
                        Venue.OKX, meta.okx_symbol, now,
                        now + freshness_ns,
                        inst_id_code=self._codes[meta.okx_symbol],
                    ),
                ))
            wal_dir = self.root / run_id / "wal.v2"
            wal_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._wal = ExecutionWal(
                wal_dir / "wal.jsonl", run_id=run_id,
                max_queue=4096, reserved_tail=64, max_durable_lag=4096,
            )
            replay = self._wal.replay()
            if not replay.integrity_ok or replay.torn_tail:
                raise RuntimeError("no_order_wal_replay_failed")
            self._bybit_socket = NoOrderTradeSocket(loop)
            self._okx_socket = NoOrderTradeSocket(loop)
            transport = ExecutionTransport(
                loop, bybit_socket=self._bybit_socket,
                okx_socket=self._okx_socket,
                finalize_frame=unsigned_frame_finalizer,
                monotonic_ns=monotonic_ns,
            )
            self._engine = ExecutionEngine(
                run_id=run_id, wal=self._wal, transport=transport,
                plan_resolver=self._resolve_plans,
                instrument_cache=InstrumentCache.from_snapshots(cached),
                risk_policy=RiskPolicy(
                    allowed_coins=frozenset(self._universe),
                    max_notional_usdt=Decimal("20"),
                ),
                readiness=snapshot_from_readonly_companions(
                    Path(status_dir) / "bybit.json",
                    Path(status_dir) / "okx.json",
                    max_age_ns=status_max_age_ns,
                ),
                ownership=self._fence,
                monotonic_ns=monotonic_ns,
            )
            self._lane = NoOrderAuditLane(
                self._engine,
                self._wal,
                lambda: snapshot_from_readonly_companions(
                    Path(status_dir) / "bybit.json",
                    Path(status_dir) / "okx.json",
                    max_age_ns=status_max_age_ns,
                ),
            )
        except Exception:
            self._fence.release()
            raise

    def _validate_metadata(self) -> None:
        if not self._universe:
            raise RuntimeError("no_order_metadata_missing")
        for coin, meta in self._universe.items():
            code = self._codes.get(meta.okx_symbol)
            if (
                not meta.okx_symbol or not meta.bybit_symbol
                or meta.okx_lot_size <= 0 or meta.okx_min_size < 0
                or meta.bybit_qty_step <= 0 or meta.bybit_min_order_qty < 0
                or isinstance(code, bool) or not isinstance(code, int) or code <= 0
            ):
                raise RuntimeError(f"no_order_metadata_invalid:{coin}")

    def _resolve_plans(self, intent: TradeIntent) -> tuple[LegPlan, LegPlan]:
        plans = self._plans.pop(intent.intent_id, None)
        if plans is None:
            raise ValueError("audit_plan_not_prepared")
        return plans

    def _build_plans(
        self,
        intent: TradeIntent,
        quotes: Mapping[str, Mapping[str, Mapping[str, Any]]],
    ) -> tuple[LegPlan, LegPlan]:
        meta = self._universe.get(intent.coin)
        if meta is None or intent.action is not IntentAction.OPEN:
            raise ValueError("audit_coin_or_action_invalid")
        okx_side, bybit_side = (
            ("buy", "sell") if intent.spread_direction is SpreadDirection.LONG
            else ("sell", "buy")
        )
        books = quotes.get(intent.coin) or {}
        okx_px = signal_price_for_leg(dict(books.get("okx") or {}), okx_side)
        bybit_px = signal_price_for_leg(dict(books.get("bybit") or {}), bybit_side)
        if okx_px <= 0 or bybit_px <= 0:
            raise ValueError("audit_price_invalid")
        notional = float(intent.notional_usdt)
        okx_qty = snap_to_lot(notional / okx_px, meta.okx_lot_size)
        bybit_qty = snap_to_lot(notional / bybit_px, meta.bybit_qty_step)
        if (
            okx_qty < meta.okx_min_size or bybit_qty < meta.bybit_min_order_qty
            or okx_qty <= 0 or bybit_qty <= 0
            or (meta.bybit_min_notional_value > 0 and bybit_qty * bybit_px < meta.bybit_min_notional_value)
        ):
            raise ValueError("audit_size_ineligible")
        return (
            LegPlan.build(
                intent_id=intent.intent_id, leg_id="leg_bybit", venue=Venue.BYBIT,
                instrument=meta.bybit_symbol, side=bybit_side,
                quantity=Decimal(str(bybit_qty)),
            ),
            LegPlan.build(
                intent_id=intent.intent_id, leg_id="leg_okx", venue=Venue.OKX,
                instrument=meta.okx_symbol, side=okx_side,
                quantity=Decimal(str(okx_qty)),
            ),
        )

    async def audit(
        self,
        intent: TradeIntent,
        quotes: Mapping[str, Mapping[str, Mapping[str, Any]]],
    ) -> DurableNoOrderAudit:
        try:
            self._plans[intent.intent_id] = self._build_plans(intent, quotes)
        except (TypeError, ValueError, KeyError):
            return DurableNoOrderAudit(
                intent.intent_id, None, False, False, False,
                "audit_plan_ineligible",
            )
        try:
            return await self._lane.audit(intent)
        finally:
            self._plans.pop(intent.intent_id, None)

    def close(self) -> None:
        if self._fence.owned:
            self._fence.release()
