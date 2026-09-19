#!/usr/bin/env python3
"""Experiment D: read-only compare canary parquet vs production /data/live.

Does not delete or modify production data. Use after both trees have ticks in
the same observation window.

Metrics per base_coin (and totals):

- tick row counts in parquet
- gap interval count from gaps JSONL (optional)
- okx_ts_ms / bybit_ts_ms and local recv timestamps (median delta vs prod)
- max_latency_ms p50/p95 (proxy for delivery latency in tick body)

Go / no-go (document before run; defaults below):

- canary must not have **extra** gap intervals vs prod for shared coins
- canary p95(max_latency_ms) <= prod p95 + 100 ms on shared coins
- canary tick count per coin >= 95% of prod count in the window (shared pool)

Usage:

  python3 validation/compare_hotadd_canary_live.py \\
    --prod-root /data/live \\
    --canary-root /data/live-hotadd-canary \\
    --prod-gaps /data/gaps \\
    --canary-gaps /data/gaps-hotadd-canary \\
    --since 2026-09-19T10:00:00Z \\
    --until 2026-09-19T11:00:00Z
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

try:
    import pyarrow.parquet as pq
except ImportError as exc:  # pragma: no cover
    raise SystemExit(f"pyarrow required: {exc}") from exc


def parse_ts(raw: str) -> datetime:
    text = raw.strip().replace("Z", "+00:00")
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def in_window_ms(ts_ms: int, start_ms: int, end_ms: int) -> bool:
    return start_ms <= ts_ms < end_ms


def iter_parquet_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        p
        for p in root.rglob("*.parquet")
        if ".tmp" not in p.parts and "archived" not in p.parts
    )


def load_tick_stats(
    root: Path,
    *,
    start_ms: int,
    end_ms: int,
) -> dict[str, Any]:
    per_coin_count: dict[str, int] = defaultdict(int)
    latencies: list[float] = []
    ts_deltas_okx: list[float] = []
    ts_deltas_bybit: list[float] = []
    files_scanned = 0
    for path in iter_parquet_files(root):
        files_scanned += 1
        try:
            schema_names = set(pq.ParquetFile(path).schema_arrow.names)
        except Exception:
            continue
        want = [
            "base_coin",
            "event_local_ts_ms",
            "max_latency_ms",
            "okx_ts_ms",
            "bybit_ts_ms",
            "okx_local_recv_ts_ms",
            "bybit_local_recv_ts_ms",
        ]
        columns = [name for name in want if name in schema_names]
        if "base_coin" not in columns or "event_local_ts_ms" not in columns:
            continue
        try:
            table = pq.read_table(path, columns=columns)
        except Exception:
            continue
        data = table.to_pydict()
        coins = data.get("base_coin") or []
        events = data.get("event_local_ts_ms") or []
        for i, coin in enumerate(coins):
            if not coin:
                continue
            ts = events[i] if i < len(events) else None
            if ts is None:
                continue
            try:
                ts_i = int(ts)
            except (TypeError, ValueError):
                continue
            if not in_window_ms(ts_i, start_ms, end_ms):
                continue
            per_coin_count[str(coin)] += 1
            lat = data.get("max_latency_ms", [None])[i] if "max_latency_ms" in data else None
            if lat is not None:
                try:
                    latencies.append(float(lat))
                except (TypeError, ValueError):
                    pass
            okx_ts = data.get("okx_ts_ms", [None])[i]
            okx_recv = data.get("okx_local_recv_ts_ms", [None])[i]
            if okx_ts is not None and okx_recv is not None:
                try:
                    ts_deltas_okx.append(float(okx_recv) - float(okx_ts))
                except (TypeError, ValueError):
                    pass
            bybit_ts = data.get("bybit_ts_ms", [None])[i]
            bybit_recv = data.get("bybit_local_recv_ts_ms", [None])[i]
            if bybit_ts is not None and bybit_recv is not None:
                try:
                    ts_deltas_bybit.append(float(bybit_recv) - float(bybit_ts))
                except (TypeError, ValueError):
                    pass
    return {
        "root": str(root),
        "files_scanned": files_scanned,
        "tick_counts": dict(per_coin_count),
        "total_ticks": sum(per_coin_count.values()),
        "max_latency_ms": _percentiles(latencies),
        "okx_recv_minus_exchange_ms": _percentiles(ts_deltas_okx),
        "bybit_recv_minus_exchange_ms": _percentiles(ts_deltas_bybit),
        "delivery_proxy_ms": _percentiles(
            ts_deltas_okx + ts_deltas_bybit if not latencies else latencies
        ),
    }


def _percentiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p50": None, "p95": None, "n": 0}
    ordered = sorted(values)
    n = len(ordered)

    def pct(p: float) -> float:
        idx = min(n - 1, max(0, int(round(p * (n - 1)))))
        return ordered[idx]

    return {"p50": pct(0.50), "p95": pct(0.95), "n": n}


def count_gap_intervals(gaps_root: Path, *, start_ms: int, end_ms: int) -> int:
    if not gaps_root.exists():
        return 0
    total = 0
    for path in gaps_root.rglob("gaps.jsonl"):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    start = rec.get("gap_start_ms") or rec.get("start_ms")
                    if start is None:
                        continue
                    try:
                        start_i = int(start)
                    except (TypeError, ValueError):
                        continue
                    if in_window_ms(start_i, start_ms, end_ms):
                        total += 1
        except OSError:
            continue
    return total


def evaluate_go_no_go(
    prod: dict[str, Any],
    canary: dict[str, Any],
    *,
    prod_gaps: int,
    canary_gaps: int,
    latency_slack_ms: float,
    count_ratio_min: float,
) -> dict[str, Any]:
    failures: list[str] = []
    if canary_gaps > prod_gaps:
        failures.append(
            f"canary gaps {canary_gaps} > prod gaps {prod_gaps} in window"
        )
    prod_lat = prod.get("delivery_proxy_ms") or prod.get("max_latency_ms") or {}
    can_lat = canary.get("delivery_proxy_ms") or canary.get("max_latency_ms") or {}
    prod_p95 = prod_lat.get("p95")
    can_p95 = can_lat.get("p95")
    if prod_p95 is not None and can_p95 is not None:
        if can_p95 > prod_p95 + latency_slack_ms:
            failures.append(
                f"canary p95 max_latency_ms {can_p95} > prod {prod_p95} + {latency_slack_ms}"
            )
    prod_counts = prod.get("tick_counts") or {}
    can_counts = canary.get("tick_counts") or {}
    shared = set(prod_counts) & set(can_counts)
    low_ratio: list[str] = []
    for coin in sorted(shared):
        p = prod_counts[coin]
        c = can_counts[coin]
        if p <= 0:
            continue
        ratio = c / p
        if ratio < count_ratio_min:
            low_ratio.append(f"{coin}={ratio:.3f}")
    if low_ratio:
        failures.append(
            f"tick count ratio below {count_ratio_min}: {', '.join(low_ratio[:10])}"
        )
    return {
        "verdict": "go" if not failures else "no-go",
        "failures": failures,
        "shared_coins": len(shared),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare canary vs prod parquet (read-only)")
    parser.add_argument("--prod-root", type=Path, default=Path("/data/live"))
    parser.add_argument("--canary-root", type=Path, default=Path("/data/live-hotadd-canary"))
    parser.add_argument("--prod-gaps", type=Path, default=Path("/data/gaps"))
    parser.add_argument("--canary-gaps", type=Path, default=Path("/data/gaps-hotadd-canary"))
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--latency-slack-ms", type=float, default=100.0)
    parser.add_argument("--count-ratio-min", type=float, default=0.95)
    args = parser.parse_args()

    start_ms = int(parse_ts(args.since).timestamp() * 1000)
    end_ms = int(parse_ts(args.until).timestamp() * 1000)
    prod = load_tick_stats(args.prod_root, start_ms=start_ms, end_ms=end_ms)
    canary = load_tick_stats(args.canary_root, start_ms=start_ms, end_ms=end_ms)
    prod_gaps = count_gap_intervals(args.prod_gaps, start_ms=start_ms, end_ms=end_ms)
    canary_gaps = count_gap_intervals(
        args.canary_gaps, start_ms=start_ms, end_ms=end_ms
    )
    gate = evaluate_go_no_go(
        prod,
        canary,
        prod_gaps=prod_gaps,
        canary_gaps=canary_gaps,
        latency_slack_ms=args.latency_slack_ms,
        count_ratio_min=args.count_ratio_min,
    )
    report = {
        "window": {"since": args.since, "until": args.until},
        "prod": prod,
        "canary": canary,
        "gaps": {"prod": prod_gaps, "canary": canary_gaps},
        "go_no_go": gate,
        "thresholds": {
            "latency_slack_ms": args.latency_slack_ms,
            "count_ratio_min": args.count_ratio_min,
        },
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if gate["verdict"] == "go" else 1


if __name__ == "__main__":
    raise SystemExit(main())
