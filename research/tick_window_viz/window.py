"""Coin + 5-minute window path helpers (no I/O)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

FIVE_MIN_MS = 5 * 60 * 1000
_COMPACTED_FMT = "%Y%m%dT%H%M%SZ"


def normalize_coin(coin: str) -> str:
    value = str(coin).strip().upper()
    if not value:
        raise ValueError("COIN is empty")
    return value


def parse_window_start(value) -> datetime:
    """Parse notebook/ISO start to timezone-aware UTC.

    Naive datetimes and strings without an offset are treated as UTC.
    """
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def window_bounds_ms(start, *, minutes: int = 5) -> tuple[int, int]:
    """Return ``[start, start + minutes)`` as UTC milliseconds."""
    if minutes <= 0:
        raise ValueError("minutes must be > 0")
    start_dt = parse_window_start(start)
    end_dt = start_dt + timedelta(minutes=int(minutes))
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    if end_ms <= start_ms:
        raise ValueError("window end must be after start")
    return start_ms, end_ms


def is_five_min_aligned(start) -> bool:
    start_ms = int(parse_window_start(start).timestamp() * 1000)
    return start_ms % FIVE_MIN_MS == 0


def _fmt_compacted_ts(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return dt.strftime(_COMPACTED_FMT)


def compacted_filename(start_ms: int, end_ms: int | None = None) -> str:
    """``spread_YYYYMMDDTHHMMSSZ_YYYYMMDDTHHMMSSZ.parquet`` for one file window."""
    if end_ms is None:
        end_ms = start_ms + FIVE_MIN_MS
    if end_ms <= start_ms:
        raise ValueError("compacted end must be after start")
    return f"spread_{_fmt_compacted_ts(start_ms)}_{_fmt_compacted_ts(end_ms)}.parquet"


def compacted_names_covering(start_ms: int, end_ms: int) -> list[str]:
    """Aligned 5-minute compacted names that overlap ``[start_ms, end_ms)``."""
    if end_ms <= start_ms:
        raise ValueError("window end must be after start")
    aligned = (start_ms // FIVE_MIN_MS) * FIVE_MIN_MS
    names: list[str] = []
    cursor = aligned
    while cursor < end_ms:
        names.append(compacted_filename(cursor, cursor + FIVE_MIN_MS))
        cursor += FIVE_MIN_MS
    return names
