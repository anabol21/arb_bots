"""Stage timestamps for the A/B private send-path experiment.

Pure instrumentation — no network, no secrets, no venue I/O.

Hypothesis: after warm private+trade WS is ready, the W6/manager stack
(recover / operator_approval / lease / journal prepare / preflight) adds
measurable milliseconds between an artificial dual-leg signal and
``ws.send``, versus the historic ``bybit_ws.py`` queue→send shape.

``else/bybit_ws.py`` is not on this branch (see git ``a1ba2b1:bybit_ws.py``).
Contour B implements that queue→send *shape* only.
"""

from __future__ import annotations

import csv
import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

STAGE_LABELS: tuple[str, ...] = (
    "warm_ready",
    "signal",
    "recover",
    "operator_approval",
    "lease",
    "order_prepared",
    "first_request_sent",
    "second_request_sent",
    "first_ack",
    "second_ack",
    "terminal_fill",
    "close_signal",
    "close_first_request_sent",
    "close_second_request_sent",
    "terminal_flat",
)

RESULTS_SCHEMA_VERSION = "ab_send_path_results.v1"

INTERVAL_SPECS: tuple[tuple[str, str, str], ...] = (
    ("warm_ready_to_signal", "warm_ready", "signal"),
    ("signal_to_recover", "signal", "recover"),
    ("signal_to_operator_approval", "signal", "operator_approval"),
    ("signal_to_lease", "signal", "lease"),
    ("signal_to_order_prepared", "signal", "order_prepared"),
    ("signal_to_first_request_sent", "signal", "first_request_sent"),
    ("signal_to_second_request_sent", "signal", "second_request_sent"),
    ("first_to_second_request_sent", "first_request_sent", "second_request_sent"),
    ("first_request_sent_to_first_ack", "first_request_sent", "first_ack"),
    ("signal_to_terminal_fill", "signal", "terminal_fill"),
    ("close_signal_to_close_first_request_sent", "close_signal", "close_first_request_sent"),
    ("signal_to_terminal_flat", "signal", "terminal_flat"),
)

# Contour B skips manager stages on the critical path (mark as skipped).
CONTOUR_B_SKIPPED_STAGES: frozenset[str] = frozenset(
    {"recover", "operator_approval", "lease", "order_prepared"}
)

PRIMARY_COMPARE_INTERVALS: tuple[str, ...] = (
    "signal_to_first_request_sent",
    "signal_to_terminal_flat",
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


def assert_contour(label: str) -> str:
    name = str(label).strip().upper()
    if name not in {"A", "B"}:
        raise StageLabelError(f"unknown contour {label!r}; expected A or B")
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
    """One trial monotonic stage map (ns since an arbitrary clock)."""

    trial_id: int
    contour: str  # A | B
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

    def apply_contour_skips(self) -> None:
        if assert_contour(self.contour) == "B":
            for name in CONTOUR_B_SKIPPED_STAGES:
                if name not in self.stamps_ns:
                    self.mark_skipped(name)

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
            "trial_id": self.trial_id,
            "contour": self.contour,
            "send_enabled": self.send_enabled,
            "stamps_ns": dict(self.stamps_ns),
            "skipped_stages": sorted(self.skipped),
            "intervals_ms": self.intervals_ms(),
            "notes": dict(self.notes),
        }


@dataclass
class AbSendPathReport:
    """Aggregated A/B send-path experiment results (machine-readable)."""

    status: str
    n_requested: int
    contour: str
    n_completed: int = 0
    send_enabled: bool = False
    dry_run: bool = True
    warm_ready: bool = False
    hold_sec: float = 0.0
    trials: list[StageTrace] = field(default_factory=list)
    error_code: Optional[str] = None
    notes: dict[str, Any] = field(default_factory=dict)

    def add_trial(self, trace: StageTrace) -> None:
        self.trials.append(trace)
        self.n_completed = len(self.trials)

    def aggregate_by_contour(self) -> dict[str, Any]:
        buckets: dict[str, dict[str, list[float]]] = {}
        for t in self.trials:
            slot = buckets.setdefault(t.contour, {})
            for iname, ms in t.intervals_ms().items():
                slot.setdefault(iname, []).append(float(ms))
        out: dict[str, Any] = {}
        for contour, intervals in sorted(buckets.items()):
            out[f"contour_{contour}"] = {
                name: summarize_ms(vals) for name, vals in sorted(intervals.items())
            }
        return out

    def contour_ab_delta_ms(self) -> dict[str, Any]:
        """Compare A vs B on shared interval names (mean/p50)."""
        by_c: dict[str, dict[str, list[float]]] = {"A": {}, "B": {}}
        for t in self.trials:
            if t.contour not in by_c:
                continue
            for iname, ms in t.intervals_ms().items():
                by_c[t.contour].setdefault(iname, []).append(float(ms))
        shared = sorted(set(by_c["A"]) & set(by_c["B"]))
        delta: dict[str, Any] = {}
        for name in shared:
            a = summarize_ms(by_c["A"][name])
            b = summarize_ms(by_c["B"][name])
            if a["n"] == 0 or b["n"] == 0:
                continue
            delta[name] = {
                "A": a,
                "B": b,
                "A_minus_B_p50_ms": float(a["p50"]) - float(b["p50"]),
                "A_minus_B_mean_ms": float(a["mean"]) - float(b["mean"]),
            }
        return delta

    def as_public_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema_version": RESULTS_SCHEMA_VERSION,
            "status": self.status,
            "experiment": "ab_send_path",
            "n_requested": self.n_requested,
            "n_completed": self.n_completed,
            "contour": self.contour,
            "send_enabled": self.send_enabled,
            "dry_run": self.dry_run,
            "warm_ready": self.warm_ready,
            "hold_sec": self.hold_sec,
            "stage_labels": list(STAGE_LABELS),
            "interval_names": [n for n, _, _ in INTERVAL_SPECS],
            "primary_compare_intervals": list(PRIMARY_COMPARE_INTERVALS),
            "summary": self.aggregate_by_contour(),
            "contour_ab_delta_ms": self.contour_ab_delta_ms(),
            "trials": [t.as_public_dict() for t in self.trials],
            "notes": dict(self.notes),
            "else_bybit_ws_on_branch": False,
            "historic_shape_ref": "a1ba2b1:bybit_ws.py (queue.put both legs → sender ws.send)",
            "contour_a_shape": (
                "warm private+trade → W6 recover/leases → operator_approval "
                "→ lease → prepare/journal/preflight → dispatch/ws.send"
            ),
            "contour_b_shape": (
                "warm private+trade → signal → asyncio.Queue.put both legs "
                "→ long-lived sender ws.send; no recover/approval/lease/journal prepare"
            ),
            "how_to_read_summary": {
                "summary_bucket": "contour_{A|B}",
                "per_interval_keys": ["n", "mean", "p50", "p95", "min", "max"],
                "units": "milliseconds",
                "primary_live_intervals": list(PRIMARY_COMPARE_INTERVALS),
                "hypothesis": (
                    "A_minus_B on signal_to_first_request_sent is the W6/manager "
                    "overhead vs primitive queue→send (X ms)."
                ),
            },
        }
        if self.error_code:
            body["error_code"] = self.error_code
        return body


