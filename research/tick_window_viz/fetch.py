"""Load one coin × window of lean ticks from a local dir or via SSH.

Local path reuses ``research.lean_ticks_io``. SSH runs a self-contained
filter on the VPS and returns only the coin slice (parquet on stdout).
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import pyarrow.parquet as pq

from research.lean_ticks_io import (
    list_lean_files_overlapping,
    read_lean_raw,
)
from research.tick_fail_closed import fail_closed_ok
from research.tick_window_viz.delay import attach_venue_latency
from research.tick_window_viz.window import (
    compacted_names_covering,
    is_five_min_aligned,
    normalize_coin,
    parse_window_start,
    window_bounds_ms,
)

REPO = Path(__file__).resolve().parents[2]
DEFAULT_TICKS_ROOT = REPO / "output" / "lean_ticks"
DEFAULT_VPS_HOST = "root@38.180.94.108"
DEFAULT_VPS_PYTHON = "/root/venv/bin/python"
DEFAULT_REMOTE_ROOTS = (
    "/data/experiments/gear22_ticks_sept",
    "/data/compacted",
    "/data/compacted/sent",
)
DEFAULT_HIVE_ROOT = "/data/live"
DEFAULT_BACKUP_URI = "backup1tb:spread-compacted"
DEFAULT_RCLONE_BIN = "/opt/rclone-1.74.4/rclone"

READ_COLS = (
    "event_local_ts_ms",
    "base_coin",
    "trigger",
    "okx_bid_price",
    "okx_ask_price",
    "bybit_bid_price",
    "bybit_ask_price",
    "calc_local_ts_ms",
    "okx_local_recv_ts_ms",
    "okx_ts_ms",
    "bybit_local_recv_ts_ms",
    "bybit_ts_ms",
)

# Self-contained: VPS does not need this repo on PYTHONPATH.
_REMOTE_SCRIPT = r"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

cfg = json.loads(sys.stdin.read())
coin = str(cfg["coin"]).upper()
start_ms = int(cfg["start_ms"])
end_ms = int(cfg["end_ms"])
roots = list(cfg.get("roots") or [])
hive_root = cfg.get("hive_root") or ""
backup_uri = cfg.get("backup_uri") or ""
rclone_bin = cfg.get("rclone_bin") or "/opt/rclone-1.74.4/rclone"
names = list(cfg.get("names") or [])
cols = list(cfg["columns"])
tmp_files = []


def parse_name(path: str):
    name = os.path.basename(path)
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


def read_filtered(path: str):
    try:
        schema = pq.read_schema(path).names
        use = [c for c in cols if c in schema]
        if "event_local_ts_ms" not in use or "base_coin" not in use:
            return None
        table = pq.read_table(path, columns=use)
    except Exception as exc:
        print(f"skip {path}: {exc}", file=sys.stderr)
        return None
    if table.num_rows == 0:
        return None
    ts = pc.cast(pc.floor(pc.cast(table["event_local_ts_ms"], pa.float64())), pa.int64())
    bc = table["base_coin"]
    if pa.types.is_dictionary(bc.type):
        bc = bc.dictionary_decode()
    keep = pc.and_(
        pc.greater_equal(ts, start_ms),
        pc.and_(pc.less(ts, end_ms), pc.equal(pc.utf8_upper(bc), coin)),
    )
    table = table.filter(keep)
    return table if table.num_rows else None


found = []
for root in roots:
    if not os.path.isdir(root):
        continue
    for name in names:
        cand = os.path.join(root, name)
        if os.path.isfile(cand):
            found.append(cand)

if not found:
    for root in roots:
        if not os.path.isdir(root):
            continue
        for cand in sorted(glob.glob(os.path.join(root, "spread_*.parquet"))):
            win = parse_name(cand)
            if win is None:
                continue
            a, b = win
            if a < end_ms and b > start_ms:
                found.append(cand)

if not found and hive_root:
    dates = set()
    for ms in (start_ms, max(start_ms, end_ms - 1)):
        dates.add(datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d"))
    for day in sorted(dates):
        hive = os.path.join(hive_root, f"base_coin={coin}", f"event_date={day}")
        found.extend(sorted(glob.glob(os.path.join(hive, "**", "*.parquet"), recursive=True)))

if not found and backup_uri and names:
    os.makedirs("/tmp/tick_window_viz", exist_ok=True)
    for name in names:
        dest = os.path.join("/tmp/tick_window_viz", name)
        src = backup_uri.rstrip("/") + "/" + name
        rc = subprocess.run(
            [rclone_bin, "copyto", src, dest],
            capture_output=True,
            text=True,
        )
        if rc.returncode == 0 and os.path.isfile(dest):
            found.append(dest)
            tmp_files.append(dest)
        else:
            err = (rc.stderr or rc.stdout or "").strip().splitlines()
            tail = err[-3:] if err else ["rclone copyto failed"]
            print(f"rclone {name}: {tail}", file=sys.stderr)

tables = []
used = []
try:
    for path in found:
        part = read_filtered(path)
        if part is None:
            continue
        tables.append(part)
        used.append(path)
    if not tables:
        print(
            json.dumps({"ok": False, "error": "no rows", "candidates": found}),
            file=sys.stderr,
        )
        sys.exit(2)
    table = pa.concat_tables(tables, promote_options="permissive")
    print(
        json.dumps(
            {
                "ok": True,
                "rows": int(table.num_rows),
                "files": used,
                "candidates": found,
            }
        ),
        file=sys.stderr,
    )
    pq.write_table(table, sys.stdout.buffer, compression="zstd")
finally:
    for path in tmp_files:
        try:
            os.unlink(path)
        except OSError:
            pass
"""


