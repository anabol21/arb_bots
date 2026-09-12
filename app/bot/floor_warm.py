"""Floor observer warm-start: pickle under BBOT data root (never D trees).

Standard path for ``gear22_would_send``:

1. Offline: build ``{BBOT_DATA_ROOT}/state/floor_warm.pkl`` from a prior
   ``floor/`` journal (or from compacted via an offline oneshot that writes
   a B-local floor journal first — the live unit cannot read ``/data/compacted``).
2. Runtime: ``BBOT_FLOOR_WARM_PATH`` (default that pickle) is loaded into
   ``LiveFloorObserver`` before books start so ``last_floor`` / SMA hist are
   finite ASAP and theta is usable.

No private imports. No collector writes.
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from app.bot.floor_watcher import (
    CLOSE_HISTORY,
    FORMULA_ID,
    SMA12_HISTORY,
    LiveFloorObserver,
    SIDES,
    _finite,
)
from app.bot.paths import floor_warm_pickle_path, resolve_data_root

WARM_SCHEMA = "bbot.floor_warm.v1"
ENV_FLOOR_WARM_PATH = "BBOT_FLOOR_WARM_PATH"


def resolve_floor_warm_path(
    data_root: Path,
    env: Optional[Mapping[str, str]] = None,
) -> Path:
    """``BBOT_FLOOR_WARM_PATH`` or ``{data_root}/state/floor_warm.pkl``."""
    e = env if env is not None else os.environ
    raw = str(e.get(ENV_FLOOR_WARM_PATH) or "").strip()
    if raw:
        return Path(raw)
    return floor_warm_pickle_path(data_root)


def save_floor_warm_pickle(path: Path, payload: Mapping[str, Any]) -> Path:
    """Atomic-ish pickle write under a B path (caller ensures not D)."""
    path = Path(path)
    text = str(path.resolve())
    for bad in ("/data/live", "/data/bars", "/data/compacted", "/data/spool"):
        if text == bad or text.startswith(bad + os.sep):
            raise RuntimeError(f"refusing floor warm pickle under D path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    body = dict(payload)
    body.setdefault("schema_version", WARM_SCHEMA)
    body.setdefault("formula_id", FORMULA_ID)
    with tmp.open("wb") as fh:
        pickle.dump(body, fh, protocol=pickle.HIGHEST_PROTOCOL)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)
    return path


def load_floor_warm_pickle(path: Path) -> dict[str, Any]:
    path = Path(path)
    with path.open("rb") as fh:
        payload = pickle.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"floor warm pickle is not a dict: {path}")
    return payload


def apply_warm_pickle_to_observer(
    observer: LiveFloorObserver,
    path: Path,
) -> int:
    """Load pickle and apply; return touched slot count."""
    payload = load_floor_warm_pickle(path)
    return int(observer.apply_warm_state(payload))


def export_observer_warm_pickle(observer: LiveFloorObserver, path: Path) -> Path:
    return save_floor_warm_pickle(path, observer.export_warm_state())


def _iter_floor_journal_rows(data_root: Path) -> list[dict[str, Any]]:
    root = Path(data_root) / "floor"
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("event_date=*/metrics.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                rows.append(rec)
    return rows


def build_warm_state_from_floor_journal(
    data_root: Path,
    *,
    coins: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """Rebuild warm payload from ``{data_root}/floor/**/metrics.jsonl``.

    Uses the trailing ``CLOSE_HISTORY`` closes and ``SMA12_HISTORY`` sma12 tips
    per ``(coin, side)``, plus the latest finite floor.
    """
    allow = {str(c).upper() for c in coins} if coins else None
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in _iter_floor_journal_rows(data_root):
        coin = str(row.get("base_coin") or "").upper()
        side = str(row.get("side") or "")
        if not coin or side not in SIDES:
            continue
        if allow is not None and coin not in allow:
            continue
        by_key[(coin, side)].append(row)

    sides: dict[str, Any] = {}
    last_floors: dict[str, Any] = {}
    for (coin, side), rows in by_key.items():
        rows.sort(key=lambda r: int(r.get("bar_end_ms") or 0))
        closes: list[float] = []
        sma_hist: list[float] = []
        last_floor_row: Optional[dict[str, Any]] = None
        for r in rows:
            c = _finite(r.get("close"))
            if c is not None:
                closes.append(float(c))
            else:
                closes.append(float("nan"))
            s = _finite(r.get("sma12"))
            if s is not None:
                sma_hist.append(float(s))
            else:
                sma_hist.append(float("nan"))
            if _finite(r.get("floor_tf_select_a25")) is not None:
                last_floor_row = r
        closes = closes[-CLOSE_HISTORY:]
        sma_hist = sma_hist[-SMA12_HISTORY:]
        key = f"{coin}|{side}"
        sides[key] = {
            "base_coin": coin,
            "side": side,
            "closes": closes,
            "sma12_hist": sma_hist,
            "bar_start_ms": None,
            "last_close": closes[-1] if closes else None,
            "tick_count": 0,
        }
        if last_floor_row is not None:
            last_floors[key] = {
                "base_coin": coin,
                "side": side,
                "floor_tf_select_a25": float(last_floor_row["floor_tf_select_a25"]),
                "bar_end_ms": int(last_floor_row.get("bar_end_ms") or 0),
                "computed_at_ms": int(last_floor_row.get("computed_at_ms") or 0),
            }
    return {
        "schema_version": WARM_SCHEMA,
        "formula_id": FORMULA_ID,
        "source": "floor_journal",
        "sides": sides,
        "last_floors": last_floors,
    }


def build_warm_pickle_from_journal(
    journal_root: Path,
    out_path: Path,
    *,
    coins: Optional[Sequence[str]] = None,
) -> Path:
    payload = build_warm_state_from_floor_journal(journal_root, coins=coins)
    return save_floor_warm_pickle(out_path, payload)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build / inspect B-bot floor warm pickle (never D trees)."
    )
    parser.add_argument(
        "--from-journal",
        type=Path,
        help="BBOT data root containing floor/event_date=*/metrics.jsonl",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Output pickle path (default: {BBOT_DATA_ROOT}/state/floor_warm.pkl)",
    )
    parser.add_argument(
        "--coins",
        type=str,
        default="",
        help="Optional CSV coin filter",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="BBOT_DATA_ROOT for default --out",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    coins = [c.strip().upper() for c in str(args.coins).split(",") if c.strip()]
    data_root = Path(args.data_root) if args.data_root else resolve_data_root()
    out = Path(args.out) if args.out else resolve_floor_warm_path(data_root)
    if args.from_journal is None:
        parser.error("require --from-journal <bbot-data-root>")
    path = build_warm_pickle_from_journal(
        Path(args.from_journal),
        out,
        coins=coins or None,
    )
    payload = load_floor_warm_pickle(path)
    n_sides = len(payload.get("sides") or {})
    n_floors = len(payload.get("last_floors") or {})
    print(f"wrote {path} sides={n_sides} last_floors={n_floors}")
    print(
        "Compacted recipe: run an offline floor oneshot against a *readable copy* "
        "of compacted (not from the live unit — InaccessiblePaths blocks "
        "/data/compacted), write floor metrics under a B data root, then "
        "--from-journal that root."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
