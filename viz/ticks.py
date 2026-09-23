"""Load lean ticks for viz API (all points; refuse over cap; no silent downsample)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pyarrow as pa

from research.gear2_coin_overview_html import WindowTooWideError
from research.lean_ticks_io import (
    gear2_lean_columns,
    iter_lean_tables,
    prepare_lean_ticks,
)
from viz.config import DEFAULT_TICKS, GAP_BREAK_MS, MAX_ALL_TICK_POINTS

OV_COLS = gear2_lean_columns(check_volume=False)


def ms_to_iso_z(ms: int) -> str:
    return (
        datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def xy_with_gaps(
    ts_ms: np.ndarray,
    y: np.ndarray,
    gap_ms: int = GAP_BREAK_MS,
) -> tuple[list[Optional[str]], list[Optional[float]]]:
    """Insert nulls on time gaps so Plotly breaks the line (honest holes)."""
    if len(ts_ms) == 0:
        return [], []
    xs: list[Optional[str]] = []
    ys: list[Optional[float]] = []
    prev = int(ts_ms[0])
    for i in range(len(ts_ms)):
        t = int(ts_ms[i])
        if i > 0 and (t - prev) > int(gap_ms):
            xs.append(None)
            ys.append(None)
        xs.append(ms_to_iso_z(t))
        v = y[i]
        ys.append(None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v))
        prev = t
    return xs, ys


def load_coin_ticks(
    ticks_dir: Path,
    coin: str,
    start_ms: int,
    end_ms: int,
    *,
    workers: int = 8,
    max_points: int = MAX_ALL_TICK_POINTS,
) -> dict[str, Any]:
    """Return series payload or raise WindowTooWideError / ValueError."""
    if end_ms <= start_ms:
        raise ValueError("END must be after START")
    u = str(coin).strip().upper()
    if not u:
        raise ValueError("empty coin")

    tables = []
    used: list[Path] = []
    n_raw = 0
    try:
        stream = iter_lean_tables(
            ticks_dir,
            int(start_ms),
            int(end_ms),
            coins={u},
            workers=int(workers),
            columns=list(OV_COLS),
            chunk=16,
        )
        for path, table in stream:
            n_raw += int(table.num_rows)
            if n_raw > int(max_points):
                raise WindowTooWideError(n_raw, max_points)
            tables.append(table)
            used.append(path)
    except FileNotFoundError as exc:
        raise ValueError(str(exc)) from exc

    if not tables:
        return {
            "ok": True,
            "coin": u,
            "start": ms_to_iso_z(start_ms),
            "end": ms_to_iso_z(end_ms),
            "n": 0,
            "n_files": 0,
            "max_points": int(max_points),
            "t": [],
            "spread_long": [],
            "spread_short": [],
            "bybit_mid": [],
        }

    raw = pa.concat_tables(tables, promote_options="permissive").to_pandas()
    del tables
    df = prepare_lean_ticks(raw, copy=False)
    del raw
    n = len(df)
    if n > int(max_points):
        raise WindowTooWideError(n, max_points)

    ts = df["event_local_ts_ms"].to_numpy(dtype=np.int64)
    sl = df["spread_long"].to_numpy(dtype=float)
    ss = df["spread_short"].to_numpy(dtype=float)
    mid = (
        (
            df["bybit_bid_price"].to_numpy(dtype=float)
            + df["bybit_ask_price"].to_numpy(dtype=float)
        )
        * 0.5
    )

    t_iso, y_long = xy_with_gaps(ts, sl)
    _, y_short = xy_with_gaps(ts, ss)
    _, y_mid = xy_with_gaps(ts, mid)

    return {
        "ok": True,
        "coin": u,
        "start": ms_to_iso_z(start_ms),
        "end": ms_to_iso_z(end_ms),
        "n": int(n),
        "n_files": len(used),
        "max_points": int(max_points),
        "gap_break_ms": int(GAP_BREAK_MS),
        "t": t_iso,
        "spread_long": y_long,
        "spread_short": y_short,
        "bybit_mid": y_mid,
    }
