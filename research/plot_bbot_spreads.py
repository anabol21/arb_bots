#!/usr/bin/env python3
"""Plotly: collector spread series + B-bot stub trades, gear-1.0 marker style.

Local notebook (preferred): ``research/plot_bbot_spreads.ipynb``
reads ``output/bbot_plot/la_ticks.parquet`` and ``output/bbot_plot/journal``.

This module is the helper behind that notebook and a CLI for VPS.

Offline research viz. Does not start collector/bot, does not write D trees or
``/data/bbot``, does not claim PnL.

Spread lines come from lean/v1 tick parquet (D hive or compacted). Journal
``fill_price`` is L1 coin price and is never used as Y. Marker Y is the same
spread column gear 1 uses: open long / close short → ``spread_long``; open
short / close long → ``spread_short``. Signal extra in the journal is hover
only; marker coordinates are looked up on the plotted tick series.

Usage (repo root, VPS or local copy of ticks+journal)::

  python3 research/plot_bbot_spreads.py \\
    --coin LA --start 2026-08-17 --end 2026-08-18 \\
    --ticks-root /data/compacted --data-root /data/bbot \\
    --out output/bbot_LA_spreads.html

``/data/compacted/*.parquet`` is only the in-flight window. The script also
reads ``compacted/sent/``, ``/data/live`` hive, and ``/data/live/archived``.
Sent/archive retention is 12h; older windows need backup. Do **not** rclone
with ``--include spread_YYYYMMDDT*``: SFTP will list the whole remote and look
hung after the host-key NOTICE. Use ``--files-from`` + ``--no-traverse -P``.

  python3 research/plot_bbot_spreads.py --demo --out /tmp/bbot_spreads_demo.html
"""

from __future__ import annotations

import argparse
import json
import re
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go

REPO = Path(__file__).resolve().parents[1]
COMPACTED_NAME = re.compile(
    r"spread_(\d{8}T\d{6}Z)_(\d{8}T\d{6}Z)",
    re.IGNORECASE,
)

BOOK_COLS = (
    "okx_bid_price",
    "okx_ask_price",
    "bybit_bid_price",
    "bybit_ask_price",
)
STAMP_COLS = (
    "okx_local_recv_ts_ms",
    "okx_ts_ms",
    "bybit_local_recv_ts_ms",
    "bybit_ts_ms",
)


@dataclass
class PlotTrade:
    """Gear-1 Trade subset used by ``plot_strategy`` markers."""

    side: str  # long | short
    status: str  # closed | open
    open_price: float
    open_ts: float
    open_dt: object
    close_price: Optional[float] = None
    close_ts: Optional[float] = None
    close_dt: Optional[object] = None
    pnl: Optional[float] = None
    signal_open_price: Optional[float] = None
    signal_open_ts: Optional[float] = None
    signal_open_dt: Optional[object] = None
    signal_close_price: Optional[float] = None
    signal_close_ts: Optional[float] = None
    signal_close_dt: Optional[object] = None
    open_fill_delay_ticks: int = 0
    close_fill_delay_ticks: int = 0
    open_intent_id: str = ""
    close_intent_id: str = ""


def _utc_ms_to_dt(ts_ms: float) -> datetime:
    return datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=timezone.utc)


