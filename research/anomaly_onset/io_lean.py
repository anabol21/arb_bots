"""Self-contained lean L1 reader for the anomaly-onset detector.

Deliberately does NOT import ``research.lean_ticks_io`` because that module pulls
``research.tick_fail_closed`` which is not committed to the repo; this reader must
run anywhere the repo is checked out.

Supported on-disk layouts (auto-detected):

- ``flat``  : compacted ``spread_YYYYMMDDTHHMMSSZ_YYYYMMDDTHHMMSSZ.parquet`` files,
              one ~5-minute window per file, all coins concatenated
              (``backup1tb:spread-compacted`` / research SoT).
- ``hive``  : ``base_coin=<COIN>/event_date=<YYYY-MM-DD>/*.parquet``
              (live collector / local lean output).

Body columns follow ``app/schema/lean_event.py::LEAN_TICK_BODY_COLS`` (16 cols).
Spreads, latency, freshness and ``event_dt`` are derived at read time.

Directional executable L1 spreads (percent), matching docs/model-data-sources.md:

    spread_long  = (bybit_bid - okx_ask)  / bybit_bid * 100
    spread_short = (okx_bid   - bybit_ask) / okx_bid   * 100
"""

from __future__ import annotations

import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

__all__ = [
    "LEAN_TICK_BODY_COLS",
    "PRICE_COLS",
    "detect_layout",
    "parse_ts_ms",
    "read_lean_ticks",
    "add_dwell_weights",
    "DwellParams",
]

LEAN_TICK_BODY_COLS: tuple[str, ...] = (
    "event_local_ts_ms",
    "base_coin",
    "trigger",
    "calc_local_ts_ms",
    "okx_local_recv_ts_ms",
    "okx_ts_ms",
    "bybit_local_recv_ts_ms",
    "bybit_ts_ms",
    "okx_bid_price",
    "okx_bid_size",
    "okx_ask_price",
    "okx_ask_size",
    "bybit_bid_price",
    "bybit_bid_size",
    "bybit_ask_price",
    "bybit_ask_size",
)

PRICE_COLS: tuple[str, ...] = (
    "okx_bid_price",
    "okx_ask_price",
    "bybit_bid_price",
    "bybit_ask_price",
)

# Minimal columns needed to derive directional spreads (no book sizes / trigger).
_MIN_COLS: tuple[str, ...] = ("event_local_ts_ms", "base_coin") + PRICE_COLS


def parse_ts_ms(x) -> int:
    """UTC ISO-8601 (``...Z``) or epoch-ms -> int epoch milliseconds."""
    if isinstance(x, (int, np.integer)):
        return int(x)
    if isinstance(x, float):
        return int(x)
    s = str(x).replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def detect_layout(root: Path) -> str:
    """Return ``"flat"``, ``"hive"`` or raise if neither is present under ``root``."""
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(f"data root does not exist: {root}")
    if any(root.glob("spread_*.parquet")):
        return "flat"
    if any(root.glob("base_coin=*")):
        return "hive"
    # Nested flat (e.g. root/ticks/spread_*.parquet)
    if any(root.rglob("spread_*.parquet")):
        return "flat"
    if any(root.rglob("base_coin=*")):
        return "hive"
    raise FileNotFoundError(
        f"no lean parquet found under {root} (expected spread_*.parquet or base_coin=*/)"
    )


def _flat_file_window(path: Path) -> Optional[tuple[int, int]]:
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


def _list_flat_files(root: Path, start_ms: int, end_ms: int) -> list[Path]:
    files = sorted(root.glob("spread_*.parquet")) or sorted(root.rglob("spread_*.parquet"))
    out: list[Path] = []
    for p in files:
        win = _flat_file_window(p)
        if win is None:
            continue
        a, b = win
        if a < end_ms and b > start_ms:  # overlap
            out.append(p)
    return out


