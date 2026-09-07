"""Live gear-2.2 floor observer for BotRuntime (public books only).

In-memory 5m bar aggregation → causal SMA-3 / SMA-12 → locked floor
``compute_chosen_floor`` (tf-select α25 of SMA-12), plus in-bar TW
``tw_p05`` / ``tw_p95`` corridor edges. Persists metric rows on bar close
only — never ticks or full books.

No private broker imports. Hot path: update last spread + open-bar sample
lists only; numpy / floor / TW work runs on bar close; journal I/O is
scheduled off the lock.

Formula import: load ``floors.py`` by path so we do not pull the research
package ``__init__`` (pandas/plotly) into the live bot process.

In-bar corridor: time-weighted p05/p95 with the same hold→next /
last→bar_end convention as gear22 ``quantiles.tick_hold_weights_ms``.
Open-bar samples are discarded after close. Cap via
``BBOT_FLOOR_BAR_SAMPLE_CAP`` (default 4096); over cap uses reservoir
sampling (TW then approximate).
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import random
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping, Optional, Sequence

import numpy as np

from app.bot.paths import floor_metrics_jsonl_path
from app.schema.lean_event import BAR_INTERVAL_MS

SCHEMA_VERSION = "bbot.floor.v1"
FORMULA_ID = "tf-select-a25-of-sma12"
BAR_MS = int(BAR_INTERVAL_MS)  # 300_000
SMA3_BARS = 3
SMA12_BARS = 12
# Bounded memory: closes for SMA-12; SMA-12 history for 12h trim warm-up.
CLOSE_HISTORY = SMA12_BARS
SMA12_HISTORY = 144  # W2_TRIM12_BARS in floors.py (asserted on load)
# Open-bar sample cap (per coin/side). Prefer all samples when under cap.
DEFAULT_BAR_SAMPLE_CAP = 4096
ENV_BAR_SAMPLE_CAP = "BBOT_FLOOR_BAR_SAMPLE_CAP"
SIDES: tuple[str, ...] = ("long", "short")

_GEAR2_FLOOR_PROFILES = frozenset(
    {"gear2_would_send", "canary_wal_eden", "gear2", "canary"}
)

_FLOORS: ModuleType | None = None


def _floors() -> ModuleType:
    """Load locked ``floors.py`` without importing research package __init__."""
    global _FLOORS
    if _FLOORS is not None:
        return _FLOORS
    path = (
        Path(__file__).resolve().parents[2]
        / "research"
        / "gear22_quiet_regime_viz"
        / "floors.py"
    )
    spec = importlib.util.spec_from_file_location(
        "research_gear22_floors_live",
        path,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load floors module from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if int(mod.W2_TRIM12_BARS) != SMA12_HISTORY:
        raise RuntimeError(
            f"floors W2_TRIM12_BARS={mod.W2_TRIM12_BARS} != SMA12_HISTORY={SMA12_HISTORY}"
        )
    _FLOORS = mod
    return mod


def causal_sma(values: np.ndarray, window: int) -> np.ndarray:
    """Right-aligned causal SMA; NaN until window fills (matches candles.causal_sma)."""
    w = int(window)
    x = np.asarray(values, dtype="float64")
    out = np.full(x.shape, np.nan, dtype="float64")
    if w <= 0 or x.size == 0:
        return out
    if w == 1:
        return x.copy()
    for i in range(w - 1, x.size):
        sl = x[i - w + 1 : i + 1]
        if np.all(np.isfinite(sl)):
            out[i] = float(np.mean(sl))
    return out


def floor_bar_start_ms(ts_ms: int, bar_ms: int = BAR_MS) -> int:
    return (int(ts_ms) // int(bar_ms)) * int(bar_ms)


def floor_watch_enabled(
    profile: str,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """``BBOT_FLOOR_WATCH``: 1/0 override; default on for gear2-style profiles."""
    e = env if env is not None else os.environ
    raw = str(e.get("BBOT_FLOOR_WATCH") or "").strip().lower()
    if raw in ("0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    return str(profile).strip().lower() in _GEAR2_FLOOR_PROFILES


def bar_sample_cap(env: Optional[Mapping[str, str]] = None) -> int:
    """Max in-bar (ts, spread) samples retained per coin/side."""
    e = env if env is not None else os.environ
    raw = str(e.get(ENV_BAR_SAMPLE_CAP) or "").strip()
    if not raw:
        return DEFAULT_BAR_SAMPLE_CAP
    try:
        n = int(raw)
    except ValueError:
        return DEFAULT_BAR_SAMPLE_CAP
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


def _causal_sma_tip(closes: Sequence[float], window: int) -> float:
    """Causal SMA tip; NaN until ``window`` finite samples fill."""
    w = int(window)
    if w <= 0 or len(closes) < w:
        return float("nan")
    arr = np.asarray(closes, dtype="float64")
    series = causal_sma(arr, w)
    return float(series[-1])


def tick_hold_weights_ms(ts_ms: np.ndarray, *, last_end_ms: int) -> np.ndarray:
    """Holding time (ms) until next tick; last → ``last_end_ms`` (gear22 convention)."""
    ts = np.asarray(ts_ms, dtype="int64")
    n = int(ts.size)
    if n == 0:
        return np.asarray([], dtype="float64")
    w = np.empty(n, dtype="float64")
    if n >= 2:
        w[:-1] = (ts[1:] - ts[:-1]).astype("float64")
    w[-1] = float(max(0, int(last_end_ms) - int(ts[-1])))
    w[w < 0] = 0.0
    return w


def time_weighted_quantile(
    values: np.ndarray,
    weights_ms: np.ndarray,
    level: float,
) -> float:
    """Single TW quantile (Hyndman-Fan type-7 analogue on weight CDF)."""
    y = np.asarray(values, dtype="float64")
    w = np.asarray(weights_ms, dtype="float64")
    if y.size == 0 or w.size != y.size:
        return float("nan")
    finite = np.isfinite(y) & np.isfinite(w) & (w > 0)
    if not np.any(finite):
        return float("nan")
    y = y[finite]
    w = w[finite]
    order = np.argsort(y, kind="mergesort")
    y = y[order]
    w = w[order]
    cw = np.cumsum(w)
    total = float(cw[-1])
    if total <= 0:
        return float("nan")
    target = float(level) * total
    j = int(np.searchsorted(cw, target, side="left"))
    j = min(max(j, 0), y.size - 1)
    return float(y[j])


def tw_p05_p95_from_samples(
    sample_ts: Sequence[int],
    sample_vals: Sequence[float],
    *,
    bar_end_ms: int,
) -> tuple[float, float]:
    """TW p05 / p95 for one closed bar. Empty → (nan, nan)."""
    if not sample_vals or len(sample_ts) != len(sample_vals):
        return float("nan"), float("nan")
    ts = np.asarray(sample_ts, dtype="int64")
    y = np.asarray(sample_vals, dtype="float64")
    order = np.argsort(ts, kind="mergesort")
    ts = ts[order]
    y = y[order]
    weights = tick_hold_weights_ms(ts, last_end_ms=int(bar_end_ms))
    return (
        time_weighted_quantile(y, weights, 0.05),
        time_weighted_quantile(y, weights, 0.95),
    )


@dataclass
class _SideBarState:
    bar_start_ms: Optional[int] = None
    last_close: Optional[float] = None
    tick_count: int = 0
    closes: deque[float] = field(default_factory=lambda: deque(maxlen=CLOSE_HISTORY))
    sma12_hist: deque[float] = field(
        default_factory=lambda: deque(maxlen=SMA12_HISTORY)
    )
    # Open-bar only; cleared on close. Parallel ts/value lists.
    sample_ts: list[int] = field(default_factory=list)
    sample_vals: list[float] = field(default_factory=list)
    samples_seen: int = 0  # includes discarded (for reservoir)


def _clear_open_bar_samples(state: _SideBarState) -> None:
    state.sample_ts.clear()
    state.sample_vals.clear()
    state.samples_seen = 0


def _append_open_bar_sample(
    state: _SideBarState,
    ts_ms: int,
    value: float,
    *,
    cap: int,
    rng: random.Random,
) -> None:
    """Retain all samples under ``cap``; reservoir thereafter."""
    state.samples_seen += 1
    n = state.samples_seen
    if len(state.sample_vals) < cap:
        state.sample_ts.append(int(ts_ms))
        state.sample_vals.append(float(value))
        return
    # Algorithm R: replace index j with probability cap/n.
    j = rng.randrange(n)
    if j < cap:
        state.sample_ts[j] = int(ts_ms)
        state.sample_vals[j] = float(value)


def _close_side_bar(
    state: _SideBarState,
    *,
    base_coin: str,
    side: str,
    computed_at_ms: int,
) -> Optional[dict[str, Any]]:
    """Finalize current bar into a metric row; update SMA/floor deques."""
    floors = _floors()
    if state.bar_start_ms is None or state.tick_count <= 0:
        return None
    close = state.last_close
    if close is None:
        close_f = float("nan")
    else:
        close_f = float(close)
    state.closes.append(close_f)
    sma3 = _causal_sma_tip(state.closes, SMA3_BARS)
    sma12 = _causal_sma_tip(state.closes, SMA12_BARS)
    state.sma12_hist.append(sma12)
    chosen = floors.compute_chosen_floor(np.asarray(state.sma12_hist, dtype="float64"))
    floor = float(chosen[floors.TF_SELECT_25_NAME][-1])
    edge = float("nan")
    if math.isfinite(close_f) and math.isfinite(floor):
        edge = close_f - floor
    bar_start = int(state.bar_start_ms)
    bar_end = bar_start + BAR_MS
    tw_p05, tw_p95 = tw_p05_p95_from_samples(
        state.sample_ts, state.sample_vals, bar_end_ms=bar_end
    )
    samples_kept = len(state.sample_vals)
    samples_seen = int(state.samples_seen)
    # Discard open-bar ticks immediately — no tick WAL.
    _clear_open_bar_samples(state)
    event_date = datetime.fromtimestamp(
        bar_end / 1000.0, tz=timezone.utc
    ).date().isoformat()
    return {
        "schema_version": SCHEMA_VERSION,
        "formula_id": FORMULA_ID,
        "base_coin": str(base_coin).upper(),
        "side": side,
        "bar_start_ms": bar_start,
        "bar_end_ms": bar_end,
        "event_date": event_date,
        "close": _json_float(close_f),
        "sma3": _json_float(sma3),
        "sma12": _json_float(sma12),
        "floor_tf_select_a25": _json_float(floor),
        "tw_p05": _json_float(tw_p05),
        "tw_p95": _json_float(tw_p95),
        "edge": _json_float(edge),
        "tick_count": int(state.tick_count),
        "samples_kept": samples_kept,
        "samples_seen": samples_seen,
        "computed_at_ms": int(computed_at_ms),
    }


class LiveFloorObserver:
    """Per-coin / per-side in-memory 5m aggregator for live BotRuntime."""

    def __init__(
        self,
        coins: Sequence[str],
        *,
        sample_cap: Optional[int] = None,
        rng: Optional[random.Random] = None,
    ) -> None:
        # Ensure formula module loads once; history maxlen matches W2_TRIM12_BARS.
        floors = _floors()
        hist_max = int(floors.W2_TRIM12_BARS)
        self.sample_cap = (
            int(sample_cap) if sample_cap is not None else bar_sample_cap()
        )
        if self.sample_cap < 16:
            self.sample_cap = 16
        self._rng = rng if rng is not None else random.Random()
        self._states: dict[tuple[str, str], _SideBarState] = {}
        for coin in coins:
            c = str(coin).upper()
            for side in SIDES:
                self._states[(c, side)] = _SideBarState(
                    closes=deque(maxlen=CLOSE_HISTORY),
                    sma12_hist=deque(maxlen=hist_max),
                )

    def memory_bound_ok(self) -> bool:
        """True when every deque / open-bar list respects MemoryMax caps."""
        floors = _floors()
        hist_max = int(floors.W2_TRIM12_BARS)
        for state in self._states.values():
            if state.closes.maxlen != CLOSE_HISTORY:
                return False
            if state.sma12_hist.maxlen != hist_max:
                return False
            if len(state.closes) > CLOSE_HISTORY:
                return False
            if len(state.sma12_hist) > hist_max:
                return False
            if len(state.sample_vals) > self.sample_cap:
                return False
            if len(state.sample_ts) != len(state.sample_vals):
                return False
        return True

    def note_spreads(
        self,
        base_coin: str,
        event_local_ts_ms: int,
        spread_long: float,
        spread_short: float,
        *,
        computed_at_ms: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Feed one valid spread tick. Returns closed-bar metric rows (0–2)."""
        coin = str(base_coin).upper()
        ts = int(event_local_ts_ms)
        now_ms = (
            int(computed_at_ms) if computed_at_ms is not None else int(time.time() * 1000)
        )
        rows: list[dict[str, Any]] = []
        for side, spread in (("long", spread_long), ("short", spread_short)):
            key = (coin, side)
            state = self._states.get(key)
            if state is None:
                floors = _floors()
                state = _SideBarState(
                    closes=deque(maxlen=CLOSE_HISTORY),
                    sma12_hist=deque(maxlen=int(floors.W2_TRIM12_BARS)),
                )
                self._states[key] = state
            row = self._note_side(
                state, coin=coin, side=side, ts_ms=ts, spread=spread, now_ms=now_ms
            )
            if row is not None:
                rows.append(row)
        return rows

    def _note_side(
        self,
        state: _SideBarState,
        *,
        coin: str,
        side: str,
        ts_ms: int,
        spread: float,
        now_ms: int,
    ) -> Optional[dict[str, Any]]:
        finite = _finite(spread)
        bar_start = floor_bar_start_ms(ts_ms)
        if state.bar_start_ms is None:
            state.bar_start_ms = bar_start
            state.last_close = finite
            state.tick_count = 1 if finite is not None else 0
            _clear_open_bar_samples(state)
            if finite is not None:
                _append_open_bar_sample(
                    state, ts_ms, finite, cap=self.sample_cap, rng=self._rng
                )
            return None
        if bar_start < state.bar_start_ms:
            # Out-of-order / clock skew: ignore for bar roll.
            return None
        if bar_start == state.bar_start_ms:
            if finite is not None:
                state.last_close = finite
                state.tick_count += 1
                _append_open_bar_sample(
                    state, ts_ms, finite, cap=self.sample_cap, rng=self._rng
                )
            return None
        # New bar → close previous (only if it had ticks).
        closed: Optional[dict[str, Any]] = None
        if state.tick_count > 0 and state.last_close is not None:
            closed = _close_side_bar(
                state, base_coin=coin, side=side, computed_at_ms=now_ms
            )
        state.bar_start_ms = bar_start
        state.last_close = finite
        state.tick_count = 1 if finite is not None else 0
        _clear_open_bar_samples(state)
        if finite is not None:
            _append_open_bar_sample(
                state, ts_ms, finite, cap=self.sample_cap, rng=self._rng
            )
        return closed


class FloorJournalWriter:
    """Append-only metric JSONL under ``{data_root}/floor/`` (never D trees)."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = Path(data_root)
        text = str(self.data_root.resolve())
        for bad in ("/data/live", "/data/bars", "/data/compacted", "/data/spool"):
            if text == bad or text.startswith(bad + os.sep):
                raise RuntimeError(
                    f"FloorJournalWriter refuses D path: {self.data_root}"
                )

    def append_rows(self, rows: Sequence[Mapping[str, Any]]) -> list[Path]:
        if not rows:
            return []
        by_date: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            event_date = str(row.get("event_date") or "")
            if not event_date:
                bar_end = int(row["bar_end_ms"])
                event_date = datetime.fromtimestamp(
                    bar_end / 1000.0, tz=timezone.utc
                ).date().isoformat()
            by_date.setdefault(event_date, []).append(row)
        written: list[Path] = []
        for event_date, batch in by_date.items():
            path = floor_metrics_jsonl_path(self.data_root, event_date)
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
