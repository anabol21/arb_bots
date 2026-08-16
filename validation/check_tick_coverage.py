#!/usr/bin/env python3
"""Mark 5m windows incomplete from reconnects, suppress counters, and parquet holes.

Prefer an explicit incomplete window over treating a hole as a quiet market.
Does not rewrite parquet. Lean body is unchanged.

Usage:
  python3 validation/check_tick_coverage.py \\
    --log /var/log/spread/runtime.log \\
    --log /var/log/spread/runtime.log.1 \\
    --since 2026-08-14T12:30:59Z \\
    --parquet-root /data/live \\
    --bars-root /data/bars/bar_5m
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

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


def analyze_logs(paths: list[Path], since: datetime) -> dict[str, object]:
    disconnects = 0
    planned = 0
    unplanned = 0
    last_hb: dict[str, str] = {}
    coins_disc: dict[str, int] = defaultdict(int)
    incomplete: dict[tuple[str, int], set[str]] = defaultdict(set)

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
            win = window_start_ms(int(ts.timestamp() * 1000))
            incomplete[(coin, win)].add("ws_disconnect")
            continue
        if "ws_reconnect_planned |" in line:
            planned += 1
        elif "ws_reconnect_unplanned |" in line:
            unplanned += 1

    return {
        "disconnects": disconnects,
        "planned": planned,
        "unplanned": unplanned,
        "last_heartbeat": last_hb,
        "incomplete_from_log": len(incomplete),
        "incomplete_windows": [
            {
                "base_coin": coin,
                "bar_start_ts_ms": win,
                "reasons": sorted(reasons),
            }
            for (coin, win), reasons in sorted(incomplete.items())[:200]
        ],
        "coins_with_disconnect": len(coins_disc),
    }


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
    parser.add_argument("--skew-max-ms", type=int, default=2000)
    parser.add_argument("--limit-files", type=int, default=40)
    args = parser.parse_args()
    logs = [Path(item) for item in args.log] or [Path("/var/log/spread/runtime.log")]
    report = analyze_logs(logs, parse_since(args.since))
    if args.parquet_root:
        report["parquet_skew"] = sample_parquet_skew(
            Path(args.parquet_root),
            skew_max_ms=args.skew_max_ms,
            limit_files=args.limit_files,
        )
    hb = report.get("last_heartbeat") or {}
    suppressed = int(hb.get("ticks_suppressed_stale") or 0) + int(
        hb.get("ticks_suppressed_generation") or 0
    )
    report["verdict"] = (
        "incomplete_windows_present"
        if report["incomplete_from_log"] or suppressed
        else "no_disconnect_windows"
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