def derive_spreads(raw: pd.DataFrame, *, apply_fail_closed: bool = False) -> pd.DataFrame:
    """Compute lean long/short spreads. Does not interpolate holes."""
    df = raw.copy()
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
        raise KeyError(f"slice missing {missing}; columns={list(df.columns)}")

    df["base_coin"] = df["base_coin"].astype(str).str.upper()
    df["event_local_ts_ms"] = pd.to_numeric(df["event_local_ts_ms"], errors="coerce")
    df = df.dropna(subset=["event_local_ts_ms"])
    df["event_local_ts_ms"] = df["event_local_ts_ms"].round().astype("int64")
    for col in (
        "okx_bid_price",
        "okx_ask_price",
        "bybit_bid_price",
        "bybit_ask_price",
    ):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    ok = (
        df["okx_bid_price"].notna()
        & df["okx_ask_price"].notna()
        & df["bybit_bid_price"].notna()
        & df["bybit_ask_price"].notna()
        & (df["bybit_bid_price"] > 0)
        & (df["okx_bid_price"] > 0)
    )
    df = df.loc[ok.fillna(False)].copy()
    n_l1 = len(df)
    n_fc = 0
    if apply_fail_closed:
        keep = fail_closed_ok(df)
        n_fc = n_l1 - int(keep.sum())
        df = df.loc[keep].copy()
    df["spread_long"] = (
        (df["bybit_bid_price"] - df["okx_ask_price"]) / df["bybit_bid_price"] * 100.0
    )
    df["spread_short"] = (
        (df["okx_bid_price"] - df["bybit_ask_price"]) / df["okx_bid_price"] * 100.0
    )
    if "trigger" in df.columns:
        df["trigger"] = df["trigger"].astype(str).str.lower()
    df = attach_venue_latency(df)
    df["event_dt"] = pd.to_datetime(df["event_local_ts_ms"], unit="ms", utc=True)
    df.attrs["n_l1"] = n_l1
    df.attrs["n_fail_closed_drop"] = n_fc
    return df.sort_values("event_local_ts_ms").reset_index(drop=True)


def _local_available(ticks_root: Path, start_ms: int, end_ms: int) -> bool:
    try:
        return bool(list_lean_files_overlapping(ticks_root, start_ms, end_ms))
    except OSError:
        return False


def _read_local(
    ticks_root: Path,
    coin: str,
    start_ms: int,
    end_ms: int,
) -> tuple[pd.DataFrame, list[Path]]:
    raw, files = read_lean_raw(
        ticks_root,
        start_ms,
        end_ms,
        coins={coin},
        columns=list(READ_COLS),
    )
    return raw, files


