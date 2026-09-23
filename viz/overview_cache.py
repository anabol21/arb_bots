"""Incremental overview cache: process each lean file at most once.

State under ``viz/data/overview_inc/``:
  - meta.duckdb: processed file names
  - stats/{COIN}.npz: serialized CoinAllTickStats
  - samples/{COIN}.npz: accumulated downsampled points (ts, long, short, mid)
  - coin_pages/{COIN}.json: ready API payloads for full-span coin overview

Default build is incremental. Use ``--full`` only to wipe and rescan everything.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from research.gear2_spread_plots import (
    CoinAllTickStats,
    day_counts_from_counter,
    update_stats_from_table,
)
from research.is_crypto import is_crypto
from research.lean_ticks_io import (
    _even_take_per_coin,
    gear2_lean_columns,
    list_lean_files_overlapping,
    parse_lean_file_window,
    prepare_lean_ticks,
)
from viz import catalog as cat
from viz.config import DEFAULT_CATALOG, DEFAULT_TICKS, GAP_BREAK_MS
from viz.overview import (
    OVERVIEW_MAX_TICKS,
    SUMMARY_PATH,
    _downsample_keep_ends,
    _fmt_pcts,
)
from viz.ticks import ms_to_iso_z, xy_with_gaps

OV_COLS = gear2_lean_columns(check_volume=False)
INC_DIR = Path(__file__).resolve().parent / "data" / "overview_inc"
STATS_DIR = INC_DIR / "stats"
SAMPLES_DIR = INC_DIR / "samples"
PAGES_DIR = INC_DIR / "coin_pages"
META_DB = INC_DIR / "meta.duckdb"


def _ensure_dirs() -> None:
    INC_DIR.mkdir(parents=True, exist_ok=True)
    STATS_DIR.mkdir(parents=True, exist_ok=True)
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    PAGES_DIR.mkdir(parents=True, exist_ok=True)


def _connect_meta() -> duckdb.DuckDBPyConnection:
    _ensure_dirs()
    con = duckdb.connect(str(META_DB))
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS processed (
            name VARCHAR PRIMARY KEY,
            size_bytes BIGINT,
            mtime_ns BIGINT,
            processed_at VARCHAR
        )
        """
    )
    return con


def list_processed() -> set[str]:
    con = _connect_meta()
    try:
        rows = con.execute("SELECT name FROM processed").fetchall()
    finally:
        con.close()
    return {r[0] for r in rows}


def mark_processed(paths: list[Path]) -> None:
    if not paths:
        return
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for p in paths:
        st = p.stat()
        rows.append(
            (
                p.name,
                int(st.st_size),
                int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))),
                now,
            )
        )
    con = _connect_meta()
    try:
        con.executemany(
            "INSERT OR REPLACE INTO processed VALUES (?, ?, ?, ?)",
            rows,
        )
    finally:
        con.close()


def seed_processed_from_catalog(
    ticks_dir: Path = DEFAULT_TICKS,
    catalog_path: Path = DEFAULT_CATALOG,
) -> int:
    """Mark all current catalog files processed without re-reading bodies.

    Use once after a full summary already exists, so later builds are incremental.
    """
    windows = cat.list_file_windows(catalog_path)
    if not windows:
        cat.rebuild_catalog(ticks_dir, catalog_path)
        windows = cat.list_file_windows(catalog_path)
    paths = [ticks_dir / w["name"] for w in windows if (ticks_dir / w["name"]).is_file()]
    mark_processed(paths)
    return len(paths)


def _stats_path(coin: str) -> Path:
    return STATS_DIR / f"{coin.upper()}.npz"


def _samples_path(coin: str) -> Path:
    return SAMPLES_DIR / f"{coin.upper()}.npz"


def _page_path(coin: str) -> Path:
    return PAGES_DIR / f"{coin.upper()}.json"


