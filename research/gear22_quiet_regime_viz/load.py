"""Load gappy L1 ticks from local compacted parquet / CSV dumps (no VPS required)."""

from __future__ import annotations

import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

# Live gear2 restart (document; override with --since).
# 2026-09-03 08:21:00 UTC == 11:21 MSK.
DEFAULT_SINCE_UTC = "2026-09-03T08:21:00Z"

# Columns needed to derive mid / edge / classic spreads.
_PRICE_COLS = (
    "okx_bid_price",
    "okx_ask_price",
    "bybit_bid_price",
    "bybit_ask_price",
)
# Venue delivery latency = local_recv − exchange_ts (same as research/lean_ticks_io).
_LATENCY_COLS = ("okx_latency_ms", "bybit_latency_ms")
_LATENCY_SRC_COLS = (
    "okx_local_recv_ts_ms",
    "okx_ts_ms",
    "bybit_local_recv_ts_ms",
    "bybit_ts_ms",
    *_LATENCY_COLS,
)
_READ_COLS = ("event_local_ts_ms", "base_coin", *_PRICE_COLS, *_LATENCY_SRC_COLS)


def parse_since_ms(value: str) -> int:
    """Parse CLI/ISO timestamp to Unix milliseconds (UTC if naive)."""
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def parse_lean_file_window(path: Path) -> Optional[tuple[int, int]]:
    """``spread_YYYYMMDDTHHMMSSZ_YYYYMMDDTHHMMSSZ.parquet`` → ``[start, end)`` ms."""
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


def list_compacted_overlapping(tick_dir: Path, start_ms: int, end_ms: int) -> list[Path]:
    files: list[Path] = []
    if not tick_dir.is_dir():
        return files
    for p in sorted(tick_dir.glob("spread_*.parquet")):
        win = parse_lean_file_window(p)
        if win is None:
            # Still try files that do not follow the window naming convention.
            files.append(p)
            continue
        a, b = win
        if a < end_ms and b > start_ms:
            files.append(p)
    return files


def list_tick_inputs(data_root: Path) -> list[Path]:
    """Discover parquet/CSV under a local dump root.

    Accepts:
    - compacted flat dir: ``spread_*.parquet``
    - hive: ``base_coin=*/event_date=*/**/*.parquet``
    - loose ``*.parquet`` / ``*.csv`` (fixture-friendly)
    """
    root = Path(data_root)
    if not root.exists():
        raise FileNotFoundError(f"data root not found: {root}")
    compacted = sorted(root.glob("spread_*.parquet"))
    if compacted:
        return compacted
    hive = sorted(root.glob("base_coin=*/event_date=*/**/*.parquet"))
    if hive:
        return hive
    nested = sorted(root.rglob("spread_*.parquet"))
    if nested:
        return nested
    loose = sorted(root.rglob("*.parquet")) + sorted(root.rglob("*.csv"))
    return [p for p in loose if p.is_file()]


def _ts_int64_ms(col: pa.ChunkedArray) -> pa.ChunkedArray:
    return pc.cast(pc.floor(pc.cast(col, pa.float64())), pa.int64())


