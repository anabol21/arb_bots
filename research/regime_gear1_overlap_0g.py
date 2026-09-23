"""Prototype: regime_on episodes × gear-1-like spread threshold hits (one coin).

Uses vacation dump: bars for 0G + lean flat ticks filtered to 0G.
Does not call model.ipynb run_backtest; uses simple threshold on derived spreads
as a stand-in for “would gear-1 have raw material in this window”.

Usage (from repo root):
  python3 research/regime_gear1_overlap_0g.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from research.regime_metrics import (
    BAR_MS,
    RegimeParams,
    build_regime_frame,
    regime_episodes,
    sanity_summary,
)

DUMP = REPO / "output" / "vacation_return_20260810"
TICK_DIR = DUMP / "ticks"
BARS_ROOT = DUMP / "bars" / "bar_5m" / "base_coin=0G"
OUT_DIR = DUMP / "_inspect"
OUT_JSON = OUT_DIR / "regime_gear1_overlap_0g.json"

# Stand-in for gear-1 open thresholds (manual; not VARIATION search)
THRESH_LONG = 0.15
THRESH_SHORT = 0.15


def load_0g_bars() -> pd.DataFrame:
    files = sorted(BARS_ROOT.glob("event_date=*/*.parquet"))
    if not files:
        raise FileNotFoundError(f"no bars under {BARS_ROOT}")
    df = pd.concat([pd.read_parquet(p) for p in files], ignore_index=True)
    return (
        df.sort_values("bar_start_ts_ms", kind="mergesort")
        .drop_duplicates(subset=["bar_start_ts_ms"], keep="last")
        .reset_index(drop=True)
    )


def load_0g_ticks(t0_ms: int, t1_ms: int) -> pd.DataFrame:
    """Load lean flat files overlapping [t0_ms, t1_ms) for base_coin=0G."""
    files = sorted(TICK_DIR.glob("spread_*.parquet"))
    # Filename windows are UTC; still filter by body ts after read.
    parts = []
    for path in files:
        table = pq.read_table(
            path,
            filters=[("base_coin", "=", "0G")],
            columns=[
                "event_local_ts_ms",
                "okx_bid_price",
                "okx_ask_price",
                "bybit_bid_price",
                "bybit_ask_price",
            ],
        )
        if table.num_rows == 0:
            continue
        parts.append(table)
    if not parts:
        return pd.DataFrame()
    df = pa.concat_tables(parts).to_pandas()
    df = df[(df["event_local_ts_ms"] >= t0_ms) & (df["event_local_ts_ms"] < t1_ms)]
    return df.sort_values("event_local_ts_ms", kind="mergesort").reset_index(drop=True)


def derive_spreads(ticks: pd.DataFrame) -> pd.DataFrame:
    t = ticks.copy()
    t["spread_long"] = (
        (t["bybit_bid_price"] - t["okx_ask_price"]) / t["bybit_bid_price"] * 100
    )
    t["spread_short"] = (
        (t["okx_bid_price"] - t["bybit_ask_price"]) / t["okx_bid_price"] * 100
    )
    return t


def overlap_report(regime_df: pd.DataFrame, ticks: pd.DataFrame) -> dict:
    episodes = regime_episodes(regime_df["regime_on"], regime_df["bar_start_ts_ms"])
    if ticks.empty:
        return {
            "n_episodes": int(len(episodes)),
            "episodes_with_spread_hit": 0,
            "note": "no ticks loaded for 0G in range",
            "episodes": episodes.to_dict(orient="records"),
        }

    ticks = derive_spreads(ticks)
    long_hit = ticks["spread_long"] >= THRESH_LONG
    short_hit = ticks["spread_short"] >= THRESH_SHORT
    hits = []
    with_hit = 0
    for ep in episodes.to_dict(orient="records"):
        m = (ticks["event_local_ts_ms"] >= ep["start_ts_ms"]) & (
            ticks["event_local_ts_ms"] < ep["end_ts_ms"]
        )
        sub = ticks.loc[m]
        n_long = int(long_hit.loc[m].sum()) if len(sub) else 0
        n_short = int(short_hit.loc[m].sum()) if len(sub) else 0
        any_hit = (n_long + n_short) > 0
        if any_hit:
            with_hit += 1
        hits.append(
            {
                **ep,
                "n_ticks": int(len(sub)),
                "n_spread_long_ge_thresh": n_long,
                "n_spread_short_ge_thresh": n_short,
                "max_spread_long": float(sub["spread_long"].max()) if len(sub) else None,
                "max_spread_short": float(sub["spread_short"].max()) if len(sub) else None,
                "spread_opportunity": any_hit,
            }
        )

    return {
        "base_coin": "0G",
        "thresh_long": THRESH_LONG,
        "thresh_short": THRESH_SHORT,
        "n_episodes": int(len(episodes)),
        "episodes_with_spread_hit": with_hit,
        "hit_rate": float(with_hit / len(episodes)) if len(episodes) else None,
        "n_ticks_in_range": int(len(ticks)),
        "episodes": hits,
    }


def main() -> None:
    bars = load_0g_bars()
    params = RegimeParams()
    t0 = int(bars["bar_start_ts_ms"].min())
    t1 = int(bars["bar_start_ts_ms"].max()) + BAR_MS
    ticks = load_0g_ticks(t0, t1)

    regime_df = build_regime_frame(bars, params=params, ticks=ticks if len(ticks) else None)
    summary = sanity_summary(regime_df)
    report = overlap_report(regime_df, ticks)
    report["sanity"] = summary
    report["params"] = params.__dict__
    report["note_multi_coin"] = (
        "vacation bars cover only 0G; treat as formula/prototype calibration, "
        "not market-wide gear 1.5 validation"
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    regime_df.to_parquet(OUT_DIR / "regime_frame_0g_with_amp.parquet", index=False)

    # Compact stdout
    slim = {k: v for k, v in report.items() if k != "episodes"}
    slim["n_episode_rows"] = len(report.get("episodes") or [])
    print(json.dumps(slim, indent=2))
    print(f"wrote {OUT_JSON}")


if __name__ == "__main__":
    main()