def save_stats(coin: str, st: CoinAllTickStats) -> None:
    _ensure_dirs()
    np.savez_compressed(
        _stats_path(coin),
        n_ticks=np.int64(st.n_ticks),
        day_keys=np.array(list(st.day_counts.keys()), dtype=object),
        day_vals=np.array([st.day_counts[k] for k in st.day_counts.keys()], dtype=np.int64),
        long_counts=st.long.counts,
        long_n_below=np.int64(st.long.n_below),
        long_n_above=np.int64(st.long.n_above),
        long_n_finite=np.int64(st.long.n_finite),
        long_lo=np.float64(st.long.lo),
        long_hi=np.float64(st.long.hi),
        long_n_bins=np.int64(st.long.n_bins),
        short_counts=st.short.counts,
        short_n_below=np.int64(st.short.n_below),
        short_n_above=np.int64(st.short.n_above),
        short_n_finite=np.int64(st.short.n_finite),
        short_lo=np.float64(st.short.lo),
        short_hi=np.float64(st.short.hi),
        short_n_bins=np.int64(st.short.n_bins),
    )


def load_stats(coin: str) -> Optional[CoinAllTickStats]:
    path = _stats_path(coin)
    if not path.is_file():
        return None
    z = np.load(path, allow_pickle=True)
    st = CoinAllTickStats()
    st.n_ticks = int(z["n_ticks"])
    keys = z["day_keys"].tolist()
    vals = z["day_vals"].tolist()
    st.day_counts.clear()
    for k, v in zip(keys, vals):
        st.day_counts[str(k)] = int(v)
    for side, prefix in ((st.long, "long"), (st.short, "short")):
        side.lo = float(z[f"{prefix}_lo"])
        side.hi = float(z[f"{prefix}_hi"])
        side.n_bins = int(z[f"{prefix}_n_bins"])
        side.width = (side.hi - side.lo) / side.n_bins
        side.counts = np.array(z[f"{prefix}_counts"], dtype=np.int64, copy=True)
        side.n_below = int(z[f"{prefix}_n_below"])
        side.n_above = int(z[f"{prefix}_n_above"])
        side.n_finite = int(z[f"{prefix}_n_finite"])
    return st


def append_samples(coin: str, ts, sl, ss, mid) -> None:
    _ensure_dirs()
    path = _samples_path(coin)
    chunk = {
        "ts": np.asarray(ts, dtype=np.int64),
        "sl": np.asarray(sl, dtype=np.float64),
        "ss": np.asarray(ss, dtype=np.float64),
        "mid": np.asarray(mid, dtype=np.float64),
    }
    if path.is_file():
        old = np.load(path)
        chunk = {
            k: np.concatenate([old[k], chunk[k]])
            for k in ("ts", "sl", "ss", "mid")
        }
        # keep memory bound: downsample to 4x max line while storing
        if len(chunk["ts"]) > OVERVIEW_MAX_TICKS * 4:
            ts2, sl2, ss2, mid2 = _downsample_keep_ends(
                chunk["ts"],
                chunk["sl"],
                chunk["ss"],
                chunk["mid"],
                max_points=OVERVIEW_MAX_TICKS * 4,
            )
            chunk = {"ts": ts2, "sl": sl2, "ss": ss2, "mid": mid2}
    np.savez_compressed(path, **chunk)


def load_samples(coin: str) -> Optional[dict[str, np.ndarray]]:
    path = _samples_path(coin)
    if not path.is_file():
        return None
    z = np.load(path)
    return {k: z[k] for k in ("ts", "sl", "ss", "mid")}


