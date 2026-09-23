#!/usr/bin/env python3
"""Rewrite output/lean_ticks dropping stale-cross rows (collector fail-closed).

In-place, one file at a time. Files already written after the 2026-08-16 gate
(0% skew/age violations) are left untouched if they still read as lean parquet.

Corrupt / unreadable files are deleted (honest hole) unless --refetch is set.
Does not touch collector, D trees, or bars.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.schema.lean_event import LEAN_TICK_BODY_COLS
from app.utils.tick_validity import DEFAULT_AGE_MAX_MS, DEFAULT_SKEW_MAX_MS

TICKS_DIR = REPO / "output" / "lean_ticks"
# First full UTC day with 0% stale-cross in the local pull.
CLEAN_FROM_DAY = "20260816"
VPS_HOST = "root@38.180.94.108"
VPS_DIR = "/root/mac_lean_pull"


def _log(msg: str) -> None:
    print(msg, flush=True)


def _keep_mask(table: pa.Table) -> pa.BooleanArray:
    okx_ts = pc.cast(table["okx_ts_ms"], pa.float64())
    bybit_ts = pc.cast(table["bybit_ts_ms"], pa.float64())
    calc = pc.cast(table["calc_local_ts_ms"], pa.float64())
    okx_recv = pc.cast(table["okx_local_recv_ts_ms"], pa.float64())
    bybit_recv = pc.cast(table["bybit_local_recv_ts_ms"], pa.float64())
    skew = pc.abs(pc.subtract(okx_ts, bybit_ts))
    age_okx = pc.subtract(calc, okx_recv)
    age_bybit = pc.subtract(calc, bybit_recv)
    finite = pc.and_(
        pc.and_(pc.is_valid(okx_ts), pc.is_valid(bybit_ts)),
        pc.and_(
            pc.and_(pc.is_valid(calc), pc.is_valid(okx_recv)),
            pc.is_valid(bybit_recv),
        ),
    )
    skew_ok = pc.less_equal(skew, pa.scalar(float(DEFAULT_SKEW_MAX_MS), type=pa.float64()))
    age_ok = pc.and_(
        pc.less_equal(age_okx, pa.scalar(float(DEFAULT_AGE_MAX_MS), type=pa.float64())),
        pc.less_equal(age_bybit, pa.scalar(float(DEFAULT_AGE_MAX_MS), type=pa.float64())),
    )
    age_floor = pc.and_(
        pc.greater_equal(age_okx, pa.scalar(-1000.0, type=pa.float64())),
        pc.greater_equal(age_bybit, pa.scalar(-1000.0, type=pa.float64())),
    )
    return pc.and_(finite, pc.and_(skew_ok, pc.and_(age_ok, age_floor)))


def _is_lean(path: Path) -> bool:
    try:
        return list(pq.read_schema(path).names) == list(LEAN_TICK_BODY_COLS)
    except Exception:
        return False


def _refetch(name: str, dest: Path) -> bool:
    staged = dest.with_name(f".{dest.name}.refetch.tmp")
    staged.unlink(missing_ok=True)
    cmd = [
        "scp",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=20",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=4",
        f"{VPS_HOST}:{VPS_DIR}/{name}",
        str(staged),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=180)
    except Exception as exc:
        staged.unlink(missing_ok=True)
        _log(f"  refetch failed {name}: {exc}")
        return False
    if not _is_lean(staged):
        staged.unlink(missing_ok=True)
        _log(f"  refetch not lean {name}")
        return False
    os.replace(staged, dest)
    return True


def _rewrite(path: Path) -> str:
    table = pq.read_table(path, columns=list(LEAN_TICK_BODY_COLS))
    n0 = table.num_rows
    if n0 == 0:
        return "empty"
    keep = _keep_mask(table)
    n1 = int(pc.sum(keep).as_py() or 0)
    if n1 == n0:
        return "clean"
    if n1 == 0:
        path.unlink()
        return f"dropped_all:{n0}"
    filtered = table.filter(keep)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        pq.write_table(filtered, tmp, compression="zstd")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return f"rewrote:{n0}->{n1}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticks-dir", type=Path, default=TICKS_DIR)
    parser.add_argument("--refetch-bad", action="store_true")
    parser.add_argument("--force-all", action="store_true",
                        help="Also rewrite files on/after CLEAN_FROM_DAY")
    args = parser.parse_args()
    ticks_dir: Path = args.ticks_dir
    files = sorted(ticks_dir.glob("spread_*.parquet"))
    _log(f"files={len(files)} dir={ticks_dir} skew/age<{DEFAULT_SKEW_MAX_MS}/{DEFAULT_AGE_MAX_MS}ms")

    t0 = time.time()
    stats = {
        "clean_skip": 0,
        "already_new_writer": 0,
        "rewrote": 0,
        "dropped_all": 0,
        "deleted_bad": 0,
        "refetched": 0,
        "rows_in": 0,
        "rows_out": 0,
        "bad": [],
    }
    for i, path in enumerate(files, 1):
        day = path.name[7:15]
        if not _is_lean(path):
            stats["bad"].append(path.name)
            if args.refetch_bad and _refetch(path.name, path):
                stats["refetched"] += 1
                _log(f"  refetched {path.name}")
            else:
                path.unlink(missing_ok=True)
                stats["deleted_bad"] += 1
                _log(f"  deleted unreadable {path.name}")
            continue
        if day >= CLEAN_FROM_DAY and not args.force_all:
            stats["already_new_writer"] += 1
            if i % 200 == 0 or i == len(files):
                _log(f"  [{i}/{len(files)}] skip-new-writer {path.name}")
            continue
        try:
            action = _rewrite(path)
        except Exception as exc:
            stats["bad"].append(path.name)
            _log(f"  rewrite error {path.name}: {exc}")
            continue
        if action == "clean":
            stats["clean_skip"] += 1
        elif action.startswith("rewrote:"):
            stats["rewrote"] += 1
            a, b = action.split(":", 1)[1].split("->")
            stats["rows_in"] += int(a)
            stats["rows_out"] += int(b)
        elif action.startswith("dropped_all:"):
            stats["dropped_all"] += 1
            stats["rows_in"] += int(action.split(":")[1])
        if i % 50 == 0 or i == len(files) or action.startswith("rewrote") or action.startswith("dropped"):
            elapsed = time.time() - t0
            _log(
                f"  [{i}/{len(files)}] {action} {path.name} "
                f"rewrote={stats['rewrote']} {elapsed:.0f}s"
            )

    report = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ticks_dir": str(ticks_dir),
        "clean_from_day": CLEAN_FROM_DAY,
        "thresholds_ms": {"skew": DEFAULT_SKEW_MAX_MS, "age": DEFAULT_AGE_MAX_MS},
        **stats,
    }
    out = ticks_dir / "_fail_closed_filtered.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    _log(f"wrote {out}")
    _log(json.dumps({k: stats[k] for k in stats if k != "bad"}, indent=2))
    if stats["bad"]:
        _log("bad/unreadable: " + ", ".join(stats["bad"][:20]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
