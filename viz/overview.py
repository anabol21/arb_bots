"""Full-span downsampled overview + all-tick stats (index.html style)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pyarrow as pa

from research.gear2_spread_plots import (
    CoinAllTickStats,
    day_counts_from_counter,
    update_stats_from_table,
)
from research.is_crypto import is_crypto
from research.lean_ticks_io import (
    _even_take_per_coin,
    gear2_lean_columns,
    iter_lean_tables,
    list_lean_files_overlapping,
    prepare_lean_ticks,
)
from viz import catalog as cat
from viz.config import DEFAULT_CATALOG, DEFAULT_TICKS, GAP_BREAK_MS
from viz.ticks import ms_to_iso_z, xy_with_gaps

OV_COLS = gear2_lean_columns(check_volume=False)
OVERVIEW_MAX_TICKS = 8000
SUMMARY_PATH = Path(__file__).resolve().parent / "data" / "overview_summary.json"


def _downsample_keep_ends(ts: np.ndarray, *arrays: np.ndarray, max_points: int):
    n = int(len(ts))
    if n <= max_points or max_points < 2:
        return (ts, *arrays)
    idx = np.linspace(0, n - 1, num=int(max_points), dtype=np.int64)
    idx[0] = 0
    idx[-1] = n - 1
    idx = np.unique(idx)
    return (ts[idx], *(a[idx] for a in arrays))


def _fmt_pcts(pcts: dict) -> str:
    if not pcts:
        return "—"
    parts = []
    for k in (50, 95, 99):
        if k in pcts and pcts[k] is not None:
            parts.append(f"p{k}={float(pcts[k]):.4f}")
    return "  ".join(parts) if parts else "—"


def load_coin_overview(
    ticks_dir: Path,
    coin: str,
    start_ms: int,
    end_ms: int,
    *,
    workers: int = 8,
    max_line: int = OVERVIEW_MAX_TICKS,
) -> dict[str, Any]:
    """Downsampled line + all-tick p50/p95/p99 for one coin over [start, end)."""
    u = str(coin).strip().upper()
    if end_ms <= start_ms:
        raise ValueError("END must be after START")

    files = list_lean_files_overlapping(ticks_dir, start_ms, end_ms)
    if not files:
        return {
            "ok": True,
            "coin": u,
            "n_all": 0,
            "n_line": 0,
            "n_files": 0,
            "start": ms_to_iso_z(start_ms),
            "end": ms_to_iso_z(end_ms),
            "t": [],
            "spread_long": [],
            "spread_short": [],
            "bybit_mid": [],
            "pct_long": {},
            "pct_short": {},
            "days_with_ticks": 0,
            "days_missing": [],
        }

    per_coin_cap = max(2, int(max_line) // max(1, len(files)))
    plot_tables = []
    accs: dict = {}
    used = 0
    for _path, table in iter_lean_tables(
        ticks_dir,
        int(start_ms),
        int(end_ms),
        coins={u},
        workers=int(workers),
        columns=list(OV_COLS),
        chunk=16,
    ):
        used += 1
        update_stats_from_table(accs, table)
        plot_tables.append(_even_take_per_coin(table, per_coin_cap))
        del table

    stats: Optional[CoinAllTickStats] = accs.get(u)
    pct_long = stats.long.percentiles() if stats else {}
    pct_short = stats.short.percentiles() if stats else {}
    n_all = int(stats.n_ticks) if stats else 0

    day_start = ms_to_iso_z(start_ms)[:10]
    day_end = ms_to_iso_z(max(start_ms, end_ms - 1))[:10]
    days_with = 0
    days_missing: list[str] = []
    if stats is not None:
        _counts, days_missing = day_counts_from_counter(
            stats.day_counts, day_start, day_end
        )
        days_with = int(len(_counts))

    if not plot_tables:
        return {
            "ok": True,
            "coin": u,
            "n_all": n_all,
            "n_line": 0,
            "n_files": used,
            "start": ms_to_iso_z(start_ms),
            "end": ms_to_iso_z(end_ms),
            "t": [],
            "spread_long": [],
            "spread_short": [],
            "bybit_mid": [],
            "pct_long": {str(k): float(v) for k, v in pct_long.items()},
            "pct_short": {str(k): float(v) for k, v in pct_short.items()},
            "days_with_ticks": days_with,
            "days_missing": days_missing,
            "line_note": f"прорежено ≤{max_line} точек (cap/file={per_coin_cap})",
        }

    raw = pa.concat_tables(plot_tables, promote_options="permissive").to_pandas()
    del plot_tables
    df = prepare_lean_ticks(raw, copy=False)
    del raw
    df = df.sort_values("event_local_ts_ms")
    ts = df["event_local_ts_ms"].to_numpy(dtype=np.int64)
    sl = df["spread_long"].to_numpy(dtype=float)
    ss = df["spread_short"].to_numpy(dtype=float)
    mid = (
        df["bybit_bid_price"].to_numpy(dtype=float)
        + df["bybit_ask_price"].to_numpy(dtype=float)
    ) * 0.5
    ts, sl, ss, mid = _downsample_keep_ends(ts, sl, ss, mid, max_points=int(max_line))

    t_iso, y_long = xy_with_gaps(ts, sl, GAP_BREAK_MS)
    _, y_short = xy_with_gaps(ts, ss, GAP_BREAK_MS)
    _, y_mid = xy_with_gaps(ts, mid, GAP_BREAK_MS)

    return {
        "ok": True,
        "coin": u,
        "n_all": n_all,
        "n_line": int(len(ts)),
        "n_files": used,
        "start": ms_to_iso_z(start_ms),
        "end": ms_to_iso_z(end_ms),
        "t": t_iso,
        "spread_long": y_long,
        "spread_short": y_short,
        "bybit_mid": y_mid,
        "pct_long": {str(k): float(v) for k, v in pct_long.items()},
        "pct_short": {str(k): float(v) for k, v in pct_short.items()},
        "days_with_ticks": days_with,
        "days_missing": days_missing[:60],
        "line_note": (
            f"линия прорежена ≤{max_line} (cap/file={per_coin_cap}); "
            f"p50/p95/p99 по всем тикам n={n_all}"
        ),
    }


def load_summary(path: Path = SUMMARY_PATH) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build_summary(
    ticks_dir: Path = DEFAULT_TICKS,
    catalog_path: Path = DEFAULT_CATALOG,
    *,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    workers: int = 8,
    max_line: int = OVERVIEW_MAX_TICKS,
    out_path: Path = SUMMARY_PATH,
) -> dict[str, Any]:
    """One-pass all-coin scan → index-like summary rows (slow; run offline)."""
    bounds = cat.catalog_bounds(catalog_path)
    if bounds is None:
        cat.rebuild_catalog(ticks_dir, catalog_path)
        bounds = cat.catalog_bounds(catalog_path)
    if bounds is None:
        raise FileNotFoundError("empty lean catalog")
    span_s = int(start_ms) if start_ms is not None else int(bounds[0])
    span_e = int(end_ms) if end_ms is not None else int(bounds[1])

    files = list_lean_files_overlapping(ticks_dir, span_s, span_e)
    per_coin_cap = max(2, int(max_line) // max(1, len(files)))
    print(
        f"overview build: files={len(files)} span={ms_to_iso_z(span_s)}→{ms_to_iso_z(span_e)} "
        f"per_file_per_coin_cap={per_coin_cap}",
        flush=True,
    )

    t0 = time.time()
    plot_tables = []
    accs: dict = {}
    n_seen = 0
    for _path, table in iter_lean_tables(
        ticks_dir,
        span_s,
        span_e,
        workers=int(workers),
        columns=list(OV_COLS),
        chunk=16,
    ):
        update_stats_from_table(accs, table)
        plot_tables.append(_even_take_per_coin(table, per_coin_cap))
        n_seen += 1
        del table

    line_counts: dict[str, int] = {}
    if plot_tables:
        raw = pa.concat_tables(plot_tables, promote_options="permissive").to_pandas()
        del plot_tables
        df = prepare_lean_ticks(raw, copy=False)
        del raw
        for coin, g in df.groupby(df["base_coin"].str.upper(), sort=False):
            n = len(g)
            if n > max_line:
                n = int(max_line)
            line_counts[str(coin)] = n
        del df
    else:
        del plot_tables

    day_start = ms_to_iso_z(span_s)[:10]
    day_end = ms_to_iso_z(max(span_s, span_e - 1))[:10]
    rows = []
    for coin, st in sorted(accs.items(), key=lambda kv: kv[0]):
        pct_l = st.long.percentiles()
        pct_s = st.short.percentiles()
        counts, missing = day_counts_from_counter(st.day_counts, day_start, day_end)
        klass = "крипто" if is_crypto(coin) else "не крипто"
        rows.append(
            {
                "coin": coin,
                "klass": klass,
                "n_all": int(st.n_ticks),
                "n_line": int(line_counts.get(coin, 0)),
                "days_with_ticks": int(len(counts)),
                "days_missing": "—" if not missing else ",".join(missing[:8])
                + ("…" if len(missing) > 8 else ""),
                "n_days_missing": len(missing),
                "long_pct": _fmt_pcts(pct_l),
                "short_pct": _fmt_pcts(pct_s),
                "pct_long": {str(k): float(v) for k, v in pct_l.items()},
                "pct_short": {str(k): float(v) for k, v in pct_s.items()},
            }
        )

    payload = {
        "ok": True,
        "built_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "start": ms_to_iso_z(span_s),
        "end": ms_to_iso_z(span_e),
        "n_files": len(files),
        "n_coins": len(rows),
        "elapsed_s": round(time.time() - t0, 1),
        "max_line": int(max_line),
        "per_file_per_coin_cap": int(per_coin_cap),
        "rows": rows,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload), encoding="utf-8")
    print(f"wrote {out_path} coins={len(rows)} in {payload['elapsed_s']}s", flush=True)
    return payload
