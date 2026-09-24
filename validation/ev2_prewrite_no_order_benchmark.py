"""Isolated EV2 durable-prewrite timing probe. No network or order credentials.

The first series runs the real ExecutionEngine.submit path with opt-in WAL
durability, but both venue sockets are memory-only captures and readiness is
explicitly simulated. The second series runs the existing network-incapable
NoOrderAuditLane while its WAL history grows. Neither series is a live order
or a private-channel qualification.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import tempfile
import time
import uuid
from decimal import Decimal
from pathlib import Path

from app.bot.execution.contracts import (
    SCHEMA_VERSION as CONTRACT_SCHEMA_VERSION,
    ExecutionEvent,
    ExecutionEventType,
    IntentAction,
    LegPlan,
    SpreadDirection,
    TradeIntent,
    Venue,
)
from app.bot.execution.engine import (
    ExecutionEngine,
    ReadinessSnapshot,
    RiskPolicy,
    SubmitStatus,
)
from app.bot.execution.no_order_audit_lane import NoOrderAuditLane
from app.bot.execution.ownership import FileOwnershipFence
from app.bot.execution.transport import (
    CachedInstrument,
    ExecutionTransport,
    InstrumentCache,
    NoOrderTradeSocket,
    unsigned_frame_finalizer,
)
from app.bot.execution.wal import ExecutionWal


class MemoryOnlySocket:
    """Implements the send interface without any network-capable object."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.owner_loop = loop
        self.calls = 0
        self.start_ns: int | None = None
        self.done_ns: int | None = None

    async def asend(self, _text: str) -> None:
        self.start_ns = time.monotonic_ns()
        self.calls += 1
        self.done_ns = time.monotonic_ns()


class TimedPrewriteEngine(ExecutionEngine):
    def _accepted_is_durable(self, accepted_event: ExecutionEvent) -> bool:
        start = time.monotonic_ns()
        try:
            return super()._accepted_is_durable(accepted_event)
        finally:
            self.fence_elapsed_ns = time.monotonic_ns() - start


def _readiness(*, simulated_trade: bool) -> ReadinessSnapshot:
    return ReadinessSnapshot(
        bybit_trade_ready=simulated_trade,
        okx_trade_ready=simulated_trade,
        bybit_private_ready=True,
        okx_private_ready=True,
        bybit_generation=1,
        okx_generation=1,
        kill_switch=False,
        pause=False,
    )


def _cache() -> InstrumentCache:
    now = time.monotonic_ns()
    # Long-running replay-growth probes must not accidentally benchmark
    # metadata TTL expiry rather than WAL latency.
    expiry = now + 3 * 60 * 60 * 1_000_000_000
    return InstrumentCache.from_snapshots((
        CachedInstrument(Venue.BYBIT, "BTCUSDT", now, expiry),
        CachedInstrument(Venue.OKX, "BTC-USDT-SWAP", now, expiry,
                         inst_id_code=193761),
    ))


def _plans(intent: TradeIntent) -> tuple[LegPlan, LegPlan]:
    return (
        LegPlan.build(intent_id=intent.intent_id, leg_id="leg_bybit",
                      venue=Venue.BYBIT, instrument="BTCUSDT", side="sell",
                      quantity=Decimal("0.001")),
        LegPlan.build(intent_id=intent.intent_id, leg_id="leg_okx",
                      venue=Venue.OKX, instrument="BTC-USDT-SWAP", side="buy",
                      quantity=Decimal("0.001")),
    )


def _intent(run_id: str) -> TradeIntent:
    signal_ns = time.monotonic_ns()
    return TradeIntent(
        schema_version=CONTRACT_SCHEMA_VERSION,
        intent_id=str(uuid.uuid4()),
        run_id=run_id,
        policy_version="ev2.prewrite.benchmark",
        action=IntentAction.OPEN,
        spread_direction=SpreadDirection.LONG,
        coin="BTC",
        notional_usdt=Decimal("10"),
        signal_mono_ns=signal_ns,
        signal_wall_ns=time.time_ns(),
        expiry_mono_ns=signal_ns + 10_000_000_000,
        signal_snapshot_ref="benchmark_no_market_data",
        canary_stage="shadow",
        risk_policy_revision="benchmark.v1",
    )


