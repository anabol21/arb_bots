"""Live theta screener: TW p50 − gear-2.2 floor (public books only).

Per ``(base_coin, side)``:

- ``theta_1m = p50_1m - floor_tf_select_a25``
- ``theta_5m = p50_5m - floor``

``p50_*`` come from the live TW-p50 watcher RAM snapshot; ``floor`` is the
latest finite gear-2.2 floor from the live floor observer (same coin/side).
If floor or the corresponding p50 is non-finite → that theta is null.

Emit/persist ~1 Hz together with (or immediately after) tw_p50 snapshots.
Never writes ticks. Low RAM: only last floor + last p50 per side for compute.

No private broker imports. No collector / D tree writes.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from app.bot.floor_watcher import LiveFloorObserver
from app.bot.paths import theta_metrics_jsonl_path
from app.bot.tw_p50_watcher import LiveTwP50Observer, TwP50Snapshot

SCHEMA_VERSION = "bbot.theta.v1"
SIDES: tuple[str, ...] = ("long", "short")
EMIT_INTERVAL_SEC = 1.0

_GEAR2_THETA_PROFILES = frozenset(
    {"gear2_would_send", "canary_wal_eden", "gear2", "canary"}
)


def theta_watch_enabled(
    profile: str,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """``BBOT_THETA_WATCH``: 1/0 override; default on for gear2-style profiles."""
    e = env if env is not None else os.environ
    raw = str(e.get("BBOT_THETA_WATCH") or "").strip().lower()
    if raw in ("0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    return str(profile).strip().lower() in _GEAR2_THETA_PROFILES


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


def compute_theta(
    p50_1m: Any,
    p50_5m: Any,
    floor: Any,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Return ``(floor, theta_1m, theta_5m)`` with null rules.

    - floor non-finite → floor/theta_1m/theta_5m all None
    - p50_1m non-finite → theta_1m None (theta_5m may still compute)
    - p50_5m non-finite → theta_5m None
    """
    floor_f = _finite(floor)
    if floor_f is None:
        return None, None, None
    p1 = _finite(p50_1m)
    p5 = _finite(p50_5m)
    theta_1m = (float(p1) - float(floor_f)) if p1 is not None else None
    theta_5m = (float(p5) - float(floor_f)) if p5 is not None else None
    return float(floor_f), theta_1m, theta_5m


@dataclass(frozen=True)
class ThetaSnapshot:
    """RAM + journal row for one ``(base_coin, side)`` theta emit."""

    base_coin: str
    side: str
    ts_ms: int
    p50_1m: Optional[float]
    p50_5m: Optional[float]
    floor_tf_select_a25: Optional[float]
    theta_1m: Optional[float]
    theta_5m: Optional[float]
    computed_at_ms: int

    def as_row(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "base_coin": self.base_coin,
            "side": self.side,
            "ts_ms": int(self.ts_ms),
            "p50_1m": self.p50_1m,
            "p50_5m": self.p50_5m,
            "floor_tf_select_a25": self.floor_tf_select_a25,
            "theta_1m": self.theta_1m,
            "theta_5m": self.theta_5m,
            "computed_at_ms": int(self.computed_at_ms),
        }


def theta_from_inputs(
    *,
    base_coin: str,
    side: str,
    ts_ms: int,
    p50_1m: Any,
    p50_5m: Any,
    floor: Any,
    computed_at_ms: Optional[int] = None,
) -> ThetaSnapshot:
    """Build one theta snapshot from raw p50 + floor inputs."""
    floor_f, th1, th5 = compute_theta(p50_1m, p50_5m, floor)
    wall = (
        int(computed_at_ms)
        if computed_at_ms is not None
        else int(time.time() * 1000)
    )
    return ThetaSnapshot(
        base_coin=str(base_coin).upper(),
        side=str(side),
        ts_ms=int(ts_ms),
        p50_1m=_finite(p50_1m),
        p50_5m=_finite(p50_5m),
        floor_tf_select_a25=floor_f,
        theta_1m=th1,
        theta_5m=th5,
        computed_at_ms=wall,
    )


