"""Bounded RAM diagnostics for one TW emit cycle's await boundaries.

Times include await scheduling/queueing. A cancelled ``to_thread`` await does
not stop its worker. Journal/manager helper returns are not durable-success or
decision-latency claims; those helpers may handle errors internally. No tick,
quote, row, decision, exception or worker payload is retained.

The collector and its snapshot accessor belong to the owning asyncio loop.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import nullcontext
from dataclasses import dataclass
import os
import time
from typing import Callable, Mapping


# Reused on the disabled path: no cycle/span objects or clock calls.
NO_TRACE = nullcontext()


def latency_trace_enabled(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return str(values.get("BBOT_LATENCY_TRACE", "")).strip().lower() in {
        "1", "true", "yes", "on"
    }


@dataclass(frozen=True, slots=True)
class EmitAwaitSpan:
    stage: str
    started_ns: int
    ended_ns: int
    outcome: str


@dataclass(frozen=True, slots=True)
class EmitLatencyCycle:
    cycle_id: int
    started_ns: int
    ended_ns: int
    outcome: str
    spans: tuple[EmitAwaitSpan, ...]


@dataclass(frozen=True, slots=True)
class EmitLatencySnapshot:
    cycles: tuple[EmitLatencyCycle, ...]
    dropped_cycles: int
    raw_to_policy: tuple[RawToPolicyLatency, ...] = ()
    dropped_raw_to_policy: int = 0


@dataclass(frozen=True, slots=True)
class RawToPolicyLatency:
    started_ns: int
    ended_ns: int
    elapsed_ns: int


def _outcome(exc_type: type[BaseException] | None) -> str:
    if exc_type is None:
        return "completed"
    return "cancelled" if issubclass(exc_type, asyncio.CancelledError) else "error"


class EmitLatencyTrace:
    def __init__(
        self, *, capacity: int = 128, clock: Callable[[], int] | None = None
    ) -> None:
        if capacity <= 0:
            raise ValueError("latency trace capacity must be positive")
        self._cycles: deque[EmitLatencyCycle] = deque(maxlen=capacity)
        self._clock = time.monotonic_ns if clock is None else clock
        self._next_cycle_id = 0
        self._dropped_cycles = 0
        self._raw_to_policy: deque[RawToPolicyLatency] = deque(maxlen=capacity)
        self._dropped_raw_to_policy = 0

    def cycle(self) -> _Cycle:
        self._next_cycle_id += 1
        return _Cycle(self, self._next_cycle_id)

    def snapshot(self) -> EmitLatencySnapshot:
        return EmitLatencySnapshot(
            tuple(self._cycles), self._dropped_cycles,
            tuple(self._raw_to_policy), self._dropped_raw_to_policy,
        )

    def record_raw_to_policy(self, started_ns: int, ended_ns: int) -> None:
        """Retain only bounded monotonic timestamps and elapsed time."""
        started = int(started_ns)
        ended = int(ended_ns)
        if started < 0 or ended < started:
            return
        if len(self._raw_to_policy) == self._raw_to_policy.maxlen:
            self._dropped_raw_to_policy += 1
        self._raw_to_policy.append(RawToPolicyLatency(started, ended, ended - started))


class _Cycle:
    def __init__(self, owner: EmitLatencyTrace, cycle_id: int) -> None:
        self._owner = owner
        self._cycle_id = cycle_id
        self._spans: list[EmitAwaitSpan] = []
        self._outcome = "completed"

    def __enter__(self) -> _Cycle:
        self._started_ns = self._owner._clock()
        return self

    def span(self, stage: str) -> _AwaitSpan:
        return _AwaitSpan(self, stage)

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc_type is not None:
            self._outcome = _outcome(exc_type)
        record = EmitLatencyCycle(
            self._cycle_id, self._started_ns, self._owner._clock(),
            self._outcome, tuple(self._spans),
        )
        if len(self._owner._cycles) == self._owner._cycles.maxlen:
            self._owner._dropped_cycles += 1
        self._owner._cycles.append(record)
        return False


class _AwaitSpan:
    def __init__(self, cycle: _Cycle, stage: str) -> None:
        self._cycle = cycle
        self._stage = stage

    def __enter__(self) -> None:
        self._started_ns = self._cycle._owner._clock()

    def __exit__(self, exc_type, exc, traceback) -> bool:
        outcome = _outcome(exc_type)
        self._cycle._spans.append(EmitAwaitSpan(
            self._stage, self._started_ns, self._cycle._owner._clock(), outcome
        ))
        if outcome != "completed":
            self._cycle._outcome = outcome
        return False
