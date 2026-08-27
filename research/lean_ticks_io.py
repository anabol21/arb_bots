"""Read compact lean tick parquet for historical simulators (gear 1 / gear 2).

On-disk body is ``LEAN_TICK_BODY_COLS`` (16 columns). Spreads, book latency,
freshness, and ``event_dt`` are derived at read time. Timestamp physical types
mix ``int64`` and ``float64`` across rewrite eras; this reader coerces to
int64 milliseconds.
"""

from __future__ import annotations

import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from app.schema.lean_event import LEAN_TICK_BODY_COLS
from research.tick_fail_closed import AGE_MAX_MS, SKEW_MAX_MS, fail_closed_ok

TS_COLS: tuple[str, ...] = (
    "event_local_ts_ms",
    "calc_local_ts_ms",
    "okx_local_recv_ts_ms",
    "okx_ts_ms",
    "bybit_local_recv_ts_ms",
    "bybit_ts_ms",
)

# Minimal on-disk columns for Gear 2 when Check_volume=False (no book sizes / trigger).
GEAR2_LEAN_COLS_NO_SIZE: tuple[str, ...] = (
    "event_local_ts_ms",
    "base_coin",
    "calc_local_ts_ms",
    "okx_local_recv_ts_ms",
    "okx_ts_ms",
    "bybit_local_recv_ts_ms",
    "bybit_ts_ms",
    "okx_bid_price",
    "okx_ask_price",
    "bybit_bid_price",
    "bybit_ask_price",
)

GEAR2_LEAN_COLS_WITH_SIZE: tuple[str, ...] = GEAR2_LEAN_COLS_NO_SIZE + (
    "okx_bid_size",
    "okx_ask_size",
    "bybit_bid_size",
    "bybit_ask_size",
)


def gear2_lean_columns(*, check_volume: bool = False) -> list[str]:
    """Arrow read columns for Gear 2; drops unused body cols when volume gate is off."""
    return list(GEAR2_LEAN_COLS_WITH_SIZE if check_volume else GEAR2_LEAN_COLS_NO_SIZE)


def slim_prepared_for_backtest(
    df: pd.DataFrame,
    *,
    check_volume: bool = False,
    need_freshness: bool = False,
) -> pd.DataFrame:
    """Drop derive-only / unused columns before ``run_backtest_market`` (one copy)."""
    keep = [
        "event_local_ts_ms",
        "base_coin",
        "event_dt",
        "spread_long",
        "spread_short",
        "okx_latency_ms",
        "bybit_latency_ms",
    ]
    if need_freshness:
        keep.extend(["okx_freshness_ms", "bybit_freshness_ms"])
    if check_volume:
        keep.extend(
            ["okx_bid_size", "okx_ask_size", "bybit_bid_size", "bybit_ask_size"]
        )
    missing = [c for c in keep if c not in df.columns]
    if missing:
        raise KeyError(f"slim_prepared_for_backtest missing {missing}")
    return df.loc[:, keep].copy()


