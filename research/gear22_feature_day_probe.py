"""Gear 2.2 backtest feature table: 1 Hz wide (coin, ts_s) parquet.

Observation / replay input — no policy, no simulator gate. Reads local
``output/lean_ticks`` read-only. Schema: ``docs/gear22-backtest-features.md``.

    PYTHONPATH=. ./venv/bin/python research/gear22_feature_day_probe.py \
        --since 2026-08-12T00:00:00Z --until 2026-08-13T00:00:00Z \
        --out-dir output/gear22_backtest_features

    PYTHONPATH=. ./venv/bin/python research/gear22_feature_day_probe.py \
        --plan usable-august --workers 2 --skip-done \
        --out-dir output/gear22_backtest_features
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from research.gear22_quiet_regime_viz.candles import (
    BAR_MS,
    SPREAD_LONG_COL,
    SPREAD_SHORT_COL,
    build_5m_bucket_stats,
    causal_sma,
)
from research.gear22_quiet_regime_viz.floors import (
    TF_SELECT_25_NAME,
    W1_BARS,
    compute_chosen_floor,
)
from research.gear22_quiet_regime_viz.load import (
    _read_parquet_filtered,
    derive_research_series,
    list_compacted_overlapping,
    parse_since_ms,
)
from research.gear22_quiet_regime_viz.quantiles import (
    ROLL_P50_MIN_MASS_FRAC,
    WINDOW_1M_MS,
    eval_grid_ms,
    rolling_tw_window_stats,
)

SIDES = (("long", SPREAD_LONG_COL), ("short", SPREAD_SHORT_COL))
SCHEMA_VERSION = "gear22_bt_features_v1"
FLOOR_WARMUP_MS = 13 * 3_600_000
# 4 legs × 0.00075 × 100 = 0.30 percentage points (same units as spread_*).
OCC_ABOVE_FLOOR_ADD = 0.30
DP50_LAG_S = 60
USABLE_COV_MIN = ROLL_P50_MIN_MASS_FRAC
KEEP = ["event_local_ts_ms", "base_coin", "spread_long", "spread_short"]
DEFAULT_COINS_HTML30 = Path("_floor_canary_coins_html30.txt")
DEFAULT_COINS_FALLBACK = Path("_floor_canary_coins.txt")
UNIVERSE_STD_CSV = Path("research/data/universe_spread_std_august.csv")
PLAN_USABLE_AUGUST = ("2026-08-10T00:00:00Z", "2026-08-27T12:00:00Z")
UINT16_MAX = 65535
OMITTED_COLUMNS = ("L1 sizes", "size_ratio_*")

NAN_RULE = (
    "p50 NaN if <2 finite positive-hold ticks or cov<0.20; "
    "never interpolate or fill NaN with 0; empty-window cov=0 is measured mass"
)


def _utc_ms_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def git_head(repo: Path) -> Optional[str]:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def parse_coins_file(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8")
    coins: list[str] = []
    for part in raw.replace("\n", ",").split(","):
        tok = part.strip().upper()
        if tok:
            coins.append(tok)
    if not coins:
        raise ValueError(f"no coins in {path}")
    return coins


def default_coins_path() -> Path:
    if DEFAULT_COINS_HTML30.is_file():
        return DEFAULT_COINS_HTML30
    if DEFAULT_COINS_FALLBACK.is_file():
        return DEFAULT_COINS_FALLBACK
    raise FileNotFoundError("neither _floor_canary_coins_html30.txt nor fallback")


def cross_check_universe(coins: Sequence[str]) -> list[str]:
    if not UNIVERSE_STD_CSV.is_file():
        print(f"universe csv missing: {UNIVERSE_STD_CSV}", flush=True)
        return []
    std = pd.read_csv(UNIVERSE_STD_CSV)
    have = set(std["base_coin"].astype(str).str.upper())
    missing = [c for c in coins if c not in have]
    if missing:
        print(f"WARNING: coins not in universe csv: {missing}", flush=True)
    else:
        print(f"universe csv: all {len(coins)} coins present", flush=True)
    return missing


def load_slim(root: Path, coins: Sequence[str], since_ms: int, until_ms: int) -> pd.DataFrame:
    """Same selection as ``load_ticks`` but drops derived columns to save RAM.

    Filename windows are only approximate labels (observed content shift up to
    ~40s), so the file list is padded by one slot on each side.
    """
    paths = list_compacted_overlapping(root, since_ms - BAR_MS, until_ms + BAR_MS)
    coins_upper = {c.upper() for c in coins}
    frames = []
    for i, p in enumerate(paths):
        if i % 50 == 0:
            print(f"  read {i}/{len(paths)}", flush=True)
        part = _read_parquet_filtered(p, since_ms, until_ms, coins_upper)
        if part is None or part.empty:
            continue
        frames.append(derive_research_series(part)[KEEP])
    if not frames:
        return pd.DataFrame(columns=KEEP)
    out = pd.concat(frames, ignore_index=True)
    return out.sort_values(["base_coin", "event_local_ts_ms"], kind="mergesort")


def step_from_closed_bars(
    bar_end_ms: np.ndarray,
    values: np.ndarray,
    grid: np.ndarray,
) -> np.ndarray:
    if bar_end_ms.size == 0:
        return np.full(grid.shape, np.nan, dtype="float64")
    idx = np.searchsorted(bar_end_ms, grid, side="right") - 1
    ok = idx >= 0
    ic = np.clip(idx, 0, len(bar_end_ms) - 1)
    return np.where(ok, values[ic], np.nan)


def spread_second_stats(
    ts: np.ndarray,
    y: np.ndarray,
    eval_ms: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Causal last tick at t; min/max of finite ticks in (t−1s, t]."""
    n = int(eval_ms.size)
    last = np.full(n, np.nan, dtype="float64")
    mn = np.full(n, np.nan, dtype="float64")
    mx = np.full(n, np.nan, dtype="float64")
    if ts.size == 0:
        return last, mn, mx
    right = np.searchsorted(ts, eval_ms, side="right")
    left = np.searchsorted(ts, eval_ms - 1000, side="right")
    idx_last = right - 1
    ok_last = idx_last >= 0
    last[ok_last] = y[idx_last[ok_last]]
    for i in range(n):
        a, b = int(left[i]), int(right[i])
        if b <= a:
            continue
        sl = y[a:b]
        finite = sl[np.isfinite(sl)]
        if finite.size:
            mn[i] = float(finite.min())
            mx[i] = float(finite.max())
    return last, mn, mx