def _read_ssh(
    *,
    host: str,
    vps_python: str,
    coin: str,
    start_ms: int,
    end_ms: int,
    names: list[str],
    remote_roots: list[str],
    hive_root: str,
    backup_uri: str,
    rclone_bin: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    cfg = {
        "coin": coin,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "names": names,
        "roots": remote_roots,
        "hive_root": hive_root,
        "backup_uri": backup_uri,
        "rclone_bin": rclone_bin,
        "columns": list(READ_COLS),
    }
    script = _REMOTE_SCRIPT.replace(
        "cfg = json.loads(sys.stdin.read())",
        "cfg = json.loads(" + json.dumps(json.dumps(cfg)) + ")",
        1,
    )
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=15",
        host,
        vps_python,
        "-",
    ]
    proc = subprocess.run(cmd, input=script.encode(), capture_output=True)
    err_text = proc.stderr.decode("utf-8", errors="replace")
    meta_line = ""
    for line in reversed(err_text.splitlines()):
        if line.startswith("{") and '"ok"' in line:
            meta_line = line
            break
    remote_meta: dict[str, Any] = {}
    if meta_line:
        try:
            remote_meta = json.loads(meta_line)
        except json.JSONDecodeError:
            remote_meta = {"raw": meta_line}
    if proc.returncode != 0:
        raise RuntimeError(
            "SSH slice failed "
            f"(exit {proc.returncode}). {err_text[-1500:] or 'no stderr'}"
        )
    if not proc.stdout:
        raise RuntimeError(f"SSH returned empty slice. {err_text[-1500:]}")
    table = pq.read_table(io.BytesIO(proc.stdout))
    return table.to_pandas(), remote_meta


def load_tick_window(
    coin: str,
    window_start,
    *,
    minutes: int = 5,
    source: str = "auto",
    ticks_root: Optional[Path] = None,
    vps_host: str = DEFAULT_VPS_HOST,
    vps_python: str = DEFAULT_VPS_PYTHON,
    remote_roots: Optional[list[str]] = None,
    hive_root: str = DEFAULT_HIVE_ROOT,
    backup_uri: str = DEFAULT_BACKUP_URI,
    rclone_bin: str = DEFAULT_RCLONE_BIN,
    apply_fail_closed: bool = False,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Return prepared ticks and a meta dict. Never copies the backup tree here.

    ``source``:
    - ``local`` — ``ticks_root`` compacted ``spread_*.parquet`` only
    - ``ssh`` — VPS filter; stream coin slice only
    - ``auto`` — local if overlapping files exist, else SSH
    """
    coin_u = normalize_coin(coin)
    start_dt = parse_window_start(window_start)
    start_ms, end_ms = window_bounds_ms(start_dt, minutes=minutes)
    root = Path(ticks_root) if ticks_root is not None else DEFAULT_TICKS_ROOT
    mode = str(source).strip().lower()
    if mode not in {"auto", "local", "ssh"}:
        raise ValueError("source must be auto, local, or ssh")
    names = compacted_names_covering(start_ms, end_ms)
    roots = list(remote_roots) if remote_roots is not None else list(DEFAULT_REMOTE_ROOTS)

    used_mode = mode
    raw: pd.DataFrame
    files: list[Any] = []
    remote_meta: dict[str, Any] = {}
    if mode == "auto":
        used_mode = "local" if _local_available(root, start_ms, end_ms) else "ssh"

    if used_mode == "local":
        raw, files = _read_local(root, coin_u, start_ms, end_ms)
    else:
        raw, remote_meta = _read_ssh(
            host=vps_host,
            vps_python=vps_python,
            coin=coin_u,
            start_ms=start_ms,
            end_ms=end_ms,
            names=names,
            remote_roots=roots,
            hive_root=hive_root,
            backup_uri=backup_uri,
            rclone_bin=rclone_bin,
        )
        files = list(remote_meta.get("files") or [])

    df = derive_spreads(raw, apply_fail_closed=apply_fail_closed)
    meta = {
        "coin": coin_u,
        "window_start_utc": start_dt.isoformat().replace("+00:00", "Z"),
        "window_end_utc": pd.Timestamp(end_ms, unit="ms", tz="UTC")
        .isoformat()
        .replace("+00:00", "Z"),
        "window_start_ms": int(start_ms),
        "window_end_ms": int(end_ms),
        "minutes": int(minutes),
        "aligned_5m": is_five_min_aligned(start_dt),
        "source": used_mode,
        "ticks_root": str(root),
        "files": [str(p) for p in files],
        "n_raw": int(len(raw)),
        "n_plot": int(len(df)),
        "n_fail_closed_drop": int(df.attrs.get("n_fail_closed_drop") or 0),
        "expected_compacted": names,
        "remote": remote_meta,
    }
    return df, meta
