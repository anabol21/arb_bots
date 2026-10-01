"""Bounded record buffer → ParquetPublisher enqueue (non-blocking offer)."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

from app.storage.writer import ParquetPublisher


class HlV2RecordBuffer:
    """Hot path offers records; a flush thread batches into the publisher."""

    def __init__(
        self,
        publisher: ParquetPublisher,
        logger: logging.Logger,
        *,
        flush_every_n: int = 500,
        flush_interval_sec: float = 2.0,
        max_pending: int = 50_000,
    ) -> None:
        self.publisher = publisher
        self.logger = logger
        self.flush_every_n = max(1, int(flush_every_n))
        self.flush_interval_sec = max(0.1, float(flush_interval_sec))
        self.max_pending = max(self.flush_every_n, int(max_pending))
        self._pending: deque[dict[str, Any]] = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.offered_total = 0
        self.dropped_total = 0
        self.flushed_total = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._flush_loop,
            name="hl-v2-flush",
            daemon=True,
        )
        self._thread.start()

    def offer(self, record: dict[str, Any]) -> None:
        with self._lock:
            self.offered_total += 1
            if len(self._pending) >= self.max_pending:
                self.dropped_total += 1
                if self.dropped_total == 1 or self.dropped_total % 1000 == 0:
                    self.logger.error(
                        "hl_v2_buffer_drop | pending=%s | dropped_total=%s",
                        len(self._pending),
                        self.dropped_total,
                    )
                return
            self._pending.append(record)
            should_flush = len(self._pending) >= self.flush_every_n
        if should_flush:
            self._flush_once()

    def stop_flush_thread(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def drain(self) -> str:
        """Flush remaining rows. Returns ok | failed."""
        while True:
            with self._lock:
                if not self._pending:
                    return "ok"
            if not self._flush_once():
                return "failed"
            time.sleep(0.01)

    def _flush_loop(self) -> None:
        while not self._stop.wait(self.flush_interval_sec):
            self._flush_once()

    def _flush_once(self) -> bool:
        with self._lock:
            if not self._pending:
                return True
            batch = list(self._pending)
            count = len(batch)
        if not self.publisher.ready_for_enqueue(count):
            return False
        if not self.publisher.enqueue_records(batch):
            return False
        with self._lock:
            for _ in range(count):
                if self._pending:
                    self._pending.popleft()
            self.flushed_total += count
        return True
