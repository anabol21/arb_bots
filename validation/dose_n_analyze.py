#!/usr/bin/env python3
"""Read-only S vs P quantile summary for one dose-N arm (E6-lite)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def qstats(name: str, s: pd.Series) -> dict:
    s = pd.to_numeric(s, errors="coerce").dropna()
    if s.empty:
        return {"series": name, "n": 0}
    return {
        "series": name,
        "n": int(len(s)),
        "p50": float(s.quantile(0.50)),
        "p95": float(s.quantile(0.95)),
        "p99": float(s.quantile(0.99)),
        "max": float(s.max()),
        "neg_n": int((s < 0).sum()),
    }


def load_ping(path: Path) -> tuple[pd.DataFrame, dict]:
    rows = []
    meta: dict = {}
    with path.open() as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            ts = pd.Timestamp(parts[0].replace("Z", "+00:00"))
            exch = parts[1]
            kvs = {}
            for p in parts[2:]:
                if "=" in p:
                    k, v = p.split("=", 1)
                    kvs[k] = v
            if exch == "meta":
                meta.setdefault(kvs.get("event", "meta"), []).append({"ts": ts, **kvs})
                continue
            if "event" in kvs or "latency_ms" not in kvs:
                continue
            rows.append(
                {
                    "ts": ts,
                    "exchange": exch,
                    "latency_ms": float(kvs["latency_ms"]),
                    "age_ts_ms": float(kvs["age_ts_ms"])
                    if kvs.get("age_ts_ms") not in (None, "None", "")
                    else np.nan,
                }
            )
    return pd.DataFrame(rows), meta


def load_lean(lean_glob: Path) -> pd.DataFrame:
    files = sorted(lean_glob.glob("*.parquet")) if lean_glob.is_dir() else sorted(Path().glob(str(lean_glob)))
    if lean_glob.is_dir():
        files = sorted(lean_glob.rglob("*.parquet")) if not files else files
    # A dose arm may cross UTC midnight.  Keep every XRP date partition rather
    # than selecting only the latest one, otherwise its steady window is cut.
    if not files and lean_glob.exists():
        files = sorted(lean_glob.rglob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"no parquet under {lean_glob}")
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
        return {"minutes": 0, "dual_gt_500": 0, "dual_gt_1000": 0}
    g = df_ss.copy()
    g["minute"] = g["event_dt"].dt.floor("min")
    okx = g.loc[g["trigger"].eq("okx")].groupby("minute")["trigger_latency_ms"].max()
    byb = g.loc[g["trigger"].eq("bybit")].groupby("minute")["trigger_latency_ms"].max()
    idx = okx.index.intersection(byb.index)
    minutes = len(idx)
    if minutes == 0:
        return {"minutes": 0, "dual_gt_500": 0, "dual_gt_1000": 0}
    dual500 = int(((okx.loc[idx] > 500) & (byb.loc[idx] > 500)).sum())
    dual1000 = int(((okx.loc[idx] > 1000) & (byb.loc[idx] > 1000)).sum())
    return {"minutes": minutes, "dual_gt_500": dual500, "dual_gt_1000": dual1000}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ping-log", required=True, type=Path)
    ap.add_argument("--lean-dir", required=True, type=Path, help="dir with XRP parquet (or parent live/)")
    ap.add_argument("--out-json", type=Path, default=None)
    ap.add_argument("--out-csv", type=Path, default=None)
    ap.add_argument("--label", default="arm")
    ap.add_argument("--steady-drop-min", type=float, default=5.0)
    args = ap.parse_args()

    ping, meta = load_ping(args.ping_log)
    if "start" not in meta or "finished" not in meta:
        raise SystemExit("ping meta missing start/finished")
    t_start = meta["start"][0]["ts"]
    t_end = meta["finished"][0]["ts"]

    lean_dir = args.lean_dir
    # Allow pointing at live/ and auto-pick XRP, retaining all event dates.
    if (lean_dir / "base_coin=XRP").exists():
        lean_dir = lean_dir / "base_coin=XRP"

    df = load_lean(lean_dir)
    mask = (df["event_dt"] >= t_start) & (df["event_dt"] <= t_end)
    dfw = df.loc[mask].copy()
    t_ss = t_start + pd.Timedelta(minutes=args.steady_drop_min)
    df_ss = dfw.loc[dfw["event_dt"] >= t_ss].copy()
    ping_ss = ping.loc[(ping["ts"] >= t_ss) & (ping["ts"] <= t_end)].copy()

    def ping_leg(exch: str) -> pd.Series:
        sub = ping_ss.loc[ping_ss["exchange"].eq(exch)]
        if exch == "bybit":
            return sub["age_ts_ms"]
        return sub["latency_ms"]

    summary = []
    for name, series in [
        ("S_okx", df_ss.loc[df_ss["trigger"].eq("okx"), "trigger_latency_ms"]),
        ("P_okx", ping_leg("okx")),
        ("S_bybit", df_ss.loc[df_ss["trigger"].eq("bybit"), "trigger_latency_ms"]),
        ("P_bybit", ping_leg("bybit")),
    ]:
        summary.append(qstats(name, series))

    by_series = {r["series"]: r for r in summary}
    ratios = {}
    for leg in ("okx", "bybit"):
        s = by_series.get(f"S_{leg}", {})
        p = by_series.get(f"P_{leg}", {})
        if s.get("n") and p.get("n") and p.get("p99"):
            ratios[f"S_over_P_p99_{leg}"] = float(s["p99"] / p["p99"])
        else:
            ratios[f"S_over_P_p99_{leg}"] = None

    dual = dual_counts(df_ss)
    out = {
        "label": args.label,
        "t_start": str(t_start),
        "t_end": str(t_end),
        "steady_start": str(t_ss),
        "overlap_min": (t_end - t_start).total_seconds() / 60.0,
        "steady_min": (t_end - t_ss).total_seconds() / 60.0,
        "lean_dir": str(lean_dir),
        "summary": summary,
        "ratios": ratios,
        "dual": dual,
    }
    text = json.dumps(out, indent=2)
    print(text)
    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(text + "\n")
    if args.out_csv:
        args.out_csv.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(summary).assign(
            label=args.label,
            t_start=str(t_start),
            t_end=str(t_end),
            steady_start=str(t_ss),
            overlap_min=out["overlap_min"],
            steady_min=out["steady_min"],
            dual_minutes=dual["minutes"],
            dual_gt_500=dual["dual_gt_500"],
            dual_gt_1000=dual["dual_gt_1000"],
            S_over_P_p99_okx=ratios["S_over_P_p99_okx"],
            S_over_P_p99_bybit=ratios["S_over_P_p99_bybit"],
        ).to_csv(args.out_csv, index=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
