"""Live rolling time-weighted p50 watcher for BotRuntime (public books only).

Per ``(base_coin, side)`` keeps compact parallel timestamp/value buffers for
the bounded one-minute reservoir plus one carry-in sample. Every ~1s an asyncio task computes
the duration-weighted one-minute median (TW p50) and journals metric rows —
never raw ticks.

No private broker imports. Hot path: cheap append + age prune only.
Compute + JSONL I/O run in the 1 Hz task (``asyncio.to_thread`` for journal).

TW convention matches gear22 / floor corridor: hold value until next tick;
last segment holds to window right edge. Carry-in from the last sample at or
before the window left edge covers the leading gap when possible.
"""

from __future__ import annotations

import json
from array import array
from bisect import bisect_left, bisect_right
import os
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

import numpy as np

from app.bot.floor_watcher import (
    tick_hold_weights_ms,
    time_weighted_median,
)
from app.bot.fsadvise import advise_dontneed
from app.bot.paths import tw_p50_metrics_jsonl_path
from app.bot.metrics_compact import MetricsDayCompactor, RotatingMetricsJsonl

SCHEMA_VERSION = "bbot.tw_p50.v2"
WINDOW_1M_MS = 60_000
# Keep one carry-in sample before the 1m window's left edge.
RETAIN_MS = WINDOW_1M_MS
DEFAULT_SAMPLE_CAP = 4096
ENV_SAMPLE_CAP = "BBOT_TW_P50_SAMPLE_CAP"
ENV_1M_ENGINE = "BBOT_TW_P50_1M_ENGINE"
DEFAULT_1M_ENGINE = "deque"
SIDES: tuple[str, ...] = ("long", "short")
EMIT_INTERVAL_SEC = 1.0