class LiveThetaScreener:
    """Read last floor + last p50; emit theta rows (~1 Hz caller).

    Holds no sample rings — only the last computed ``ThetaSnapshot`` per side
    for optional consumers. Floor / TW-p50 observers own their history.
    """

    def __init__(
        self,
        coins: Sequence[str],
        *,
        floor_observer: Optional[LiveFloorObserver] = None,
        tw_p50_observer: Optional[LiveTwP50Observer] = None,
    ) -> None:
        self.coins = [str(c).upper() for c in coins]
        self.floor_observer = floor_observer
        self.tw_p50_observer = tw_p50_observer
        self._snapshots: dict[tuple[str, str], ThetaSnapshot] = {}

    def compute_from_tw_snapshots(
        self,
        tw_snapshots: Sequence[TwP50Snapshot],
        *,
        computed_at_ms: Optional[int] = None,
    ) -> list[ThetaSnapshot]:
        """Follow-on to TW p50 emit: map each TW snapshot → theta row."""
        wall = (
            int(computed_at_ms)
            if computed_at_ms is not None
            else int(time.time() * 1000)
        )
        out: list[ThetaSnapshot] = []
        for tw in tw_snapshots:
            floor_val: Optional[float] = None
            if self.floor_observer is not None:
                floor_val = self.floor_observer.last_floor(tw.base_coin, tw.side)
            snap = theta_from_inputs(
                base_coin=tw.base_coin,
                side=tw.side,
                ts_ms=tw.ts_ms,
                p50_1m=tw.p50_1m,
                p50_5m=tw.p50_5m,
                floor=floor_val,
                computed_at_ms=wall,
            )
            out.append(snap)
            self._snapshots[(snap.base_coin, snap.side)] = snap
        return out

    def compute_snapshots(
        self,
        *,
        now_ms: Optional[int] = None,
        computed_at_ms: Optional[int] = None,
    ) -> list[ThetaSnapshot]:
        """Pull last TW RAM snapshots + last floors for all known coins/sides."""
        now = int(now_ms) if now_ms is not None else int(time.time() * 1000)
        wall = (
            int(computed_at_ms)
            if computed_at_ms is not None
            else int(time.time() * 1000)
        )
        out: list[ThetaSnapshot] = []
        for coin in self.coins:
            for side in SIDES:
                p50_1m: Optional[float] = None
                p50_5m: Optional[float] = None
                ts_ms = now
                if self.tw_p50_observer is not None:
                    tw = self.tw_p50_observer.get_snapshot(coin, side)
                    if tw is not None:
                        p50_1m = tw.p50_1m
                        p50_5m = tw.p50_5m
                        ts_ms = int(tw.ts_ms)
                floor_val: Optional[float] = None
                if self.floor_observer is not None:
                    floor_val = self.floor_observer.last_floor(coin, side)
                # Skip sides that have never seen p50 or floor (cold start).
                if p50_1m is None and p50_5m is None and floor_val is None:
                    continue
                snap = theta_from_inputs(
                    base_coin=coin,
                    side=side,
                    ts_ms=ts_ms,
                    p50_1m=p50_1m,
                    p50_5m=p50_5m,
                    floor=floor_val,
                    computed_at_ms=wall,
                )
                out.append(snap)
                self._snapshots[(snap.base_coin, snap.side)] = snap
        return out

    def get_snapshot(self, base_coin: str, side: str) -> Optional[ThetaSnapshot]:
        return self._snapshots.get((str(base_coin).upper(), str(side)))


class ThetaJournalWriter:
    """Append-only metric JSONL under ``{data_root}/theta/`` (never D trees)."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = Path(data_root)
        text = str(self.data_root.resolve())
        for bad in ("/data/live", "/data/bars", "/data/compacted", "/data/spool"):
            if text == bad or text.startswith(bad + os.sep):
                raise RuntimeError(
                    f"ThetaJournalWriter refuses D path: {self.data_root}"
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
            path = theta_metrics_jsonl_path(self.data_root, event_date)
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