def payload_from_summary(
    coin: str,
    start_ms: int,
    end_ms: int,
    *,
    summary_path: Path = SUMMARY_PATH,
) -> Optional[dict[str, Any]]:
    """Instant stats from overview_summary.json (no parquet scan, no line)."""
    from viz.overview import load_summary as _load_summary

    payload = _load_summary(summary_path)
    if not payload or not payload.get("rows"):
        return None
    u = coin.upper()
    row = next((r for r in payload["rows"] if str(r.get("coin", "")).upper() == u), None)
    if row is None:
        return None
    return {
        "ok": True,
        "coin": u,
        "n_all": int(row.get("n_all") or 0),
        "n_line": 0,
        "n_files": int(payload.get("n_files") or 0),
        "start": ms_to_iso_z(start_ms),
        "end": ms_to_iso_z(end_ms),
        "t": [],
        "spread_long": [],
        "spread_short": [],
        "bybit_mid": [],
        "pct_long": row.get("pct_long") or {},
        "pct_short": row.get("pct_short") or {},
        "days_with_ticks": int(row.get("days_with_ticks") or 0),
        "days_missing": [],
        "line_note": (
            "статы из overview_summary.json (мгновенно). "
            "Линия overview не в кэше — нажмите «Построить линию»."
        ),
        "cached": True,
        "source": "summary_stats_only",
        "has_line": False,
    }


def payload_from_store(
    coin: str,
    start_ms: int,
    end_ms: int,
    *,
    max_line: int = OVERVIEW_MAX_TICKS,
) -> Optional[dict[str, Any]]:
    """Build coin overview from incremental store (no parquet re-read)."""
    u = coin.upper()
    st = load_stats(u)
    samples = load_samples(u)
    if st is None and samples is None:
        return None

    pct_long = st.long.percentiles() if st else {}
    pct_short = st.short.percentiles() if st else {}
    n_all = int(st.n_ticks) if st else 0
    day_start = ms_to_iso_z(start_ms)[:10]
    day_end = ms_to_iso_z(max(start_ms, end_ms - 1))[:10]
    days_with = 0
    days_missing: list[str] = []
    if st is not None:
        counts, days_missing = day_counts_from_counter(
            st.day_counts, day_start, day_end
        )
        days_with = int(len(counts))

    t_iso: list = []
    y_long: list = []
    y_short: list = []
    y_mid: list = []
    n_line = 0
    if samples is not None and len(samples["ts"]):
        ts = samples["ts"]
        mask = (ts >= start_ms) & (ts < end_ms)
        ts = ts[mask]
        sl = samples["sl"][mask]
        ss = samples["ss"][mask]
        mid = samples["mid"][mask]
        if len(ts):
            order = np.argsort(ts)
            ts, sl, ss, mid = ts[order], sl[order], ss[order], mid[order]
            ts, sl, ss, mid = _downsample_keep_ends(
                ts, sl, ss, mid, max_points=int(max_line)
            )
            t_iso, y_long = xy_with_gaps(ts, sl, GAP_BREAK_MS)
            _, y_short = xy_with_gaps(ts, ss, GAP_BREAK_MS)
            _, y_mid = xy_with_gaps(ts, mid, GAP_BREAK_MS)
            n_line = int(len(ts))

    return {
        "ok": True,
        "coin": u,
        "n_all": n_all,
        "n_line": n_line,
        "n_files": -1,
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
            f"из инкрементального кэша; линия ≤{max_line}; "
            f"p50/p95/p99 по всем учтённым тикам n={n_all}"
        ),
        "cached": True,
        "source": "incremental_store",
    }


def write_coin_page(coin: str, payload: dict[str, Any]) -> None:
    _ensure_dirs()
    _page_path(coin).write_text(json.dumps(payload), encoding="utf-8")


