"""WebSocket gap interval contract (not lean tick body).

Producer: collector reconnect loop, one row per ``ws_disconnect`` closed by
the matching ``ws_subscribe_ok`` for the same ``(exchange, channel, base_coin)``.
Consumers: ``validation/check_tick_coverage.py`` and the historical fill check.

On-disk layout (separate from ``/data/live``, ``/data/bars``, ``/data/bbot``):

    <SPREAD_GAPS_ROOT>/event_date=<YYYY-MM-DD>/gaps.jsonl

``event_date`` is the UTC calendar day of ``t_down_ms``. The lean parquet body
does not grow a gap column.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

WS_GAP_SCHEMA_VERSION = 1
WS_GAP_FILENAME = "gaps.jsonl"

WS_GAP_REQUIRED_FIELDS: tuple[str, ...] = (
    "base_coin",
    "exchange",
    "channel",
    "t_down_ms",
    "t_up_ms",
    "close_code",
)

WS_GAP_OPTIONAL_FIELDS: tuple[str, ...] = ("schema_version",)


class WsGapSchemaError(ValueError):
    """Gap JSONL record is missing required fields or has invalid types."""


def utc_event_date(ts_ms: int) -> str:
    """UTC ``YYYY-MM-DD`` of a millisecond timestamp."""
    return datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%d"
    )


def gaps_jsonl_path(root: Path | str, event_date: str) -> Path:
    """Day file: ``<root>/event_date=<YYYY-MM-DD>/gaps.jsonl``."""
    if not event_date or len(event_date) < 10:
        raise WsGapSchemaError(f"invalid event_date for gaps path: {event_date!r}")
    return Path(root) / f"event_date={event_date}" / WS_GAP_FILENAME


def encode_gap_record(
    *,
    base_coin: str,
    exchange: str,
    channel: str,
    t_down_ms: int,
    t_up_ms: int,
    close_code: int | None,
    schema_version: int = WS_GAP_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Build a closed gap record; raises if the contract is not met."""
    record = {
        "schema_version": int(schema_version),
        "base_coin": str(base_coin),
        "exchange": str(exchange),
        "channel": str(channel),
        "t_down_ms": int(t_down_ms),
        "t_up_ms": int(t_up_ms),
        "close_code": None if close_code is None else int(close_code),
    }
    return validate_gap_record(record)


def validate_gap_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return a normalized closed-interval record or raise ``WsGapSchemaError``."""
    missing = [name for name in WS_GAP_REQUIRED_FIELDS if name not in raw]
    if missing:
        raise WsGapSchemaError(f"gap record missing fields: {missing}")
    base_coin = str(raw["base_coin"]).strip()
    exchange = str(raw["exchange"]).strip()
    channel = str(raw["channel"]).strip()
    if not base_coin or not exchange or not channel:
        raise WsGapSchemaError("base_coin, exchange, and channel must be non-empty")
    try:
        t_down_ms = int(raw["t_down_ms"])
        t_up_ms = int(raw["t_up_ms"])
    except (TypeError, ValueError) as exc:
        raise WsGapSchemaError("t_down_ms and t_up_ms must be integers") from exc
    if t_down_ms <= 0 or t_up_ms <= 0:
        raise WsGapSchemaError("t_down_ms and t_up_ms must be positive")
    if t_up_ms < t_down_ms:
        raise WsGapSchemaError(
            f"t_up_ms {t_up_ms} is before t_down_ms {t_down_ms}"
        )
    close_raw = raw["close_code"]
    close_code: int | None
    if close_raw is None or close_raw == "":
        close_code = None
    else:
        try:
            close_code = int(close_raw)
        except (TypeError, ValueError) as exc:
            raise WsGapSchemaError("close_code must be int or null") from exc
    version_raw = raw.get("schema_version", WS_GAP_SCHEMA_VERSION)
    try:
        schema_version = int(version_raw)
    except (TypeError, ValueError) as exc:
        raise WsGapSchemaError("schema_version must be int") from exc
    return {
        "schema_version": schema_version,
        "base_coin": base_coin,
        "exchange": exchange,
        "channel": channel,
        "t_down_ms": t_down_ms,
        "t_up_ms": t_up_ms,
        "close_code": close_code,
    }
