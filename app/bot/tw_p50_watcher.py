"""Live rolling time-weighted p50 watcher for BotRuntime (public books only).

Per ``(base_coin, side)`` keeps an in-memory deque of ``(ts_ms, spread)`` for
the last ~5 minutes (plus one carry-in sample). Every ~1s an asyncio task
computes duration-weighted median (TW p50) over 1m and 5m windows and journals
metric rows — never raw ticks.

No private broker imports. Hot path: cheap append + age prune only.
Compute + JSONL I/O run in the 1 Hz task (``asyncio.to_thread`` for journal).

TW convention matches gear22 / floor corridor: hold value until next tick;
last segment holds to window right edge. Carry-in from the last sample at or
before the window left edge covers the leading gap when possible.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import numpy as np

from app.bot.floor_watcher import tick_hold_weights_ms, time_weighted_quantile
from app.bot.paths import tw_p50_metrics_jsonl_path

SCHEMA_VERSION = "bbot.tw_p50.v1"
WINDOW_1M_MS = 60_000
WINDOW_5M_MS = 300_000
# Retain slightly past 5m so age-prune can keep one carry-in before the left edge.
RETAIN_MS = WINDOW_5M_MS
DEFAULT_SAMPLE_CAP = 4096
ENV_SAMPLE_CAP = "BBOT_TW_P50_SAMPLE_CAP"
# Fall back to floor cap env when TW-specific unset (same floor-style default).
ENV_FLOOR_SAMPLE_CAP = "BBOT_FLOOR_BAR_SAMPLE_CAP"
SIDES: tuple[str, ...] = ("long", "short")
EMIT_INTERVAL_SEC = 1.0

_GEAR2_TW_P50_PROFILES = frozenset(
    {"gear2_would_send", "canary_wal_eden", "gear2", "canary"}
)


def tw_p50_watch_enabled(
    profile: str,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """``BBOT_TW_P50_WATCH``: 1/0 override; default on for gear2-style profiles."""
    e = env if env is not None else os.environ
    raw = str(e.get("BBOT_TW_P50_WATCH") or "").strip().lower()
    if raw in ("0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    return str(profile).strip().lower() in _GEAR2_TW_P50_PROFILES


def tw_p50_sample_cap(env: Optional[Mapping[str, str]] = None) -> int:
    """Max retained ``(ts, spread)`` samples per coin/side."""
    e = env if env is not None else os.environ
    raw = str(e.get(ENV_SAMPLE_CAP) or e.get(ENV_FLOOR_SAMPLE_CAP) or "").strip()
    if not raw:
        return DEFAULT_SAMPLE_CAP
    try:
        n = int(raw)
    except ValueError:
        return DEFAULT_SAMPLE_CAP
    return max(16, n)


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _json_float(value: float) -> Optional[float]:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


@dataclass
class TwP50Snapshot:
    """RAM snapshot for one ``(base_coin, side)`` after a 1 Hz compute."""

    base_coin: str
    side: str
    ts_ms: int
    p50_1m: Optional[float]
    p50_5m: Optional[float]
    n_1m: int
    n_5m: int
    coverage_1m: float
    coverage_5m: float
    computed_at_ms: int

    def as_row(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "base_coin": self.base_coin,
            "side": self.side,
            "ts_ms": int(self.ts_ms),
            "p50_1m": self.p50_1m,
            "p50_5m": self.p50_5m,
            "n_1m": int(self.n_1m),
            "n_5m": int(self.n_5m),
            "coverage_1m": float(self.coverage_1m),
            "coverage_5m": float(self.coverage_5m),
            "computed_at_ms": int(self.computed_at_ms),
        }


@dataclass
class _SideRing:
    """Bounded ring of spread samples + optional pre-window carry."""

    samples: deque[tuple[int, float]] = field(default_factory=deque)
    samples_seen: int = 0  # for reservoir (includes discarded)


def tw_p50_window(
    samples: Sequence[tuple[int, float]],
    *,
    now_ms: int,
    window_ms: int,
) -> tuple[float, int, float]:
    """Duration-weighted median over ``[now-window, now]`` with carry-in.

    Returns ``(p50, n_inside, coverage)`` where ``n_inside`` counts samples
    with ``left < ts <= now``, and coverage is observed mass / window_ms.
    """
    w_ms = int(window_ms)
    right = int(now_ms)
    left = right - w_ms
    if w_ms <= 0:
        return float("nan"), 0, 0.0
    if not samples:
        return float("nan"), 0, 0.0

    carry_val: Optional[float] = None
    inside_ts: list[int] = []
    inside_val: list[float] = []
    for ts_raw, val_raw in samples:
        ts = int(ts_raw)
        val = float(val_raw)
        if ts <= left:
            carry_val = val
        elif ts <= right:
            inside_ts.append(ts)
            inside_val.append(val)

    if carry_val is not None:
        eff_ts = [left, *inside_ts]
        eff_val = [float(carry_val), *inside_val]
    else:
        eff_ts = inside_ts
        eff_val = inside_val

    n_inside = len(inside_ts)
    if not eff_ts:
        return float("nan"), 0, 0.0

    ts_arr = np.asarray(eff_ts, dtype="int64")
    y_arr = np.asarray(eff_val, dtype="float64")
    weights = tick_hold_weights_ms(ts_arr, last_end_ms=right)
    # Guard: no mass before left / after right (construction should already ensure).
    total = float(np.sum(weights)) if weights.size else 0.0
    coverage = max(0.0, min(1.0, total / float(w_ms))) if w_ms > 0 else 0.0
    p50 = time_weighted_quantile(y_arr, weights, 0.50)
    return float(p50), int(n_inside), float(coverage)


class LiveTwP50Observer:
    """Per-coin / per-side rolling 5m sample ring + 1 Hz TW p50 snapshots."""

    def __init__(
        self,
        coins: Sequence[str],
        *,
        sample_cap: Optional[int] = None,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.sample_cap = (
            int(sample_cap) if sample_cap is not None else tw_p50_sample_cap()
        )
        if self.sample_cap < 16:
            self.sample_cap = 16
        self._rng = rng if rng is not None else random.Random()
        self._lock = threading.Lock()
        self._rings: dict[tuple[str, str], _SideRing] = {}
        self._snapshots: dict[tuple[str, str], TwP50Snapshot] = {}
        for coin in coins:
            c = str(coin).upper()
            for side in SIDES:
                self._rings[(c, side)] = _SideRing()

    def memory_bound_ok(self) -> bool:
        with self._lock:
            for ring in self._rings.values():
                if len(ring.samples) > self.sample_cap + 1:
                    # +1 allows a single carry-in older than RETAIN_MS.
                    return False
        return True

    def note_spreads(
        self,
        base_coin: str,
        event_local_ts_ms: int,
        spread_long: float,
        spread_short: float,
    ) -> None:
        """Append one valid spread tick (hot path — no p50 compute)."""
        coin = str(base_coin).upper()
        ts = int(event_local_ts_ms)
        with self._lock:
            for side, spread in (("long", spread_long), ("short", spread_short)):
                finite = _finite(spread)
                if finite is None:
                    continue
                key = (coin, side)
                ring = self._rings.get(key)
                if ring is None:
                    ring = _SideRing()
                    self._rings[key] = ring
                self._append_sample(ring, ts, finite)

    def _append_sample(self, ring: _SideRing, ts_ms: int, value: float) -> None:
        ring.samples_seen += 1
        n = ring.samples_seen
        if len(ring.samples) < self.sample_cap:
            ring.samples.append((int(ts_ms), float(value)))
        else:
            # Algorithm R: replace index j with probability cap/n.
            j = self._rng.randrange(n)
            if j < self.sample_cap:
                # Replace among non-carry body when possible.
                body = list(ring.samples)
                if body:
                    idx = j % len(body)
                    body[idx] = (int(ts_ms), float(value))
                    ring.samples = deque(body)
        self._prune_ring(ring, now_ms=int(ts_ms))

    def _prune_ring(self, ring: _SideRing, *, now_ms: int) -> None:
        """Drop samples older than 5m except one carry-in at/before left edge."""
        left = int(now_ms) - RETAIN_MS
        if not ring.samples:
            return
        # Keep chronological order; samples may be slightly out of order under reservoir.
        ordered = sorted(ring.samples, key=lambda p: p[0])
        carry: Optional[tuple[int, float]] = None
        kept: list[tuple[int, float]] = []
        for ts, val in ordered:
            if ts <= left:
                carry = (ts, val)
            else:
                kept.append((ts, val))
        out: list[tuple[int, float]] = []
        if carry is not None:
            out.append(carry)
        out.extend(kept)
        # Hard cap: prefer newest after carry.
        if len(out) > self.sample_cap + 1:
            carry_part = out[:1] if carry is not None else []
            body = out[len(carry_part) :]
            body = body[-(self.sample_cap) :]
            out = carry_part + body
            if len(out) > self.sample_cap + 1:
                out = out[-(self.sample_cap + 1) :]
        ring.samples = deque(out)

    def snapshot_samples(
        self,
    ) -> dict[tuple[str, str], list[tuple[int, float]]]:
        """Copy sample rings for off-lock compute."""
        with self._lock:
            return {k: list(v.samples) for k, v in self._rings.items()}

    def compute_snapshots(
        self,
        *,
        now_ms: Optional[int] = None,
        computed_at_ms: Optional[int] = None,
    ) -> list[TwP50Snapshot]:
        """Compute TW p50 for all rings; update RAM snapshots. Off hot path."""
        now = int(now_ms) if now_ms is not None else int(time.time() * 1000)
        wall = (
            int(computed_at_ms)
            if computed_at_ms is not None
            else int(time.time() * 1000)
        )
        copies = self.snapshot_samples()
        # Age-prune under lock using compute time so rings stay bounded.
        with self._lock:
            for ring in self._rings.values():
                self._prune_ring(ring, now_ms=now)

        out: list[TwP50Snapshot] = []
        for (coin, side), samples in copies.items():
            p50_1m, n_1m, cov_1m = tw_p50_window(
                samples, now_ms=now, window_ms=WINDOW_1M_MS
            )
            p50_5m, n_5m, cov_5m = tw_p50_window(
                samples, now_ms=now, window_ms=WINDOW_5M_MS
            )
            snap = TwP50Snapshot(
                base_coin=coin,
                side=side,
                ts_ms=now,
                p50_1m=_json_float(p50_1m),
                p50_5m=_json_float(p50_5m),
                n_1m=n_1m,
                n_5m=n_5m,
                coverage_1m=float(cov_1m),
                coverage_5m=float(cov_5m),
                computed_at_ms=wall,
            )
            out.append(snap)
        with self._lock:
            for snap in out:
                self._snapshots[(snap.base_coin, snap.side)] = snap
        return out

    def get_snapshot(self, base_coin: str, side: str) -> Optional[TwP50Snapshot]:
        key = (str(base_coin).upper(), str(side))
        with self._lock:
            return self._snapshots.get(key)


class TwP50JournalWriter:
    """Append-only metric JSONL under ``{data_root}/tw_p50/`` (never D trees)."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = Path(data_root)
        text = str(self.data_root.resolve())
        for bad in ("/data/live", "/data/bars", "/data/compacted", "/data/spool"):
            if text == bad or text.startswith(bad + os.sep):
                raise RuntimeError(
                    f"TwP50JournalWriter refuses D path: {self.data_root}"
                )

    def append_rows(self, rows: Sequence[Mapping[str, Any]]) -> list[Path]:
        if not rows:
            return []
        by_date: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            ts_ms = int(row.get("ts_ms") or row.get("computed_at_ms") or 0)
            event_date = datetime.fromtimestamp(
                ts_ms / 1000.0, tz=timezone.utc
            ).date().isoformat()
            by_date.setdefault(event_date, []).append(row)
        written: list[Path] = []
        for event_date, batch in by_date.items():
            path = tw_p50_metrics_jsonl_path(self.data_root, event_date)
            with path.open("a", encoding="utf-8") as fh:
                for rec in batch:
                    line = json.dumps(
                        dict(rec), separators=(",", ":"), ensure_ascii=False
                    )
                    fh.write(line)
                    fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            written.append(path)
        return written