_GEAR2_TW_P50_PROFILES = frozenset(
    {
        "gear2_would_send",
        "canary_wal_eden",
        "gear2",
        "canary",
        "gear22_would_send",
        "gear22",
        "gear22_live_canary",
        "gear22_live",
    }
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
    raw = str(e.get(ENV_SAMPLE_CAP) or "").strip()
    if not raw:
        return DEFAULT_SAMPLE_CAP
    try:
        n = int(raw)
    except ValueError:
        return DEFAULT_SAMPLE_CAP
    return max(16, n)


def tw_p50_1m_engine(env: Optional[Mapping[str, str]] = None) -> str:
    e = env if env is not None else os.environ
    engine = str(e.get(ENV_1M_ENGINE) or DEFAULT_1M_ENGINE).strip().lower()
    if engine not in {"legacy", "deque"}:
        raise ValueError(f"{ENV_1M_ENGINE} must be legacy or deque")
    return engine


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
    n_1m: int
    coverage_1m: float
    computed_at_ms: int

    def as_row(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "base_coin": self.base_coin,
            "side": self.side,
            "ts_ms": int(self.ts_ms),
            "p50_1m": self.p50_1m,
            "n_1m": int(self.n_1m),
            "coverage_1m": float(self.coverage_1m),
            "computed_at_ms": int(self.computed_at_ms),
        }


@dataclass
class _SideRing:
    """Compact parallel sample columns plus reservoir bookkeeping."""

    timestamps: array = field(default_factory=lambda: array("q"))
    values: array = field(default_factory=lambda: array("d"))
    samples_seen: int = 0  # for reservoir (includes discarded)
    timestamps_ordered: bool = True
    min_ts: Optional[int] = None

    def __len__(self) -> int:
        return len(self.timestamps)

    @property
    def samples(self) -> list[tuple[int, float]]:
        """Compatibility/debug view; hot queries use immutable numeric cuts."""
        return list(zip(self.timestamps, self.values))

    def append(self, sample: tuple[int, float]) -> None:
        self.timestamps.append(sample[0])
        self.values.append(sample[1])
        if self.min_ts is None or sample[0] < self.min_ts:
            self.min_ts = sample[0]

    def replace_sorted(self, index: int, sample: tuple[int, float]) -> None:
        old_min = self.min_ts
        old_ts = self.timestamps.pop(index)
        self.values.pop(index)
        left = bisect_left(self.timestamps, sample[0])
        right = bisect_right(self.timestamps, sample[0])
        insert_at = min(max(index, left), right)
        self.timestamps.insert(insert_at, sample[0])
        self.values.insert(insert_at, sample[1])
        if old_min is None or sample[0] < old_min:
            self.min_ts = sample[0]
        elif old_ts == old_min:
            self.min_ts = (int(np.min(np.frombuffer(self.timestamps, dtype=np.int64))) if self.timestamps else None)

    def snapshot_arrays(self) -> tuple[np.ndarray, np.ndarray]:
        # Bytes back the arrays so workers own an immutable cut after lock release.
        return (
            np.frombuffer(self.timestamps.tobytes(), dtype=np.int64),
            np.frombuffer(self.values.tobytes(), dtype=np.float64),
        )

    def replace_arrays(self, timestamps: np.ndarray, values: np.ndarray) -> None:
        self.timestamps = array("q")
        self.timestamps.frombytes(np.asarray(timestamps, dtype=np.int64).tobytes())
        self.values = array("d")
        self.values.frombytes(np.asarray(values, dtype=np.float64).tobytes())
        self.min_ts = (int(np.min(np.frombuffer(self.timestamps, dtype=np.int64))) if self.timestamps else None)


def tw_p50_window(
    samples: Sequence[tuple[int, float]] | tuple[np.ndarray, np.ndarray],
    *,
    now_ms: int,
    window_ms: int,
    samples_are_ordered: bool = False,
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
    if isinstance(samples, tuple) and len(samples) == 2 and isinstance(samples[0], np.ndarray):
        ts = samples[0]
        values = samples[1]
        if ts.size == 0:
            return float("nan"), 0, 0.0
    else:
        if not samples:
            return float("nan"), 0, 0.0
        ts = np.fromiter(
            (int(sample[0]) for sample in samples), dtype="int64", count=len(samples)
        )
        values = np.fromiter(
            (float(sample[1]) for sample in samples), dtype="float64", count=len(samples)
        )
    if not samples_are_ordered:
        order = np.argsort(ts, kind="stable")
        ts = ts[order]
        values = values[order]

    first_inside = int(np.searchsorted(ts, left, side="right"))
    after_right = int(np.searchsorted(ts, right, side="right"))
    inside_ts = ts[first_inside:after_right]
    inside_values = values[first_inside:after_right]
    n_inside = int(after_right - first_inside)

    if first_inside:
        eff_ts = np.empty(n_inside + 1, dtype="int64")
        eff_values = np.empty(n_inside + 1, dtype="float64")
        eff_ts[0] = left
        eff_values[0] = values[first_inside - 1]
        eff_ts[1:] = inside_ts
        eff_values[1:] = inside_values
    else:
        eff_ts = inside_ts
        eff_values = inside_values
    if eff_ts.size == 0:
        return float("nan"), n_inside, 0.0

    weights = tick_hold_weights_ms(eff_ts, last_end_ms=right)
    total = float(np.sum(weights))
    coverage = max(0.0, min(1.0, total / float(w_ms)))
    p50 = time_weighted_median(eff_values, weights)
    return float(p50), n_inside, float(coverage)


class LiveTwP50Observer:
    """Per-coin / per-side rolling one-minute samples + 1 Hz TW p50 snapshots."""

    def __init__(
        self,
        coins: Sequence[str],
        *,
        sample_cap: Optional[int] = None,
        rng: Optional[random.Random] = None,
        one_minute_engine: Optional[str] = None,
    ) -> None:
        self.sample_cap = (
            int(sample_cap) if sample_cap is not None else tw_p50_sample_cap()
        )
        if self.sample_cap < 16:
            self.sample_cap = 16
        self._rng = rng if rng is not None else random.Random()
        self.one_minute_engine = one_minute_engine or tw_p50_1m_engine()
        if self.one_minute_engine not in {"legacy", "deque"}:
            raise ValueError("one_minute_engine must be legacy or deque")
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
                if len(ring) > self.sample_cap + 1:
                    return False
                if len(ring.timestamps) != len(ring.values):
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
        if self.one_minute_engine == "deque" and len(ring) >= self.sample_cap:
            left = int(ts_ms) - RETAIN_MS
            if (
                not ring.timestamps_ordered
                or (ring.min_ts is not None and ring.min_ts <= left)
            ):
                self._prune_ring(ring, now_ms=int(ts_ms))
        ring.samples_seen += 1
        n = ring.samples_seen
        sample = (int(ts_ms), float(value))
        if len(ring) < self.sample_cap:
            if len(ring) and int(ts_ms) < ring.timestamps[-1]:
                ring.timestamps_ordered = False
            ring.append(sample)
        else:
            # Algorithm R: replacement index/RNG semantics remain unchanged.
            j = self._rng.randrange(n)
            if j < self.sample_cap and len(ring):
                idx = j % len(ring)
                ring.replace_sorted(idx, sample)
                ring.timestamps_ordered = True
        if self.one_minute_engine == "legacy":
            self._prune_ring(ring, now_ms=int(ts_ms))

    def _prune_ring(self, ring: _SideRing, *, now_ms: int) -> None:
        """Drop samples older than 1m except one carry-in at/before left edge."""
        left = int(now_ms) - RETAIN_MS
        if not len(ring):
            return
        if ring.timestamps_ordered:
            start = int(np.searchsorted(
                np.frombuffer(ring.timestamps, dtype=np.int64), left, side="right"
            ))
            if start > 1:
                carry = (ring.timestamps[start - 1], ring.values[start - 1])
                del ring.timestamps[:start]
                del ring.values[:start]
                ring.timestamps.insert(0, carry[0])
                ring.values.insert(0, carry[1])
                ring.min_ts = carry[0]
            return
        timestamps = np.frombuffer(ring.timestamps, dtype=np.int64)
        values = np.frombuffer(ring.values, dtype=np.float64)
        order = np.argsort(timestamps, kind="stable")
        sorted_ts = timestamps[order]
        sorted_values = values[order]
        first_inside = int(np.searchsorted(sorted_ts, left, side="right"))
        after_left = first_inside - 1
        if after_left >= 0:
            kept_ts = np.concatenate((sorted_ts[after_left:after_left + 1], sorted_ts[first_inside:]))
            kept_values = np.concatenate((sorted_values[after_left:after_left + 1], sorted_values[first_inside:]))
        else:
            kept_ts = sorted_ts[first_inside:]
            kept_values = sorted_values[first_inside:]
        if kept_ts.size > self.sample_cap + 1:
            if after_left >= 0:
                kept_ts = np.concatenate((kept_ts[:1], kept_ts[-self.sample_cap:]))
                kept_values = np.concatenate((kept_values[:1], kept_values[-self.sample_cap:]))
            else:
                kept_ts = kept_ts[-(self.sample_cap + 1):]
                kept_values = kept_values[-(self.sample_cap + 1):]
        ring.replace_arrays(kept_ts, kept_values)
        ring.timestamps_ordered = True

    def snapshot_samples(
        self,
    ) -> dict[tuple[str, str], list[tuple[int, float]]]:
        """Copy sample rings for off-lock compute."""
        with self._lock:
            return {k: v.samples for k, v in self._rings.items()}

    def maintain(self, *, now_ms: int) -> None:
        """Apply canonical age pruning without computing or publishing snapshots."""
        now = int(now_ms)
        with self._lock:
            for ring in self._rings.values():
                self._prune_ring(ring, now_ms=now)

    def _copy_sample_cut(
        self, *, now_ms: int, maintain: bool
    ) -> dict[tuple[str, str], tuple[tuple[np.ndarray, np.ndarray], bool]]:
        """Copy one atomic numeric ring cut; optionally maintain it first."""
        with self._lock:
            if maintain:
                for ring in self._rings.values():
                    self._prune_ring(ring, now_ms=int(now_ms))
            return {
                key: (ring.snapshot_arrays(), ring.timestamps_ordered)
                for key, ring in self._rings.items()
            }

    def _query_sample_cut(
        self,
        cut: Mapping[tuple[str, str], tuple[tuple[np.ndarray, np.ndarray], bool]],
        *,
        now_ms: int,
        computed_at_ms: int,
    ) -> list[TwP50Snapshot]:
        out: list[TwP50Snapshot] = []
        for (coin, side), (samples, ordered) in cut.items():
            p50_1m, n_1m, cov_1m = tw_p50_window(
                samples, now_ms=now_ms, window_ms=WINDOW_1M_MS,
                samples_are_ordered=ordered,
            )
            out.append(
                TwP50Snapshot(
                    base_coin=coin,
                    side=side,
                    ts_ms=now_ms,
                    p50_1m=_json_float(p50_1m),
                    n_1m=n_1m,
                    coverage_1m=float(cov_1m),
                    computed_at_ms=computed_at_ms,
                )
            )
        with self._lock:
            for snap in out:
                self._snapshots[(snap.base_coin, snap.side)] = snap
        return out

    def compute_snapshots(
        self,
        *,
        now_ms: Optional[int] = None,
        computed_at_ms: Optional[int] = None,
    ) -> list[TwP50Snapshot]:
        """Compatibility path: atomically maintain and capture the canonical 1 Hz cut."""
        now = int(now_ms) if now_ms is not None else int(time.time() * 1000)
        wall = (
            int(computed_at_ms)
            if computed_at_ms is not None
            else int(time.time() * 1000)
        )
        cut = self._copy_sample_cut(now_ms=now, maintain=True)
        return self._query_sample_cut(cut, now_ms=now, computed_at_ms=wall)

    def query_snapshots(
        self,
        *,
        now_ms: int,
        computed_at_ms: Optional[int] = None,
    ) -> list[TwP50Snapshot]:
        """Read a fixed retained sample cut; does not prune or consume RNG."""
        now = int(now_ms)
        wall = (
            int(computed_at_ms)
            if computed_at_ms is not None
            else int(time.time() * 1000)
        )
        cut = self._copy_sample_cut(now_ms=now, maintain=False)
        return self._query_sample_cut(cut, now_ms=now, computed_at_ms=wall)

    def get_snapshot(self, base_coin: str, side: str) -> Optional[TwP50Snapshot]:
        key = (str(base_coin).upper(), str(side))
        with self._lock:
            return self._snapshots.get(key)


def _dumps_row(rec: Mapping[str, Any]) -> str:
    return json.dumps(dict(rec), separators=(",", ":"), ensure_ascii=False)


class TwP50JournalWriter:
    """Append-only metric JSONL under ``{data_root}/tw_p50/`` (never D trees).

    ``rotate_compress=True`` (``BBOT_METRICS_ROTATE_COMPRESS=1``): on UTC
    event_date rollover the closed day is zstd-compressed off the hot path
    (see ``app.bot.metrics_compact``). Default off = legacy behaviour.
    """

    def __init__(
        self,
        data_root: Path,
        *,
        rotate_compress: bool = False,
        compactor: Optional["MetricsDayCompactor"] = None,
        grace_sec: Optional[float] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.data_root = Path(data_root)
        text = str(self.data_root.resolve())
        for bad in ("/data/live", "/data/bars", "/data/compacted", "/data/spool"):
            if text == bad or text.startswith(bad + os.sep):
                raise RuntimeError(
                    f"TwP50JournalWriter refuses D path: {self.data_root}"
                )
        self._rot = RotatingMetricsJsonl(
            self.data_root / "tw_p50",
            lambda d: tw_p50_metrics_jsonl_path(self.data_root, d),
            rotate_compress=rotate_compress,
            compactor=compactor,
            grace_sec=grace_sec,
            clock=clock or time.time,
            dontneed=lambda fd: advise_dontneed(fd),
        )

    @property
    def rollover(self):  # noqa: ANN201 — test/introspection hook
        return self._rot.rollover

    def append_rows(self, rows: Sequence[Mapping[str, Any]]) -> list[Path]:
        return self._rot.append_rows(rows, _dumps_row)
