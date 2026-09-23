#!/usr/bin/env python3
"""Download chunks in parallel using concurrent tar streams over SSH."""

import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEST = REPO / "output" / "lean_ticks"
DEST.mkdir(parents=True, exist_ok=True)

N_CHUNKS = 6
VPS_HOST = "root@38.180.94.108"
VPS_DIR = "/root/mac_lean_pull"

procs = []
print(f"Launching {N_CHUNKS} parallel tar streams to {DEST}...", flush=True)

t0 = time.time()
initial_files = len(list(DEST.glob("spread_*.parquet")))

for i in range(N_CHUNKS):
    chunk_vps = f"/tmp/chunk_{i}.txt"
    cmd = (
        f'ssh -o BatchMode=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=6 {VPS_HOST} '
        f'"tar -C {VPS_DIR} -cf - -T {chunk_vps}" | tar -C {DEST} -xf -'
    )
    p = subprocess.Popen(cmd, shell=True)
    procs.append((i, p))

print(f"All {N_CHUNKS} streams started. Monitoring progress...", flush=True)

while True:
    running = [i for i, p in procs if p.poll() is None]
    current_files = len(list(DEST.glob("spread_*.parquet")))
    elapsed = time.time() - t0
    added = current_files - initial_files
    rate = added / elapsed if elapsed > 0 else 0
    print(
        f"[{elapsed:5.1f}s] Total files: {current_files} (+{added}), Rate: {rate:4.1f} files/s, Active streams: {len(running)}/{N_CHUNKS}",
        flush=True,
    )
    if not running:
        break
    time.sleep(10)

exit_codes = [p.poll() for _, p in procs]
print(f"\nAll streams finished in {time.time() - t0:.1f}s. Exit codes: {exit_codes}", flush=True)
PY