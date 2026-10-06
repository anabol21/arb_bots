"""Hot-add history warm for gear 2.2 would_send (BBOT_* only).

On hot-add, load ~12h of spread / floor history from a configurable root and
hydrate the live floor observer (same warm payload as restart floor_warm.pkl).
Optionally seed TW-p50 rings from recent ticks so theta = p50 - floor can
emit once books start.

Sources (first hit wins per coin):
1. Floor journals under ``{history_root}/floor/event_date=*/metrics.jsonl``
   — same seam as ``app.bot.floor_warm.build_warm_state_from_floor_journal``.
2. Slim spread ticks under history_root:
   - ``spread_ticks.jsonl`` / ``spreads/*.jsonl`` (one JSON object per line)
   - ``spread_ticks.csv`` / ``spreads/*.csv`` with columns
     ``event_local_ts_ms,base_coin,spread_long,spread_short``
   Replay via ``LiveFloorObserver.note_spreads`` then export warm state.

Compacted parquet is intentionally *not* read in-process (no pandas/pyarrow
on the bot path). Offline: build a floor journal or slim ticks copy under
``BBOT_HOT_ADD_HISTORY_ROOT``, then point the canary unit at it read-only.

Fail-closed for trading eligibility when history is missing/short (no finite
floor after warm). Quotes / WS may still run for observability.
"""

from __future__ import annotations

import csv
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

from app.bot.floor_warm import build_warm_state_from_floor_journal
from app.bot.floor_watcher import (
    BAR_MS,
    CLOSE_HISTORY,
    LiveFloorObserver,
    SMA12_HISTORY,
    SIDES,
    _finite,
)

HISTORY_ROOT_ENV = "BBOT_HOT_ADD_HISTORY_ROOT"
HISTORY_HOURS_ENV = "BBOT_HOT_ADD_HISTORY_HOURS"
HISTORY_MIN_SMA_ENV = "BBOT_HOT_ADD_HISTORY_MIN_SMA12"
WARM_ENABLE_ENV = "BBOT_HOT_ADD_WARM"

DEFAULT_HISTORY_HOURS = 12.0
# Floor tf-select needs ~29 finite SMA-12 tips (0.20 * 144); require a bit more.
DEFAULT_MIN_SMA12 = 40

_LOG = logging.getLogger("bbot.hot_add_warm")