def write_results_json(report: AbSendPathReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report.as_public_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def write_results_csv(report: AbSendPathReport, path: Path) -> Path:
    """One row per trial × interval (long form)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "trial_id",
        "contour",
        "send_enabled",
        "interval",
        "ms",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for t in report.trials:
            for iname, ms in t.intervals_ms().items():
                w.writerow(
                    {
                        "trial_id": t.trial_id,
                        "contour": t.contour,
                        "send_enabled": str(bool(t.send_enabled)).lower(),
                        "interval": iname,
                        "ms": f"{ms:.6f}",
                    }
                )
    return path


def write_summary_csv(report: AbSendPathReport, path: Path) -> Path:
    """One row per contour × interval with p50/p95."""
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
    agg = report.aggregate_by_contour()
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


def merge_interval_lists(traces: Iterable[StageTrace]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for t in traces:
        for k, v in t.intervals_ms().items():
            out.setdefault(k, []).append(float(v))
    return out


def apply_journal_monotonic(
    trace: StageTrace,
    events: Sequence[Mapping[str, Any]],
    *,
    phase: str,
) -> None:
    """Copy journal monotonic stamps onto a trace (live Contour A).

    ``phase`` is ``open`` or ``close``. Open maps first two ``request_sent``
    place events to first/second request_sent + acks + terminal_fill.
    Close maps flatten/reduce-only ``request_sent`` to close_* + terminal_flat.
    """
    places = [
        e
        for e in events
        if e.get("event_type") == "request_sent" and e.get("request_kind") == "place"
    ]
    acks = [e for e in events if e.get("event_type") == "ack_received"]
    terms = [
        e
        for e in events
        if e.get("event_type") in {"terminal_update", "fill"}
        or (e.get("event_type") == "order_update" and e.get("order_state") in {"filled", "flat"})
    ]
    approvals = [e for e in events if e.get("event_type") == "operator_approval"]
    prepared = [e for e in events if e.get("event_type") == "order_prepared"]

    def _mono(ev: Mapping[str, Any], *keys: str) -> Optional[int]:
        for k in keys:
            raw = ev.get(k)
            if isinstance(raw, int):
                return raw
        return None

    if phase == "open":
        if approvals:
            ns = _mono(approvals[0], "event_monotonic_ns")
            if ns is not None:
                trace.mark("operator_approval", mono_ns=ns)
        if prepared:
            ns = _mono(prepared[0], "event_monotonic_ns")
            if ns is not None:
                trace.mark("order_prepared", mono_ns=ns)
        if len(places) >= 1:
            ns = _mono(places[0], "send_monotonic_ns", "event_monotonic_ns")
            if ns is not None:
                trace.mark("first_request_sent", mono_ns=ns)
        if len(places) >= 2:
            ns = _mono(places[1], "send_monotonic_ns", "event_monotonic_ns")
            if ns is not None:
                trace.mark("second_request_sent", mono_ns=ns)
        if len(acks) >= 1:
            ns = _mono(acks[0], "receive_monotonic_ns", "event_monotonic_ns")
            if ns is not None:
                trace.mark("first_ack", mono_ns=ns)
        if len(acks) >= 2:
            ns = _mono(acks[1], "receive_monotonic_ns", "event_monotonic_ns")
            if ns is not None:
                trace.mark("second_ack", mono_ns=ns)
        if terms:
            ns = _mono(terms[-1], "receive_monotonic_ns", "event_monotonic_ns")
            if ns is not None:
                trace.mark("terminal_fill", mono_ns=ns)
        return

    if phase == "close":
        if len(places) >= 1:
            ns = _mono(places[0], "send_monotonic_ns", "event_monotonic_ns")
            if ns is not None:
                trace.mark("close_first_request_sent", mono_ns=ns)
        if len(places) >= 2:
            ns = _mono(places[1], "send_monotonic_ns", "event_monotonic_ns")
            if ns is not None:
                trace.mark("close_second_request_sent", mono_ns=ns)
        if terms:
            ns = _mono(terms[-1], "receive_monotonic_ns", "event_monotonic_ns")
            if ns is not None:
                trace.mark("terminal_flat", mono_ns=ns)
