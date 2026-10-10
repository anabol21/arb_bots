"""Append-only step chronometry next to the theta trade journal.

Not the canary chronometry dashboard. Rows are stamped at the block boundary
and flushed after ``ws_send`` exits so the file write is outside that interval.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from app.bot.paths import theta_step_chrono_jsonl_path

_D_ROOTS = ("/data/live", "/data/bars", "/data/compacted", "/data/spool")

DURATION_BLOCKS = (
    "preprocess",
    "channel_check",
    "ws_send",
    "journal_pending",
    "wait_fill",
    "fill_done",
    "abort",
)

_SEND_TIMING_FIELDS = (
    "queue_enqueued_ns",
    "dequeued_ns",
    "callback_started_ns",
    "callback_returned_ns",
    "owner_ws_send_started_ns",
    "owner_ws_send_returned_ns",
)
_PRE_SEND_TIMING_FIELDS = (
    "decision_gates_done",
    "task_scheduled",
    "task_started",
    "worker_thread_started",
    "meta_done",
    "guard_done",
    "place_io_requested",
    "place_io_acquired",
    "chrono_created",
)


class StepChrono:
    """Buffer enter/exit stamps, then append JSONL."""

    def __init__(
        self,
        data_root: Path,
        *,
        intent_id: str,
        signal_ts_ms: int,
        signal_monotonic_ns: Optional[int] = None,
    ) -> None:
        self.data_root = Path(data_root)
        text = str(self.data_root.resolve())
        for bad in _D_ROOTS:
            if text == bad or text.startswith(bad + os.sep):
                raise RuntimeError(f"step chrono refuses D path: {self.data_root}")
        self.intent_id = str(intent_id)
        self.signal_ts_ms = int(signal_ts_ms)
        self.signal_monotonic_ns = (
            int(signal_monotonic_ns) if signal_monotonic_ns is not None else None
        )
        self._rows: list[dict[str, Any]] = []
        self._written = 0
        self._enter_mono: dict[str, int] = {}
        self._enter_wall: dict[str, int] = {}
        if self.signal_monotonic_ns is not None:
            self._rows.append(
                {
                    "intent_id": self.intent_id,
                    "block": "signal_decision",
                    "edge": "selected",
                    "wall_ms": self.signal_ts_ms,
                    "mono_ns": self.signal_monotonic_ns,
                    "signal_ts_ms": self.signal_ts_ms,
                    "signal_mono_ns": self.signal_monotonic_ns,
                }
            )

    def _stamp(self) -> tuple[int, int]:
        return int(time.time() * 1000), int(time.monotonic_ns())

    def enter(self, block: str) -> None:
        wall, mono = self._stamp()
        self._enter_wall[block] = wall
        self._enter_mono[block] = mono
        self._rows.append(self._row(block, edge="enter", wall_ms=wall, mono_ns=mono))

    def exit(self, block: str, **extra: Any) -> None:
        wall, mono = self._stamp()
        prev_w = self._enter_wall.get(block)
        prev_m = self._enter_mono.get(block)
        if prev_w is not None and wall < prev_w:
            wall = prev_w
        if prev_m is not None and mono < prev_m:
            mono = prev_m
        self._rows.append(
            self._row(block, edge="exit", wall_ms=wall, mono_ns=mono, extra=extra or None)
        )

    def abort(self, reason: str) -> None:
        self.enter("abort")
        self.exit("abort", reason=str(reason))

    def venue_message(self, venue: str, *, wall_ms: Optional[int] = None) -> None:
        wall, mono = self._stamp()
        if wall_ms is not None:
            wall = int(wall_ms)
        self._rows.append(
            {
                "intent_id": self.intent_id,
                "block": "venue_message",
                "venue": str(venue).strip().lower(),
                "wall_ms": wall,
                "mono_ns": mono,
                "signal_ts_ms": self.signal_ts_ms,
            }
        )

    def send_timing(
        self, timings: Mapping[str, Mapping[str, Optional[int]]], *, phase: str
    ) -> None:
        """Snapshot only allowed monotonic fields after ``ws_send`` exits.

        Never serialize the send result or its items: they contain signed
        frames and request ids. Missing markers remain null, not zero.
        """
        safe: dict[str, dict[str, Optional[int]]] = {}
        for venue in ("bybit", "okx"):
            markers = timings.get(venue)
            if not isinstance(markers, Mapping):
                continue
            safe[venue] = {}
            for name in _SEND_TIMING_FIELDS:
                value = markers.get(name)
                safe[venue][name] = value if type(value) is int else None
        if not safe:
            return
        wall, mono = self._stamp()
        self._rows.append(
            {
                "intent_id": self.intent_id,
                "block": "send_timing",
                "phase": "close" if phase == "close" else "open",
                "wall_ms": wall,
                "mono_ns": mono,
                "signal_ts_ms": self.signal_ts_ms,
                "send_timing_monotonic_ns": safe,
            }
        )

    def pre_send_timing(self, stamps: Mapping[str, int]) -> None:
        """Keep scheduling stamps in memory until the normal post-send flush."""
        safe = {
            name: value if type(value) is int else None
            for name in _PRE_SEND_TIMING_FIELDS
            for value in (stamps.get(name),)
        }
        if not any(value is not None for value in safe.values()):
            return
        wall, mono = self._stamp()
        self._rows.append(
            {
                "intent_id": self.intent_id,
                "block": "pre_send_timing",
                "wall_ms": wall,
                "mono_ns": mono,
                "signal_ts_ms": self.signal_ts_ms,
                "pre_send_timing_monotonic_ns": safe,
            }
        )

    def _row(
        self,
        block: str,
        *,
        edge: str,
        wall_ms: int,
        mono_ns: int,
        extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {
            "intent_id": self.intent_id,
            "block": str(block),
            "edge": edge,
            "wall_ms": int(wall_ms),
            "mono_ns": int(mono_ns),
            "signal_ts_ms": self.signal_ts_ms,
        }
        if extra:
            row.update(extra)
        return row

    def flush(self) -> None:
        pending = self._rows[self._written :]
        if not pending:
            return
        event_date = datetime.fromtimestamp(
            self.signal_ts_ms / 1000.0, tz=timezone.utc
        ).date().isoformat()
        path = theta_step_chrono_jsonl_path(self.data_root, event_date)
        with path.open("a", encoding="utf-8") as fh:
            for rec in pending:
                fh.write(json.dumps(rec, separators=(",", ":"), ensure_ascii=False))
                fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._written = len(self._rows)