def _flag_on(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def bbot_hot_add_warm_enabled() -> bool:
    """Default ON when ``BBOT_HOT_ADD`` is the only gate; ``BBOT_HOT_ADD_WARM=0`` disables."""
    return _flag_on(WARM_ENABLE_ENV, default=True)


def bbot_hot_add_history_hours() -> float:
    raw = os.environ.get(HISTORY_HOURS_ENV, str(DEFAULT_HISTORY_HOURS)).strip()
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{HISTORY_HOURS_ENV} must be > 0, got {value}")
    return value


def bbot_hot_add_history_min_sma12() -> int:
    raw = os.environ.get(HISTORY_MIN_SMA_ENV, str(DEFAULT_MIN_SMA12)).strip()
    value = int(raw)
    if value < 1:
        raise ValueError(f"{HISTORY_MIN_SMA_ENV} must be >= 1, got {value}")
    return value


def resolve_hot_add_history_root(
    data_root: Path,
    env: Optional[Mapping[str, str]] = None,
) -> Optional[Path]:
    """``BBOT_HOT_ADD_HISTORY_ROOT`` or None (caller may fall back to data_root).

    Absolute only for safety when pointing at a collector read-only mount.
    Relative paths resolve under ``data_root``. Never under writable D live trees
    as a *write* target — reading a RO mount of compacted/live is allowed when
    the operator sets an absolute path (canary ReadOnlyPaths).
    """
    e = env if env is not None else os.environ
    raw = str(e.get(HISTORY_ROOT_ENV) or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(data_root) / path
    return path


@dataclass(frozen=True)
class HotAddWarmResult:
    """Outcome of a per-coin history warm attempt."""

    coin: str
    ok: bool
    reason: str
    source: str = ""
    touched_slots: int = 0
    sma12_long: int = 0
    sma12_short: int = 0
    floor_long: Optional[float] = None
    floor_short: Optional[float] = None
    ticks_fed: int = 0

    def as_log_fields(self) -> str:
        return (
            f"base_coin={self.coin} | ok={str(self.ok).lower()} | "
            f"reason={self.reason} | source={self.source or '-'} | "
            f"touched={self.touched_slots} | sma12_long={self.sma12_long} | "
            f"sma12_short={self.sma12_short} | "
            f"floor_long={self.floor_long} | floor_short={self.floor_short} | "
            f"ticks_fed={self.ticks_fed}"
        )


def _sma12_len(observer: LiveFloorObserver, coin: str, side: str) -> int:
    state = observer._states.get((coin, side))  # noqa: SLF001 — test/warm seam
    if state is None:
        return 0
    return sum(1 for v in state.sma12_hist if _finite(v) is not None)


def _warm_ok(
    observer: LiveFloorObserver,
    coin: str,
    *,
    min_sma12: int,
) -> tuple[bool, str, int, int, Optional[float], Optional[float]]:
    """Require finite floors on both sides and enough SMA-12 history."""
    fl = observer.last_floor(coin, "long")
    fs = observer.last_floor(coin, "short")
    n_long = _sma12_len(observer, coin, "long")
    n_short = _sma12_len(observer, coin, "short")
    if fl is None or fs is None:
        return False, "floor_not_finite", n_long, n_short, fl, fs
    if n_long < min_sma12 or n_short < min_sma12:
        return False, "sma12_history_short", n_long, n_short, fl, fs
    return True, "warmed", n_long, n_short, fl, fs


def _iter_spread_jsonl(path: Path, coin: str) -> Iterator[tuple[int, float, float]]:
    coin_u = coin.upper()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        if str(rec.get("base_coin") or "").strip().upper() != coin_u:
            continue
        ts = rec.get("event_local_ts_ms")
        sl = _finite(rec.get("spread_long"))
        ss = _finite(rec.get("spread_short"))
        if ts is None or sl is None or ss is None:
            continue
        try:
            yield int(ts), float(sl), float(ss)
        except (TypeError, ValueError):
            continue


def _iter_spread_csv(path: Path, coin: str) -> Iterator[tuple[int, float, float]]:
    coin_u = coin.upper()
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for rec in reader:
                if str(rec.get("base_coin") or "").strip().upper() != coin_u:
                    continue
                ts = rec.get("event_local_ts_ms")
                sl = _finite(rec.get("spread_long"))
                ss = _finite(rec.get("spread_short"))
                if ts is None or sl is None or ss is None:
                    continue
                try:
                    yield int(float(ts)), float(sl), float(ss)
                except (TypeError, ValueError):
                    continue
    except OSError:
        return


def iter_slim_spread_ticks(
    history_root: Path,
    coin: str,
    *,
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
) -> list[tuple[int, float, float]]:
    """Collect slim spread ticks for ``coin`` under history_root (sorted)."""
    root = Path(history_root)
    candidates: list[Path] = []
    for name in ("spread_ticks.jsonl", "spread_ticks.csv"):
        p = root / name
        if p.is_file():
            candidates.append(p)
    spreads_dir = root / "spreads"
    if spreads_dir.is_dir():
        candidates.extend(sorted(spreads_dir.glob("*.jsonl")))
        candidates.extend(sorted(spreads_dir.glob("*.csv")))
    rows: list[tuple[int, float, float]] = []
    for path in candidates:
        it: Iterator[tuple[int, float, float]]
        if path.suffix.lower() == ".csv":
            it = _iter_spread_csv(path, coin)
        else:
            it = _iter_spread_jsonl(path, coin)
        for ts, sl, ss in it:
            if since_ms is not None and ts < since_ms:
                continue
            if until_ms is not None and ts > until_ms:
                continue
            rows.append((ts, sl, ss))
    rows.sort(key=lambda r: r[0])
    return rows


def build_warm_state_from_slim_spreads(
    ticks: Sequence[tuple[int, float, float]],
    coin: str,
) -> dict[str, Any]:
    """Replay slim ticks into a throwaway observer; return warm payload."""
    obs = LiveFloorObserver([coin])
    for ts, sl, ss in ticks:
        obs.note_spreads(coin, int(ts), float(sl), float(ss))
    # Force-close the open bar by noting one tick into the next bar if needed.
    if ticks:
        last_ts = int(ticks[-1][0])
        bar_start = (last_ts // BAR_MS) * BAR_MS
        # One sample just after bar end closes the open bar.
        obs.note_spreads(
            coin,
            bar_start + BAR_MS + 1,
            float(ticks[-1][1]),
            float(ticks[-1][2]),
        )
    payload = obs.export_warm_state()
    payload["source"] = "slim_spreads"
    return payload


def _filter_warm_payload_to_coin(payload: Mapping[str, Any], coin: str) -> dict[str, Any]:
    coin_u = coin.upper()
    sides = {}
    for key, raw in (payload.get("sides") or {}).items():
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("base_coin") or "").upper() == coin_u:
            sides[key] = dict(raw)
    floors = {}
    for key, raw in (payload.get("last_floors") or {}).items():
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("base_coin") or "").upper() == coin_u:
            floors[key] = dict(raw)
    return {
        "schema_version": payload.get("schema_version") or "bbot.floor_warm.v1",
        "formula_id": payload.get("formula_id"),
        "source": payload.get("source") or "floor_journal",
        "sides": sides,
        "last_floors": floors,
    }


def warm_floor_from_history(
    observer: LiveFloorObserver,
    coin: str,
    history_root: Path,
    *,
    hours: Optional[float] = None,
    min_sma12: Optional[int] = None,
    now_ms: Optional[int] = None,
    logger: Optional[logging.Logger] = None,
) -> HotAddWarmResult:
    """Warm ``observer`` for ``coin`` from history_root. Fail-closed on short/missing."""
    log = logger or _LOG
    coin_u = str(coin).strip().upper()
    hrs = float(hours if hours is not None else bbot_hot_add_history_hours())
    need = int(min_sma12 if min_sma12 is not None else bbot_hot_add_history_min_sma12())
    root = Path(history_root)
    if not root.exists():
        return HotAddWarmResult(
            coin=coin_u, ok=False, reason="history_root_missing", source=str(root)
        )

    # 1) Floor journal seam (same as restart warm).
    journal_payload = build_warm_state_from_floor_journal(root, coins=[coin_u])
    filtered = _filter_warm_payload_to_coin(journal_payload, coin_u)
    if filtered["sides"] or filtered["last_floors"]:
        touched = int(observer.apply_warm_state(filtered))
        ok, reason, n_l, n_s, fl, fs = _warm_ok(observer, coin_u, min_sma12=need)
        result = HotAddWarmResult(
            coin=coin_u,
            ok=ok,
            reason=reason if ok else (reason or "journal_insufficient"),
            source="floor_journal",
            touched_slots=touched,
            sma12_long=n_l,
            sma12_short=n_s,
            floor_long=fl,
            floor_short=fs,
        )
        if ok:
            log.info("bbot_hot_add_warm_ok | %s", result.as_log_fields())
            return result
        # Fall through to slim ticks if journal was present but short.
        log.info(
            "bbot_hot_add_warm_journal_short | %s | trying_slim_ticks=1",
            result.as_log_fields(),
        )

    # 2) Slim spread ticks → replay.
    wall = int(now_ms) if now_ms is not None else None
    since = None
    until = None
    if wall is not None:
        until = wall
        since = wall - int(hrs * 3600 * 1000)
    ticks = iter_slim_spread_ticks(root, coin_u, since_ms=since, until_ms=until)
    if not ticks:
        return HotAddWarmResult(
            coin=coin_u,
            ok=False,
            reason="history_missing",
            source=str(root),
        )
    # Span check: prefer ~hours of coverage; fail-closed if far too short.
    span_ms = int(ticks[-1][0]) - int(ticks[0][0])
    min_span_ms = int(hrs * 3600 * 1000 * 0.5)  # allow 50% short window
    if span_ms < min_span_ms:
        return HotAddWarmResult(
            coin=coin_u,
            ok=False,
            reason="history_span_short",
            source="slim_spreads",
            ticks_fed=len(ticks),
        )
    payload = build_warm_state_from_slim_spreads(ticks, coin_u)
    touched = int(observer.apply_warm_state(payload))
    ok, reason, n_l, n_s, fl, fs = _warm_ok(observer, coin_u, min_sma12=need)
    result = HotAddWarmResult(
        coin=coin_u,
        ok=ok,
        reason=reason if ok else reason,
        source="slim_spreads",
        touched_slots=touched,
        sma12_long=n_l,
        sma12_short=n_s,
        floor_long=fl,
        floor_short=fs,
        ticks_fed=len(ticks),
    )
    if ok:
        log.info("bbot_hot_add_warm_ok | %s", result.as_log_fields())
    else:
        log.error("bbot_hot_add_warm_fail_closed | %s", result.as_log_fields())
    return result


def seed_tw_p50_from_slim_ticks(
    tw_observer: Any,
    coin: str,
    history_root: Path,
    *,
    retain_ms: int = 5 * 60 * 1000,
    now_ms: Optional[int] = None,
) -> int:
    """Feed recent slim ticks into TW-p50 rings. Returns ticks fed."""
    if tw_observer is None:
        return 0
    ticks = iter_slim_spread_ticks(Path(history_root), coin)
    if not ticks:
        return 0
    wall = int(now_ms) if now_ms is not None else int(ticks[-1][0])
    left = wall - int(retain_ms)
    fed = 0
    for ts, sl, ss in ticks:
        if ts < left:
            continue
        tw_observer.note_spreads(coin, int(ts), float(sl), float(ss))
        fed += 1
    return fed


def drop_floor_coin_state(observer: Optional[LiveFloorObserver], coin: str) -> int:
    """Remove per-coin floor state. Returns number of keys removed."""
    if observer is None:
        return 0
    coin_u = str(coin).strip().upper()
    removed = 0
    for side in SIDES:
        key = (coin_u, side)
        if key in observer._states:  # noqa: SLF001
            del observer._states[key]
            removed += 1
        if key in observer._last_floors:  # noqa: SLF001
            del observer._last_floors[key]
            removed += 1
    return removed


def drop_tw_p50_coin_state(observer: Any, coin: str) -> int:
    """Remove per-coin TW-p50 rings + snapshots."""
    if observer is None:
        return 0
    coin_u = str(coin).strip().upper()
    removed = 0
    lock = getattr(observer, "_lock", None)
    def _drop() -> int:
        n = 0
        rings = getattr(observer, "_rings", None)
        snaps = getattr(observer, "_snapshots", None)
        for side in SIDES:
            key = (coin_u, side)
            if isinstance(rings, dict) and key in rings:
                del rings[key]
                n += 1
            if isinstance(snaps, dict) and key in snaps:
                del snaps[key]
                n += 1
        return n
    if lock is not None:
        with lock:
            removed = _drop()
    else:
        removed = _drop()
    return removed


__all__ = [
    "HISTORY_ROOT_ENV",
    "HISTORY_HOURS_ENV",
    "HISTORY_MIN_SMA_ENV",
    "WARM_ENABLE_ENV",
    "HotAddWarmResult",
    "bbot_hot_add_warm_enabled",
    "bbot_hot_add_history_hours",
    "bbot_hot_add_history_min_sma12",
    "resolve_hot_add_history_root",
    "iter_slim_spread_ticks",
    "build_warm_state_from_slim_spreads",
    "warm_floor_from_history",
    "seed_tw_p50_from_slim_ticks",
    "drop_floor_coin_state",
    "drop_tw_p50_coin_state",
    "CLOSE_HISTORY",
    "SMA12_HISTORY",
    "BAR_MS",
]