def _read_parquet_filtered(
    path: Path,
    start_ms: int,
    end_ms: int,
    coins_upper: Optional[set[str]],
) -> Optional[pd.DataFrame]:
    try:
        names = pq.read_schema(path).names
        cols = [c for c in _READ_COLS if c in names]
        if "event_local_ts_ms" not in cols:
            warnings.warn(f"skip {path.name}: no event_local_ts_ms", stacklevel=2)
            return None
        # Alias legacy short names if present.
        for short, long in (
            ("okx_bid", "okx_bid_price"),
            ("okx_ask", "okx_ask_price"),
            ("bybit_bid", "bybit_bid_price"),
            ("bybit_ask", "bybit_ask_price"),
        ):
            if long not in cols and short in names:
                cols.append(short)
        table = pq.read_table(path, columns=cols)
    except Exception as exc:
        warnings.warn(f"skip unreadable {path}: {exc}", stacklevel=2)
        return None
    if table.num_rows == 0:
        return None
    rename = {}
    for short, long in (
        ("okx_bid", "okx_bid_price"),
        ("okx_ask", "okx_ask_price"),
        ("bybit_bid", "bybit_bid_price"),
        ("bybit_ask", "bybit_ask_price"),
    ):
        if short in table.column_names and long not in table.column_names:
            rename[short] = long
    if rename:
        table = table.rename_columns(
            [rename.get(n, n) for n in table.column_names]
        )
    ts = _ts_int64_ms(table["event_local_ts_ms"])
    keep = pc.and_(pc.greater_equal(ts, start_ms), pc.less(ts, end_ms))
    if coins_upper and "base_coin" in table.column_names:
        bc = table["base_coin"]
        if pa.types.is_dictionary(bc.type):
            bc = bc.dictionary_decode()
        keep = pc.and_(keep, pc.is_in(pc.utf8_upper(bc), pa.array(sorted(coins_upper))))
    table = table.filter(keep)
    if table.num_rows == 0:
        return None
    return table.to_pandas()


def _read_csv_filtered(
    path: Path,
    start_ms: int,
    end_ms: int,
    coins_upper: Optional[set[str]],
) -> Optional[pd.DataFrame]:
    try:
        df = pd.read_csv(path)
    except Exception as exc:
        warnings.warn(f"skip unreadable {path}: {exc}", stacklevel=2)
        return None
    if df.empty:
        return None
    rename = {}
    for short, long in (
        ("okx_bid", "okx_bid_price"),
        ("okx_ask", "okx_ask_price"),
        ("bybit_bid", "bybit_bid_price"),
        ("bybit_ask", "bybit_ask_price"),
    ):
        if short in df.columns and long not in df.columns:
            rename[short] = long
    if rename:
        df = df.rename(columns=rename)
    if "event_local_ts_ms" not in df.columns:
        warnings.warn(f"skip {path.name}: no event_local_ts_ms", stacklevel=2)
        return None
    df["event_local_ts_ms"] = pd.to_numeric(df["event_local_ts_ms"], errors="coerce")
    df = df.dropna(subset=["event_local_ts_ms"])
    df["event_local_ts_ms"] = df["event_local_ts_ms"].round().astype("int64")
    mask = (df["event_local_ts_ms"] >= start_ms) & (df["event_local_ts_ms"] < end_ms)
    if coins_upper and "base_coin" in df.columns:
        mask &= df["base_coin"].astype(str).str.upper().isin(coins_upper)
    df = df.loc[mask]
    return None if df.empty else df


