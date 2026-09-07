"""Idempotent parquet / jsonl journal for floor snapshots."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Sequence, Union

import pandas as pd

from research.gear22_floor_watcher.builder import SNAPSHOT_COLUMNS

JOURNAL_KEY_COLS: tuple[str, ...] = ("bar_end_ms", "base_coin", "side")
PathLike = Union[str, Path]


def _ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in SNAPSHOT_COLUMNS:
        if col not in out.columns:
            out[col] = pd.NA
    return out[list(SNAPSHOT_COLUMNS)]


def merge_snapshot_frames(
    existing: Optional[pd.DataFrame],
    incoming: pd.DataFrame,
) -> pd.DataFrame:
    """Overwrite-by-key merge: later ``incoming`` wins on duplicate keys."""
    inc = _ensure_columns(incoming) if not incoming.empty else pd.DataFrame(
        columns=list(SNAPSHOT_COLUMNS)
    )
    if existing is None or existing.empty:
        merged = inc
    else:
        ex = _ensure_columns(existing)
        if inc.empty:
            merged = ex
        else:
            merged = pd.concat([ex, inc], ignore_index=True)
    if merged.empty:
        return pd.DataFrame(columns=list(SNAPSHOT_COLUMNS))
    # Last occurrence wins for the same (bar_end_ms, base_coin, side).
    merged = merged.drop_duplicates(subset=list(JOURNAL_KEY_COLS), keep="last")
    return merged.sort_values(
        ["bar_end_ms", "base_coin", "side"], kind="mergesort"
    ).reset_index(drop=True)


def read_journal(path: PathLike) -> pd.DataFrame:
    """Read parquet or jsonl journal; empty frame if missing."""
    p = Path(path)
    if not p.exists():
        return pd.DataFrame(columns=list(SNAPSHOT_COLUMNS))
    if p.suffix.lower() == ".parquet":
        df = pd.read_parquet(p)
    elif p.suffix.lower() in (".jsonl", ".json"):
        rows: list[dict] = []
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
        df = pd.DataFrame(rows) if rows else pd.DataFrame()
    else:
        raise ValueError(f"unsupported journal suffix: {p.suffix}")
    if df.empty:
        return pd.DataFrame(columns=list(SNAPSHOT_COLUMNS))
    return _ensure_columns(df)


def write_journal(
    path: PathLike,
    df: pd.DataFrame,
    *,
    fmt: Optional[str] = None,
) -> Path:
    """Write snapshot frame to parquet or jsonl (by suffix or ``fmt``)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    out = _ensure_columns(df)
    kind = (fmt or p.suffix.lstrip(".")).lower()
    if kind in ("parquet", ""):
        if p.suffix.lower() != ".parquet":
            p = p.with_suffix(".parquet")
        out.to_parquet(p, index=False)
    elif kind in ("jsonl", "json"):
        if p.suffix.lower() not in (".jsonl", ".json"):
            p = p.with_suffix(".jsonl")
        with p.open("w", encoding="utf-8") as fh:
            for row in out.to_dict(orient="records"):
                # Native JSON: convert numpy / pandas scalars.
                clean = {}
                for k, v in row.items():
                    if v is None or (isinstance(v, float) and pd.isna(v)):
                        clean[k] = None
                    elif hasattr(v, "item"):
                        try:
                            clean[k] = v.item()
                        except Exception:
                            clean[k] = v
                    else:
                        clean[k] = v
                fh.write(json.dumps(clean, ensure_ascii=False) + "\n")
    else:
        raise ValueError(f"unsupported journal format: {kind}")
    return p


def journal_max_bar_end_ms(df: pd.DataFrame) -> Optional[int]:
    """Latest ``bar_end_ms`` in journal, or None if empty."""
    if df is None or df.empty or "bar_end_ms" not in df.columns:
        return None
    vals = pd.to_numeric(df["bar_end_ms"], errors="coerce").dropna()
    if vals.empty:
        return None
    return int(vals.max())


def write_dual(
    out_path: PathLike,
    df: pd.DataFrame,
    *,
    formats: Sequence[str] = ("parquet",),
) -> list[Path]:
    """Write one or more formats next to ``out_path`` stem."""
    base = Path(out_path)
    written: list[Path] = []
    for fmt in formats:
        if fmt == "parquet":
            written.append(write_journal(base.with_suffix(".parquet"), df, fmt="parquet"))
        elif fmt in ("jsonl", "json"):
            written.append(write_journal(base.with_suffix(".jsonl"), df, fmt="jsonl"))
        else:
            raise ValueError(f"unsupported format {fmt!r}")
    return written
