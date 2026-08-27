#!/usr/bin/env python3
"""Mark reconnect holes as time intervals, not whole 5-minute slots.

Prefer ``/data/gaps/.../gaps.jsonl`` (durable). Fall back to pairing
``ws_disconnect`` with ``ws_subscribe_ok`` in runtime.log. Ticks outside the
interval stay valid; the 5-minute compaction grain is unchanged.

Usage:
  python3 validation/check_tick_coverage.py \\
    --log /var/log/spread/runtime.log \\
    --gaps-root /data/gaps \\
    --since 2026-08-14T12:30:59Z \\
    --parquet-root /data/live
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from app.schema.ws_gap import validate_gap_record

BAR_INTERVAL_MS = 300_000
TS_RE = re.compile(r"^(\d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
FIELD_RE = re.compile(r"(\w+)=([^\s|]+)")


def parse_ts(line: str) -> datetime | None:
    match = TS_RE.match(line)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    )


def parse_since(raw: str) -> datetime:
    text = raw.strip().replace("Z", "+00:00")
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def window_start_ms(ts_ms: int) -> int:
    return (int(ts_ms) // BAR_INTERVAL_MS) * BAR_INTERVAL_MS


def iter_log_lines(paths: Iterable[Path]) -> Iterable[str]:
    for path in paths:
        if not path.exists():
            continue
        with path.open("r", errors="replace") as handle:
            yield from handle


def _conn_key(fields: dict[str, str]) -> tuple[str, str, str]:
    return (
        fields.get("exchange", "?"),
        fields.get("channel", "?"),
        fields.get("coin", "?"),
    )


def _interval_row(
    *,
    base_coin: str,
    exchange: str,
    channel: str,
    t_down_ms: int,
    t_up_ms: int | None,
    close_code: object = None,
    source: str,
) -> dict[str, Any]:
    duration_ms = None if t_up_ms is None else max(0, int(t_up_ms) - int(t_down_ms))
    slot_gap_frac = (
        None if duration_ms is None else float(duration_ms) / float(BAR_INTERVAL_MS)
    )
    slot_tick_frac_est = None if slot_gap_frac is None else max(0.0, 1.0 - slot_gap_frac)
    return {
        "base_coin": base_coin,
        "exchange": exchange,
        "channel": channel,
        "t_down_ms": int(t_down_ms),
        "t_up_ms": None if t_up_ms is None else int(t_up_ms),
        "duration_ms": duration_ms,
        "close_code": close_code,
        "bar_start_ts_ms": window_start_ms(int(t_down_ms)),
        "slot_gap_frac": slot_gap_frac,
        "slot_tick_frac_est": slot_tick_frac_est,
        "source": source,
        "reasons": ["ws_disconnect"],
    }


def pair_log_intervals(paths: list[Path], since: datetime) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    disconnects = 0
    planned = 0
    unplanned = 0
    last_hb: dict[str, str] = {}
    coins_disc: dict[str, int] = defaultdict(int)
    pending: dict[tuple[str, str, str], dict[str, Any]] = {}
    intervals: list[dict[str, Any]] = []

    for line in iter_log_lines(paths):
        ts = parse_ts(line)
        if ts is None or ts < since:
            continue
        if "heartbeat |" in line:
            last_hb = dict(FIELD_RE.findall(line))
            last_hb["_ts"] = ts.isoformat()
            continue
        if "ws_disconnect |" in line:
            disconnects += 1
            fields = dict(FIELD_RE.findall(line))
            coin = fields.get("coin", "?")
            coins_disc[coin] += 1
            key = _conn_key(fields)
            t_down_ms = int(ts.timestamp() * 1000)
            if key not in pending:
                pending[key] = {
                    "t_down_ms": t_down_ms,
                    "close_code": fields.get("close_code"),
                    "fields": fields,
                }
            continue
        if "ws_subscribe_ok |" in line:
            fields = dict(FIELD_RE.findall(line))
            key = _conn_key(fields)
            open_gap = pending.pop(key, None)
            if open_gap is None:
                continue
            t_up_ms = int(ts.timestamp() * 1000)
            f = open_gap["fields"]
            intervals.append(
                _interval_row(
                    base_coin=f.get("coin", "?"),
                    exchange=f.get("exchange", "?"),
                    channel=f.get("channel", "?"),
                    t_down_ms=int(open_gap["t_down_ms"]),
                    t_up_ms=t_up_ms,
                    close_code=open_gap.get("close_code"),
                    source="runtime_log",
                )
            )
            continue
        if "ws_reconnect_planned |" in line:
            planned += 1
        elif "ws_reconnect_unplanned |" in line:
            unplanned += 1

    for open_gap in pending.values():
        f = open_gap["fields"]
        intervals.append(
            _interval_row(
                base_coin=f.get("coin", "?"),
                exchange=f.get("exchange", "?"),
                channel=f.get("channel", "?"),
                t_down_ms=int(open_gap["t_down_ms"]),
                t_up_ms=None,
                close_code=open_gap.get("close_code"),
                source="runtime_log",
            )
        )

    stats = {
        "disconnects": disconnects,
        "planned": planned,
        "unplanned": unplanned,
        "last_heartbeat": last_hb,
        "coins_with_disconnect": len(coins_disc),
    }
    return stats, intervals


def load_jsonl_intervals(root: Path, since: datetime) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    since_ms = int(since.timestamp() * 1000)
    intervals: list[dict[str, Any]] = []
    for path in sorted(root.glob("event_date=*/gaps.jsonl")):
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    raw = json.loads(text)
                    record = validate_gap_record(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if int(record["t_up_ms"]) < since_ms:
                    continue
                intervals.append(
                    _interval_row(
                        base_coin=record["base_coin"],
                        exchange=record["exchange"],
                        channel=record["channel"],
                        t_down_ms=int(record["t_down_ms"]),
                        t_up_ms=int(record["t_up_ms"]),
                        close_code=record["close_code"],
                        source="gaps_jsonl",
                    )
                )
    return intervals


def _verdict(intervals: list[dict[str, Any]], suppressed: int) -> str:
    closed = [row for row in intervals if row.get("duration_ms") is not None]
    open_n = sum(1 for row in intervals if row.get("t_up_ms") is None)
    if not intervals and not suppressed:
        return "no_gap_intervals"
    if closed:
        max_ms = max(int(row["duration_ms"]) for row in closed)
        if max_ms >= BAR_INTERVAL_MS:
            return "gap_spans_full_slot"
        sec = int(round(max_ms / 1000.0))
        return f"hole_{sec}s_inside_slot"
    if open_n:
        return "open_gap_interval"
    if suppressed:
        return "ticks_suppressed"
    return "gap_intervals_present"


def analyze_logs(
    paths: list[Path],
    since: datetime,
    *,
    gaps_root: Path | None = None,
) -> dict[str, Any]:
    stats, log_intervals = pair_log_intervals(paths, since)
    jsonl_intervals: list[dict[str, Any]] = []
    if gaps_root is not None:
        jsonl_intervals = load_jsonl_intervals(gaps_root, since)
    intervals = jsonl_intervals if jsonl_intervals else log_intervals
    closed = [row for row in intervals if row.get("duration_ms") is not None]
    durations = [int(row["duration_ms"]) for row in closed]
    report: dict[str, Any] = {
        **stats,
        "incomplete_from_log": len(intervals),
        "incomplete_intervals": intervals[:200],
        "incomplete_windows": [
            {
                "base_coin": row["base_coin"],
                "bar_start_ts_ms": row["bar_start_ts_ms"],
                "reasons": row["reasons"],
                "t_down_ms": row["t_down_ms"],
                "t_up_ms": row["t_up_ms"],
                "duration_ms": row["duration_ms"],
                "slot_gap_frac": row["slot_gap_frac"],
                "slot_tick_frac_est": row["slot_tick_frac_est"],
            }
            for row in intervals[:200]
        ],
        "gaps_closed": len(closed),
        "gaps_open": sum(1 for row in intervals if row.get("t_up_ms") is None),
        "gap_source": "gaps_jsonl" if jsonl_intervals else "runtime_log",
        "max_gap_ms": max(durations) if durations else None,
        "median_gap_ms": sorted(durations)[len(durations) // 2] if durations else None,
    }
    hb = report.get("last_heartbeat") or {}
    suppressed = int(hb.get("ticks_suppressed_stale") or 0) + int(
        hb.get("ticks_suppressed_generation") or 0
    )
    report["verdict"] = _verdict(intervals, suppressed)
    return report


def sample_parquet_skew(
    root: Path,
    *,
    skew_max_ms: int,
    limit_files: int,
) -> dict[str, object]:
    if not root.exists():
        return {"parquet_root_exists": False, "files_scanned": 0, "skew_violations": 0}
    files = [
        path
        for path in root.rglob("*.parquet")
        if ".tmp" not in path.parts and "archived" not in path.parts
    ]
    files = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:limit_files]
    violations = 0
    rows = 0
    try:
        import pyarrow.parquet as pq
    except ImportError:
        return {"parquet_root_exists": True, "files_scanned": 0, "error": "pyarrow_missing"}

    for path in files:
        try:
            table = pq.read_table(path, columns=["okx_ts_ms", "bybit_ts_ms"])
        except Exception:
            continue
        okx = table.column("okx_ts_ms").to_pylist()
        bybit = table.column("bybit_ts_ms").to_pylist()
        for left, right in zip(okx, bybit):
            if left is None or right is None:
                continue
            rows += 1
            if abs(float(left) - float(right)) > skew_max_ms:
                violations += 1
    return {
        "parquet_root_exists": True,
        "files_scanned": len(files),
        "rows_scanned": rows,
        "skew_violations": violations,
        "skew_max_ms": skew_max_ms,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", action="append", default=[], help="runtime.log path")
    parser.add_argument("--since", required=True, help="ISO UTC start, e.g. 2026-08-15T12:00:00Z")
    parser.add_argument("--parquet-root", default="")
    parser.add_argument("--gaps-root", default="")
    parser.add_argument("--skew-max-ms", type=int, default=2000)
    parser.add_argument("--limit-files", type=int, default=40)
    args = parser.parse_args()
    logs = [Path(item) for item in args.log] or [Path("/var/log/spread/runtime.log")]
    gaps_root = Path(args.gaps_root) if args.gaps_root else None
    report = analyze_logs(logs, parse_since(args.since), gaps_root=gaps_root)
    if args.parquet_root:
        report["parquet_skew"] = sample_parquet_skew(
            Path(args.parquet_root),
            skew_max_ms=args.skew_max_ms,
            limit_files=args.limit_files,
        )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
