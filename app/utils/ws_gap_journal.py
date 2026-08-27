"""Append-only WS gap JSONL journal.

Lifecycle only: pair ``ws_disconnect`` with the next ``ws_subscribe_ok`` for
the same ``(exchange, channel, base_coin)``. Does not parse exchange payloads
or compute spreads. First connect (subscribe_ok without a prior disconnect)
does not write a row.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

from app.schema.ws_gap import encode_gap_record, gaps_jsonl_path, utc_event_date

_Key = tuple[str, str, str]


class WsGapJournal:
    """In-process open intervals plus durable closed rows under ``root``."""

    def __init__(
        self,
        root: Path | str,
        *,
        logger: logging.Logger | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.root = Path(root)
        self.logger = logger
        self._clock_ms = clock_ms if clock_ms is not None else _wall_ms
        self._open: dict[_Key, dict[str, Any]] = {}

    def note_disconnect(
        self,
        *,
        exchange: str,
        channel: str,
        base_coin: str,
        close_code: object = None,
        t_down_ms: int | None = None,
    ) -> None:
        """Record the start of a hole. Keeps the first t_down across retries."""
        key = _conn_key(exchange, channel, base_coin)
        ts = int(t_down_ms) if t_down_ms is not None else self._clock_ms()
        pending = self._open.get(key)
        if pending is None:
            self._open[key] = {
                "base_coin": str(base_coin),
                "exchange": str(exchange),
                "channel": str(channel),
                "t_down_ms": ts,
                "close_code": close_code,
            }
            return
        pending["close_code"] = close_code

    def note_subscribe_ok(
        self,
        *,
        exchange: str,
        channel: str,
        base_coin: str,
        t_up_ms: int | None = None,
    ) -> Optional[dict[str, Any]]:
        """Close a pending hole and append JSONL. No-op on first connect."""
        key = _conn_key(exchange, channel, base_coin)
        pending = self._open.pop(key, None)
        if pending is None:
            return None
        ts = int(t_up_ms) if t_up_ms is not None else self._clock_ms()
        try:
            record = encode_gap_record(
                base_coin=str(pending["base_coin"]),
                exchange=str(pending["exchange"]),
                channel=str(pending["channel"]),
                t_down_ms=int(pending["t_down_ms"]),
                t_up_ms=ts,
                close_code=_as_close_code(pending.get("close_code")),
            )
        except (TypeError, ValueError) as exc:
            self._warn("ws_gap_encode_failed | %s | %s", key, exc)
            return None
        try:
            self._append(record)
        except OSError as exc:
            self._open[key] = pending
            self._warn("ws_gap_write_failed | %s | %s", key, exc)
            return None
        return record

    def _append(self, record: dict[str, Any]) -> None:
        path = gaps_jsonl_path(self.root, utc_event_date(int(record["t_down_ms"])))
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def _warn(self, message: str, *args: object) -> None:
        if self.logger is not None:
            self.logger.warning(message, *args)


def _conn_key(exchange: str, channel: str, base_coin: str) -> _Key:
    return (str(exchange), str(channel), str(base_coin))


def _wall_ms() -> int:
    return int(time.time() * 1000)


def _as_close_code(raw: object) -> int | None:
    if raw is None or raw == "":
        return None
    return int(raw)
