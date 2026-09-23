#!/usr/bin/env python3
"""Read-only analysis for a completed A/B/C WebSocket fan-out run.

For r2 and later, prefer raw XRP delivery and loop-lag CSV files when present.
Older runs without them retain their explicitly labelled minute-summary output;
the tool never fabricates pooled shadow percentiles from minute percentiles.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * quantile)]


def numeric_stats(values: Iterable[float]) -> dict[str, float | int | None]:
    values = list(values)
    return {
        "n": len(values),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": max(values) if values else None,
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def raw_csv_stats(path: Path, value_field: str, warmup_sec: float) -> dict[str, Any] | None:
    """Return exact pooled raw values after warmup, or None when absent."""
    if not path.is_file():
        return None
    values: list[float] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                if float(row["elapsed_sec"]) >= warmup_sec:
                    values.append(float(row[value_field]))
            except (KeyError, TypeError, ValueError):
                continue
    return numeric_stats(values)


def steady_rows(rows: list[dict[str, Any]], start: datetime, warmup: timedelta) -> list[dict[str, Any]]:
    boundary = start + warmup
    return [row for row in rows if parse_time(row["ts_utc"]) >= boundary]


def process_snapshots(rows: list[dict[str, Any]]) -> dict[str, Any]:
    snapshots = [row for row in rows if row["event"] == "metrics_1s" and row.get("process")]
    if len(snapshots) < 2:
        return {"samples": len(snapshots)}
    first, last = snapshots[0], snapshots[-1]
    elapsed = float(last["elapsed_sec"]) - float(first["elapsed_sec"])
    first_process, last_process = first["process"], last["process"]
    cpu_first = (first_process.get("cpu_user_sec") or 0) + (first_process.get("cpu_system_sec") or 0)
    cpu_last = (last_process.get("cpu_user_sec") or 0) + (last_process.get("cpu_system_sec") or 0)
    return {
        "samples": len(snapshots),
        "elapsed_sec": elapsed,
        "cpu_percent_one_core": round((cpu_last - cpu_first) * 100 / elapsed, 3) if elapsed else None,
        "rss_bytes": numeric_stats(
            snapshot["process"]["rss_bytes"] for snapshot in snapshots if snapshot["process"].get("rss_bytes") is not None
        ),
        "fd_count": numeric_stats(
            snapshot["process"]["fd_count"] for snapshot in snapshots if snapshot["process"].get("fd_count") is not None
        ),
        "host_load_1": numeric_stats(
            snapshot["process"]["host_load_1"]
            for snapshot in snapshots
            if snapshot["process"].get("host_load_1") is not None
        ),
    }


def counter_delta(rows: list[dict[str, Any]], exchange: str) -> dict[str, Any]:
    snapshots = [row for row in rows if row["event"] == "metrics_1s" and exchange in row.get("exchanges", {})]
    if len(snapshots) < 2:
        return {"samples": len(snapshots)}
    first, last = snapshots[0], snapshots[-1]
    elapsed = float(last["elapsed_sec"]) - float(first["elapsed_sec"])
    start, end = first["exchanges"][exchange], last["exchanges"][exchange]
    counters = (
        "frames_received",
        "bytes_received",
        "xrp_frames",
        "non_xrp_frames",
        "discarded_data_frames",
        "control_frames",
        "json_loads",
        "connection_errors",
        "connections_opened",
        "connections_closed",
        "protocol_errors",
    )
    result = {"elapsed_sec": elapsed}
    for name in counters:
        result[name] = int(end.get(name, 0)) - int(start.get(name, 0))
    result["frames_per_sec"] = round(result["frames_received"] / elapsed, 3) if elapsed else None
    result["bytes_per_sec"] = round(result["bytes_received"] / elapsed, 3) if elapsed else None
    return result


def minute_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    minute_rows = [row for row in rows if row["event"] == "metrics_minute"]
    delivery: dict[str, dict[str, Any]] = {}
    for exchange in ("okx", "bybit"):
        values = [row["xrp_delivery_latency"][exchange] for row in minute_rows]
        delivery[exchange] = {
            "sample_n_sum": sum(value["n"] for value in values),
            "minute_count": len(values),
            "minute_p50_ms": numeric_stats(value["p50"] for value in values if value["p50"] is not None),
            "minute_p95_ms": numeric_stats(value["p95"] for value in values if value["p95"] is not None),
            "minute_p99_ms": numeric_stats(value["p99"] for value in values if value["p99"] is not None),
            "max_of_minute_max_ms": max(
                (value["max"] for value in values if value["max"] is not None), default=None
            ),
            "negative_n_sum": sum(value["negative_n"] for value in values),
        }
    lag = [row["loop_lag_ms"] for row in minute_rows]
    denominator = sum(
        bool(row["xrp_delivery_latency"]["okx"]["n"]) and bool(row["xrp_delivery_latency"]["bybit"]["n"])
        for row in minute_rows
    )
    return {
        "metrics_minute_count": len(minute_rows),
        "delivery": delivery,
        "dual_gt_500": sum(row["dual_gt_500"] for row in minute_rows),
        "dual_gt_1000": sum(row["dual_gt_1000"] for row in minute_rows),
        "dual_denominator": denominator,
        "loop_lag_ms": {
            "minute_p50_ms": numeric_stats(value["p50"] for value in lag if value["p50"] is not None),
            "minute_p95_ms": numeric_stats(value["p95"] for value in lag if value["p95"] is not None),
            "minute_p99_ms": numeric_stats(value["p99"] for value in lag if value["p99"] is not None),
            "max_of_minute_max_ms": max((value["max"] for value in lag if value["max"] is not None), default=None),
            "gt_200_sum": sum(row["counters"].get("loop", {}).get("lag_gt_200", 0) for row in minute_rows),
            "gt_500_sum": sum(row["counters"].get("loop", {}).get("lag_gt_500", 0) for row in minute_rows),
            "gt_1000_sum": sum(row["counters"].get("loop", {}).get("lag_gt_1000", 0) for row in minute_rows),
        },
    }


def ping_summary(rows: list[dict[str, Any]], start: datetime, warmup: timedelta) -> dict[str, Any]:
    result = {}
    for exchange in ("okx", "bybit"):
        metric = "latency_ms" if exchange == "okx" else "age_ts_ms"
        values = [
            float(row[metric])
            for row in steady_rows(rows, start, warmup)
            if row.get("exchange") == exchange and metric in row
        ]
        errors = sum(
            row.get("event") == "connection_error" and row.get("exchange") == exchange
            for row in steady_rows(rows, start, warmup)
        )
        result[exchange] = {**numeric_stats(values), "connection_errors": errors, "metric": metric}
    return result


def analyze_arm(root: Path, arm: str, warmup: timedelta) -> dict[str, Any]:
    runtime = read_jsonl(root / f"arm_{arm}" / "runtime.jsonl")
    ping = read_jsonl(root / f"arm_{arm}" / "ping_xrp.log")
    start_row = next(row for row in runtime if row["event"] == "start")
    finish_row = next(row for row in runtime if row["event"] == "finished")
    ping_start_row = next(row for row in ping if row.get("event") == "start")
    ping_finish_row = next(row for row in ping if row.get("event") == "finished")
    start, finish = parse_time(start_row["ts_utc"]), parse_time(finish_row["ts_utc"])
    steady_runtime = steady_rows(runtime, start, warmup)
    steady_ping = steady_rows(ping, start, warmup)
    minute = minute_summary(steady_runtime)
    ping_stats = ping_summary(ping, start, warmup)
    raw_delivery = {
        exchange: raw_csv_stats(
            root / f"arm_{arm}" / f"xrp_delivery_{exchange}.csv",
            "delivery_latency_ms",
            warmup.total_seconds(),
        )
        for exchange in ("okx", "bybit")
    }
    raw_loop_lag = raw_csv_stats(
        root / f"arm_{arm}" / "loop_lag.csv",
        "lag_ms",
        warmup.total_seconds(),
    )
    delivery_p99_for_ratio = {
        exchange: (
            raw_delivery[exchange]["p99"]
            if raw_delivery[exchange] is not None
            else minute["delivery"][exchange]["minute_p99_ms"]["p50"]
        )
        for exchange in ("okx", "bybit")
    }
    p99_ratios = {
        exchange: (
            delivery_p99_for_ratio[exchange] / ping_stats[exchange]["p99"]
            if delivery_p99_for_ratio[exchange] is not None and ping_stats[exchange]["p99"]
            else None
        )
        for exchange in ("okx", "bybit")
    }
    return {
        "arm": arm,
        "run_id": start_row["run_id"],
        "start_utc": start_row["ts_utc"],
        "finish_utc": finish_row["ts_utc"],
        "duration_sec": (finish - start).total_seconds(),
        "warmup_sec": warmup.total_seconds(),
        "steady_duration_sec": (finish - start - warmup).total_seconds(),
        "expected_connections": start_row["expected_connections"],
        "universe_count": start_row["universe_count"],
        "full_handling": start_row["full_handling"],
        "subscription_sent_count": sum(row["event"] == "subscription_sent" for row in runtime),
        "active_connection_max": max(
            (
                row.get("active_connections", 0)
                for row in runtime
                if row["event"] in {"metrics_1s", "metrics_minute", "subscription_sent"}
            ),
            default=0,
        ),
        "finished_clean_shutdown": finish_row.get("clean_shutdown"),
        "safety_abort_count": sum(row["event"] == "safety_abort" for row in runtime),
        "connection_error_events": sum(row["event"] == "connection_error" for row in runtime),
        "metrics_1s_count": sum(row["event"] == "metrics_1s" for row in runtime),
        "ping_sample_events": sum("latency_ms" in row for row in steady_ping),
        "ping_start_utc": ping_start_row["ts_utc"],
        "ping_finish_utc": ping_finish_row["ts_utc"],
        "ping_duration_sec": (parse_time(ping_finish_row["ts_utc"]) - parse_time(ping_start_row["ts_utc"])).total_seconds(),
        "ping": ping_stats,
        "delivery_raw": raw_delivery,
        "loop_lag_raw": raw_loop_lag,
        "delivery_and_dual": minute,
        "minute_p99_to_ping_p99_ratio": p99_ratios,
        "resources": process_snapshots(steady_runtime),
        "exchange_counters_steady_delta": {
            exchange: counter_delta(steady_runtime, exchange) for exchange in ("okx", "bybit")
        },
        "limitations": (
            [
                "Raw XRP delivery and loop-lag CSV were used for exact pooled quantiles after warmup.",
                "Dual-minute counts still come from runtime metrics_minute records.",
            ]
            if all(raw_delivery.values()) and raw_loop_lag is not None
            else [
                "Shadow delivery and/or loop-lag raw samples are absent; only per-minute summaries are available.",
                "Exact arm-wide shadow p50/p95/p99 cannot be reconstructed from minute summaries.",
                "minute_pXX_ms.p50 is the median of minute-level percentiles, not an arm-wide percentile.",
            ]
        ),
    }


def write_csv(path: Path, arms: dict[str, dict[str, Any]]) -> None:
    fields = [
        "arm",
        "start_utc",
        "finish_utc",
        "duration_sec",
        "steady_duration_sec",
        "expected_connections",
        "subscription_sent_count",
        "active_connection_max",
        "safety_abort_count",
        "exchange",
        "shadow_n",
        "shadow_minute_p99_median_ms",
        "shadow_minute_p99_max_ms",
        "shadow_max_ms",
        "ping_n",
        "ping_p99_ms",
        "minute_p99_median_to_ping_p99_ratio",
        "dual_gt_500",
        "dual_gt_1000",
        "dual_denominator",
        "frames_per_sec",
        "discarded_data_frames",
        "connection_errors",
        "protocol_errors",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for arm, data in arms.items():
            summary = data["delivery_and_dual"]
            for exchange in ("okx", "bybit"):
                delivery = summary["delivery"][exchange]
                counter = data["exchange_counters_steady_delta"][exchange]
                writer.writerow(
                    {
                        "arm": arm,
                        "start_utc": data["start_utc"],
                        "finish_utc": data["finish_utc"],
                        "duration_sec": data["duration_sec"],
                        "steady_duration_sec": data["steady_duration_sec"],
                        "expected_connections": data["expected_connections"],
                        "subscription_sent_count": data["subscription_sent_count"],
                        "active_connection_max": data["active_connection_max"],
                        "safety_abort_count": data["safety_abort_count"],
                        "exchange": exchange,
                        "shadow_n": delivery["sample_n_sum"],
                        "shadow_minute_p99_median_ms": delivery["minute_p99_ms"]["p50"],
                        "shadow_minute_p99_max_ms": delivery["minute_p99_ms"]["max"],
                        "shadow_max_ms": delivery["max_of_minute_max_ms"],
                        "ping_n": data["ping"][exchange]["n"],
                        "ping_p99_ms": data["ping"][exchange]["p99"],
                        "minute_p99_median_to_ping_p99_ratio": data["minute_p99_to_ping_p99_ratio"][exchange],
                        "dual_gt_500": summary["dual_gt_500"],
                        "dual_gt_1000": summary["dual_gt_1000"],
                        "dual_denominator": summary["dual_denominator"],
                        "frames_per_sec": counter.get("frames_per_sec"),
                        "discarded_data_frames": counter.get("discarded_data_frames"),
                        "connection_errors": counter.get("connection_errors"),
                        "protocol_errors": counter.get("protocol_errors"),
                    }
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Local copy of the VPS experiment root.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--warmup-sec", type=int, default=600)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    arms = {arm: analyze_arm(args.root, arm, timedelta(seconds=args.warmup_sec)) for arm in ("A", "B", "C")}
    payload = {
        "run_id": arms["A"]["run_id"],
        "source_root": str(args.root),
        "warmup_sec": args.warmup_sec,
        "arms": arms,
    }
    (args.output_dir / "analysis.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(args.output_dir / "summary.csv", arms)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
