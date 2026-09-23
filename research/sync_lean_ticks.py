#!/usr/bin/env python3
"""Sync remaining compacted 5-minute parquet files from VPS /root/mac_lean_pull to Mac output/lean_ticks.

Features:
- Parallel download with configurable workers (default 6).
- Automatic conversion of 25-column v1 files into 16-column lean format on the fly.
- Direct installation into output/lean_ticks.
- Resumable: skips already existing valid lean files in dest.
- Graceful retry on network glitches.
- Progress logging.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.schema.lean_event import LEAN_TICK_BODY_COLS

BOOK_COLS = (
    "okx_bid_price",
    "okx_ask_price",
    "bybit_bid_price",
    "bybit_ask_price",
)


def schema_ok(path: Path) -> tuple[bool, list[str]]:
    try:
        names = list(pq.read_schema(path).names)
    except Exception:
        return False, []
    missing = [c for c in LEAN_TICK_BODY_COLS if c not in names]
    if missing:
        return False, names
    if any(c not in names for c in BOOK_COLS):
        return False, names
    return True, names


def is_lean_only(names: list[str]) -> bool:
    return list(names) == list(LEAN_TICK_BODY_COLS) or set(names) == set(LEAN_TICK_BODY_COLS)


def rewrite_to_lean(src: Path, dest: Path) -> bool:
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    try:
        pf = pq.ParquetFile(src)
        writer: pq.ParquetWriter | None = None
        for batch in pf.iter_batches(columns=list(LEAN_TICK_BODY_COLS), batch_size=65536):
            if writer is None:
                writer = pq.ParquetWriter(where=str(tmp), schema=batch.schema, compression="zstd")
            writer.write_batch(batch)
        if writer is not None:
            writer.close()
        if not tmp.exists():
            return False
        os.replace(tmp, dest)
        return True
    except Exception as e:
        print(f"Error rewriting {src.name} to lean: {e}", flush=True)
        return False
    finally:
        tmp.unlink(missing_ok=True)


def fetch_file(filename: str, vps_host: str, vps_dir: str, staging_dir: Path, dest_dir: Path) -> tuple[str, bool, str]:
    dest = dest_dir / filename
    if dest.exists():
        ok, _ = schema_ok(dest)
        if ok:
            return filename, True, "already_exists"

    staged = staging_dir / filename
    for attempt in range(3):
        cmd = [
            "scp",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4",
            f"{vps_host}:{vps_dir}/{filename}",
            str(staged),
        ]
        res = subprocess.run(cmd, capture_output=True)
        if res.returncode == 0 and staged.exists():
            break
        time.sleep(1 + attempt * 2)
    else:
        return filename, False, f"scp_failed: {res.stderr.decode(errors='replace').strip()}"

    ok, names = schema_ok(staged)
    if not ok:
        staged.unlink(missing_ok=True)
        return filename, False, "invalid_schema"

    if is_lean_only(names):
        os.replace(staged, dest)
        return filename, True, "lean"
    else:
        success = rewrite_to_lean(staged, dest)
        staged.unlink(missing_ok=True)
        if success:
            return filename, True, "rewritten_to_lean"
        else:
            return filename, False, "rewrite_failed"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vps", default="root@38.180.94.108")
    parser.add_argument("--vps-dir", default="/root/mac_lean_pull")
    parser.add_argument("--dest", type=Path, default=REPO / "output" / "lean_ticks")
    parser.add_argument("--staging", type=Path, default=REPO / "output" / "_backup_pull")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    dest_dir = args.dest
    staging_dir = args.staging
    dest_dir.mkdir(parents=True, exist_ok=True)
    staging_dir.mkdir(parents=True, exist_ok=True)

    # 1. Get list of files on VPS
    print(f"Fetching file list from VPS {args.vps}:{args.vps_dir}...", flush=True)
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ServerAliveInterval=15",
        args.vps,
        f"ls {args.vps_dir}/spread_*.parquet",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Failed to list VPS files: {res.stderr}", file=sys.stderr)
        return 1

    vps_files = [os.path.basename(line.strip()) for line in res.stdout.splitlines() if line.strip().endswith(".parquet")]
    print(f"Total files available on VPS: {len(vps_files)}", flush=True)

    # 2. Filter out what already exists locally in dest_dir
    local_files = set(f.name for f in dest_dir.glob("spread_*.parquet"))
    needed = [f for f in vps_files if f not in local_files]
    if args.limit:
        needed = needed[:args.limit]

    print(f"Already in {dest_dir}: {len(local_files)} files", flush=True)
    print(f"Files to download: {len(needed)}", flush=True)

    if not needed:
        print("All files already downloaded and present in dest! Nothing to do.")
        return 0

    t0 = time.time()
    completed = 0
    errors = 0
    total = len(needed)

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(fetch_file, f, args.vps, args.vps_dir, staging_dir, dest_dir): f
            for f in needed
        }
        for future in as_completed(futures):
            fname, ok, reason = future.result()
            completed += 1
            if not ok:
                errors += 1
                print(f"[{completed}/{total}] ERROR {fname}: {reason}", flush=True)
            elif completed % 25 == 0 or completed == total:
                elapsed = time.time() - t0
                speed = completed / elapsed if elapsed > 0 else 0
                eta = (total - completed) / speed if speed > 0 else 0
                print(
                    f"[{completed}/{total} - {completed/total*100:.1f}%] "
                    f"Speed: {speed:.1f} files/s, ETA: {eta/60:.1f}m, Errors: {errors}",
                    flush=True,
                )

    total_time = time.time() - t0
    print(f"\nCompleted sync in {total_time/60:.1f} minutes. Processed: {completed}, Errors: {errors}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
