#!/usr/bin/env python3
"""Read-only backup tick audit: listing coverage + sampled parquet from rclone.

Does not delete remote data. Sample downloads land under --verify-dir.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq

LEAN_COLS = [
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
]
WINDOW_FMT = "%Y%m%dT%H%M%SZ"
SKEW_MAX_MS = 2000
AGE_MAX_MS = 2000
FAIL_CLOSED_TS = datetime(2026, 8, 15, 17, 30, 12, tzinfo=timezone.utc)


def parse_name(name: str) -> tuple[datetime, datetime]:
    body = name[len("spread_") : -len(".parquet")]
    a, b = body.split("_")
    t0 = datetime.strptime(a, WINDOW_FMT).replace(tzinfo=timezone.utc)
    t1 = datetime.strptime(b, WINDOW_FMT).replace(tzinfo=timezone.utc)
    return t0, t1


def rclone_copyto(rclone: str, key: str, remote: str, name: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    cmd = [
        rclone,
        "copyto",
        f"{remote}/{name}",
        str(dest),
        "--timeout",
        "180s",
        "--retries",
        "2",
        "--contimeout",
        "30s",
        "--sftp-key-file",
        key,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-800:] or proc.stdout[-800:])


def analyze_file(path: Path) -> dict:
    out: dict = {"name": path.name, "bytes": path.stat().st_size}
    try:
        schema = pq.read_schema(path)
    except Exception as exc:
        out["unreadable"] = str(exc)
        return out
    names = list(schema.names)
    out["columns"] = names
    out["n_cols"] = len(names)
    out["schema_kind"] = (
        "lean" if names == LEAN_COLS else ("v1_superset" if set(LEAN_COLS) <= set(names) else "other")
    )
    missing = [c for c in LEAN_COLS if c not in names]
    extra = [c for c in names if c not in LEAN_COLS]
    out["missing_lean"] = missing
    out["extra_cols"] = extra
    pf = pq.ParquetFile(path)
    out["rows"] = int(pf.metadata.num_rows)
    if out["rows"] == 0:
        return out
    cols = [c for c in LEAN_COLS if c in names]
    table = pf.read(columns=cols)
    nulls = {}
    for c in cols:
        nulls[c] = int(pc.sum(pc.is_null(table[c])).as_py() or 0)
    out["nulls"] = {k: v for k, v in nulls.items() if v}
    if "base_coin" in names:
        out["n_coins"] = int(pc.count_distinct(table["base_coin"]).as_py())
        triggers = {}
        if "trigger" in names:
            vc = table["trigger"].value_counts()
            values = vc.field(0)
            counts = vc.field(1)
            for i in range(len(values)):
                triggers[str(values[i].as_py())] = int(counts[i].as_py())
        out["triggers"] = triggers
    needed = [
        "okx_ts_ms",
        "bybit_ts_ms",
        "okx_local_recv_ts_ms",
        "bybit_local_recv_ts_ms",
        "calc_local_ts_ms",
        "okx_bid_price",
        "okx_ask_price",
        "bybit_bid_price",
        "bybit_ask_price",
        "okx_bid_size",
        "okx_ask_size",
        "bybit_bid_size",
        "bybit_ask_size",
    ]
    if not all(c in names for c in needed):
        return out
    okx_ts = table["okx_ts_ms"].to_numpy()
    bybit_ts = table["bybit_ts_ms"].to_numpy()
    skew = abs(okx_ts - bybit_ts)
    age_okx = table["calc_local_ts_ms"].to_numpy() - table["okx_local_recv_ts_ms"].to_numpy()
    age_by = table["calc_local_ts_ms"].to_numpy() - table["bybit_local_recv_ts_ms"].to_numpy()
    age = age_okx if True else age_by
    import numpy as np

    age_max = np.maximum(age_okx, age_by)
    stale = (skew > SKEW_MAX_MS) | (age_max > AGE_MAX_MS)
    out["max_skew_ms"] = float(np.nanmax(skew)) if len(skew) else None
    out["max_age_ms"] = float(np.nanmax(age_max)) if len(age_max) else None
    out["stale_rows"] = int(np.nansum(stale))
    out["stale_pct"] = round(100.0 * out["stale_rows"] / max(out["rows"], 1), 4)
    bb = table["bybit_bid_price"].to_numpy()
    oa = table["okx_ask_price"].to_numpy()
    ob = table["okx_bid_price"].to_numpy()
    ba = table["bybit_ask_price"].to_numpy()
    ok_l = np.isfinite(bb) & np.isfinite(oa) & (bb != 0)
    ok_s = np.isfinite(ob) & np.isfinite(ba) & (ob != 0)
    sl = np.full(bb.shape, np.nan)
    ss = np.full(ob.shape, np.nan)
    sl[ok_l] = (bb[ok_l] - oa[ok_l]) * 100.0 / bb[ok_l]
    ss[ok_s] = (ob[ok_s] - ba[ok_s]) * 100.0 / ob[ok_s]
    absmax = np.nanmax(np.maximum(np.abs(sl), np.abs(ss)))
    out["max_abs_spread_pct"] = None if absmax != absmax else float(absmax)
    hi = (np.abs(sl) >= 2.0) | (np.abs(ss) >= 2.0)
    out["spread_ge_2pct"] = int(np.nansum(hi))
    out["spread_ge_2pct_and_stale"] = int(np.nansum(hi & stale))
    crossed_okx = table["okx_bid_price"].to_numpy() > table["okx_ask_price"].to_numpy()
    crossed_by = table["bybit_bid_price"].to_numpy() > table["bybit_ask_price"].to_numpy()
    out["crossed_okx"] = int(np.nansum(crossed_okx))
    out["crossed_bybit"] = int(np.nansum(crossed_by))
    neg_size = 0
    for c in (
        "okx_bid_size",
        "okx_ask_size",
        "bybit_bid_size",
        "bybit_ask_size",
    ):
        neg_size += int(np.nansum(table[c].to_numpy() < 0))
    out["negative_size"] = neg_size
    lat_okx = table["okx_local_recv_ts_ms"].to_numpy() - table["okx_ts_ms"].to_numpy()
    lat_by = table["bybit_local_recv_ts_ms"].to_numpy() - table["bybit_ts_ms"].to_numpy()
    out["okx_latency_p50"] = float(np.nanpercentile(lat_okx, 50))
    out["okx_latency_p99"] = float(np.nanpercentile(lat_okx, 99))
    out["bybit_latency_p50"] = float(np.nanpercentile(lat_by, 50))
    out["bybit_latency_p99"] = float(np.nanpercentile(lat_by, 99))
    out["neg_okx_latency"] = int(np.nansum(lat_okx < 0))
    out["neg_bybit_latency"] = int(np.nansum(lat_by < 0))
    return out


def pick_samples(wins: list[tuple[datetime, datetime, str]]) -> list[str]:
    names = [n for _, _, n in wins]
    picks: list[str] = []

    def add(name: str | None) -> None:
        if name and name not in picks:
            picks.append(name)

    add(names[0])
    add(names[-1])
    # nearest to fail-closed boundary
    pre = max((w for w in wins if w[1] <= FAIL_CLOSED_TS), default=None)
    post = min((w for w in wins if w[0] >= FAIL_CLOSED_TS), default=None)
    if pre:
        add(pre[2])
    if post:
        add(post[2])
    # mid of each era
    pre_all = [w for w in wins if w[1] < FAIL_CLOSED_TS]
    post_all = [w for w in wins if w[0] >= FAIL_CLOSED_TS]
    if pre_all:
        add(pre_all[len(pre_all) // 2][2])
    if post_all:
        add(post_all[len(post_all) // 2][2])
    # known damaged local copies
    wanted = {
        "spread_20260816T052500Z_20260816T053000Z.parquet",
        "spread_20260819T113000Z_20260819T113500Z.parquet",
        "spread_20260815T173000Z_20260815T173500Z.parquet",
        "spread_20260816T000000Z_20260816T000500Z.parquet",
    }
    have = {n for *_, n in wins}
    for name in sorted(wanted):
        if name in have:
            add(name)
    return picks


def coverage(wins: list[tuple[datetime, datetime, str]]) -> dict:
    by: dict[str, list[datetime]] = defaultdict(list)
    bad_len = []
    for t0, t1, n in wins:
        by[t0.strftime("%Y-%m-%d")].append(t0)
        if (t1 - t0) != timedelta(minutes=5):
            bad_len.append({"name": n, "delta_s": (t1 - t0).total_seconds()})
    all_starts = [t0 for t0, _, _ in wins]
    holes = []
    for i in range(len(all_starts) - 1):
        gap_min = (all_starts[i + 1] - all_starts[i]).total_seconds() / 60.0
        if gap_min != 5:
            holes.append(
                {
                    "after": all_starts[i].isoformat(),
                    "next": all_starts[i + 1].isoformat(),
                    "missing_min": int(gap_min - 5),
                }
            )
    days = []
    for d in sorted(by):
        xs = sorted(by[d])
        intra = 0
        for i in range(len(xs) - 1):
            g = (xs[i + 1] - xs[i]).total_seconds() / 60.0
            if g != 5:
                intra += int(g - 5)
        days.append(
            {
                "day": d,
                "files": len(xs),
                "expected_288": 288,
                "missing_slots": max(0, 288 - len(xs)),
                "intra_gap_min": intra,
                "first": xs[0].strftime("%H:%M"),
                "last": xs[-1].strftime("%H:%M"),
            }
        )
    return {
        "n_files": len(wins),
        "first": wins[0][2] if wins else None,
        "last": wins[-1][2] if wins else None,
        "bad_window_len": bad_len,
        "n_hole_runs": len(holes),
        "missing_minutes": int(sum(h["missing_min"] for h in holes)),
        "large_holes": [h for h in holes if h["missing_min"] >= 10],
        "small_hole_runs": sum(1 for h in holes if h["missing_min"] < 10),
        "days": days,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listing", default="/tmp/backup_tick_files.txt")
    parser.add_argument("--rclone", default="/opt/rclone-1.74.4/rclone")
    parser.add_argument("--key", default="/root/.ssh/id_ed25519_uploader")
    parser.add_argument("--remote", default="backup1tb:spread-compacted")
    parser.add_argument("--verify-dir", default="/tmp/backup_tick_audit")
    parser.add_argument("--out", default="/tmp/backup_tick_audit.json")
    parser.add_argument("--skip-download", action="store_true")
    args = parser.parse_args()

    names = [
        ln.strip()
        for ln in Path(args.listing).read_text(encoding="utf-8").splitlines()
        if ln.strip().startswith("spread_") and ln.strip().endswith(".parquet")
    ]
    wins = []
    parse_bad = []
    for n in sorted(set(names)):
        try:
            t0, t1 = parse_name(n)
            wins.append((t0, t1, n))
        except Exception as exc:
            parse_bad.append({"name": n, "error": str(exc)})
    wins.sort()
    cov = coverage(wins)
    cov["parse_bad"] = parse_bad
    cov["duplicate_listing_rows"] = len(names) - len(set(names))

    samples = pick_samples(wins)
    sample_reports = []
    verify = Path(args.verify_dir)
    verify.mkdir(parents=True, exist_ok=True)
    for name in samples:
        dest = verify / name
        rec: dict = {"name": name}
        if not args.skip_download:
            try:
                rclone_copyto(args.rclone, args.key, args.remote, name, dest)
                rec["download_ok"] = True
            except Exception as exc:
                rec["download_ok"] = False
                rec["download_error"] = str(exc)[:500]
                sample_reports.append(rec)
                continue
        if dest.is_file():
            rec.update(analyze_file(dest))
        else:
            rec["missing_local"] = True
        sample_reports.append(rec)

    report = {
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "remote": args.remote,
        "fail_closed_from": FAIL_CLOSED_TS.isoformat(),
        "coverage": cov,
        "samples": sample_reports,
        "note": (
            "Listing is rclone lsf of backup1tb:spread-compacted. "
            "Samples are copyto of selected windows. Generation suppress is not in parquet."
        ),
    }
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"out": args.out, "n_files": cov["n_files"], "n_samples": len(sample_reports)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
