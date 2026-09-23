#!/usr/bin/env python3
"""Hour-by-hour subprocess driver for gear22 signal-fill (OOM-safe).

Usage:
  python3 research/run_gear22_hourly.py           # all August segments
  python3 research/run_gear22_hourly.py --one-hour  # process next missing hour only
"""

from __future__ import annotations

import argparse
import gc
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "research" / "output" / "gear22_signal_fill"
PARTS = OUT / "parts"


def _segments():
    sys.path.insert(0, str(REPO))
    from research.gear22_signal_fill_lib import AUGUST_SEGMENTS, parse_ts_ms

    hours = []
    for start_iso, end_iso in AUGUST_SEGMENTS:
        a = parse_ts_ms(start_iso)
        b = parse_ts_ms(end_iso)
        t = a
        while t < b:
            u = min(t + 3_600_000, b)
            hours.append((t, u))
            t = u
    return hours


def _run_one(idx: int, a: int, b: int) -> None:
    """Child entry: write parts/part_XXXX.parquet for [a,b)."""
    sys.path.insert(0, str(REPO))
    from research.gear22_signal_fill_lib import RunConfig, process_window

    PARTS.mkdir(parents=True, exist_ok=True)
    path = PARTS / f"part_{idx:04d}.parquet"
    if path.exists():
        print(f"exists {path}", flush=True)
        return
    cfg = RunConfig(workers=2, out_dir=OUT)
    load_a = a - cfg.lookback_ms
    load_b = b + int(max(cfg.latencies_ms) + cfg.gap_slack_ms + 50)
    print(
        f"hour {idx} "
        f"{datetime.fromtimestamp(a/1000, tz=timezone.utc).isoformat()} → "
        f"{datetime.fromtimestamp(b/1000, tz=timezone.utc).isoformat()}",
        flush=True,
    )
    ev = process_window(load_a, load_b, cfg)
    if len(ev):
        ev = ev.loc[(ev["signal_ts_ms"] >= a) & (ev["signal_ts_ms"] < b)]
    if len(ev):
        ev.to_parquet(path, index=False)
        print(f"wrote {len(ev)} → {path}", flush=True)
    else:
        # touch empty marker as empty parquet with schema? skip — leave missing
        print("empty hour", flush=True)
    del ev
    gc.collect()


def _concat_and_summarize() -> str:
    sys.path.insert(0, str(REPO))
    import pandas as pd
    from research.gear22_signal_fill_lib import (
        AUGUST_SEGMENTS,
        LATENCIES_MS,
        write_summaries,
    )

    part_files = sorted(PARTS.glob("part_*.parquet"))
    if not part_files:
        raise SystemExit("no parts")
    out_path = OUT / "events.parquet"
    print(f"concat {len(part_files)} → {out_path}", flush=True)
    frames = []
    batch = []
    for p in part_files:
        batch.append(pd.read_parquet(p))
        if len(batch) >= 24:
            frames.append(pd.concat(batch, ignore_index=True))
            batch = []
            gc.collect()
    if batch:
        frames.append(pd.concat(batch, ignore_index=True))
    pd.concat(frames, ignore_index=True).to_parquet(out_path, index=False)
    meta = {
        "segments": AUGUST_SEGMENTS,
        "n_parts": len(part_files),
        "latencies_ms": list(LATENCIES_MS),
        "driver": "run_gear22_hourly.py",
    }
    (OUT / "run_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return write_summaries(out_path, OUT)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", nargs=3, type=int, metavar=("IDX", "A", "B"))
    ap.add_argument("--one-hour", action="store_true")
    ap.add_argument("--summarize-only", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    PARTS.mkdir(parents=True, exist_ok=True)

    if args.child:
        _run_one(args.child[0], args.child[1], args.child[2])
        return 0
    if args.summarize_only:
        print("VERDICT:", _concat_and_summarize(), flush=True)
        return 0

    hours = _segments()
    print(f"total hours {len(hours)}", flush=True)
    for idx, (a, b) in enumerate(hours):
        path = PARTS / f"part_{idx:04d}.parquet"
        if path.exists():
            print(f"skip {path.name}", flush=True)
            if args.one_hour:
                continue
            continue
        import os

        cmd = [
            sys.executable,
            "-u",
            str(Path(__file__).resolve()),
            "--child",
            str(idx),
            str(a),
            str(b),
        ]
        env = os.environ.copy()
        env["PYTHONWARNINGS"] = "ignore"
        env["PYTHONPATH"] = str(REPO)
        print("spawn", " ".join(cmd[-5:]), flush=True)
        r = subprocess.run(cmd, cwd=str(REPO), env=env)
        if r.returncode != 0:
            print(f"child failed rc={r.returncode} hour={idx}", flush=True)
        if args.one_hour:
            break
    else:
        # completed all hours (no --one-hour break)
        if not args.one_hour:
            print("VERDICT:", _concat_and_summarize(), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