def _ready_wal(path: Path, run_id: str) -> ExecutionWal:
    wal = ExecutionWal(path, run_id=run_id, max_queue=4096,
                       reserved_tail=64, max_durable_lag=4096)
    wal.replay()
    tokens: list[str] = []
    recon_intent_id = str(uuid.uuid4())
    for seq, venue in enumerate((Venue.BYBIT, Venue.OKX), start=1):
        event = ExecutionEvent(
            schema_version=CONTRACT_SCHEMA_VERSION,
            event_id=uuid.uuid4().hex,
            event_type=ExecutionEventType.RECONCILIATION,
            intent_id=recon_intent_id,
            run_id=run_id,
            sequence=seq,
            monotonic_ns=time.monotonic_ns(),
            venue=venue,
            leg_id=None,
            payload={"matched": True},
        )
        if not wal.enqueue(event).accepted:
            raise RuntimeError("benchmark_reconciliation_enqueue_failed")
        durable = wal.drain_once()
        if durable is None or not durable.reconciliation_token:
            raise RuntimeError("benchmark_reconciliation_not_durable")
        tokens.append(durable.reconciliation_token)
    for token in tokens:
        wal.mark_venue_reconciled(token)
    if not wal.health().venue_reconciliation_complete:
        raise RuntimeError("benchmark_reconciliation_not_matched")
    return wal


def _engine(
    *, loop: asyncio.AbstractEventLoop, run_id: str, wal: ExecutionWal,
    ownership: FileOwnershipFence, bybit: object, okx: object,
    simulated_trade: bool, durable_prewrite: bool,
) -> TimedPrewriteEngine:
    transport = ExecutionTransport(
        loop, bybit_socket=bybit, okx_socket=okx,
        finalize_frame=unsigned_frame_finalizer,
        monotonic_ns=time.monotonic_ns,
    )
    anchor = wal.replay()
    return TimedPrewriteEngine(
        run_id=run_id,
        wal=wal,
        transport=transport,
        plan_resolver=_plans,
        instrument_cache=_cache(),
        risk_policy=RiskPolicy(allowed_coins=frozenset({"BTC"}),
                               max_notional_usdt=Decimal("10")),
        readiness=_readiness(simulated_trade=simulated_trade),
        ownership=ownership,
        monotonic_ns=time.monotonic_ns,
        state=anchor.state,
        durable_prewrite=durable_prewrite,
        prewrite_anchor=anchor if durable_prewrite else None,
    )


def _stats(samples_ns: list[int]) -> dict[str, float | int]:
    if not samples_ns:
        raise RuntimeError("benchmark_no_samples")
    sorted_ns = sorted(samples_ns)

    def percentile(p: float) -> float:
        index = max(0, min(len(sorted_ns) - 1, int((len(sorted_ns) - 1) * p + 0.5)))
        return round(sorted_ns[index] / 1_000, 3)

    return {
        "n": len(sorted_ns),
        "p50_us": percentile(0.50),
        "p95_us": percentile(0.95),
        "p99_us": percentile(0.99),
        "max_us": round(sorted_ns[-1] / 1_000, 3),
        "mean_us": round(statistics.mean(sorted_ns) / 1_000, 3),
    }