def _list_hive_files(root: Path, start_ms: int, end_ms: int, coins_upper: Optional[set[str]]) -> list[Path]:
    start_d = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).date()
    end_d = datetime.fromtimestamp((end_ms - 1) / 1000, tz=timezone.utc).date()
    out: list[Path] = []
    coin_dirs = sorted(root.glob("base_coin=*")) or sorted(root.rglob("base_coin=*"))
    for cdir in coin_dirs:
        coin = cdir.name.split("=", 1)[1].upper()
        if coins_upper and coin not in coins_upper:
            continue
        for ddir in sorted(cdir.glob("event_date=*")):
            try:
                d = datetime.strptime(ddir.name.split("=", 1)[1], "%Y-%m-%d").date()
            except ValueError:
                continue
            if start_d <= d <= end_d:
                out.extend(sorted(ddir.glob("*.parquet")))
    return out


def _read_one(
    path: Path,
    start_ms: int,
    end_ms: int,
    coins_upper: Optional[set[str]],
    columns: list[str],
) -> Optional[pa.Table]:
    try:
        names = set(pq.read_schema(path).names)
        cols = [c for c in columns if c in names]
        if "event_local_ts_ms" not in cols or "base_coin" not in cols:
            return None
        table = pq.read_table(path, columns=cols)
    except Exception as exc:  # noqa: BLE001 - skip unreadable file, keep run alive
        warnings.warn(f"skip unreadable {path.name}: {exc}", stacklevel=2)
        return None
    if table.num_rows == 0:
        return None
    ts = pc.cast(pc.floor(pc.cast(table["event_local_ts_ms"], pa.float64())), pa.int64())
    keep = pc.and_(pc.greater_equal(ts, start_ms), pc.less(ts, end_ms))
    if coins_upper:
        bc = table["base_coin"]
        if pa.types.is_dictionary(bc.type):
            bc = bc.dictionary_decode()
        keep = pc.and_(keep, pc.is_in(pc.utf8_upper(bc), pa.array(sorted(coins_upper))))
    table = table.filter(keep)
    return table if table.num_rows else None


def read_lean_ticks(
    root,
    start,
    end,
    *,
    coins: Optional[Iterable[str]] = None,
    workers: int = 4,
    with_sizes: bool = False,
    layout: Optional[str] = None,
    progress: bool = True,
) -> pd.DataFrame:
    """Read + prepare lean ticks over ``[start, end)`` for ``coins``.

    ``start``/``end`` accept UTC ISO strings or epoch-ms. Returns a tidy DataFrame
    sorted by ``(base_coin, event_local_ts_ms)`` with derived ``spread_long`` /
    ``spread_short`` (percent) and ``event_dt`` (UTC). Dwell weights are added
    separately by :func:`add_dwell_weights` (per coin, hole-aware).
    """
    root = Path(root)
    start_ms = parse_ts_ms(start)
    end_ms = parse_ts_ms(end)
    if end_ms <= start_ms:
        raise ValueError("END must be after START")
    layout = layout or detect_layout(root)
    coins_upper = {c.upper() for c in coins} if coins else None

    columns = list(LEAN_TICK_BODY_COLS if with_sizes else _MIN_COLS)

    if layout == "flat":
        files = _list_flat_files(root, start_ms, end_ms)
    elif layout == "hive":
        files = _list_hive_files(root, start_ms, end_ms, coins_upper)
    else:
        raise ValueError(f"unknown layout {layout!r}")
    if not files:
        raise FileNotFoundError(f"no lean files overlapping window under {root} ({layout})")
    if progress:
        print(f"[io_lean] layout={layout} files={len(files)} coins={'all' if not coins_upper else len(coins_upper)}", flush=True)

    def _job(p: Path):
        return _read_one(p, start_ms, end_ms, coins_upper, columns)

    tables: list[pa.Table] = []
    nw = max(1, int(workers))
    if nw == 1:
        parts = [_job(p) for p in files]
    else:
        with ThreadPoolExecutor(max_workers=nw) as pool:
            parts = list(pool.map(_job, files))
    tables = [t for t in parts if t is not None]
    if not tables:
        raise ValueError("no rows in window after time/coin filter")
    table = pa.concat_tables(tables, promote_options="permissive")
    df = table.to_pandas()
    return _prepare(df)


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    df["base_coin"] = df["base_coin"].astype(str).str.upper()
    df["event_local_ts_ms"] = pd.to_numeric(df["event_local_ts_ms"], errors="coerce")
    df = df.dropna(subset=["event_local_ts_ms"])
    df["event_local_ts_ms"] = df["event_local_ts_ms"].round().astype("int64")
    for c in PRICE_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    ok = np.ones(len(df), dtype=bool)
    for c in PRICE_COLS:
        ok &= df[c].notna().to_numpy()
    ok &= (df["bybit_bid_price"] > 0).to_numpy()
    ok &= (df["okx_bid_price"] > 0).to_numpy()
    df = df.loc[ok].copy()

    df["spread_long"] = (
        (df["bybit_bid_price"] - df["okx_ask_price"]) / df["bybit_bid_price"] * 100.0
    )
    df["spread_short"] = (
        (df["okx_bid_price"] - df["bybit_ask_price"]) / df["okx_bid_price"] * 100.0
    )
    df["event_dt"] = pd.to_datetime(df["event_local_ts_ms"], unit="ms", utc=True)
    df = df.sort_values(["base_coin", "event_local_ts_ms"], kind="mergesort")
    return df.reset_index(drop=True)


