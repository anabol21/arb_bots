"""Hot-add delta contract. Sibling of universe_csv — not a rewrite of take=yes.

Discovery writes only this file (atomic replace). The live universe CSV is
read-only for the sidecar. Collector hot-add reads the delta; it does not
call REST.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Mapping, Optional, Sequence, Union

from .universe_csv import read_universe_dicts

DeltaPath = Union[str, Path]

# Keep lot/tick meta so a later B1 bot seam can fail-closed without D parquet.
DELTA_FIELDNAMES = (
    "base_coin",
    "okx_symbol",
    "bybit_symbol",
    "okx_tick_size",
    "okx_lot_size",
    "okx_min_size",
    "bybit_tick_size",
    "bybit_qty_step",
    "bybit_min_order_qty",
    "bybit_min_notional_value",
    "discovered_at_utc",
)

REQUIRED_DELTA_FIELDS = ("base_coin", "okx_symbol", "bybit_symbol")
DROP_FIELDNAMES = ("base_coin",)

FORBIDDEN_DELTA_PREFIXES = (
    "/data/live",
    "/data/bars",
    "/data/compacted",
    "/data/spool",
)


class DeltaPathError(ValueError):
    """Delta path would rewrite the live universe or a D parquet tree."""


def _as_path(path: DeltaPath) -> Path:
    return Path(path)


def assert_delta_path_safe(delta_path: DeltaPath, universe_path: DeltaPath) -> Path:
    """Refuse universe rewrite and D hive paths. Fail loud."""
    delta = _as_path(delta_path).expanduser()
    universe = _as_path(universe_path).expanduser()
    try:
        delta_res = delta.resolve()
    except OSError:
        delta_res = delta.absolute()
    try:
        universe_res = universe.resolve()
    except OSError:
        universe_res = universe.absolute()
    if delta_res == universe_res:
        raise DeltaPathError(
            f"delta path must not be the universe CSV ({universe_res}); "
            "discovery must not rewrite live take=yes rows"
        )
    text = str(delta_res)
    for prefix in FORBIDDEN_DELTA_PREFIXES:
        if text == prefix or text.startswith(prefix + os.sep):
            raise DeltaPathError(
                f"refusing to write delta under {prefix}: {delta_res}"
            )
    return delta


def csv_base_coins(path: DeltaPath) -> set[str]:
    """Every base_coin in the given universe CSV (any take value).

    Diff the path you are given. Local Desktop CSV is not a substitute for
    the VPS staging CSV.
    """
    coins: set[str] = set()
    for row in read_universe_dicts(path):
        coin = str(row.get("base_coin", "")).strip()
        if coin:
            coins.add(coin)
    return coins


def _require_row_fields(row: Mapping[str, object], *, where: str) -> dict[str, str]:
    missing = [name for name in REQUIRED_DELTA_FIELDS if not str(row.get(name, "")).strip()]
    if missing:
        raise ValueError(f"{where}: missing required fields {missing}")
    out: dict[str, str] = {}
    for name in DELTA_FIELDNAMES:
        out[name] = str(row.get(name, "")).strip()
    return out


def normalize_delta_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    where: str = "delta",
) -> list[dict[str, str]]:
    """Validate, drop duplicate base_coin (first wins after sort)."""
    normalized: list[dict[str, str]] = []
    for index, row in enumerate(rows):
        normalized.append(_require_row_fields(row, where=f"{where}[{index}]"))
    normalized.sort(key=lambda r: (r["base_coin"], r["okx_symbol"], r["bybit_symbol"]))
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for row in normalized:
        coin = row["base_coin"]
        if coin in seen:
            continue
        seen.add(coin)
        unique.append(row)
    return unique


def diff_intersection_against_csv(
    intersection_rows: Sequence[Mapping[str, object]],
    csv_coins: set[str],
) -> list[dict[str, str]]:
    """Coins in REST intersection that are absent from the given CSV."""
    csv_upper = {coin.upper() for coin in csv_coins}
    fresh: list[dict[str, str]] = []
    for row in normalize_delta_rows(intersection_rows, where="intersection"):
        if row["base_coin"].upper() in csv_upper:
            continue
        fresh.append(row)
    return fresh


def apply_hard_cap(
    rows: Sequence[Mapping[str, str]],
    max_new: int,
) -> tuple[list[dict[str, str]], int]:
    """Keep at most max_new rows (already sorted). Returns (kept, dropped)."""
    if max_new < 0:
        raise ValueError(f"max_new must be >= 0, got {max_new}")
    material = [dict(row) for row in rows]
    if len(material) <= max_new:
        return material, 0
    return material[:max_new], len(material) - max_new


def read_drop_coins(path: DeltaPath) -> list[str]:
    """Read a drop snapshot (base_coin column). Empty/missing file → empty list."""
    csv_path = _as_path(path)
    if not csv_path.exists():
        return []
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise ValueError(f"drop file has no header: {csv_path}")
        if "base_coin" not in reader.fieldnames:
            raise ValueError(
                f"drop file missing required column 'base_coin': {csv_path}"
            )
        coins: list[str] = []
        seen: set[str] = set()
        for row in reader:
            coin = str(row.get("base_coin", "")).strip()
            if not coin or coin in seen:
                continue
            seen.add(coin)
            coins.append(coin)
        return coins


def write_drop_atomic(path: DeltaPath, coins: Sequence[str]) -> Path:
    """Atomic replace of the drop snapshot (base_coin rows only)."""
    drop_path = _as_path(path)
    text = str(drop_path.expanduser().absolute())
    for prefix in FORBIDDEN_DELTA_PREFIXES:
        if text == prefix or text.startswith(prefix + os.sep):
            raise DeltaPathError(f"refusing to write drop list under {prefix}: {drop_path}")
    drop_path.parent.mkdir(parents=True, exist_ok=True)
    unique = []
    seen: set[str] = set()
    for raw in coins:
        coin = str(raw).strip()
        if not coin or coin in seen:
            continue
        seen.add(coin)
        unique.append(coin)
    tmp_path = drop_path.with_name(drop_path.name + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(DROP_FIELDNAMES))
            writer.writeheader()
            for coin in unique:
                writer.writerow({"base_coin": coin})
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, drop_path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return drop_path


def read_delta_rows(path: DeltaPath) -> list[dict[str, str]]:
    """Read a delta snapshot. Empty/missing file → empty list (caller logs)."""
    csv_path = _as_path(path)
    if not csv_path.exists():
        return []
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise ValueError(f"delta file has no header: {csv_path}")
        names = list(reader.fieldnames)
        for required in REQUIRED_DELTA_FIELDS:
            if required not in names:
                raise ValueError(
                    f"delta file missing required column {required!r}: {csv_path}"
                )
        return normalize_delta_rows(list(reader), where=str(csv_path))


def write_delta_atomic(
    path: DeltaPath,
    rows: Sequence[Mapping[str, str]],
    *,
    universe_path: Optional[DeltaPath] = None,
) -> Path:
    """Atomic replace of the delta snapshot. Never opens the universe CSV for write."""
    delta_path = _as_path(path)
    if universe_path is not None:
        assert_delta_path_safe(delta_path, universe_path)
    else:
        text = str(delta_path.expanduser().absolute())
        for prefix in FORBIDDEN_DELTA_PREFIXES:
            if text == prefix or text.startswith(prefix + os.sep):
                raise DeltaPathError(
                    f"refusing to write delta under {prefix}: {delta_path}"
                )
    delta_path.parent.mkdir(parents=True, exist_ok=True)
    payload = normalize_delta_rows(rows, where=str(delta_path))
    tmp_path = delta_path.with_name(delta_path.name + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=list(DELTA_FIELDNAMES),
                extrasaction="ignore",
            )
            writer.writeheader()
            writer.writerows(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, delta_path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return delta_path
