"""DuckDB catalog over local lean_ticks parquet filenames."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import duckdb

from research.lean_ticks_io import parse_lean_file_window
from viz.config import DEFAULT_CATALOG, DEFAULT_TICKS, FIVE_MIN_MS

SPREAD_NAME_RE = re.compile(
    r"^spread_(\d{8}T\d{6}Z)_(\d{8}T\d{6}Z)\.parquet$"
)


def _ms_to_iso(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def connect(catalog_path: Path = DEFAULT_CATALOG) -> duckdb.DuckDBPyConnection:
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(catalog_path))
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS lean_files (
            name VARCHAR PRIMARY KEY,
            path VARCHAR NOT NULL,
            start_ms BIGINT NOT NULL,
            end_ms BIGINT NOT NULL,
            size_bytes BIGINT NOT NULL,
            mtime_ns BIGINT NOT NULL
        )
        """
    )
    return con


def rebuild_catalog(
    ticks_dir: Path = DEFAULT_TICKS,
    catalog_path: Path = DEFAULT_CATALOG,
) -> dict[str, Any]:
    """Rescan lean_ticks filenames into DuckDB (cheap: no parquet body read)."""
    ticks_dir = ticks_dir.resolve()
    if not ticks_dir.is_dir():
        raise FileNotFoundError(f"lean ticks dir missing: {ticks_dir}")

    rows: list[tuple] = []
    skipped = 0
    for path in sorted(ticks_dir.glob("spread_*.parquet")):
        win = parse_lean_file_window(path)
        if win is None:
            skipped += 1
            continue
        st = path.stat()
        rows.append(
            (
                path.name,
                str(path),
                int(win[0]),
                int(win[1]),
                int(st.st_size),
                int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))),
            )
        )

    con = connect(catalog_path)
    try:
        con.execute("DELETE FROM lean_files")
        if rows:
            con.executemany(
                "INSERT INTO lean_files VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        n = con.execute("SELECT COUNT(*) FROM lean_files").fetchone()[0]
        bounds = con.execute(
            "SELECT MIN(start_ms), MAX(end_ms) FROM lean_files"
        ).fetchone()
    finally:
        con.close()

    return {
        "ok": True,
        "n_files": int(n),
        "skipped": int(skipped),
        "start": _ms_to_iso(int(bounds[0])) if bounds and bounds[0] is not None else None,
        "end": _ms_to_iso(int(bounds[1])) if bounds and bounds[1] is not None else None,
        "catalog": str(catalog_path.resolve()),
        "ticks": str(ticks_dir),
    }


def catalog_bounds(catalog_path: Path = DEFAULT_CATALOG) -> Optional[tuple[int, int]]:
    con = connect(catalog_path)
    try:
        row = con.execute(
            "SELECT MIN(start_ms), MAX(end_ms) FROM lean_files"
        ).fetchone()
    finally:
        con.close()
    if not row or row[0] is None:
        return None
    return int(row[0]), int(row[1])


def list_file_windows(
    catalog_path: Path = DEFAULT_CATALOG,
    *,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> list[dict[str, Any]]:
    con = connect(catalog_path)
    try:
        q = "SELECT name, start_ms, end_ms, size_bytes FROM lean_files"
        params: list[Any] = []
        clauses: list[str] = []
        if start_ms is not None and end_ms is not None:
            clauses.append("start_ms < ? AND end_ms > ?")
            params.extend([int(end_ms), int(start_ms)])
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY start_ms"
        rows = con.execute(q, params).fetchall()
    finally:
        con.close()
    return [
        {
            "name": r[0],
            "start_ms": int(r[1]),
            "end_ms": int(r[2]),
            "start": _ms_to_iso(int(r[1])),
            "end": _ms_to_iso(int(r[2])),
            "size_bytes": int(r[3]),
        }
        for r in rows
    ]


def coverage_holes(
    catalog_path: Path = DEFAULT_CATALOG,
    *,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    slot_ms: int = FIVE_MIN_MS,
) -> dict[str, Any]:
    """Honest 5-minute calendar holes from file windows (not tick emptiness)."""
    con = connect(catalog_path)
    try:
        bounds = con.execute(
            "SELECT MIN(start_ms), MAX(end_ms) FROM lean_files"
        ).fetchone()
        if not bounds or bounds[0] is None:
            return {
                "ok": True,
                "n_files": 0,
                "n_slots_expected": 0,
                "n_slots_present": 0,
                "n_holes": 0,
                "holes": [],
                "span_start": None,
                "span_end": None,
            }
        span_s = int(start_ms) if start_ms is not None else int(bounds[0])
        span_e = int(end_ms) if end_ms is not None else int(bounds[1])
        files = con.execute(
            """
            SELECT start_ms, end_ms FROM lean_files
            WHERE start_ms < ? AND end_ms > ?
            ORDER BY start_ms
            """,
            [span_e, span_s],
        ).fetchall()
        n_files = con.execute("SELECT COUNT(*) FROM lean_files").fetchone()[0]
    finally:
        con.close()

    present: set[int] = set()
    for a, b in files:
        t = int(a)
        while t < int(b):
            if span_s <= t < span_e:
                present.add(t)
            t += int(slot_ms)

    # Align span to slot grid
    aligned_s = span_s - (span_s % slot_ms)
    if aligned_s < span_s:
        aligned_s += slot_ms
    expected = list(range(aligned_s, span_e, slot_ms))
    missing = [t for t in expected if t not in present]

    holes: list[dict[str, Any]] = []
    if missing:
        run_start = missing[0]
        prev = missing[0]
        for t in missing[1:]:
            if t == prev + slot_ms:
                prev = t
                continue
            holes.append(
                {
                    "start": _ms_to_iso(run_start),
                    "end": _ms_to_iso(prev + slot_ms),
                    "n_slots": int((prev - run_start) // slot_ms) + 1,
                    "duration_min": int(
                        ((prev - run_start) // slot_ms + 1) * slot_ms // 60_000
                    ),
                }
            )
            run_start = t
            prev = t
        holes.append(
            {
                "start": _ms_to_iso(run_start),
                "end": _ms_to_iso(prev + slot_ms),
                "n_slots": int((prev - run_start) // slot_ms) + 1,
                "duration_min": int(
                    ((prev - run_start) // slot_ms + 1) * slot_ms // 60_000
                ),
            }
        )

    return {
        "ok": True,
        "n_files": int(n_files),
        "n_slots_expected": len(expected),
        "n_slots_present": len(present),
        "n_holes": len(holes),
        "holes": holes[:200],  # cap payload
        "holes_truncated": len(holes) > 200,
        "span_start": _ms_to_iso(span_s),
        "span_end": _ms_to_iso(span_e),
        "slot_min": int(slot_ms // 60_000),
    }


def distinct_coins_sample(
    ticks_dir: Path,
    catalog_path: Path = DEFAULT_CATALOG,
    *,
    sample_files: int = 24,
) -> list[str]:
    """Infer coin universe from a sample of recent parquet files via DuckDB."""
    windows = list_file_windows(catalog_path)
    if not windows:
        return []
    # Prefer recent files (more complete universe)
    sample = windows[-sample_files:] if len(windows) > sample_files else windows
    paths = [str(ticks_dir / w["name"]) for w in sample]
    existing = [p for p in paths if Path(p).is_file()]
    if not existing:
        return []

    con = duckdb.connect()
    try:
        # DuckDB read_parquet accepts a VARCHAR[] of paths
        coins = con.execute(
            """
            SELECT DISTINCT upper(cast(base_coin AS VARCHAR)) AS c
            FROM read_parquet(?)
            WHERE base_coin IS NOT NULL
            ORDER BY 1
            """,
            [existing],
        ).fetchall()
    finally:
        con.close()
    return [str(r[0]) for r in coins if r[0]]
