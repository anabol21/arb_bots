"""EV2 dual-venue readiness source for warm private/trade sessions.

No sockets are opened here. The bridge samples already-owned warm runtimes and
publishes immutable ``ReadinessSnapshot`` values onto the execution loop.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from app.bot.execution.engine import ReadinessSnapshot
from app.bot.private.readiness_status import read_readonly_status

ReadinessPublish = Callable[[ReadinessSnapshot], object]


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value)).strip().lower()


def _connected(socket: object) -> bool:
    return bool(socket is not None and getattr(socket, "connected", False))


def _venue_state(runtime: Any) -> tuple[bool, bool, int]:
    generation = getattr(runtime, "reconnect_generation", 0)
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        generation = 0
    private_ready = bool(
        _connected(getattr(runtime, "private_socket", None))
        and getattr(runtime, "authenticated", False) is True
        and _enum_value(getattr(runtime, "subscription_readiness", "")) == "ready"
        and _enum_value(getattr(runtime, "sequence_state", "")) == "healthy"
        and not bool(getattr(runtime, "sends_blocked", True))
        and not bool(getattr(runtime, "reseed_required", True))
    )
    trade_ready = bool(
        _connected(getattr(runtime, "trade_socket", None))
        and getattr(runtime, "trade_authenticated", False) is True
    )
    return trade_ready, private_ready, generation


def snapshot_from_warm_session(
    session: Any,
    *,
    pause: bool = False,
    kill_switch: bool = False,
) -> ReadinessSnapshot:
    """Build one fail-closed snapshot from both warm venue runtimes."""
    bybit_trade, bybit_private, bybit_generation = _venue_state(
        getattr(session, "bybit_runtime", None)
    )
    okx_trade, okx_private, okx_generation = _venue_state(
        getattr(session, "okx_runtime", None)
    )
    return ReadinessSnapshot(
        bybit_trade_ready=bybit_trade,
        okx_trade_ready=okx_trade,
        bybit_private_ready=bybit_private,
        okx_private_ready=okx_private,
        bybit_generation=bybit_generation,
        okx_generation=okx_generation,
        kill_switch=bool(kill_switch),
        pause=bool(pause),
    )


def snapshot_from_readonly_companions(
    bybit_path: Path,
    okx_path: Path,
    *,
    max_age_ns: int,
    pause: bool = False,
    kill_switch: bool = False,
) -> ReadinessSnapshot:
    """Sample separate read-only processes for a no-order audit only.

    This snapshot deliberately never asserts trade readiness. It must be
    sampled immediately before each audit; it is not a live-send lease.
    """
    bybit_ready, bybit_generation = read_readonly_status(
        bybit_path, "bybit", max_age_ns=max_age_ns,
    )
    okx_ready, okx_generation = read_readonly_status(
        okx_path, "okx", max_age_ns=max_age_ns,
    )
    return ReadinessSnapshot(
        bybit_trade_ready=False,
        okx_trade_ready=False,
        bybit_private_ready=bybit_ready,
        okx_private_ready=okx_ready,
        bybit_generation=bybit_generation,
        okx_generation=okx_generation,
        kill_switch=bool(kill_switch),
        pause=bool(pause),
    )


class WarmSessionReadinessBridge:
    """Publish ordered warm-session snapshots onto the execution owner loop."""

    def __init__(
        self,
        session: Any,
        publish: ReadinessPublish,
        *,
        owner_loop: Optional[asyncio.AbstractEventLoop] = None,
        pause: bool = False,
        kill_switch: bool = False,
    ) -> None:
        if not callable(publish):
            raise TypeError("publish must be callable")
        self._session = session
        self._publish = publish
        self._owner_loop = owner_loop
        self._pause = bool(pause)
        self._kill_switch = bool(kill_switch)
        self._lock = threading.Lock()
        self._next_sequence = 0
        self._applied_sequence = -1
        self._closed = False
        self._runtimes = tuple(
            runtime
            for runtime in (
                getattr(session, "bybit_runtime", None),
                getattr(session, "okx_runtime", None),
            )
            if runtime is not None
        )
        for runtime in self._runtimes:
            add = getattr(runtime, "add_readiness_listener", None)
            if callable(add):
                add(self.notify)
        self.notify()

    def set_controls(
        self,
        *,
        pause: Optional[bool] = None,
        kill_switch: Optional[bool] = None,
    ) -> None:
        with self._lock:
            if pause is not None:
                self._pause = bool(pause)
            if kill_switch is not None:
                self._kill_switch = bool(kill_switch)
        self.notify()

    def notify(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._next_sequence += 1
            sequence = self._next_sequence
            pause = self._pause
            kill_switch = self._kill_switch
        snapshot = snapshot_from_warm_session(
            self._session,
            pause=pause,
            kill_switch=kill_switch,
        )
        loop = self._owner_loop
        if loop is None:
            self._apply(sequence, snapshot)
            return
        if loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._apply, sequence, snapshot)
        except RuntimeError:
            return

    def _apply(self, sequence: int, snapshot: ReadinessSnapshot) -> None:
        with self._lock:
            if self._closed or sequence <= self._applied_sequence:
                return
            self._applied_sequence = sequence
        self._publish(snapshot)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        for runtime in self._runtimes:
            remove = getattr(runtime, "remove_readiness_listener", None)
            if callable(remove):
                remove(self.notify)
