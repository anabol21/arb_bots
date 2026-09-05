"""Tight stage timestamps for warm-session trade-latency experiments.

Pure instrumentation — no network, no secrets, no venue I/O.
Stage labels isolate framework overhead (approval/lease/profile/prepare)
from warm-socket send → ack → terminal once private+trade WS is ready.

``else/bybit_ws.py`` is not on this branch; Path A uses the documented
queue→send shape (pre-built payload → queue → long-lived ``ws.send``)
without copying secrets/config from that legacy script.
"""

from __future__ import annotations

import csv
import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

# Ordered critical-path marks for Warm-Lat experiments.
STAGE_LABELS: tuple[str, ...] = (
    "warm_ready",
    "intent",
    "approval",
    "lease",
    "profile",
    "order_prepared",
    "request_sent",
    "ack",
    "terminal",
)

# Bump when top-level JSON keys or summary bucket shape change.
RESULTS_SCHEMA_VERSION = "warm_lat_results.v1"

# Named intervals derived from consecutive / key stage pairs.
INTERVAL_SPECS: tuple[tuple[str, str, str], ...] = (
    ("warm_ready_to_intent", "warm_ready", "intent"),
    ("intent_to_approval", "intent", "approval"),
    ("approval_to_lease", "approval", "lease"),
    ("lease_to_profile", "lease", "profile"),
    ("profile_to_order_prepared", "profile", "order_prepared"),
    ("order_prepared_to_request_sent", "order_prepared", "request_sent"),
    ("request_sent_to_ack", "request_sent", "ack"),
    ("ack_to_terminal", "ack", "terminal"),
    ("warm_ready_to_request_sent", "warm_ready", "request_sent"),
    ("warm_ready_to_terminal", "warm_ready", "terminal"),
    ("intent_to_request_sent", "intent", "request_sent"),
)

# Path A skips framework stages on the critical path (mark as skipped).
PATH_A_SKIPPED_STAGES: frozenset[str] = frozenset(
    {"approval", "lease", "profile", "order_prepared"}
)


class StageLabelError(ValueError):
    """Unknown or out-of-order stage label."""


def assert_known_stage(label: str) -> str:
    name = str(label).strip()
    if name not in STAGE_LABELS:
        raise StageLabelError(
            f"unknown stage {label!r}; expected one of {STAGE_LABELS}"
        )
    return name


def ns_to_ms(delta_ns: int) -> float:
    return float(delta_ns) / 1_000_000.0


def percentile(sorted_vals: Sequence[float], p: float) -> float:
    """Linear interpolation percentile; ``p`` in [0, 1]."""
    if not sorted_vals:
        raise ValueError("percentile requires non-empty values")
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    if p <= 0:
        return float(sorted_vals[0])
    if p >= 1:
        return float(sorted_vals[-1])
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return float(sorted_vals[f])
    return float(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f))


def summarize_ms(values: Sequence[float]) -> dict[str, Any]:
    """Public summary: n / mean / p50 / p95 / min / max (ms)."""
    if not values:
        return {"n": 0}
    s = sorted(float(v) for v in values)
    return {
        "n": len(s),
        "mean": float(statistics.fmean(s)),
        "p50": percentile(s, 0.50),
        "p95": percentile(s, 0.95),
        "min": s[0],
        "max": s[-1],
    }


@dataclass
class StageTrace:
    """One place-cycle monotonic stage map (ns since an arbitrary clock)."""

    cycle_id: int
    path: str  # "A" | "B"
    venue: str  # bybit | okx | dual
    open_mode: str  # serial | parallel | single
    send_enabled: bool
    stamps_ns: dict[str, int] = field(default_factory=dict)
    skipped: set[str] = field(default_factory=set)
    notes: dict[str, Any] = field(default_factory=dict)

    def mark(self, label: str, *, mono_ns: Optional[int] = None) -> int:
        name = assert_known_stage(label)
        ts = int(mono_ns if mono_ns is not None else time.monotonic_ns())
        self.stamps_ns[name] = ts
        self.skipped.discard(name)
        return ts

    def mark_skipped(self, label: str) -> None:
        name = assert_known_stage(label)
        self.skipped.add(name)
        self.stamps_ns.pop(name, None)

    def interval_ms(self, start: str, end: str) -> Optional[float]:
        a = assert_known_stage(start)
        b = assert_known_stage(end)
        if a in self.skipped or b in self.skipped:
            return None
        if a not in self.stamps_ns or b not in self.stamps_ns:
            return None
        return ns_to_ms(self.stamps_ns[b] - self.stamps_ns[a])

    def intervals_ms(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for name, start, end in INTERVAL_SPECS:
            v = self.interval_ms(start, end)
            if v is not None:
                out[name] = v
        return out

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "path": self.path,
            "venue": self.venue,
            "open_mode": self.open_mode,
            "send_enabled": self.send_enabled,
            "stamps_ns": dict(self.stamps_ns),
            "skipped_stages": sorted(self.skipped),
            "intervals_ms": self.intervals_ms(),
            "notes": dict(self.notes),
        }