def _derive_venue_latency(out: pd.DataFrame) -> pd.DataFrame:
    """Attach ``okx_latency_ms`` / ``bybit_latency_ms`` when source cols exist.

    Convention (matches ``research/lean_ticks_io``)::

        okx_latency_ms   = okx_local_recv_ts_ms − okx_ts_ms
        bybit_latency_ms = bybit_local_recv_ts_ms − bybit_ts_ms

    Precomputed latency columns are kept (coerced numeric). Missing sources or
    non-finite diffs become NaN and are skipped by in-bar hist builders.
    """
    if "okx_latency_ms" not in out.columns and {
        "okx_local_recv_ts_ms",
        "okx_ts_ms",
    } <= set(out.columns):
        out["okx_latency_ms"] = (
            pd.to_numeric(out["okx_local_recv_ts_ms"], errors="coerce")
            - pd.to_numeric(out["okx_ts_ms"], errors="coerce")
        )
    if "bybit_latency_ms" not in out.columns and {
        "bybit_local_recv_ts_ms",
        "bybit_ts_ms",
    } <= set(out.columns):
        out["bybit_latency_ms"] = (
            pd.to_numeric(out["bybit_local_recv_ts_ms"], errors="coerce")
            - pd.to_numeric(out["bybit_ts_ms"], errors="coerce")
        )
    for c in _LATENCY_COLS:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def derive_research_series(df: pd.DataFrame) -> pd.DataFrame:
    """Add mid, mid-edge, policy spreads, and venue delivery latency.

    Primary research series (match ``app.policy.features`` / gear2)::

        spread_long  = (bybit_bid - okx_ask) / bybit_bid * 100   → open_long
        spread_short = (okx_bid - bybit_ask) / okx_bid * 100     → open_short

    Also derived for context (not the dual-stack primary)::

        edge_pct = (okx_mid - bybit_mid) / bybit_mid * 100

    Latency (when source timestamps present)::

        okx_latency_ms / bybit_latency_ms  — see ``_derive_venue_latency``
    """
    out = df.copy()
    missing = [c for c in _PRICE_COLS if c not in out.columns]
    if missing:
        raise KeyError(f"ticks missing price columns {missing}")
    if "base_coin" not in out.columns:
        raise KeyError("ticks missing base_coin")
    out["base_coin"] = out["base_coin"].astype(str).str.upper()
    out["event_local_ts_ms"] = (
        pd.to_numeric(out["event_local_ts_ms"], errors="coerce").round().astype("int64")
    )
    for c in _PRICE_COLS:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    ok = (
        out["okx_bid_price"].notna()
        & out["okx_ask_price"].notna()
        & out["bybit_bid_price"].notna()
        & out["bybit_ask_price"].notna()
        & (out["okx_bid_price"] > 0)
        & (out["okx_ask_price"] > 0)
        & (out["bybit_bid_price"] > 0)
        & (out["bybit_ask_price"] > 0)
    )
    out = out.loc[ok].copy()
    out["okx_mid"] = (out["okx_bid_price"] + out["okx_ask_price"]) / 2.0
    out["bybit_mid"] = (out["bybit_bid_price"] + out["bybit_ask_price"]) / 2.0
    out["edge_pct"] = (out["okx_mid"] - out["bybit_mid"]) / out["bybit_mid"] * 100.0
    out["spread_long"] = (
        (out["bybit_bid_price"] - out["okx_ask_price"]) / out["bybit_bid_price"] * 100.0
    )
    out["spread_short"] = (
        (out["okx_bid_price"] - out["bybit_ask_price"]) / out["okx_bid_price"] * 100.0
    )
    out = _derive_venue_latency(out)
    out["event_dt"] = pd.to_datetime(out["event_local_ts_ms"], unit="ms", utc=True)
    out = out.sort_values(["base_coin", "event_local_ts_ms"], kind="mergesort")
    return out.reset_index(drop=True)


def load_ticks(
    data_root: Path,
    *,
    coins: Sequence[str],
    since_ms: int,
    until_ms: Optional[int] = None,
) -> pd.DataFrame:
    """Load and derive research series for ``coins`` in ``[since_ms, until_ms)``.

    ``until_ms`` defaults to now (UTC). Empty result raises ``ValueError``.
    """
    if until_ms is None:
        until_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    if until_ms <= since_ms:
        raise ValueError("until must be after since")
    coins_upper = {c.upper() for c in coins}
    root = Path(data_root)
    paths = list_tick_inputs(root)
    if not paths:
        raise FileNotFoundError(f"no parquet/csv under {root}")

    # Prefer window-aware filter when compacted names are present.
    compacted = [p for p in paths if parse_lean_file_window(p) is not None]
    use_paths: Iterable[Path]
    if compacted and all(p.parent == compacted[0].parent for p in compacted):
        use_paths = list_compacted_overlapping(compacted[0].parent, since_ms, until_ms)
        if not use_paths:
            # Fall back to all discovered paths (fixture names may not overlap).
            use_paths = paths
    else:
        use_paths = paths

    frames: list[pd.DataFrame] = []
    for path in use_paths:
        if path.suffix.lower() == ".csv":
            part = _read_csv_filtered(path, since_ms, until_ms, coins_upper)
        else:
            part = _read_parquet_filtered(path, since_ms, until_ms, coins_upper)
        if part is not None and not part.empty:
            frames.append(part)
    if not frames:
        raise ValueError(
            f"no rows for coins={sorted(coins_upper)} in "
            f"[{since_ms}, {until_ms}) under {root}"
        )
    raw = pd.concat(frames, ignore_index=True)
    return derive_research_series(raw)
