"""Same-owner-loop live EV2 submit and private-evidence pump.

This is a transport route, not an arm switch or standalone experiment. Its
caller must have already established account ownership, a complete flat
baseline, position mode, fresh metadata/books, and an EV2 durable WAL.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import replace
from typing import Any, Callable, Optional, Sequence

from app.bot.execution.contracts import LegPlan, TradeIntent, Venue
from app.bot.execution.engine import ExecutionEngine, SubmitResult
from app.bot.execution.live_private_bridge import LivePrivateEvidenceBridge


class LiveOwnerRouteError(RuntimeError):
    """The route cannot safely accept another planned live submission."""


class LiveOwnerLoopRoute:
    """One bounded EV2 route; callbacks and submit execute on trade WS loop."""

    def __init__(
        self,
        *,
        owner_loop: asyncio.AbstractEventLoop,
        engine: ExecutionEngine,
        private_bridge: LivePrivateEvidenceBridge,
        plan_resolver: Callable[[TradeIntent], Sequence[LegPlan]],
        max_planned_submissions: int = 6,
    ) -> None:
        if (
            not isinstance(owner_loop, asyncio.AbstractEventLoop)
            or not isinstance(engine, ExecutionEngine)
            or not isinstance(private_bridge, LivePrivateEvidenceBridge)
            or getattr(getattr(engine, "_transport", None), "loop", None) is not owner_loop
            or getattr(private_bridge, "_engine", None) is not engine
            or not callable(plan_resolver)
            or isinstance(max_planned_submissions, bool)
            or not 1 <= max_planned_submissions <= 6
        ):
            raise LiveOwnerRouteError("invalid_live_route")
        self.owner_loop = owner_loop
        self.engine = engine
        self.bridge = private_bridge
        self.plan_resolver = plan_resolver
        self.max_planned_submissions = max_planned_submissions
        self.planned_submissions = 0
        self._wake: Optional[asyncio.Event] = None
        self._pump_task: Optional[asyncio.Task[None]] = None
        self._warm_loop: Any = None
        self._fatal: Optional[str] = None

    @property
    def fatal_reason(self) -> Optional[str]:
        return self._fatal or self.bridge.fatal_reason

    def _assert_owner(self) -> None:
        if asyncio.get_running_loop() is not self.owner_loop:
            raise LiveOwnerRouteError("foreign_loop")

    async def start(self, *, warm_loop: Any, session: Any) -> None:
        """Install non-consuming private/trade taps on the warm owner loop."""
        self._assert_owner()
        if self._pump_task is not None or getattr(warm_loop, "loop", None) is not self.owner_loop:
            raise LiveOwnerRouteError("invalid_warm_owner")
        self._wake = asyncio.Event()
        installed: list[str] = []
        try:
            for name, venue, runtime in (
                ("bybit", Venue.BYBIT, session.bybit_runtime),
                ("okx", Venue.OKX, session.okx_runtime),
            ):
                def private_cb(text: str, recv_ns: int, *, v: Venue = venue, rt: Any = runtime) -> None:
                    self.bridge.observe(
                        text, recv_ns, venue=v, generation=rt.reconnect_generation,
                    )
                    assert self._wake is not None
                    self._wake.set()

                def trade_cb(text: str, recv_ns: int, *, v: Venue = venue, rt: Any = runtime) -> None:
                    self.bridge.observe_trade(
                        text, recv_ns, venue=v, generation=rt.reconnect_generation,
                    )
                    assert self._wake is not None
                    self._wake.set()

                installed.append(name)
                warm_loop.set_private_frame_observer(name, private_cb)
                warm_loop.set_trade_frame_observer(name, trade_cb)
        except Exception:
            for name in installed:
                warm_loop.set_private_frame_observer(name, None)
                warm_loop.set_trade_frame_observer(name, None)
            self._wake = None
            raise
        self._warm_loop = warm_loop
        self._pump_task = asyncio.create_task(self._pump(), name="ev2-live-private-pump")

    async def stop(self) -> None:
        self._assert_owner()
        warm = self._warm_loop
        if warm is not None:
            for name in ("bybit", "okx"):
                warm.set_private_frame_observer(name, None)
                warm.set_trade_frame_observer(name, None)
        self._warm_loop = None
        task = self._pump_task
        self._pump_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _halt(self, reason: str) -> None:
        self._fatal = reason
        self.engine.publish_readiness(replace(self.engine.readiness, kill_switch=True))

    async def _pump(self) -> None:
        assert self._wake is not None
        while True:
            await self._wake.wait()
            self._wake.clear()
            if self.bridge.pending_count == 0 or not self.bridge.bound:
                continue
            try:
                await self.bridge.drain()
            except Exception:
                self._halt(self.bridge.fatal_reason or "private_evidence_failed")
                return

    async def submit_intent(self, intent: TradeIntent) -> SubmitResult:
        """Count the entire dual-leg attempt, including partial WS writes."""
        self._assert_owner()
        if self._pump_task is None or self.fatal_reason is not None:
            raise LiveOwnerRouteError("live_route_not_healthy")
        if self.planned_submissions >= self.max_planned_submissions:
            raise LiveOwnerRouteError("planned_submission_budget_exhausted")
        plans = tuple(self.plan_resolver(intent))
        self.bridge.begin_submission(plans)
        try:
            result = await self.engine.submit(intent, prepared_plans=plans)
        except BaseException:
            # A raised submit may have written one or both sockets. Freeze
            # instead of guessing whether the attempt was unwritten.
            self._halt("submit_outcome_unknown")
            raise
        state = self.engine.state
        attempted_write = bool(
            result.dispatch is not None and any(
                evidence.asend_start_mono_ns is not None
                for evidence in (result.dispatch.bybit, result.dispatch.okx)
            )
        )
        if attempted_write and (state.intent_id != intent.intent_id or not state.legs):
            self.planned_submissions += 1
            self._halt("write_without_committed_state")
            raise LiveOwnerRouteError("write_without_committed_state")
        if state.intent_id == intent.intent_id and state.legs:
            if result.dispatch is None:
                self._halt("dispatch_evidence_missing")
                raise LiveOwnerRouteError("dispatch_evidence_missing")
            self.planned_submissions += 1
            ready = self.engine.readiness
            try:
                self.bridge.bind_submitted(
                    intent, plans,
                    bybit_generation=ready.bybit_generation,
                    okx_generation=ready.okx_generation,
                    dispatch=result.dispatch,
                )
            except Exception:
                self._halt("private_bind_failed")
                raise
            assert self._wake is not None
            self._wake.set()
        else:
            try:
                self.bridge.abort_unwritten_submission()
            except Exception:
                self._halt("unwritten_submission_unsettled")
                raise
        return result

    def submit_threadsafe(self, intent: TradeIntent) -> concurrent.futures.Future[SubmitResult]:
        """Cross-thread entry for a public-feed loop; never runs send there."""
        if self.owner_loop.is_closed():
            raise LiveOwnerRouteError("owner_loop_closed")
        return asyncio.run_coroutine_threadsafe(self.submit_intent(intent), self.owner_loop)
