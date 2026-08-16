#!/usr/bin/env python3
"""Per-coin trigger-leg latency describe + p99 histogram for an L1 window."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def qstats(s: pd.Series) -> dict:
    s = pd.to_numeric(s, errors="coerce").dropna()
    if s.empty:
        return {"n": 0}
    desc = s.describe(percentiles=[0.25, 0.5, 0.75, 0.95, 0.99])
    return {
        "n": int(len(s)),
        "mean": round(float(desc["mean"]), 3),
        "std": round(float(desc["std"]), 3) if len(s) > 1 else 0.0,
        "min": round(float(desc["min"]), 3),
        "p25": round(float(desc["25%"]), 3),
        "p50": round(float(desc["50%"]), 3),
        "p75": round(float(desc["75%"]), 3),
        "p95": round(float(desc["95%"]), 3),
        "p99": round(float(desc["99%"]), 3),
        "max": round(float(s.max()), 3),
    }


def histogram(values: list[float], edges: list[float]) -> list[dict]:
    counts, bins = np.histogram(values, bins=edges)
    out = []
    for i, count in enumerate(counts):
        out.append({"lo": float(bins[i]), "hi": float(bins[i + 1]), "n": int(count)})
    return out


def load_window(files: list[Path], start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    parts = []
    cols = [
        "event_local_ts_ms",
        "base_coin",
        "trigger",
        "okx_local_recv_ts_ms",
        "okx_ts_ms",
        "bybit_local_recv_ts_ms",
        "bybit_ts_ms",
    ]
    for path in files:
        df = pd.read_parquet(path, columns=cols)
        parts.append(df)
    if not parts:
        raise SystemExit("no parquet files")
    df = pd.concat(parts, ignore_index=True)
    df["event_dt"] = pd.to_datetime(df["event_local_ts_ms"], unit="ms", utc=True)
    df = df.loc[(df["event_dt"] >= start) & (df["event_dt"] <= end)].copy()
    df["latency_ms"] = np.where(
        df["trigger"].eq("okx"),
        df["okx_local_recv_ts_ms"] - df["okx_ts_ms"],
        df["bybit_local_recv_ts_ms"] - df["bybit_ts_ms"],
    )
    return df


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--out-json", required=True, type=Path)
    ap.add_argument("files", nargs="+", type=Path)
    args = ap.parse_args()
    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)
    df = load_window(args.files, start, end)

    coins = []
    for coin, g in df.groupby("base_coin", sort=True):
        row = {"base_coin": str(coin), "n": int(len(g))}
        for trig in ("okx", "bybit"):
            stats = qstats(g.loc[g["trigger"].eq(trig), "latency_ms"])
            row[trig] = stats
        coins.append(row)

    okx_p99 = [c["okx"]["p99"] for c in coins if c["okx"].get("n")]
    byb_p99 = [c["bybit"]["p99"] for c in coins if c["bybit"].get("n")]
    okx_p50 = [c["okx"]["p50"] for c in coins if c["okx"].get("n")]
    byb_p50 = [c["bybit"]["p50"] for c in coins if c["bybit"].get("n")]
    okx_max = [c["okx"]["max"] for c in coins if c["okx"].get("n")]
    byb_max = [c["bybit"]["max"] for c in coins if c["bybit"].get("n")]
    edges = [0, 25, 50, 75, 100, 150, 200, 300, 500, 1000, 5000]

    def top(items: list[dict], leg: str, key: str, n: int = 10, reverse: bool = True):
        ranked = [c for c in items if c[leg].get("n")]
        ranked.sort(key=lambda c: c[leg][key], reverse=reverse)
        return [
            {"base_coin": c["base_coin"], "n": c[leg]["n"], "p50": c[leg]["p50"], "p99": c[leg]["p99"], "max": c[leg]["max"]}
            for c in ranked[:n]
        ]

    out = {
        "start": str(start),
        "end": str(end),
        "files": [str(p) for p in args.files],
        "rows": int(len(df)),
        "coins": int(df["base_coin"].nunique()),
        "overall": {
            "okx": qstats(df.loc[df["trigger"].eq("okx"), "latency_ms"]),
            "bybit": qstats(df.loc[df["trigger"].eq("bybit"), "latency_ms"]),
        },
        "per_coin_p99_describe": {
            "okx": qstats(pd.Series(okx_p99)),
            "bybit": qstats(pd.Series(byb_p99)),
        },
        "per_coin_p50_describe": {
            "okx": qstats(pd.Series(okx_p50)),
            "bybit": qstats(pd.Series(byb_p50)),
        },
        "per_coin_max_describe": {
            "okx": qstats(pd.Series(okx_max)),
            "bybit": qstats(pd.Series(byb_max)),
        },
        "hist_per_coin_p99_ms": {
            "okx": histogram(okx_p99, edges),
            "bybit": histogram(byb_p99, edges),
        },
        "hist_per_coin_p50_ms": {
            "okx": histogram(okx_p50, edges),
            "bybit": histogram(byb_p50, edges),
        },
        "top10_p99_okx": top(coins, "okx", "p99"),
        "top10_p99_bybit": top(coins, "bybit", "p99"),
        "bottom10_p99_okx": top(coins, "okx", "p99", reverse=False),
        "xrp": next((c for c in coins if c["base_coin"] == "XRP"), None),
        "coins_detail": coins,
    }
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps({k: out[k] for k in out if k != "coins_detail"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
