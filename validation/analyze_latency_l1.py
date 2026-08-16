#!/usr/bin/env python3
"""L1 window analysis: lean XRP S vs JSONL matched ping P."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


STEADY_START = pd.Timestamp("2026-08-16T18:05:27Z")
STEADY_END = pd.Timestamp("2026-08-16T19:05:27Z")
PING_PATH = Path("/data/experiments/l1_n337_write_20260816/ping_xrp.jsonl")
LEAN_DIR = Path("/data/live/base_coin=XRP")
OUT = Path("/data/experiments/l1_n337_write_20260816/analysis.json")


def qstats(name: str, s: pd.Series) -> dict:
    s = pd.to_numeric(s, errors="coerce").dropna()
    if s.empty:
        return {"series": name, "n": 0}
    return {
        "series": name,
        "n": int(len(s)),
        "p50": round(float(s.quantile(0.50)), 3),
        "p95": round(float(s.quantile(0.95)), 3),
        "p99": round(float(s.quantile(0.99)), 3),
        "max": round(float(s.max()), 3),
        "neg_n": int((s < 0).sum()),
    }


def load_ping(path: Path) -> tuple[pd.DataFrame, dict]:
    rows = []
    meta: dict = {}
    events = []
    with path.open() as handle:
        for line in handle:
            rec = json.loads(line)
            ts = pd.Timestamp(rec["ts_utc"])
            if rec.get("event"):
                events.append({"ts": str(ts), **{k: rec[k] for k in rec if k != "ts_utc"}})
                if rec.get("exchange") == "meta":
                    meta[rec["event"]] = rec
                continue
            if "latency_ms" not in rec:
                continue
            rows.append(
                {
                    "ts": ts,
                    "exchange": rec["exchange"],
                    "latency_ms": float(rec["latency_ms"]),
                    "age_ts_ms": float(rec["age_ts_ms"]) if rec.get("age_ts_ms") is not None else np.nan,
                }
            )
    return pd.DataFrame(rows), meta, events


def load_lean(lean_dir: Path) -> pd.DataFrame:
    files = sorted(lean_dir.rglob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"no parquet under {lean_dir}")
    df = pd.concat([pd.read_parquet(p) for p in files], ignore_index=True)
    df["event_dt"] = pd.to_datetime(df["event_local_ts_ms"], unit="ms", utc=True)
    df["okx_latency_ms"] = df["okx_local_recv_ts_ms"] - df["okx_ts_ms"]
    df["bybit_latency_ms"] = df["bybit_local_recv_ts_ms"] - df["bybit_ts_ms"]
    df["trigger_latency_ms"] = df["okx_latency_ms"].where(
        df["trigger"].eq("okx"), df["bybit_latency_ms"]
    )
    return df


def dual_counts(df_ss: pd.DataFrame) -> dict:
    if df_ss.empty:
        return {"minutes": 0, "dual_gt_500": 0, "dual_gt_1000": 0, "per_minute": []}
    g = df_ss.copy()
    g["minute"] = g["event_dt"].dt.floor("min")
    okx = g.loc[g["trigger"].eq("okx")].groupby("minute")["trigger_latency_ms"].max()
    byb = g.loc[g["trigger"].eq("bybit")].groupby("minute")["trigger_latency_ms"].max()
    idx = okx.index.intersection(byb.index)
    per_minute = []
    for minute in idx:
        per_minute.append(
            {
                "minute": str(minute),
                "okx_max": round(float(okx.loc[minute]), 3),
                "bybit_max": round(float(byb.loc[minute]), 3),
            }
        )
    return {
        "minutes": int(len(idx)),
        "dual_gt_500": int(((okx.loc[idx] > 500) & (byb.loc[idx] > 500)).sum()),
        "dual_gt_1000": int(((okx.loc[idx] > 1000) & (byb.loc[idx] > 1000)).sum()),
        "per_minute": per_minute,
    }


def main() -> int:
    ping, meta, events = load_ping(PING_PATH)
    df = load_lean(LEAN_DIR)
    mask = (df["event_dt"] >= STEADY_START) & (df["event_dt"] <= STEADY_END)
    df_ss = df.loc[mask].copy()
    ping_ss = ping.loc[(ping["ts"] >= STEADY_START) & (ping["ts"] <= STEADY_END)].copy()

    def ping_leg(exch: str) -> pd.Series:
        sub = ping_ss.loc[ping_ss["exchange"].eq(exch)]
        if exch == "bybit":
            return sub["age_ts_ms"]
        return sub["latency_ms"]

    summary = [
        qstats("S_okx", df_ss.loc[df_ss["trigger"].eq("okx"), "trigger_latency_ms"]),
        qstats("P_okx", ping_leg("okx")),
        qstats("S_bybit", df_ss.loc[df_ss["trigger"].eq("bybit"), "trigger_latency_ms"]),
        qstats("P_bybit", ping_leg("bybit")),
    ]
    by_series = {row["series"]: row for row in summary}
    ratios = {}
    for leg in ("okx", "bybit"):
        s = by_series[f"S_{leg}"]
        p = by_series[f"P_{leg}"]
        ratios[f"S_over_P_p99_{leg}"] = (
            round(float(s["p99"] / p["p99"]), 3) if s.get("n") and p.get("p99") else None
        )

    ping_events = [e for e in events if e.get("event") not in {None, "start", "finished"}]
    out = {
        "label": "l1_n337_write_20260816",
        "steady_start": str(STEADY_START),
        "steady_end": str(STEADY_END),
        "ping_meta": {k: meta[k] for k in meta},
        "lean_files": len(list(LEAN_DIR.rglob("*.parquet"))),
        "lean_rows_window": int(len(df_ss)),
        "summary": summary,
        "ratios": ratios,
        "dual": dual_counts(df_ss),
        "ping_event_counts": pd.Series([e.get("event") for e in ping_events]).value_counts().to_dict()
        if ping_events
        else {},
        "ping_events": ping_events[:50],
    }
    text = json.dumps(out, indent=2, default=str)
    print(text)
    OUT.write_text(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