async def run_benchmark(*, samples: int, history: int) -> dict[str, object]:
    if not 5 <= samples <= 500 or not 5 <= history <= 1000:
        raise ValueError("benchmark_counts_out_of_bounds")
    loop = asyncio.get_running_loop()
    fence_ns: list[int] = []
    first_asend_ns: list[int] = []
    slowest_asend_ns: list[int] = []
    history_total_ns: list[int] = []
    grown_exact: dict[str, float | int] = {}
    with tempfile.TemporaryDirectory(prefix="ev2-prewrite-no-order-") as tmp:
        root = Path(tmp)
        for index in range(samples):
            run_id = "benchmark_" + uuid.uuid4().hex
            sample_root = root / f"fence-{index:04d}"
            wal = _ready_wal(sample_root / "wal.v2" / "wal.jsonl", run_id)
            ownership = FileOwnershipFence(sample_root / "owner.lock")
            ownership.acquire()
            try:
                bybit = MemoryOnlySocket(loop)
                okx = MemoryOnlySocket(loop)
                engine = _engine(
                    loop=loop, run_id=run_id, wal=wal, ownership=ownership,
                    bybit=bybit, okx=okx, simulated_trade=True,
                    durable_prewrite=True,
                )
                intent = _intent(run_id)
                result = await engine.submit(intent)
                if result.status is not SubmitStatus.ACCEPTED:
                    raise RuntimeError(f"benchmark_submit_failed:{result.reason_code}")
                if bybit.calls != 1 or okx.calls != 1:
                    raise RuntimeError("benchmark_memory_boundary_not_reached")
                assert bybit.start_ns is not None and okx.start_ns is not None
                assert bybit.done_ns is not None and okx.done_ns is not None
                if engine._wal.health().durable_wal_seq < 3:
                    raise RuntimeError("benchmark_prewrite_not_durable")
                fence_ns.append(engine.fence_elapsed_ns)
                first_asend_ns.append(min(bybit.start_ns, okx.start_ns) - intent.signal_mono_ns)
                slowest_asend_ns.append(max(bybit.done_ns, okx.done_ns) - intent.signal_mono_ns)
            finally:
                ownership.release()

        run_id = "benchmark_" + uuid.uuid4().hex
        sample_root = root / "history"
        wal = _ready_wal(sample_root / "wal.v2" / "wal.jsonl", run_id)
        ownership = FileOwnershipFence(sample_root / "owner.lock")
        ownership.acquire()
        try:
            bybit = NoOrderTradeSocket(loop)
            okx = NoOrderTradeSocket(loop)
            engine = _engine(
                loop=loop, run_id=run_id, wal=wal, ownership=ownership,
                bybit=bybit, okx=okx, simulated_trade=False,
                durable_prewrite=False,
            )
            lane = NoOrderAuditLane(
                engine, wal, lambda: _readiness(simulated_trade=False)
            )
            for _ in range(history):
                intent = _intent(run_id)
                result = await lane.audit(intent)
                if not result.passed:
                    raise RuntimeError(f"benchmark_no_order_audit_failed:{result.reason_code}")
                history_total_ns.append(time.monotonic_ns() - intent.signal_mono_ns)
            if bybit.write_attempts or okx.write_attempts:
                raise RuntimeError("benchmark_no_order_socket_was_used")
            history_bytes = wal.path.stat().st_size
            if wal.health().queue_depth != 0:
                raise RuntimeError("benchmark_history_wal_not_durable")
            # One exact engine signal after a long audit-only WAL proves the
            # hot-path fence is not replaying the entire accumulated file.
            ownership.release()
            ownership.acquire()
            bybit_exact = MemoryOnlySocket(loop)
            okx_exact = MemoryOnlySocket(loop)
            exact_engine = _engine(
                loop=loop, run_id=run_id, wal=wal, ownership=ownership,
                bybit=bybit_exact, okx=okx_exact, simulated_trade=True,
                durable_prewrite=True,
            )
            exact_intent = _intent(run_id)
            exact_result = await exact_engine.submit(exact_intent)
            if exact_result.status is not SubmitStatus.ACCEPTED:
                raise RuntimeError(f"benchmark_grown_exact_failed:{exact_result.reason_code}")
            if bybit_exact.start_ns is None or okx_exact.start_ns is None:
                raise RuntimeError("benchmark_grown_exact_missing_memory_asend")
            grown_exact = {
                "prior_wal_bytes": history_bytes,
                "prior_history_attempts": history,
                "fence_us": round(exact_engine.fence_elapsed_ns / 1_000, 3),
                "signal_to_first_memory_asend_us": round(
                    (min(bybit_exact.start_ns, okx_exact.start_ns) - exact_intent.signal_mono_ns) / 1_000, 3
                ),
            }
        finally:
            ownership.release()
    quartile = max(5, history // 4)
    return {
        "schema_version": "bbot.ev2.prewrite_no_order_benchmark.v2",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": platform.node(),
        "python": platform.python_version(),
        "orders_sent": 0,
        "network_capable_sockets": False,
        "private_status_simulated": True,
        "live_latency_gate_eligible": False,
        "latency_boundary": "memory_asend_not_ws_write_or_exchange_fill",
        "fresh_wal_exact_engine_fence": {
            "fence": _stats(fence_ns),
            "signal_to_first_memory_asend": _stats(first_asend_ns),
            "signal_to_slowest_memory_asend_done": _stats(slowest_asend_ns),
        },
        "growing_wal_no_order_audit": {
            "history_attempts": history,
            "final_wal_bytes": history_bytes,
            "all": _stats(history_total_ns),
            "first_quartile": _stats(history_total_ns[:quartile]),
            "last_quartile": _stats(history_total_ns[-quartile:]),
        },
        "grown_wal_exact_engine_single_sample": grown_exact,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--history", type=int, default=300)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run_benchmark(samples=args.samples, history=args.history)),
                     sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
