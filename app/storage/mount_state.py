"""Shared mount-failure state for the runtime and publisher thread."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class MountFailure:
    source: str
    reason: str
    detected_at_unix_ms: int
    batch_id: str | None = None


class MountFailureState:
    """Thread-safe, first-failure-wins mount state."""

    def __init__(self) -> None:
        self._dead = threading.Event()
        self._lock = threading.Lock()
        self._failure: MountFailure | None = None

    def mark_dead(
        self,
        *,
        source: str,
        reason: str,
        batch_id: str | None = None,
    ) -> bool:
        """Record the first mount failure and return whether this call won."""
        with self._lock:
            if self._failure is not None:
                return False
            self._failure = MountFailure(
                source=source,
                reason=reason,
                detected_at_unix_ms=int(time.time() * 1000),
                batch_id=batch_id,
            )
            self._dead.set()
            return True

    def is_dead(self) -> bool:
        return self._dead.is_set()

    def failure(self) -> MountFailure | None:
        with self._lock:
            return self._failure