def read_coin_page(coin: str) -> Optional[dict[str, Any]]:
    path = _page_path(coin)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def rebuild_summary_from_store(
    *,
    start_ms: int,
    end_ms: int,
    n_files_catalog: int,
    max_line: int = OVERVIEW_MAX_TICKS,
    out_path: Path = SUMMARY_PATH,
) -> dict[str, Any]:
    """Fast summary from stats/*.npz — no lean re-read."""
    _ensure_dirs()
    day_start = ms_to_iso_z(start_ms)[:10]
    day_end = ms_to_iso_z(max(start_ms, end_ms - 1))[:10]
    rows = []
    for path in sorted(STATS_DIR.glob("*.npz")):
        coin = path.stem.upper()
        st = load_stats(coin)
        if st is None:
            continue
        pct_l = st.long.percentiles()
        pct_s = st.short.percentiles()
        counts, missing = day_counts_from_counter(st.day_counts, day_start, day_end)
        samples = load_samples(coin)
        n_line = 0
        if samples is not None and len(samples["ts"]):
            n_line = min(int(len(samples["ts"])), int(max_line))
        klass = "крипто" if is_crypto(coin) else "не крипто"
        rows.append(
            {
                "coin": coin,
                "klass": klass,
                "n_all": int(st.n_ticks),
                "n_line": n_line,
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
    rows.sort(key=lambda r: r["coin"])
    payload = {
        "ok": True,
        "built_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "start": ms_to_iso_z(start_ms),
        "end": ms_to_iso_z(end_ms),
        "n_files": int(n_files_catalog),
        "n_coins": len(rows),
        "elapsed_s": 0.0,
        "max_line": int(max_line),
        "per_file_per_coin_cap": None,
        "incremental": True,
        "rows": rows,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _read_one_file(path: Path, start_ms: int, end_ms: int) -> Optional[pa.Table]:
    try:
        names = pq.read_schema(path).names
        cols = [c for c in OV_COLS if c in names]
        if "event_local_ts_ms" not in cols:
            return None
        table = pq.read_table(path, columns=cols)
    except Exception:
        return None
    if table.num_rows == 0:
        return None
    import pyarrow.compute as pc

    ts = table["event_local_ts_ms"]
    ts = pc.cast(pc.floor(pc.cast(ts, pa.float64())), pa.int64())
    keep = pc.and_(pc.greater_equal(ts, start_ms), pc.less(ts, end_ms))
    table = table.filter(keep)
    return table if table.num_rows else None


def process_new_files(
    ticks_dir: Path,
    new_paths: list[Path],
    *,
    start_ms: int,
    end_ms: int,
    per_coin_cap: int,
    workers: int = 4,
) -> list[str]:
    """Update stats+samples for new files only. Returns touched coins."""
    from concurrent.futures import ThreadPoolExecutor

    touched: set[str] = set()
    if not new_paths:
        return []

    def _one(p: Path):
        return p, _read_one_file(p, start_ms, end_ms)

    batch_stats: dict = {}
    sample_acc: dict[str, list] = {}

    n_workers = max(1, int(workers))
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        for i in range(0, len(new_paths), 64):
            batch = new_paths[i : i + 64]
            for path, table in pool.map(_one, batch):
                if table is None:
                    continue
                update_stats_from_table(batch_stats, table)
                taken = _even_take_per_coin(table, per_coin_cap)
                if taken is None or taken.num_rows == 0:
                    continue
                pdf = prepare_lean_ticks(taken.to_pandas(), copy=False)
                for coin, g in pdf.groupby(pdf["base_coin"].str.upper(), sort=False):
                    c = str(coin)
                    touched.add(c)
                    sample_acc.setdefault(c, []).append(g)
            print(
                f"  incremental {min(i + 64, len(new_paths))}/{len(new_paths)}",
                flush=True,
            )

    for coin, st_new in batch_stats.items():
        existing = load_stats(coin)
        if existing is None:
            save_stats(coin, st_new)
        else:
            existing.merge_from(st_new)
            save_stats(coin, existing)
        touched.add(coin)

    for coin, frames in sample_acc.items():
        import pandas as pd

        df = pd.concat(frames, ignore_index=True).sort_values("event_local_ts_ms")
        ts = df["event_local_ts_ms"].to_numpy(dtype=np.int64)
        sl = df["spread_long"].to_numpy(dtype=float)
        ss = df["spread_short"].to_numpy(dtype=float)
        mid = (
            df["bybit_bid_price"].to_numpy(dtype=float)
            + df["bybit_ask_price"].to_numpy(dtype=float)
        ) * 0.5
        append_samples(coin, ts, sl, ss, mid)
        # refresh coin page cache
        page = payload_from_store(coin, start_ms, end_ms)
        if page is not None:
            write_coin_page(coin, page)

    mark_processed(new_paths)
    return sorted(touched)


def build_incremental(
    ticks_dir: Path = DEFAULT_TICKS,
    catalog_path: Path = DEFAULT_CATALOG,
    *,
    workers: int = 8,
    max_line: int = OVERVIEW_MAX_TICKS,
    full: bool = False,
    seed_if_summary: bool = True,
) -> dict[str, Any]:
    """Default entry: process only missing files, refresh summary.json."""
    t0 = time.time()
    _ensure_dirs()
    if full:
        # wipe incremental state
        import shutil

        if INC_DIR.is_dir():
            shutil.rmtree(INC_DIR)
        _ensure_dirs()

    bounds = cat.catalog_bounds(catalog_path)
    if bounds is None:
        cat.rebuild_catalog(ticks_dir, catalog_path)
        bounds = cat.catalog_bounds(catalog_path)
    if bounds is None:
        raise FileNotFoundError("empty lean catalog")
    span_s, span_e = bounds

    windows = cat.list_file_windows(catalog_path)
    all_paths = [
        ticks_dir / w["name"]
        for w in windows
        if (ticks_dir / w["name"]).is_file()
    ]

    processed = list_processed()
    if not processed and seed_if_summary and SUMMARY_PATH.is_file() and not full:
        # Trust existing full summary: don't re-read history
        n_seed = seed_processed_from_catalog(ticks_dir, catalog_path)
        print(
            f"seeded processed={n_seed} from catalog (summary already exists); "
            "only future syncs will be scanned",
            flush=True,
        )
        # Also materialize coin pages from a one-time? skip — pages fill on demand/cache miss
        processed = list_processed()
        # Rebuild summary timestamp only
        summary = rebuild_summary_from_store(
            start_ms=span_s,
            end_ms=span_e,
            n_files_catalog=len(all_paths),
            max_line=max_line,
        )
        # If store empty (no stats npz), keep existing SUMMARY_PATH as-is
        if summary["n_coins"] == 0 and SUMMARY_PATH.is_file():
            print(
                "incremental store empty after seed — keeping existing overview_summary.json; "
                "coin pages will cache on first visit",
                flush=True,
            )
            return {
                "ok": True,
                "mode": "seed_only",
                "n_new": 0,
                "n_processed_total": len(processed),
                "elapsed_s": round(time.time() - t0, 1),
            }

    new_paths = [p for p in all_paths if p.name not in processed]
    files = list_lean_files_overlapping(ticks_dir, span_s, span_e)
    per_coin_cap = max(2, int(max_line) // max(1, len(files)))

    print(
        f"incremental: catalog={len(all_paths)} processed={len(processed)} "
        f"new={len(new_paths)} cap/file={per_coin_cap}",
        flush=True,
    )

    touched: list[str] = []
    if new_paths:
        touched = process_new_files(
            ticks_dir,
            new_paths,
            start_ms=span_s,
            end_ms=span_e,
            per_coin_cap=per_coin_cap,
            workers=workers,
        )

    if list(STATS_DIR.glob("*.npz")):
        summary = rebuild_summary_from_store(
            start_ms=span_s,
            end_ms=span_e,
            n_files_catalog=len(all_paths),
            max_line=max_line,
        )
        summary["elapsed_s"] = round(time.time() - t0, 1)
        summary["n_new_files"] = len(new_paths)
        summary["touched_coins"] = touched
        print(
            f"summary refreshed coins={summary['n_coins']} new_files={len(new_paths)} "
            f"in {summary['elapsed_s']}s",
            flush=True,
        )
        return summary

    return {
        "ok": True,
        "mode": "no_stats_yet",
        "n_new": len(new_paths),
        "n_processed_total": len(list_processed()),
        "elapsed_s": round(time.time() - t0, 1),
        "hint": "run with --full once, or visit coins to populate page cache",
    }