def _parse_day(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _parse_compacted_window(path: Path) -> Optional[tuple[datetime, datetime]]:
    match = COMPACTED_NAME.search(path.name)
    if not match:
        return None
    start = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    end = datetime.strptime(match.group(2), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    return start, end


SKIP_DIR_NAMES = frozenset({".tmp", ".state"})
COMPACT_INTERVAL_SEC = 300


def _log(msg: str) -> None:
    print(msg, flush=True)


def compacted_window_names(start: datetime, end_exclusive: datetime) -> list[str]:
    """Filenames the compactor uses: spread_<start>_<end>.parquet, 5-minute UTC windows."""
    t0 = int(start.timestamp())
    t1 = int(end_exclusive.timestamp())
    window = (t0 // COMPACT_INTERVAL_SEC) * COMPACT_INTERVAL_SEC
    names: list[str] = []
    fmt = "%Y%m%dT%H%M%SZ"
    while window < t1:
        a = datetime.fromtimestamp(window, timezone.utc)
        b = datetime.fromtimestamp(window + COMPACT_INTERVAL_SEC, timezone.utc)
        names.append(f"spread_{a.strftime(fmt)}_{b.strftime(fmt)}.parquet")
        window += COMPACT_INTERVAL_SEC
    return names


def _hive_files(
    root: Path,
    coin: str,
    start: datetime,
    end_exclusive: datetime,
) -> list[Path]:
    hive = root / f"base_coin={coin}"
    if not hive.is_dir():
        return []
    files: list[Path] = []
    day = start.date()
    last = (end_exclusive - timedelta(microseconds=1)).date()
    while day <= last:
        part = hive / f"event_date={day.isoformat()}"
        if part.is_dir():
            files.extend(
                sorted(
                    p
                    for p in part.glob("*.parquet")
                    if p.is_file() and ".tmp" not in p.name and ".inprogress" not in p.name
                )
            )
        day += timedelta(days=1)
    return files


def _compacted_files(
    root: Path,
    start: datetime,
    end_exclusive: datetime,
) -> list[Path]:
    """Recursive ``spread_*.parquet`` including ``sent/``; skip tmp/state dirs."""
    if not root.exists():
        return []
    kept: list[Path] = []
    unparsed = 0
    for path in root.rglob("spread_*.parquet"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if ".tmp" in path.name or ".inprogress" in path.name:
            continue
        window = _parse_compacted_window(path)
        if window is None:
            unparsed += 1
            kept.append(path)
            continue
        w0, w1 = window
        if w1 > start and w0 < end_exclusive:
            kept.append(path)
    if unparsed:
        warnings.warn(
            f"{unparsed} compacted files under {root} had no spread_<start>_<end> name; "
            "included and filtered by event_local_ts_ms",
            stacklevel=2,
        )
    return sorted(kept)


def collect_tick_files(
    ticks_roots: list[Path],
    coin: str,
    start: datetime,
    end_exclusive: datetime,
    *,
    live_root: Optional[Path] = None,
) -> tuple[list[Path], list[str]]:
    """Union hive + compacted (incl. sent/) + optional live/archived hive."""
    summaries: list[str] = []
    files: list[Path] = []
    seen: set[Path] = set()

    def _add(label: str, batch: list[Path]) -> None:
        new: list[Path] = []
        for path in batch:
            key = path.resolve() if path.exists() else path
            if key in seen:
                continue
            seen.add(key)
            new.append(path)
        files.extend(new)
        summaries.append(f"{label} files={len(new)}")

    for root in ticks_roots:
        hive = _hive_files(root, coin, start, end_exclusive)
        compacted = _compacted_files(root, start, end_exclusive)
        if hive:
            _add(f"hive {root}", hive)
        if compacted:
            _add(f"compacted {root}", compacted)
        if not hive and not compacted:
            summaries.append(f"empty {root}")

    if live_root is not None:
        live_key = live_root.resolve() if live_root.exists() else live_root
        already = {r.resolve() if r.exists() else r for r in ticks_roots}
        if live_key not in already:
            _add(f"hive {live_root}", _hive_files(live_root, coin, start, end_exclusive))
            archived = live_root / "archived"
            _add(
                f"hive {archived}",
                _hive_files(archived, coin, start, end_exclusive),
            )
    return files, summaries


def backup_pull_hint(start: datetime, end_exclusive: datetime) -> str:
    last = (end_exclusive - timedelta(microseconds=1)).date()
    dest = "/tmp/bbot_ticks"
    listing = "/tmp/bbot_tick_files.txt"
    n = len(compacted_window_names(start, end_exclusive))
    return (
        "Local compacted/sent and live/archived keep ~12h. Older ticks are on backup.\n"
        f"Write {n} window names then copy those files only (no remote listing):\n"
        f"  python3 plot_bbot_spreads.py --write-rclone-files {listing} "
        f"--start {start.date().isoformat()} --end {last.isoformat()}\n"
        f"  mkdir -p {dest}\n"
        f"  /opt/rclone-1.74.4/rclone copy backup1tb:spread-compacted {dest} "
        f"--files-from {listing} --no-traverse -P --transfers 4\n"
        f"  python3 plot_bbot_spreads.py --ticks-root {dest} --no-live "
        "--coin LA --start "
        f"{start.date().isoformat()} --end {last.isoformat()} --data-root /data/bbot "
        "--out output/bbot_LA_spreads.html"
    )


def _schema_names(path: Path) -> set[str]:
    import pyarrow.parquet as pq

    return set(pq.read_schema(path).names)


def _load_ticks_table(files: list[Path], coin: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Read LA rows file-by-file with progress. Avoids a silent dataset scan of all coins."""
    import pyarrow.parquet as pq

    want = ["event_local_ts_ms", "base_coin", "spread_long", "spread_short"]
    want += ["okx_latency_ms", "bybit_latency_ms", *BOOK_COLS, *STAMP_COLS]
    names = _schema_names(files[0])
    columns = [c for c in want if c in names]
    if "event_local_ts_ms" not in columns:
        raise KeyError(f"{files[0]} has no event_local_ts_ms")

    parts: list[pd.DataFrame] = []
    rows = 0
    n = len(files)
    for i, path in enumerate(files, start=1):
        if i == 1 or i % 10 == 0 or i == n:
            _log(f"read_ticks {i}/{n} rows={rows} {path.name}")
        have = _schema_names(path)
        cols = [c for c in columns if c in have]
        if "event_local_ts_ms" not in cols:
            continue
        try:
            kwargs: dict[str, Any] = {"columns": cols}
            if "base_coin" in have:
                kwargs["filters"] = [
                    ("base_coin", "==", coin),
                    ("event_local_ts_ms", ">=", start_ms),
                    ("event_local_ts_ms", "<", end_ms),
                ]
            table = pq.read_table(str(path), **kwargs)
            part = table.to_pandas()
        except Exception:
            part = pd.read_parquet(path, columns=cols)
            if "base_coin" in part.columns:
                part = part[part["base_coin"].astype(str).str.upper() == coin.upper()]
            if not part.empty:
                part = part[
                    (part["event_local_ts_ms"] >= start_ms)
                    & (part["event_local_ts_ms"] < end_ms)
                ]
        if part.empty:
            continue
        if "base_coin" in part.columns:
            part = part[part["base_coin"].astype(str).str.upper() == coin.upper()]
        if part.empty:
            continue
        parts.append(part)
        rows += len(part)
    _log(f"read_ticks done files={n} rows={rows}")
    if not parts:
        return pd.DataFrame(columns=columns)
    return pd.concat(parts, ignore_index=True)


def enrich_spreads(df: pd.DataFrame) -> pd.DataFrame:
    """Derive spread/latency at read if lean body omitted them."""
    out = df.copy()
    if "spread_long" not in out.columns:
        missing = [c for c in ("bybit_bid_price", "okx_ask_price") if c not in out.columns]
        if missing:
            raise KeyError(f"cannot derive spread_long; missing {missing}")
        bid = out["bybit_bid_price"].to_numpy(dtype="float64")
        ask = out["okx_ask_price"].to_numpy(dtype="float64")
        with np.errstate(divide="ignore", invalid="ignore"):
            out["spread_long"] = np.where(bid > 0, (bid - ask) / bid * 100.0, np.nan)
    if "spread_short" not in out.columns:
        missing = [c for c in ("okx_bid_price", "bybit_ask_price") if c not in out.columns]
        if missing:
            raise KeyError(f"cannot derive spread_short; missing {missing}")
        bid = out["okx_bid_price"].to_numpy(dtype="float64")
        ask = out["bybit_ask_price"].to_numpy(dtype="float64")
        with np.errstate(divide="ignore", invalid="ignore"):
            out["spread_short"] = np.where(bid > 0, (bid - ask) / bid * 100.0, np.nan)
    if "okx_latency_ms" not in out.columns and {"okx_local_recv_ts_ms", "okx_ts_ms"} <= set(
        out.columns
    ):
        out["okx_latency_ms"] = out["okx_local_recv_ts_ms"] - out["okx_ts_ms"]
    if "bybit_latency_ms" not in out.columns and {
        "bybit_local_recv_ts_ms",
        "bybit_ts_ms",
    } <= set(out.columns):
        out["bybit_latency_ms"] = out["bybit_local_recv_ts_ms"] - out["bybit_ts_ms"]
    out["event_dt"] = pd.to_datetime(out["event_local_ts_ms"], unit="ms", utc=True)
    out = out.sort_values("event_local_ts_ms", kind="mergesort").reset_index(drop=True)
    return out


def compute_gate_b_ma(
    data: pd.DataFrame,
    *,
    avg_window_sec: float,
    max_latency_okx_ms: Optional[float] = None,
    max_latency_bybit_ms: Optional[float] = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Causal Gate B MA from ``model.ipynb`` (valid-latency samples only)."""
    if avg_window_sec <= 0:
        raise ValueError("avg_window_sec must be > 0")
    n = len(data)
    ts_ms = data["event_local_ts_ms"].to_numpy(dtype="float64", copy=False)
    spread_long_arr = data["spread_long"].to_numpy(dtype="float64", copy=False)
    spread_short_arr = data["spread_short"].to_numpy(dtype="float64", copy=False)
    okx_lat_arr = data["okx_latency_ms"].to_numpy(dtype="float64", copy=False)
    bybit_lat_arr = data["bybit_latency_ms"].to_numpy(dtype="float64", copy=False)
    avg_valid = np.ones(n, dtype=bool)
    if max_latency_okx_ms is not None:
        avg_valid &= okx_lat_arr <= max_latency_okx_ms
    if max_latency_bybit_ms is not None:
        avg_valid &= bybit_lat_arr <= max_latency_bybit_ms
    window_ms = float(avg_window_sec) * 1000.0
    ma_long = np.full(n, np.nan, dtype="float64")
    ma_short = np.full(n, np.nan, dtype="float64")
    left = 0
    sum_long = 0.0
    sum_short = 0.0
    win_count = 0
    for i in range(n):
        t_lo = ts_ms[i] - window_ms
        while left <= i and ts_ms[left] < t_lo:
            if avg_valid[left]:
                sum_long -= spread_long_arr[left]
                sum_short -= spread_short_arr[left]
                win_count -= 1
            left += 1
        if avg_valid[i]:
            sum_long += spread_long_arr[i]
            sum_short += spread_short_arr[i]
            win_count += 1
        if win_count > 0:
            ma_long[i] = sum_long / win_count
            ma_short[i] = sum_short / win_count
    return ma_long, ma_short


def _downsample_for_plot(
    data: pd.DataFrame,
    max_points: Optional[int] = None,
) -> pd.DataFrame:
    """Downsample spread lines only; markers stay on the full series."""
    if max_points is None or max_points <= 0 or len(data) <= max_points:
        return data
    step = max(1, len(data) // max_points)
    idx = list(range(0, len(data), step))
    if idx[-1] != len(data) - 1:
        idx.append(len(data) - 1)
    return data.iloc[idx]


def _load_legs(path: Path) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    truncated = 0
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    for i, line in enumerate(lines, start=1):
        raw = line.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            if i == len(lines):
                truncated += 1
                continue
            raise
        if isinstance(rec, dict):
            rows.append(rec)
    return rows, truncated


def load_intents(
    journal_root: Path,
    *,
    coin: str,
    start_ms: int,
    end_ms: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """One filled intent per dual-leg pair, ordered by fill then signal time."""
    notes: list[str] = []
    files = sorted(journal_root.glob("event_date=*/legs.jsonl"))
    if not files:
        raise FileNotFoundError(f"no event_date=*/legs.jsonl under {journal_root}")
    by_intent: dict[str, list[dict[str, Any]]] = {}
    for path in files:
        rows, truncated = _load_legs(path)
        if truncated:
            notes.append(f"truncated_tail | {path}")
        for rec in rows:
            by_intent.setdefault(str(rec.get("intent_id")), []).append(rec)

    intents: list[dict[str, Any]] = []
    for intent_id, legs in by_intent.items():
        filled = [x for x in legs if str(x.get("status")) == "filled"]
        if not filled:
            continue
        sample = filled[0]
        if str(sample.get("base_coin", "")).upper() != coin.upper():
            continue
        fill_ts = sample.get("fill_ts_ms")
        signal_ts = int(sample["signal_ts_ms"])
        t_ms = int(fill_ts) if fill_ts is not None else signal_ts
        if t_ms < start_ms or t_ms >= end_ms:
            continue
        intents.append(sample)
    intents.sort(key=lambda r: (int(r.get("fill_ts_ms") or r["signal_ts_ms"]), r["intent_id"]))
    return intents, notes


def _journal_spread(rec: dict[str, Any], *, use_long: bool) -> Optional[float]:
    key = "spread_long" if use_long else "spread_short"
    val = rec.get(key)
    if val is None:
        return None
    return float(val)


def _use_long(side: str, *, is_open: bool) -> bool:
    return (side == "long") if is_open else (side == "short")


def _lookup_tick(
    ts_ms: np.ndarray,
    target_ms: float,
    *,
    tolerance_ms: float,
) -> Optional[int]:
    if len(ts_ms) == 0:
        return None
    i = int(np.searchsorted(ts_ms, target_ms))
    candidates: list[int] = []
    if i < len(ts_ms):
        candidates.append(i)
    if i > 0:
        candidates.append(i - 1)
    best = min(candidates, key=lambda j: abs(float(ts_ms[j]) - target_ms))
    if abs(float(ts_ms[best]) - target_ms) > tolerance_ms:
        return None
    return best


def intents_to_trades(
    intents: Iterable[dict[str, Any]],
    ticks: pd.DataFrame,
    *,
    tolerance_ms: float = 1000.0,
) -> tuple[list[PlotTrade], list[str]]:
    """Pair open/close intents. Leftover open is EOD (diamond), same as gear 1."""
    notes: list[str] = []
    ts_ms = ticks["event_local_ts_ms"].to_numpy(dtype="float64", copy=False)
    trades: list[PlotTrade] = []
    current: Optional[PlotTrade] = None

    def _point(rec: dict[str, Any], *, side: str, is_open: bool) -> tuple[float, object, float, object, float, int]:
        signal_ts = float(rec["signal_ts_ms"])
        fill_ts = float(rec["fill_ts_ms"])
        use_long = _use_long(side, is_open=is_open)
        journal_spread = _journal_spread(rec, use_long=use_long)
        i_fill = _lookup_tick(ts_ms, fill_ts, tolerance_ms=tolerance_ms)
        i_sig = _lookup_tick(ts_ms, signal_ts, tolerance_ms=tolerance_ms)
        col = "spread_long" if use_long else "spread_short"
        if i_fill is None:
            if journal_spread is None:
                raise LookupError(
                    f"no tick within {tolerance_ms}ms of fill_ts_ms={fill_ts} "
                    f"and no journal {col}"
                )
            notes.append(
                f"fill_lookup_miss intent={rec.get('intent_id')} ts={int(fill_ts)}; "
                "Y from journal extra (bot book, not D tick)"
            )
            fill_px = journal_spread
            fill_dt = _utc_ms_to_dt(fill_ts)
            fill_i = i_sig if i_sig is not None else 0
        else:
            fill_px = float(ticks.iloc[i_fill][col])
            fill_dt = ticks.iloc[i_fill]["event_dt"]
            fill_i = i_fill
        if i_sig is None:
            sig_px = journal_spread if journal_spread is not None else fill_px
            sig_dt = _utc_ms_to_dt(signal_ts)
            delay = 0
        else:
            sig_px = float(ticks.iloc[i_sig][col])
            sig_dt = ticks.iloc[i_sig]["event_dt"]
            delay = max(0, int(fill_i - i_sig))
        return fill_ts, fill_dt, fill_px, sig_dt, sig_px, delay

    for rec in intents:
        spread_side = str(rec.get("spread_side"))
        if spread_side in ("open_long", "open_short"):
            side = "long" if spread_side == "open_long" else "short"
            if current is not None:
                notes.append(
                    f"open_while_open prev={current.open_intent_id} "
                    f"new={rec.get('intent_id')}; previous marked EOD"
                )
                trades.append(current)
            fill_ts, fill_dt, fill_px, sig_dt, sig_px, delay = _point(
                rec, side=side, is_open=True
            )
            current = PlotTrade(
                side=side,
                status="open",
                open_price=fill_px,
                open_ts=fill_ts,
                open_dt=fill_dt,
                signal_open_price=sig_px,
                signal_open_ts=float(rec["signal_ts_ms"]),
                signal_open_dt=sig_dt,
                open_fill_delay_ticks=delay,
                open_intent_id=str(rec.get("intent_id", "")),
            )
            continue
        if spread_side != "close":
            notes.append(f"skip intent={rec.get('intent_id')} spread_side={spread_side}")
            continue
        if current is None:
            notes.append(f"close_without_open intent={rec.get('intent_id')}")
            continue
        fill_ts, fill_dt, fill_px, sig_dt, sig_px, delay = _point(
            rec, side=current.side, is_open=False
        )
        current.status = "closed"
        current.close_price = fill_px
        current.close_ts = fill_ts
        current.close_dt = fill_dt
        current.signal_close_price = sig_px
        current.signal_close_ts = float(rec["signal_ts_ms"])
        current.signal_close_dt = sig_dt
        current.close_fill_delay_ticks = delay
        current.close_intent_id = str(rec.get("intent_id", ""))
        current.pnl = float(current.open_price) + float(fill_px)
        trades.append(current)
        current = None
    if current is not None:
        trades.append(current)
    return trades, notes


def plot_strategy(
    df: pd.DataFrame,
    trades: Optional[list[PlotTrade]] = None,
    *,
    title: Optional[str] = None,
    width: int = 1400,
    height: int = 700,
    max_points: Optional[int] = 4000,
    marker_mode: str = "both",
    avg_window_sec: Optional[float] = 2.0,
    max_latency_okx_ms: Optional[float] = 40.0,
    max_latency_bybit_ms: Optional[float] = 25.0,
) -> go.Figure:
    """Gear-1 ``plot_strategy``: spread_long/short, Gate B MA, fill/signal markers."""
    if marker_mode not in ("fill", "signal", "both"):
        raise ValueError("marker_mode must be 'fill', 'signal', or 'both'")
    if title is None:
        title = "spreads and entries/exits"

    data = df.sort_values("event_dt").reset_index(drop=True)
    if avg_window_sec is not None:
        _log(f"gate_b_ma ticks={len(data)} window_sec={avg_window_sec}")
        ma_long, ma_short = compute_gate_b_ma(
            data,
            avg_window_sec=float(avg_window_sec),
            max_latency_okx_ms=max_latency_okx_ms,
            max_latency_bybit_ms=max_latency_bybit_ms,
        )
        data = data.assign(_ma_long=ma_long, _ma_short=ma_short)
    plot_data = _downsample_for_plot(data, max_points=max_points)
    fig = go.Figure()
    fig.add_trace(
        go.Scattergl(
            x=plot_data["event_dt"],
            y=plot_data["spread_long"],
            mode="lines",
            name="spread_long",
            line=dict(width=1.4, color="#1f77b4"),
        )
    )
    fig.add_trace(
        go.Scattergl(
            x=plot_data["event_dt"],
            y=plot_data["spread_short"],
            mode="lines",
            name="spread_short",
            line=dict(width=1.4, color="#d62728"),
        )
    )
    if avg_window_sec is not None:
        fig.add_trace(
            go.Scattergl(
                x=plot_data["event_dt"],
                y=plot_data["_ma_long"],
                mode="lines",
                name=f"MA long ({avg_window_sec}s)",
                line=dict(width=1.6, color="#17becf", dash="dash"),
            )
        )
        fig.add_trace(
            go.Scattergl(
                x=plot_data["event_dt"],
                y=plot_data["_ma_short"],
                mode="lines",
                name=f"MA short ({avg_window_sec}s)",
                line=dict(width=1.6, color="#bcbd22", dash="dash"),
            )
        )
    else:
        fig.add_annotation(
            text="Gate B MA: off (avg_window_sec=None)",
            xref="paper",
            yref="paper",
            x=0.01,
            y=0.99,
            showarrow=False,
            font=dict(size=11, color="#666"),
        )

    def _add_marker_trace(*, name, xs, ys, hover, symbol, color, size):
        if not xs:
            return
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=ys,
                mode="markers",
                name=name,
                marker=dict(symbol=symbol, color=color, size=size, line=dict(width=1, color="#222")),
                text=hover,
                hovertemplate="%{text}<extra></extra>",
            )
        )

    if trades:
        side_colors = {"long": "#2ca02c", "short": "#ff7f0e"}
        close_colors = {"long": "#1f77b4", "short": "#d62728"}
        closed = [t for t in trades if t.status == "closed"]
        eod = [t for t in trades if t.status == "open"]
        show_fill = marker_mode in ("fill", "both")
        show_signal = marker_mode in ("signal", "both")

        for side in ("long", "short"):
            group = [t for t in closed if t.side == side]
            if not group:
                continue
            if show_fill:
                _add_marker_trace(
                    name=f"{side} open (fill)",
                    xs=[t.open_dt for t in group],
                    ys=[t.open_price for t in group],
                    hover=[
                        (
                            f"{t.side} open исполнение"
                            f"<br>fill={t.open_price:.6f} ts={t.open_ts}"
                            f"<br>signal={t.signal_open_price} ts={t.signal_open_ts}"
                            f"<br>delay_ticks={t.open_fill_delay_ticks}"
                            f"<br>intent={t.open_intent_id}"
                        )
                        for t in group
                    ],
                    symbol="triangle-up",
                    color=side_colors[side],
                    size=12,
                )
                _add_marker_trace(
                    name=f"{side} close (fill)",
                    xs=[t.close_dt for t in group],
                    ys=[t.close_price for t in group],
                    hover=[
                        (
                            f"{t.side} close исполнение"
                            f"<br>fill={t.close_price:.6f} ts={t.close_ts}"
                            f"<br>signal={t.signal_close_price} ts={t.signal_close_ts}"
                            f"<br>open+close={t.pnl:.6f} delay_ticks={t.close_fill_delay_ticks}"
                            f"<br>intent={t.close_intent_id}"
                        )
                        for t in group
                    ],
                    symbol="triangle-down",
                    color=close_colors[side],
                    size=12,
                )
            if show_signal:
                _add_marker_trace(
                    name=f"{side} open (signal)",
                    xs=[
                        t.signal_open_dt if t.signal_open_dt is not None else t.open_dt
                        for t in group
                    ],
                    ys=[
                        t.signal_open_price if t.signal_open_price is not None else t.open_price
                        for t in group
                    ],
                    hover=[
                        (
                            f"{t.side} open сигнал"
                            f"<br>signal={t.signal_open_price} ts={t.signal_open_ts}"
                            f"<br>fill={t.open_price:.6f} ts={t.open_ts}"
                            f"<br>delay_ticks={t.open_fill_delay_ticks}"
                        )
                        for t in group
                    ],
                    symbol="circle-open",
                    color=side_colors[side],
                    size=10,
                )
                _add_marker_trace(
                    name=f"{side} close (signal)",
                    xs=[
                        t.signal_close_dt if t.signal_close_dt is not None else t.close_dt
                        for t in group
                    ],
                    ys=[
                        t.signal_close_price
                        if t.signal_close_price is not None
                        else t.close_price
                        for t in group
                    ],
                    hover=[
                        (
                            f"{t.side} close сигнал"
                            f"<br>signal={t.signal_close_price} ts={t.signal_close_ts}"
                            f"<br>fill={t.close_price:.6f} ts={t.close_ts}"
                        )
                        for t in group
                    ],
                    symbol="circle-open",
                    color=close_colors[side],
                    size=10,
                )

        for side in ("long", "short"):
            group = [t for t in eod if t.side == side]
            if not group:
                continue
            _add_marker_trace(
                name=f"{side} open (EOD fill)",
                xs=[t.open_dt for t in group],
                ys=[t.open_price for t in group],
                hover=[
                    (
                        f"{t.side} open конец-дня исполнение"
                        f"<br>fill={t.open_price:.6f} ts={t.open_ts}"
                        f"<br>signal={t.signal_open_price} ts={t.signal_open_ts}"
                        f"<br>intent={t.open_intent_id}"
                    )
                    for t in group
                ],
                symbol="diamond",
                color=side_colors[side],
                size=13,
            )
            if show_signal:
                _add_marker_trace(
                    name=f"{side} open (EOD signal)",
                    xs=[
                        t.signal_open_dt if t.signal_open_dt is not None else t.open_dt
                        for t in group
                    ],
                    ys=[
                        t.signal_open_price if t.signal_open_price is not None else t.open_price
                        for t in group
                    ],
                    hover=[
                        (
                            f"{t.side} open конец-дня сигнал"
                            f"<br>signal={t.signal_open_price} ts={t.signal_open_ts}"
                            f"<br>fill={t.open_price:.6f}"
                        )
                        for t in group
                    ],
                    symbol="diamond-open",
                    color=side_colors[side],
                    size=11,
                )

    fig.update_layout(
        title=title,
        width=width,
        height=height,
        xaxis_title="event_dt",
        yaxis_title="spread",
        hovermode="closest",
        legend=dict(font=dict(size=11)),
        margin=dict(l=50, r=20, t=60, b=40),
    )
    return fig


def load_bbot_plot_inputs(
    ticks_path: Path,
    journal_dir: Path,
    *,
    coin: str,
    start: str,
    end: str,
    lookup_tolerance_ms: float = 1000.0,
) -> tuple[pd.DataFrame, list[PlotTrade], list[dict[str, Any]], list[str], dict[str, Any]]:
    """Load a local LA (or other coin) parquet + journal bundle for the notebook."""
    ticks_path = Path(ticks_path)
    journal_dir = Path(journal_dir)
    raw = pd.read_parquet(ticks_path)
    if "base_coin" in raw.columns:
        raw = raw[raw["base_coin"].astype(str).str.upper() == coin.upper()]
    ticks = enrich_spreads(raw)
    ticks = ticks.drop_duplicates(subset=["event_local_ts_ms"], keep="first").reset_index(
        drop=True
    )
    start_dt = _parse_day(start)
    end_exclusive = _parse_day(end) + timedelta(days=1)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_exclusive.timestamp() * 1000)
    intents, journal_notes = load_intents(
        journal_dir, coin=coin, start_ms=start_ms, end_ms=end_ms
    )
    trades, pair_notes = intents_to_trades(
        intents, ticks, tolerance_ms=float(lookup_tolerance_ms)
    )
    meta = {
        "n_ticks": len(ticks),
        "n_intents": len(intents),
        "n_trades": len(trades),
        "n_closed": sum(1 for t in trades if t.status == "closed"),
        "n_eod_open": sum(1 for t in trades if t.status == "open"),
        "tick_span": (
            None
            if ticks.empty
            else (
                _utc_ms_to_dt(float(ticks["event_local_ts_ms"].min())).isoformat(),
                _utc_ms_to_dt(float(ticks["event_local_ts_ms"].max())).isoformat(),
            )
        ),
    }
    return ticks, trades, intents, journal_notes + pair_notes, meta


def build_demo_ticks() -> pd.DataFrame:
    """Synthetic lean-like ticks so the figure can be checked without VPS data."""
    n = 400
    ts = 1_713_200_000_000 + np.arange(n, dtype=np.int64) * 50
    t = np.arange(n, dtype="float64")
    spread_long = 0.04 + 0.08 * np.sin(t / 18.0)
    spread_short = 0.03 + 0.07 * np.cos(t / 22.0)
    spread_long[120:140] = 0.12
    spread_short[260:275] = 0.11
    spread_long[300:310] = 0.02
    return pd.DataFrame(
        {
            "event_local_ts_ms": ts,
            "spread_long": spread_long,
            "spread_short": spread_short,
            "okx_latency_ms": np.full(n, 8.0),
            "bybit_latency_ms": np.full(n, 6.0),
        }
    )


def build_demo_intents(ticks: pd.DataFrame) -> list[dict[str, Any]]:
    def _rec(intent_id: str, spread_side: str, i: int) -> dict[str, Any]:
        row = ticks.iloc[i]
        return {
            "intent_id": intent_id,
            "base_coin": "LA",
            "spread_side": spread_side,
            "status": "filled",
            "signal_ts_ms": int(row.event_local_ts_ms),
            "fill_ts_ms": int(ticks.iloc[min(i + 2, len(ticks) - 1)].event_local_ts_ms),
            "spread_long": float(row.spread_long),
            "spread_short": float(row.spread_short),
        }

    return [
        _rec("open-1", "open_short", 125),
        _rec("close-1", "close", 200),
        _rec("open-2", "open_long", 265),
        _rec("close-2", "close", 305),
        _rec("open-3", "open_short", 340),
    ]


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--coin", default="LA", help="base_coin (default LA: all intents in the Aug 17–18 run)")
    p.add_argument("--start", default="2026-08-17", help="UTC start date YYYY-MM-DD inclusive")
    p.add_argument("--end", default="2026-08-18", help="UTC end date YYYY-MM-DD inclusive")
    p.add_argument(
        "--ticks-root",
        action="append",
        default=None,
        help="hive parent (base_coin=…) or compacted dir; repeatable. "
        "Default /data/compacted (includes sent/ via recursive glob)",
    )
    p.add_argument(
        "--live-root",
        default="/data/live",
        help="also read hive + archived hive here (disabled by --no-live)",
    )
    p.add_argument(
        "--no-live",
        action="store_true",
        help="do not auto-read /data/live (use when ticks-root is a backup copy)",
    )
    p.add_argument("--data-root", default="/data/bbot", help="B-bot data root; journal is {root}/journal")
    p.add_argument("--journal", default=None, help="override journal dir (event_date=*/legs.jsonl)")
    p.add_argument("--out", default=None, help="HTML path (default output/bbot_{coin}_spreads.html)")
    p.add_argument("--max-points", type=int, default=4000, help="line downsample; 0 = every tick")
    p.add_argument("--marker-mode", choices=("fill", "signal", "both"), default="both")
    p.add_argument("--avg-window-sec", type=float, default=2.0)
    p.add_argument("--no-ma", action="store_true")
    p.add_argument("--max-latency-okx-ms", type=float, default=40.0)
    p.add_argument("--max-latency-bybit-ms", type=float, default=25.0)
    p.add_argument("--lookup-tolerance-ms", type=float, default=1000.0)
    p.add_argument("--width", type=int, default=1400)
    p.add_argument("--height", type=int, default=700)
    p.add_argument("--demo", action="store_true", help="synthetic ticks+intents; no parquet/journal")
    p.add_argument(
        "--write-rclone-files",
        default=None,
        help="write compacted window names for rclone --files-from, then exit",
    )
    p.add_argument(
        "--tick-from",
        default=None,
        help="UTC ISO start for rclone windows, e.g. 2026-08-17T15:15:00Z (default --start 00:00)",
    )
    p.add_argument(
        "--tick-to",
        default=None,
        help="UTC ISO exclusive end for rclone windows, e.g. 2026-08-18T10:00:00Z",
    )
    return p.parse_args(argv)


def _parse_iso(text: str) -> datetime:
    raw = text.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    if args.write_rclone_files:
        start = _parse_iso(args.tick_from) if args.tick_from else _parse_day(args.start)
        if args.tick_to:
            end_exclusive = _parse_iso(args.tick_to)
        else:
            end_exclusive = _parse_day(args.end) + timedelta(days=1)
        names = compacted_window_names(start, end_exclusive)
        out = Path(args.write_rclone_files)
        out.write_text("\n".join(names) + "\n", encoding="utf-8")
        _log(f"wrote {len(names)} names to {out}")
        _log(
            f"rclone: /opt/rclone-1.74.4/rclone copy backup1tb:spread-compacted /tmp/bbot_ticks "
            f"--files-from {out} --no-traverse -P --transfers 4"
        )
        return 0

    if args.demo:
        ticks = enrich_spreads(build_demo_ticks())
        intents = build_demo_intents(ticks)
        journal_notes: list[str] = []
        coin = "LA"
        period = "demo"
    else:
        coin = str(args.coin).upper()
        start = _parse_day(args.start)
        end_exclusive = _parse_day(args.end) + timedelta(days=1)
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end_exclusive.timestamp() * 1000)
        ticks_roots = [Path(p) for p in (args.ticks_root or ["/data/compacted"])]
        live_root = None if args.no_live else Path(args.live_root)
        files, source_notes = collect_tick_files(
            ticks_roots,
            coin,
            start,
            end_exclusive,
            live_root=live_root,
        )
        for line in source_notes:
            print(line)
        if not files:
            raise FileNotFoundError(
                f"no parquet for {coin} in [{args.start}..{args.end}] under {ticks_roots}"
            )
        print(f"tick_files={len(files)}", flush=True)
        raw = _load_ticks_table(files, coin, start_ms, end_ms)
        if raw.empty:
            raise FileNotFoundError(
                f"parquet files found but 0 rows for {coin} in [{args.start}..{args.end}]"
            )
        ticks = enrich_spreads(raw)
        ticks = ticks.drop_duplicates(subset=["event_local_ts_ms"], keep="first").reset_index(
            drop=True
        )
        journal_root = Path(args.journal) if args.journal else Path(args.data_root) / "journal"
        intents, journal_notes = load_intents(
            journal_root, coin=coin, start_ms=start_ms, end_ms=end_ms
        )
        period = args.start if args.start == args.end else f"{args.start}..{args.end}"
        tmin = int(ticks["event_local_ts_ms"].min())
        tmax = int(ticks["event_local_ts_ms"].max())
        print(
            f"tick_span_utc={_utc_ms_to_dt(tmin).isoformat()} .. {_utc_ms_to_dt(tmax).isoformat()}"
        )
        if intents:
            intent_times = [
                int(r["fill_ts_ms"] if r.get("fill_ts_ms") is not None else r["signal_ts_ms"])
                for r in intents
            ]
            imin, imax = min(intent_times), max(intent_times)
            print(
                f"intent_span_utc={_utc_ms_to_dt(imin).isoformat()} .. "
                f"{_utc_ms_to_dt(imax).isoformat()}"
            )
            if imin < tmin - 1000 or imax > tmax + 1000:
                print("ERROR: tick parquet does not cover the journal window.")
                print(backup_pull_hint(start, end_exclusive))

    trades, pair_notes = intents_to_trades(
        intents, ticks, tolerance_ms=float(args.lookup_tolerance_ms)
    )
    closed_n = sum(1 for t in trades if t.status == "closed")
    eod_n = sum(1 for t in trades if t.status == "open")
    avg_window = None if args.no_ma else float(args.avg_window_sec)
    title = (
        f"{coin} {period} — D ticks + bbot stub "
        f"(closed={closed_n} eod_open={eod_n} marker={args.marker_mode})"
    )
    fig = plot_strategy(
        ticks,
        trades,
        title=title,
        width=args.width,
        height=args.height,
        max_points=args.max_points,
        marker_mode=args.marker_mode,
        avg_window_sec=avg_window,
        max_latency_okx_ms=args.max_latency_okx_ms,
        max_latency_bybit_ms=args.max_latency_bybit_ms,
    )
    out = Path(args.out) if args.out else REPO / "output" / f"bbot_{coin}_spreads.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(out), include_plotlyjs=True, full_html=True)
    plotted = len(_downsample_for_plot(ticks, max_points=args.max_points))
    print(f"ticks={len(ticks)} line_points={plotted} intents={len(intents)} trades={len(trades)}")
    print(f"closed={closed_n} eod_open={eod_n}")
    print(f"wrote {out}")
    miss_notes = [n for n in pair_notes if n.startswith("fill_lookup_miss")]
    other_notes = journal_notes + [n for n in pair_notes if not n.startswith("fill_lookup_miss")]
    hit = len(intents) - len(miss_notes)
    print(f"fill_lookup_hit={hit} miss={len(miss_notes)}")
    for note in other_notes:
        print(f"note: {note}")
    for note in miss_notes[:5]:
        print(f"note: {note}")
    if len(miss_notes) > 5:
        print(f"note: … {len(miss_notes) - 5} more fill_lookup_miss")
    if miss_notes and hit == 0 and not args.demo:
        print(backup_pull_hint(_parse_day(args.start), _parse_day(args.end) + timedelta(days=1)))
    print(
        "Y is spread percent on D ticks (or journal extra if lookup missed). "
        "Journal fill_price is L1 and was not plotted. Not a PnL claim."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