def parse_ts_ms(x) -> int:
    if isinstance(x, (int, np.integer)):
        return int(x)
    s = str(x).replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def parse_lean_file_window(path: Path) -> Optional[tuple[int, int]]:
    """spread_YYYYMMDDTHHMMSSZ_YYYYMMDDTHHMMSSZ.parquet → [start, end) ms."""
    name = path.name
    if not (name.startswith("spread_") and name.endswith(".parquet")):
        return None
    stem = name[len("spread_") : -len(".parquet")]
    parts = stem.split("_")
    if len(parts) != 2:
        return None
    try:
        a = datetime.strptime(parts[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        b = datetime.strptime(parts[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(a.timestamp() * 1000), int(b.timestamp() * 1000)


def list_lean_files_overlapping(tick_dir: Path, start_ms: int, end_ms: int) -> list[Path]:
    files: list[Path] = []
    if not tick_dir.is_dir():
        return files
    for p in sorted(tick_dir.glob("spread_*.parquet")):
        win = parse_lean_file_window(p)
        if win is None:
            continue
        a, b = win
        if a < end_ms and b > start_ms:
            files.append(p)
    return files


def _ts_int64_ms(col: pa.ChunkedArray) -> pa.ChunkedArray:
    return pc.cast(pc.floor(pc.cast(col, pa.float64())), pa.int64())


def _even_take(table: pa.Table, cap: int) -> pa.Table:
    n = table.num_rows
    k = int(cap)
    if k <= 0 or n <= k:
        return table
    if k == 1:
        idx = [0]
    else:
        idx = [int(round(i * (n - 1) / (k - 1))) for i in range(k)]
    return table.take(idx)


def _even_take_per_coin(table: pa.Table, cap: int) -> pa.Table:
    """Even-take at most ``cap`` rows per ``base_coin`` (file already time-filtered)."""
    k = int(cap)
    if k <= 0 or table.num_rows == 0:
        return table
    if "base_coin" not in table.column_names:
        return _even_take(table, k)
    bc = table["base_coin"]
    if pa.types.is_dictionary(bc.type):
        bc = bc.dictionary_decode()
    coins = pc.utf8_upper(bc).to_numpy(zero_copy_only=False)
    n = int(len(coins))
    if k == 1:
        _, first = np.unique(coins, return_index=True)
        idx = np.sort(first)
        return table.take(idx.tolist())
    # First/last per coin (k==2) or even grid along each coin's rows.
    order = np.argsort(coins, kind="stable")
    sorted_c = coins[order]
    # Group starts in the sorted coin array
    change = np.empty(len(sorted_c), dtype=bool)
    change[0] = True
    change[1:] = sorted_c[1:] != sorted_c[:-1]
    starts = np.flatnonzero(change)
    ends = np.append(starts[1:], len(sorted_c))
    take: list = []
    for a, b in zip(starts.tolist(), ends.tolist()):
        m = b - a
        grp = order[a:b]
        if m <= k:
            take.extend(int(i) for i in grp)
            continue
        if k == 2:
            take.append(int(grp[0]))
            take.append(int(grp[-1]))
            continue
        pick = [int(round(i * (m - 1) / (k - 1))) for i in range(k)]
        take.extend(int(grp[j]) for j in pick)
    take_arr = np.unique(np.asarray(take, dtype=np.int64))
    return table.take(take_arr.tolist())


def _read_file_filtered(
    path: Path,
    start_ms: int,
    end_ms: int,
    coins_upper: Optional[set[str]],
    columns: Optional[list[str]] = None,
    per_file_cap: Optional[int] = None,
    per_file_per_coin_cap: Optional[int] = None,
) -> Optional[pa.Table]:
    try:
        names = pq.read_schema(path).names
        wanted = list(columns) if columns else list(LEAN_TICK_BODY_COLS)
        cols = [c for c in wanted if c in names]
        if "event_local_ts_ms" not in cols:
            return None
        table = pq.read_table(path, columns=cols)
    except Exception as exc:
        warnings.warn(f"skip unreadable {path.name}: {exc}", stacklevel=2)
        return None
    if table.num_rows == 0:
        return None
    ts = _ts_int64_ms(table["event_local_ts_ms"])
    keep = pc.and_(pc.greater_equal(ts, start_ms), pc.less(ts, end_ms))
    if coins_upper:
        bc = table["base_coin"]
        if pa.types.is_dictionary(bc.type):
            bc = bc.dictionary_decode()
        keep = pc.and_(keep, pc.is_in(pc.utf8_upper(bc), pa.array(sorted(coins_upper))))
    table = table.filter(keep)
    if table.num_rows == 0:
        return None
    if per_file_per_coin_cap is not None:
        table = _even_take_per_coin(table, per_file_per_coin_cap)
        if table.num_rows == 0:
            return None
    elif per_file_cap is not None:
        table = _even_take(table, per_file_cap)
    return table


def iter_lean_tables(
    tick_dir: Path,
    start_ms: int,
    end_ms: int,
    *,
    coins: Optional[set[str]] = None,
    workers: int = 1,
    columns: Optional[list[str]] = None,
    chunk: Optional[int] = None,
    per_file_cap: Optional[int] = None,
    per_file_per_coin_cap: Optional[int] = None,
):
    """Yield ``(path, table)`` for overlapping files after time/coin filter.

    Tables are not concatenated. Caps are optional: omit both to stream **every**
    matching row (all-tick stats), then even-take in the caller for plots.
    """
    if end_ms <= start_ms:
        raise ValueError("END must be after START")
    if per_file_cap is not None and per_file_per_coin_cap is not None:
        raise ValueError("set only one of per_file_cap, per_file_per_coin_cap")
    files = list_lean_files_overlapping(tick_dir, start_ms, end_ms)
    if not files:
        raise FileNotFoundError(
            f"no spread_*.parquet overlapping window under {tick_dir}"
        )
    coins_upper = {c.upper() for c in coins} if coins else None

    def _one(path: Path):
        return path, _read_file_filtered(
            path,
            start_ms,
            end_ms,
            coins_upper,
            columns=columns,
            per_file_cap=per_file_cap,
            per_file_per_coin_cap=per_file_per_coin_cap,
        )

    n_workers = max(1, int(workers))
    n_files = len(files)
    chunk_size = int(chunk) if chunk is not None else (250 if n_files > 250 else n_files)
    chunk_size = max(1, chunk_size)
    if n_files > 250:
        print(f"lean read: {n_files} files, workers={n_workers}", flush=True)

    def _run_batch(batch):
        if n_workers == 1:
            return [_one(p) for p in batch]
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            return list(pool.map(_one, batch))

    printed = 0
    for i in range(0, n_files, chunk_size):
        batch = files[i : i + chunk_size]
        for p, part in _run_batch(batch):
            if part is None:
                continue
            yield p, part
        processed = min(i + chunk_size, n_files)
        if n_files > 250 and (processed - printed >= 250 or processed == n_files):
            print(f"  {processed}/{n_files}", flush=True)
            printed = processed


def read_lean_raw(
    tick_dir: Path,
    start_ms: int,
    end_ms: int,
    *,
    coins: Optional[set[str]] = None,
    workers: int = 1,
    columns: Optional[list[str]] = None,
    per_file_cap: Optional[int] = None,
    per_file_per_coin_cap: Optional[int] = None,
) -> tuple[pd.DataFrame, list[Path]]:
    """Load overlapping lean files; filter time (and optional coins) in Arrow.

    ``per_file_cap`` even-takes the whole file. ``per_file_per_coin_cap`` even-takes
    each ``base_coin`` inside the file (batched all-coin overview; one pass).
    For a stats-vs-plot split, use ``iter_lean_tables`` (no cap) then even-take.
    """
    tables: list[pa.Table] = []
    used: list[Path] = []
    for p, part in iter_lean_tables(
        tick_dir,
        start_ms,
        end_ms,
        coins=coins,
        workers=workers,
        columns=columns,
        per_file_cap=per_file_cap,
        per_file_per_coin_cap=per_file_per_coin_cap,
    ):
        tables.append(part)
        used.append(p)
    if not tables:
        raise ValueError("no rows in window after time/coin filter")
    table = pa.concat_tables(tables, promote_options="permissive")
    return table.to_pandas(), used


def prepare_lean_ticks(
    raw: pd.DataFrame,
    *,
    copy: bool = True,
) -> pd.DataFrame:
    """Alias lean prices, derive spreads + book latency, drop bad L1 / stale-cross.

    ``copy=False`` mutates ``raw`` in place (chunked day path: avoid a full duplicate).
    """
    df = raw.copy() if copy else raw
    rename = {}
    for old, new in (
        ("okx_bid", "okx_bid_price"),
        ("okx_ask", "okx_ask_price"),
        ("bybit_bid", "bybit_bid_price"),
        ("bybit_ask", "bybit_ask_price"),
    ):
        if old in df.columns and new not in df.columns:
            rename[old] = new
    if rename:
        df = df.rename(columns=rename)

    need = [
        "event_local_ts_ms",
        "base_coin",
        "okx_bid_price",
        "okx_ask_price",
        "bybit_bid_price",
        "bybit_ask_price",
    ]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise KeyError(f"lean ticks missing {missing}; columns={list(df.columns)}")

    df["base_coin"] = df["base_coin"].astype(str).str.upper()
    for c in TS_COLS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["event_local_ts_ms"])
    df["event_local_ts_ms"] = df["event_local_ts_ms"].round().astype("int64")
    for c in TS_COLS[1:]:
        if c in df.columns:
            s = df[c]
            df[c] = s.round().astype("int64") if s.notna().all() else s.round()

    px = ["okx_bid_price", "okx_ask_price", "bybit_bid_price", "bybit_ask_price"]
    for c in px:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    ok = (
        df["okx_bid_price"].notna()
        & df["okx_ask_price"].notna()
        & df["bybit_bid_price"].notna()
        & df["bybit_ask_price"].notna()
        & (df["bybit_bid_price"] > 0)
        & (df["okx_bid_price"] > 0)
    )
    df = df.loc[ok.fillna(False)]
    n_l1 = len(df)
    df = df.loc[fail_closed_ok(df)].copy()
    n_fc = n_l1 - len(df)
    if n_fc:
        print(
            f"fail-closed drop (skew/age > {SKEW_MAX_MS}/{AGE_MAX_MS} ms): "
            f"{n_fc} rows  remaining={len(df)}"
        )
    df["spread_long"] = (
        (df["bybit_bid_price"] - df["okx_ask_price"]) / df["bybit_bid_price"] * 100.0
    )
    df["spread_short"] = (
        (df["okx_bid_price"] - df["bybit_ask_price"]) / df["okx_bid_price"] * 100.0
    )

    if "okx_latency_ms" not in df.columns and {
        "okx_local_recv_ts_ms",
        "okx_ts_ms",
    } <= set(df.columns):
        df["okx_latency_ms"] = df["okx_local_recv_ts_ms"] - df["okx_ts_ms"]
    if "bybit_latency_ms" not in df.columns and {
        "bybit_local_recv_ts_ms",
        "bybit_ts_ms",
    } <= set(df.columns):
        df["bybit_latency_ms"] = df["bybit_local_recv_ts_ms"] - df["bybit_ts_ms"]
    if "okx_freshness_ms" not in df.columns and {
        "calc_local_ts_ms",
        "okx_local_recv_ts_ms",
    } <= set(df.columns):
        df["okx_freshness_ms"] = df["calc_local_ts_ms"] - df["okx_local_recv_ts_ms"]
    if "bybit_freshness_ms" not in df.columns and {
        "calc_local_ts_ms",
        "bybit_local_recv_ts_ms",
    } <= set(df.columns):
        df["bybit_freshness_ms"] = df["calc_local_ts_ms"] - df["bybit_local_recv_ts_ms"]

    for c in ("okx_latency_ms", "bybit_latency_ms", "okx_freshness_ms", "bybit_freshness_ms"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    df["event_dt"] = pd.to_datetime(df["event_local_ts_ms"], unit="ms", utc=True)
    return df.reset_index(drop=True)


def read_and_prepare_lean_ticks(
    tick_dir: Path,
    start_ms: int,
    end_ms: int,
    *,
    coins: Optional[set[str]] = None,
    workers: int = 1,
    columns: Optional[list[str]] = None,
    slim_backtest: bool = False,
    check_volume: bool = False,
    need_freshness: bool = False,
) -> tuple[pd.DataFrame, list[Path]]:
    """Load + prepare lean ticks.

    Prefer ``coins=`` / ``columns=`` (or ``gear2_lean_columns``) so Arrow never
    materializes the full universe U then filters in pandas.
    ``slim_backtest=True`` drops derive-only columns before return.
    """
    read_cols = columns
    if read_cols is None and slim_backtest:
        read_cols = gear2_lean_columns(check_volume=check_volume)
    raw, files = read_lean_raw(
        tick_dir,
        start_ms,
        end_ms,
        coins=coins,
        workers=workers,
        columns=read_cols,
    )
    print(f"read {len(files)} files, raw rows={len(raw)}")
    df = prepare_lean_ticks(raw, copy=False)
    del raw
    if slim_backtest:
        df = slim_prepared_for_backtest(
            df, check_volume=check_volume, need_freshness=need_freshness
        )
    return df, files
