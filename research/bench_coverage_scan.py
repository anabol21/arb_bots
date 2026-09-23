"""Read-only sampled scan: per coin per hour 1 Hz p50 coverage on local lean ticks.

Reads only ``event_local_ts_ms`` + ``base_coin`` (cheap). Applies the locked
gear-2.2 mass/tick rule analytically instead of running rolling_tw_p50.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from research.gear22_quiet_regime_viz.load import (
    list_compacted_overlapping,
    parse_since_ms,
)

W = 60_000
MASS_FRAC = 0.20
HOUR_MS = 3_600_000


def hour_coverage(ts: np.ndarray, h0: int) -> float:
    grid = np.arange(h0, h0 + HOUR_MS, 1000, dtype="int64")
    left = np.searchsorted(ts, grid - W, side="left")
    right = np.searchsorted(ts, grid, side="right")
    early = np.searchsorted(ts, grid - int(MASS_FRAC * W), side="right")
    return float(((right - left >= 2) & (early - left >= 1)).mean())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default="output/lean_ticks")
    ap.add_argument("--coins", required=True)
    ap.add_argument("--days", required=True, help="comma list of YYYY-MM-DD")
    ap.add_argument("--min-cov", type=float, default=0.95)
    args = ap.parse_args()

    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    cset = pa.array(sorted(coins))
    out: dict = {"min_cov": args.min_cov, "days": {}, "read_s": 0.0}

    for day in args.days.split(","):
        day = day.strip()
        d0 = parse_since_ms(f"{day}T00:00:00Z")
        d1 = d0 + 24 * HOUR_MS
        t0 = time.perf_counter()
        paths = list_compacted_overlapping(Path(args.data_root), d0, d1)
        per_coin: dict[str, list[np.ndarray]] = {c: [] for c in coins}
        for p in paths:
            t = pq.read_table(p, columns=["event_local_ts_ms", "base_coin"])
            bc = t["base_coin"]
            if pa.types.is_dictionary(bc.type):
                bc = bc.dictionary_decode()
            bcu = pc.utf8_upper(bc)
            t = t.append_column("c", bcu).filter(pc.is_in(bcu, cset))
            if t.num_rows == 0:
                continue
            ts_all = pc.cast(pc.floor(pc.cast(t["event_local_ts_ms"], pa.float64())),
                             pa.int64()).to_numpy(zero_copy_only=False)
            cc = t["c"].to_numpy(zero_copy_only=False)
            for c in np.unique(cc):
                per_coin[str(c)].append(ts_all[cc == c])
        read_s = time.perf_counter() - t0
        out["read_s"] += read_s

        day_rec: dict = {"files": len(paths), "read_s": round(read_s, 2), "coins": {}}
        for c in coins:
            chunks = [a for a in per_coin[c] if a.size]
            if not chunks:
                day_rec["coins"][c] = {"present": False}
                continue
            ts = np.sort(np.concatenate(chunks))
            covs = [hour_coverage(ts, d0 + h * HOUR_MS) for h in range(24)]
            covs_a = np.asarray(covs)
            day_rec["coins"][c] = {
                "present": True,
                "ticks": int(ts.size),
                "ticks_per_s": round(ts.size / 86400.0, 2),
                "mean_cov": round(float(covs_a.mean()), 4),
                "hours_ok": int((covs_a >= args.min_cov).sum()),
            }
        out["days"][day] = day_rec

    # Aggregate.
    agg: dict[str, dict] = {}
    for day, rec in out["days"].items():
        for c, r in rec["coins"].items():
            a = agg.setdefault(c, {"hours_ok": 0, "hours_total": 0, "ticks": 0,
                                   "days_present": 0})
            a["hours_total"] += 24
            if r.get("present"):
                a["days_present"] += 1
                a["hours_ok"] += r["hours_ok"]
                a["ticks"] += r["ticks"]
    for c, a in agg.items():
        a["frac_hours_ok"] = round(a["hours_ok"] / a["hours_total"], 4)
        a["ticks_per_s"] = round(a["ticks"] / (86400.0 * max(a["days_present"], 1)), 2)
    out["aggregate"] = agg
    tot_ok = sum(a["hours_ok"] for a in agg.values())
    tot = sum(a["hours_total"] for a in agg.values())
    out["overall_frac_hours_ok"] = round(tot_ok / tot, 4) if tot else None
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