@dataclass
class WarmLatencyReport:
    """Aggregated Warm-Lat experiment results (machine-readable)."""

    status: str
    n_requested: int
    n_completed: int = 0
    path: str = "AB"
    open_mode: str = "serial"
    send_enabled: bool = False
    dry_run: bool = True
    warm_ready: bool = False
    trade_lat_model_ms: float = 100.0
    cycles: list[StageTrace] = field(default_factory=list)
    error_code: Optional[str] = None
    notes: dict[str, Any] = field(default_factory=dict)

    def add_cycle(self, trace: StageTrace) -> None:
        self.cycles.append(trace)
        self.n_completed = len(self.cycles)

    def aggregate_by_path_venue(self) -> dict[str, Any]:
        buckets: dict[tuple[str, str, str], dict[str, list[float]]] = {}
        for c in self.cycles:
            key = (c.path, c.venue, c.open_mode)
            slot = buckets.setdefault(key, {})
            for iname, ms in c.intervals_ms().items():
                slot.setdefault(iname, []).append(float(ms))
        out: dict[str, Any] = {}
        for (path, venue, mode), intervals in sorted(buckets.items()):
            label = f"path_{path}|venue_{venue}|mode_{mode}"
            out[label] = {
                name: summarize_ms(vals) for name, vals in sorted(intervals.items())
            }
        return out

    def path_ab_delta_ms(self) -> dict[str, Any]:
        """Compare Path A vs Path B on shared interval names (mean/p50)."""
        by_path: dict[str, dict[str, list[float]]] = {"A": {}, "B": {}}
        for c in self.cycles:
            if c.path not in by_path:
                continue
            for iname, ms in c.intervals_ms().items():
                by_path[c.path].setdefault(iname, []).append(float(ms))
        shared = sorted(set(by_path["A"]) & set(by_path["B"]))
        delta: dict[str, Any] = {}
        for name in shared:
            a = summarize_ms(by_path["A"][name])
            b = summarize_ms(by_path["B"][name])
            if a["n"] == 0 or b["n"] == 0:
                continue
            delta[name] = {
                "A": a,
                "B": b,
                "B_minus_A_p50_ms": float(b["p50"]) - float(a["p50"]),
                "B_minus_A_mean_ms": float(b["mean"]) - float(a["mean"]),
            }
        return delta

    def as_public_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema_version": RESULTS_SCHEMA_VERSION,
            "status": self.status,
            "experiment": "Warm-Lat",
            "n_requested": self.n_requested,
            "n_completed": self.n_completed,
            "path": self.path,
            "open_mode": self.open_mode,
            "send_enabled": self.send_enabled,
            "dry_run": self.dry_run,
            "warm_ready": self.warm_ready,
            "trade_lat_model_ms": self.trade_lat_model_ms,
            "stage_labels": list(STAGE_LABELS),
            "interval_names": [n for n, _, _ in INTERVAL_SPECS],
            "summary": self.aggregate_by_path_venue(),
            "path_ab_delta_ms": self.path_ab_delta_ms(),
            "cycles": [c.as_public_dict() for c in self.cycles],
            "notes": dict(self.notes),
            "else_bybit_ws_on_branch": False,
            "path_a_shape": (
                "prebuilt_json → asyncio.Queue → long-lived sender ws.send; "
                "no approval/lease/journal on critical path"
            ),
            "how_to_read_summary": {
                "summary_bucket": "path_{A|B}|venue_{bybit|okx}|mode_{serial|parallel|single}",
                "per_interval_keys": ["n", "mean", "p50", "p95", "min", "max"],
                "units": "milliseconds",
                "primary_live_intervals": [
                    "order_prepared_to_request_sent",
                    "request_sent_to_ack",
                    "ack_to_terminal",
                    "warm_ready_to_terminal",
                ],
            },
        }
        if self.error_code:
            body["error_code"] = self.error_code
        return body


def write_results_json(report: WarmLatencyReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report.as_public_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def write_results_csv(report: WarmLatencyReport, path: Path) -> Path:
    """One row per cycle × interval (long form)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "cycle_id",
        "path",
        "venue",
        "open_mode",
        "send_enabled",
        "interval",
        "ms",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for c in report.cycles:
            for iname, ms in c.intervals_ms().items():
                w.writerow(
                    {
                        "cycle_id": c.cycle_id,
                        "path": c.path,
                        "venue": c.venue,
                        "open_mode": c.open_mode,
                        "send_enabled": str(bool(c.send_enabled)).lower(),
                        "interval": iname,
                        "ms": f"{ms:.6f}",
                    }
                )
    return path


def write_summary_csv(report: WarmLatencyReport, path: Path) -> Path:
    """One row per path|venue|mode × interval with p50/p95."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "bucket",
        "interval",
        "n",
        "mean",
        "p50",
        "p95",
        "min",
        "max",
    ]
    agg = report.aggregate_by_path_venue()
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for bucket, intervals in agg.items():
            for iname, stats in intervals.items():
                if int(stats.get("n") or 0) == 0:
                    continue
                w.writerow(
                    {
                        "bucket": bucket,
                        "interval": iname,
                        "n": stats["n"],
                        "mean": f"{stats['mean']:.6f}",
                        "p50": f"{stats['p50']:.6f}",
                        "p95": f"{stats['p95']:.6f}",
                        "min": f"{stats['min']:.6f}",
                        "max": f"{stats['max']:.6f}",
                    }
                )
    return path


def merge_interval_lists(
    traces: Iterable[StageTrace],
) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for t in traces:
        for k, v in t.intervals_ms().items():
            out.setdefault(k, []).append(float(v))
    return out
