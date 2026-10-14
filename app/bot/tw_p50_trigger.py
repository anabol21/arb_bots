"""Lossless coalescing wakeup for experimental event-driven TW p50."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Mapping


def tw_p50_event_driven_enabled(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return str(values.get("BBOT_TW_P50_EVENT_DRIVEN", "")).strip().lower() in {
        "1", "true", "yes", "on"
    }


def tw_p50_min_interval_sec(env: Mapping[str, str] | None = None) -> float:
    """Bound event-driven recomputes to at most 20/s by default and always."""
    values = os.environ if env is None else env
    try:
        milliseconds = int(values.get("BBOT_TW_P50_MIN_INTERVAL_MS", "50"))
    except (TypeError, ValueError):
        milliseconds = 50
    return max(50, min(milliseconds, 10_000)) / 1000.0


def assert_shadow_canary_context(
    *,
    profile: str,
    mode: str,
    broker_is_stub: bool,
    env: Mapping[str, str] | None = None,
) -> None:
    """Refuse event-driven evaluation outside the isolated no-send profile."""
    values = os.environ if env is None else env
    live_orders = str(values.get("LIVE_ORDERS", "0")).strip().lower()
    live_send = str(values.get("BBOT_THETA_LIVE_SEND", "0")).strip().lower()
    if (
        str(profile).strip().lower() != "gear22_would_send"
        or str(mode).strip().lower() != "policy"
        or not broker_is_stub
        or live_orders in {"1", "true", "yes", "on"}
        or live_send in {"1", "true", "yes", "on"}
    ):
        raise ValueError("event-driven TW p50 requires gear22_would_send policy with stub/no-send")


class CoalescedTrigger:
    """Keep one pending wakeup and its first event-arrival timestamp.

    All methods are called on the owning asyncio loop. Calling ``signal``
    repeatedly before ``consume`` coalesces updates because the observer holds
    the latest state; an update during compute remains pending for the next
    cycle.
    """

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self._started_ns: int | None = None

    def signal(self, started_ns: int) -> None:
        if not self.event.is_set():
            self._started_ns = int(started_ns)
            self.event.set()

    def consume(self) -> int | None:
        if not self.event.is_set():
            return None
        started_ns = self._started_ns
        self._started_ns = None
        self.event.clear()
        return started_ns


async def wait_for_trigger(
    trigger: CoalescedTrigger | None,
    stop_event: asyncio.Event,
    *,
    timeout: float,
) -> str:
    """Return ``event``, ``stopped`` or ``timeout``; ``None`` is periodic mode."""
    if trigger is None:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=timeout)
            return "stopped"
        except asyncio.TimeoutError:
            return "timeout"

    trigger_task = asyncio.create_task(trigger.event.wait())
    stop_task = asyncio.create_task(stop_event.wait())
    tasks = (trigger_task, stop_task)
    try:
        done, _ = await asyncio.wait(
            tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if stop_task in done:
            return "stopped"
        if trigger_task in done:
            return "event"
        return "timeout"
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def wait_until_allowed(
    stop_event: asyncio.Event,
    next_allowed_ns: int,
    *,
    clock_ns=time.monotonic_ns,
) -> bool:
    """Honor the minimum start-to-start interval; return True on stop."""
    remaining_ns = int(next_allowed_ns) - int(clock_ns())
    if remaining_ns <= 0:
        return False
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=remaining_ns / 1e9)
        return True
    except asyncio.TimeoutError:
        return False