@dataclass(frozen=True)
class DwellParams:
    """Time-weighting / hole policy.

    max_gap_ms : gaps longer than this break the causal segment (honest holes are
        NOT quiet; rolling windows must not bridge them).
    max_dwell_ms : clamp for a single tick's dwell weight so a pre-hole tick or a
        sparse period cannot dominate the time-weighted statistics.
    """

    max_gap_ms: int = 5 * 60_000       # 5 min: matches compacted window granularity
    max_dwell_ms: int = 10_000         # 10 s cap on one tick's time weight


def add_dwell_weights(df: pd.DataFrame, params: DwellParams = DwellParams()) -> pd.DataFrame:
    """Add per-coin ``dwell_ms`` (time weight) and ``segment`` (hole-split id).

    ``dwell_ms[i] = clamp(ts[i+1] - ts[i], 0, max_dwell_ms)``; a new ``segment``
    starts whenever the raw gap exceeds ``max_gap_ms``. The last tick of each
    segment inherits the segment's median dwell (clamped), never zero.
    """
    if df.empty:
        df = df.copy()
        df["dwell_ms"] = np.array([], dtype=np.float64)
        df["segment"] = np.array([], dtype=np.int64)
        return df

    out = []
    seg_base = 0
    for _, g in df.groupby("base_coin", sort=False):
        g = g.sort_values("event_local_ts_ms", kind="mergesort")
        ts = g["event_local_ts_ms"].to_numpy(dtype=np.int64)
        n = len(ts)
        raw = np.diff(ts, append=ts[-1])  # last -> 0 placeholder
        gap = raw > params.max_gap_ms
        # segment id increments after each hole
        seg = np.cumsum(np.concatenate([[0], gap[:-1].astype(np.int64)]))
        dwell = np.clip(raw.astype(np.float64), 0.0, float(params.max_dwell_ms))
        # last tick of each segment (incl. global last) has placeholder dwell 0
        is_seg_end = np.zeros(n, dtype=bool)
        is_seg_end[-1] = True
        is_seg_end[:-1] |= gap[:-1]
        # replace segment-end dwell with segment median (clamped), else small floor
        for s in np.unique(seg):
            mask = seg == s
            body = mask & ~is_seg_end
            med = np.median(dwell[body]) if body.any() else float(params.max_dwell_ms) * 0.0
            med = float(np.clip(med if med > 0 else 1.0, 1.0, params.max_dwell_ms))
            dwell[mask & is_seg_end] = med
        g = g.copy()
        g["dwell_ms"] = dwell
        g["segment"] = seg + seg_base
        seg_base = int(g["segment"].max()) + 1
        out.append(g)
    res = pd.concat(out, axis=0)
    return res.sort_values(["base_coin", "event_local_ts_ms"], kind="mergesort").reset_index(drop=True)