def _f32(x: np.ndarray) -> np.ndarray:
    return np.asarray(x, dtype="float32")


def _u16_clip(x: np.ndarray) -> np.ndarray:
    clipped = np.clip(np.asarray(x, dtype="int32"), 0, UINT16_MAX)
    return clipped.astype("uint16")


def features_for_coin(
    sub: pd.DataFrame,
    *,
    coin: str,
    coins: Sequence[str],
    t0: int,
    t1: int,
) -> pd.DataFrame:
    store_grid = eval_grid_ms(t0, t1, 1000)
    if store_grid.size == 0:
        return pd.DataFrame()
    # Extra 60s of p50 so dp50_60 is defined at t0.
    full_grid = eval_grid_ms(t0 - WINDOW_1M_MS, t1, 1000)
    n_prefix = int(full_grid.size - store_grid.size)
    bar_start0 = ((t0 - FLOOR_WARMUP_MS) // BAR_MS) * BAR_MS
    ts = (
        sub["event_local_ts_ms"].to_numpy("int64")
        if not sub.empty
        else np.asarray([], dtype="int64")
    )
    d: dict[str, Any] = {"ts_s": (store_grid // 1000).astype("int64")}
    for side, col in SIDES:
        y = (
            sub[col].to_numpy("float64")
            if not sub.empty
            else np.asarray([], dtype="float64")
        )
        if sub.empty:
            buckets = pd.DataFrame()
        else:
            buckets = build_5m_bucket_stats(
                sub,
                value_col=col,
                start_ms=bar_start0,
                end_ms=t1,
                fill_empty_buckets=True,
            )
        if buckets.empty:
            floor_full = np.full(full_grid.shape, np.nan, dtype="float64")
            gap_full = np.full(full_grid.shape, np.nan, dtype="float64")
        else:
            close = buckets["close"].to_numpy("float64")
            sma12 = causal_sma(close, W1_BARS)
            floor_bar = compute_chosen_floor(sma12)[TF_SELECT_25_NAME]
            bend = buckets["bar_end_ms"].to_numpy("int64")
            floor_full = step_from_closed_bars(bend, floor_bar, full_grid)
            gap_full = step_from_closed_bars(
                bend, buckets["gap_fraction"].to_numpy("float64"), full_grid
            )
        occ_thr = floor_full + OCC_ABOVE_FLOOR_ADD
        stats = rolling_tw_window_stats(
            ts,
            y,
            window_ms=WINDOW_1M_MS,
            eval_ts_ms=full_grid,
            occ_threshold=occ_thr,
        )
        p50 = stats.p50[n_prefix:]
        floor = floor_full[n_prefix:]
        cov = stats.cov[n_prefix:]
        n_ticks = stats.n_ticks[n_prefix:]
        occ = stats.occ[n_prefix:]
        gap = gap_full[n_prefix:]
        p50_full = stats.p50
        dp50 = np.full(store_grid.shape, np.nan, dtype="float64")
        lag = DP50_LAG_S
        if n_prefix >= lag:
            dp50 = p50_full[n_prefix:] - p50_full[n_prefix - lag : n_prefix - lag + p50.size]
        last, smin, smax = spread_second_stats(ts, y, store_grid)
        usable = np.isfinite(p50) & np.isfinite(floor) & (cov >= USABLE_COV_MIN)
        d[f"p50_1m_{side}"] = _f32(p50)
        d[f"floor_{side}"] = _f32(floor)
        d[f"theta_1m_{side}"] = _f32(p50 - floor)
        d[f"gapfrac5m_{side}"] = _f32(gap)
        d[f"cov_1m_{side}"] = _f32(cov)
        d[f"n_ticks_1m_{side}"] = _u16_clip(n_ticks)
        d[f"usable_{side}"] = usable.astype(bool)
        d[f"spread_last_{side}"] = _f32(last)
        d[f"spread_min_{side}"] = _f32(smin)
        d[f"spread_max_{side}"] = _f32(smax)
        d[f"dp50_60_{side}"] = _f32(dp50)
        d[f"occ60_{side}"] = _f32(occ)
    frame = pd.DataFrame(d)
    frame["coin"] = pd.Categorical([coin] * len(frame), categories=list(coins))
    return frame


DEFAULT_PART_NAME = "part-000.parquet"


def partition_path(
    out_dir: Path,
    coin: str,
    event_date: str,
    part_name: str = DEFAULT_PART_NAME,
) -> Path:
    name = str(part_name).strip() or DEFAULT_PART_NAME
    if "/" in name or name.startswith(".") or not name.endswith(".parquet"):
        raise ValueError(f"illegal part name {part_name!r}")
    return out_dir / f"coin={coin}" / f"event_date={event_date}" / name


def shard_ok_path(out_dir: Path, since_ms: int, until_ms: int) -> Path:
    return out_dir / "status" / f"{since_ms}_{until_ms}.ok"


def write_coin_partitions(
    frame: pd.DataFrame,
    out_dir: Path,
    *,
    part_name: str = DEFAULT_PART_NAME,
    overwrite: bool = False,
) -> list[Path]:
    if frame.empty:
        return []
    event_date = pd.to_datetime(frame["ts_s"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    written: list[Path] = []
    for ed, part in frame.groupby(event_date, sort=True):
        coin = str(part["coin"].iloc[0])
        dest = partition_path(out_dir, coin, str(ed), part_name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and not overwrite:
            print(f"  skip existing {dest}", flush=True)
            written.append(dest)
            continue
        table = pa.Table.from_pandas(part, preserve_index=False)
        tmp = dest.with_name(dest.name + ".tmp")
        pq.write_table(table, tmp, compression="zstd")
        tmp.replace(dest)
        written.append(dest)
    return written


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def utc_day_shards(since_ms: int, until_ms: int) -> list[tuple[int, int]]:
    """Split ``[since, until)`` on UTC midnight; last shard may be short."""
    shards: list[tuple[int, int]] = []
    t = since_ms
    while t < until_ms:
        dt = datetime.fromtimestamp(t / 1000.0, tz=timezone.utc)
        nxt = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc) + timedelta(days=1)
        t1 = min(int(nxt.timestamp() * 1000), until_ms)
        shards.append((t, t1))
        t = t1
    return shards


def run_shard(
    *,
    data_root: str,
    coins: Sequence[str],
    since_ms: int,
    until_ms: int,
    out_dir: str,
    skip_done: bool = False,
    part_name: str = DEFAULT_PART_NAME,
    overwrite: bool = False,
    write_ok: bool = True,
) -> dict[str, Any]:
    root = Path(data_root)
    out = Path(out_dir)
    ok_path = shard_ok_path(out, since_ms, until_ms)
    label = f"{_utc_ms_iso(since_ms)} → {_utc_ms_iso(until_ms)}"
    if skip_done and ok_path.is_file():
        print(f"skip done {label}", flush=True)
        return json.loads(ok_path.read_text(encoding="utf-8"))
    print(f"shard start {label} pid={os.getpid()} part={part_name}", flush=True)
    t_all = time.perf_counter()
    ticks = load_slim(root, coins, since_ms - FLOOR_WARMUP_MS, until_ms)
    t_read = time.perf_counter() - t_all
    print(
        f"  read {t_read:.1f}s rows={len(ticks):,} "
        f"rss_mb~{ticks.memory_usage(deep=True).sum() / 2**20:.0f}",
        flush=True,
    )
    row_counts: dict[str, int] = {}
    finite: dict[str, dict[str, float]] = {}
    written: list[str] = []
    t_compute = time.perf_counter()
    for coin in coins:
        sub = ticks.loc[ticks["base_coin"] == coin] if not ticks.empty else ticks
        frame = features_for_coin(sub, coin=coin, coins=coins, t0=since_ms, t1=until_ms)
        paths = write_coin_partitions(
            frame, out, part_name=part_name, overwrite=overwrite
        )
        written.extend(str(p) for p in paths)
        row_counts[coin] = int(len(frame))
        if frame.empty:
            continue
        finite[coin] = {
            "p50_1m_long": float(np.isfinite(frame["p50_1m_long"]).mean()),
            "p50_1m_short": float(np.isfinite(frame["p50_1m_short"]).mean()),
            "floor_long": float(np.isfinite(frame["floor_long"]).mean()),
            "floor_short": float(np.isfinite(frame["floor_short"]).mean()),
            "usable_long": float(frame["usable_long"].mean()),
            "usable_short": float(frame["usable_short"].mean()),
        }
        print(
            f"  {coin:8s} rows={len(frame):,} "
            f"p50L={finite[coin]['p50_1m_long']:.3f} "
            f"floorL={finite[coin]['floor_long']:.3f} "
            f"usableL={finite[coin]['usable_long']:.3f}",
            flush=True,
        )
    summary = {
        "since": _utc_ms_iso(since_ms),
        "until": _utc_ms_iso(until_ms),
        "since_ms": since_ms,
        "until_ms": until_ms,
        "rows_total": int(sum(row_counts.values())),
        "row_counts": row_counts,
        "finite_frac": finite,
        "read_s": round(t_read, 2),
        "compute_s": round(time.perf_counter() - t_compute, 2),
        "wall_s": round(time.perf_counter() - t_all, 2),
        "written": written,
        "schema_version": SCHEMA_VERSION,
        "part_name": part_name,
    }
    if write_ok:
        atomic_write_json(ok_path, summary)
    print(f"shard done {label} wall={summary['wall_s']:.1f}s rows={summary['rows_total']:,}", flush=True)
    return summary


def _worker(payload: dict[str, Any]) -> dict[str, Any]:
    return run_shard(
        data_root=payload["data_root"],
        coins=payload["coins"],
        since_ms=payload["since_ms"],
        until_ms=payload["until_ms"],
        out_dir=payload["out_dir"],
        skip_done=payload["skip_done"],
        part_name=payload.get("part_name", DEFAULT_PART_NAME),
        overwrite=bool(payload.get("overwrite", False)),
        write_ok=bool(payload.get("write_ok", True)),
    )


def write_manifest(
    out_dir: Path,
    *,
    coins: Sequence[str],
    since: str,
    until: str,
    shards: Sequence[dict[str, Any]],
    status: str,
) -> Path:
    rows = 0
    for sh in shards:
        rows += int(sh.get("rows_total") or 0)
    obj = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "coins": list(coins),
        "n_coins": len(coins),
        "since": since,
        "until": until,
        "cadence": "1Hz",
        "nan_rule": NAN_RULE,
        "occ_above_floor_add": OCC_ABOVE_FLOOR_ADD,
        "occ_note": "4 legs × 0.00075 × 100 = 0.30 percentage points",
        "usable": "finite p50 AND finite floor AND cov_1m >= 0.20",
        "omitted_columns": list(OMITTED_COLUMNS),
        "git_head": git_head(Path(".")),
        "rows_total": rows,
        "shards": [
            {
                "since": sh.get("since"),
                "until": sh.get("until"),
                "rows_total": sh.get("rows_total"),
                "wall_s": sh.get("wall_s"),
            }
            for sh in shards
        ],
        "wide": True,
        "partition": "coin=/event_date=",
        "compression": "zstd",
        "floor": "tf-select α25 of SMA-12(close), step at closed 5m bar_end",
        "state": "p50_roll_1m hold→next 1Hz",
    }
    path = out_dir / "MANIFEST.json"
    atomic_write_json(path, obj)
    return path


def sanity_check(
    out_dir: Path,
    coins: Sequence[str],
    event_date: str,
    *,
    part_name: str = DEFAULT_PART_NAME,
) -> dict[str, Any]:
    """Read one day's hive partitions; print row counts and finite fractions."""
    report: dict[str, Any] = {"event_date": event_date, "coins": {}}
    total = 0
    for coin in coins:
        path = partition_path(out_dir, coin, event_date, part_name)
        if not path.is_file():
            report["coins"][coin] = {"error": f"missing {path}"}
            continue
        df = pd.read_parquet(path)
        n = int(len(df))
        total += n
        rec = {
            "rows": n,
            "parquet_ok": True,
            "p50_1m_long_finite": float(np.isfinite(df["p50_1m_long"]).mean()) if n else 0.0,
            "floor_long_finite": float(np.isfinite(df["floor_long"]).mean()) if n else 0.0,
            "usable_long": float(df["usable_long"].mean()) if n else 0.0,
        }
        report["coins"][coin] = rec
        print(
            f"sanity {coin:8s} rows={n:,} "
            f"p50L={rec['p50_1m_long_finite']:.3f} "
            f"floorL={rec['floor_long_finite']:.3f}",
            flush=True,
        )
    report["rows_total"] = total
    expected = len(coins) * 86400
    report["expected_rows_24h"] = expected
    print(
        f"sanity {event_date}: rows={total:,} expected_24h={expected:,} "
        f"ratio={total / expected if expected else 0:.3f}",
        flush=True,
    )
    return report


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Build gear 2.2 1 Hz backtest feature parquet (observation only)."
    )
    p.add_argument("--data-root", default="output/lean_ticks")
    p.add_argument("--coins-file", default=None)
    p.add_argument("--coins", default=None, help="Comma-separated override")
    p.add_argument("--top-n", type=int, default=None, help="Legacy: head of universe csv")
    p.add_argument("--since", default=None)
    p.add_argument("--until", default=None)
    p.add_argument("--day", default=None, help="UTC calendar day YYYY-MM-DD (24h)")
    p.add_argument("--hours", type=float, default=None)
    p.add_argument("--out-dir", default="output/gear22_backtest_features")
    p.add_argument("--out", default=None, help="Legacy single-file parquet (no hive)")
    p.add_argument(
        "--plan",
        choices=("usable-august",),
        default=None,
        help="Expand to the locked usable-August window and shard by UTC day",
    )
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--skip-done", action="store_true")
    p.add_argument(
        "--part-name",
        default=DEFAULT_PART_NAME,
        help="Hive file name inside event_date= (default part-000.parquet)",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing hive parts. Default: never overwrite.",
    )
    p.add_argument(
        "--no-manifest",
        action="store_true",
        help="Do not rewrite MANIFEST.json (batch runs / backup gaps).",
    )
    p.add_argument(
        "--no-status-ok",
        action="store_true",
        help="Do not write status/*.ok (needed when batching coins).",
    )
    p.add_argument("--sanity-date", default=None, help="After build, sanity-read this UTC date")
    return p


def resolve_coins(args: argparse.Namespace) -> list[str]:
    if args.coins:
        coins = [c.strip().upper() for c in str(args.coins).split(",") if c.strip()]
        return coins
    if args.top_n:
        std = pd.read_csv(UNIVERSE_STD_CSV)
        return (
            std.sort_values("std_spread", ascending=False)
            .head(int(args.top_n))["base_coin"]
            .astype(str)
            .str.upper()
            .tolist()
        )
    path = Path(args.coins_file) if args.coins_file else default_coins_path()
    print(f"coins file: {path}", flush=True)
    return parse_coins_file(path)


def resolve_window(args: argparse.Namespace) -> tuple[int, int]:
    if args.plan == "usable-august":
        return parse_since_ms(PLAN_USABLE_AUGUST[0]), parse_since_ms(PLAN_USABLE_AUGUST[1])
    if args.day:
        t0 = parse_since_ms(f"{args.day}T00:00:00Z")
        hours = 24.0 if args.hours is None else float(args.hours)
        return t0, t0 + int(hours * 3_600_000)
    if args.since and args.until:
        return parse_since_ms(args.since), parse_since_ms(args.until)
    if args.since and args.hours is not None:
        t0 = parse_since_ms(args.since)
        return t0, t0 + int(float(args.hours) * 3_600_000)
    raise SystemExit("need --since/--until, --day, or --plan usable-august")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    coins = resolve_coins(args)
    cross_check_universe(coins)
    since_ms, until_ms = resolve_window(args)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    workers = max(1, min(int(args.workers), 3))
    shards = utc_day_shards(since_ms, until_ms)
    print(
        f"{SCHEMA_VERSION} coins={len(coins)} shards={len(shards)} "
        f"workers={workers} {_utc_ms_iso(since_ms)} → {_utc_ms_iso(until_ms)}",
        flush=True,
    )
    print(f"omitted: {', '.join(OMITTED_COLUMNS)}; occ60 included (add={OCC_ABOVE_FLOOR_ADD})", flush=True)
    part_name = str(args.part_name)
    write_ok = not bool(args.no_status_ok)
    overwrite = bool(args.overwrite)

    if args.out and len(shards) == 1 and workers == 1:
        # Legacy single-file path used by the old day probe.
        summary = run_shard(
            data_root=args.data_root,
            coins=coins,
            since_ms=since_ms,
            until_ms=until_ms,
            out_dir=str(out_dir),
            skip_done=args.skip_done,
            part_name=part_name,
            overwrite=overwrite,
            write_ok=write_ok,
        )
        # Also copy-concat to --out for the old size-probe workflow.
        frames = []
        for coin in coins:
            for p in Path(out_dir).glob(f"coin={coin}/event_date=*/part-000.parquet"):
                frames.append(pd.read_parquet(p))
        if frames:
            pd.concat(frames, ignore_index=True).to_parquet(args.out, compression="zstd", index=False)
            sz = os.path.getsize(args.out)
            print(f"legacy --out {args.out} zstd={sz:,} B", flush=True)
        if not args.no_manifest:
            write_manifest(
                out_dir,
                coins=coins,
                since=_utc_ms_iso(since_ms),
                until=_utc_ms_iso(until_ms),
                shards=[summary],
                status="complete",
            )
        return 0

    payloads = [
        {
            "data_root": str(Path(args.data_root)),
            "coins": list(coins),
            "since_ms": a,
            "until_ms": b,
            "out_dir": str(out_dir),
            "skip_done": bool(args.skip_done),
            "part_name": part_name,
            "overwrite": overwrite,
            "write_ok": write_ok,
        }
        for a, b in shards
    ]
    results: list[dict[str, Any]] = []

    def _maybe_manifest(status: str) -> None:
        if args.no_manifest:
            return
        write_manifest(
            out_dir,
            coins=coins,
            since=_utc_ms_iso(since_ms),
            until=_utc_ms_iso(until_ms),
            shards=results,
            status=status,
        )

    _maybe_manifest("running")
    if workers == 1:
        for payload in payloads:
            results.append(_worker(payload))
            _maybe_manifest("running")
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_worker, p) for p in payloads]
            for fut in as_completed(futs):
                results.append(fut.result())
                _maybe_manifest("running")
    results.sort(key=lambda s: int(s.get("since_ms") or 0))
    _maybe_manifest("complete")
    sanity_date = args.sanity_date
    if sanity_date is None and shards:
        sanity_date = _utc_ms_iso(shards[0][0])[:10]
        if (shards[0][1] - shards[0][0]) >= 86_400_000 - 1:
            pass
    if sanity_date:
        sanity_check(out_dir, coins, sanity_date, part_name=part_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
