"""Bounded raw-record buffer in front of ``ParquetPublisher``.

The websocket thread only appends. Disk I/O stays on the publisher thread.
A full publisher queue or a full buffer does not block ingest.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any, Protocol


class RecordSink(Protocol):
    def enqueue_records(self, records: list[dict[str, Any]]) -> bool:
        """Non-blocking enqueue. False means the caller still owns the records."""

    def durably_spool_records(
        self,
        records: list[dict[str, Any]],
        *,
        reason: str,
    ) -> bool:
        """Shutdown path. May write the local spool. Not used on the hot path."""


class HlRecordBuffer:
    def __init__(
        self,
        publisher: RecordSink,
        logger: logging.Logger,
        *,
        max_pending: int = 50_000,
        batch_rows: int = 500,
        flush_sec: float = 0.5,
    ) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be >= 1")
        if batch_rows < 1:
            raise ValueError("batch_rows must be >= 1")
        if flush_sec <= 0:
            raise ValueError("flush_sec must be > 0")
        self.publisher = publisher
        self.logger = logger
        self.max_pending = max_pending
        self.batch_rows = batch_rows
        self.flush_sec = flush_sec
        self._pending: deque[dict[str, Any]] = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.dropped_total = 0
        self.offered_total = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop,
            name="hl-l1-flush",
            daemon=True,
        )
        self._thread.start()

    def offer(self, record: dict[str, Any]) -> None:
        """Append one record. Never waits on the publisher."""
        with self._lock:
            self.offered_total += 1
            if len(self._pending) >= self.max_pending:
                self.dropped_total += 1
                dropped = self.dropped_total
            else:
                self._pending.append(record)
                dropped = 0
        if dropped == 1 or (dropped and dropped % 1000 == 0):
            self.logger.warning(
                "hl_ingest_drop | reason=buffer_full | dropped_total=%s | max_pending=%s",
                dropped,
                self.max_pending,
            )

    def flush_once(self) -> bool:
        """Try one non-blocking enqueue. False means the batch was put back."""
        with self._lock:
            if not self._pending:
                return True
            batch: list[dict[str, Any]] = []
            while self._pending and len(batch) < self.batch_rows:
                batch.append(self._pending.popleft())
        try:
            accepted = self.publisher.enqueue_records(batch)
        except Exception:
            self.logger.exception("hl_enqueue_error | rows=%s", len(batch))
            accepted = False
        if accepted:
            return True
        with self._lock:
            for record in reversed(batch):
                self._pending.appendleft(record)
            while len(self._pending) > self.max_pending:
                self._pending.pop()
                self.dropped_total += 1
        self.logger.warning(
            "hl_enqueue_rejected | rows=%s | pending=%s | policy=retain_until_cap",
            len(batch),
            self.pending_count(),
        )
        return False

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def stop_flush_thread(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=self.flush_sec + 2.0)

    def drain(self) -> str:
        """Move leftover records to the publisher or the local spool."""
        with self._lock:
            pending = list(self._pending)
            self._pending.clear()
        if not pending:
            return "empty"
        outcome = "enqueued"
        for offset in range(0, len(pending), self.batch_rows):
            chunk = pending[offset : offset + self.batch_rows]
            try:
                enqueued = self.publisher.enqueue_records(chunk)
            except Exception:
                self.logger.exception("hl_drain_enqueue_error | rows=%s", len(chunk))
                enqueued = False
            if enqueued:
                continue
            try:
                spooled = self.publisher.durably_spool_records(
                    chunk,
                    reason="hl_shutdown_enqueue_rejected",
                )
            except Exception:
                self.logger.exception("hl_drain_spool_error | rows=%s", len(chunk))
                spooled = False
            if spooled:
                outcome = "spooled"
                continue
            with self._lock:
                self._pending.extend(chunk)
            self.logger.error(
                "hl_drain_failed | rows=%s | durability=none",
                len(chunk),
            )
            return "failed"
        return outcome

    def _loop(self) -> None:
        while not self._stop.wait(self.flush_sec):
            self.flush_once()
