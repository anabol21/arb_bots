"""Read-only: measure parquet bytes/row for the 1 Hz backtest-input table.

Computes real p50_roll_1m (both sides) + floor for a few coins over a short
window, writes two candidate layouts to a temp dir, reports bytes per row.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from research.gear22_quiet_regime_viz.candles import (
    SPREAD_LONG_COL,
    SPREAD_SHORT_COL,
    build_5m_bucket_stats,
    causal_sma,
)
from research.gear22_quiet_regime_viz.floors import TF_SELECT_25_NAME, compute_chosen_floor
from research.gear22_quiet_regime_viz.load import load_ticks, parse_since_ms
from research.gear22_quiet_regime_viz.quantiles import (
    WINDOW_1M_MS,
    eval_grid_ms,
    rolling_tw_p50,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default="output/lean_ticks")
    ap.add_argument("--coins", required=True)
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    args = ap.parse_args()

    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    since, until = parse_since_ms(args.since), parse_since_ms(args.until)
    t0 = time.perf_counter()
    ticks = load_ticks(args.data_root, coins=coins, since_ms=since, until_ms=until)
    load_s = time.perf_counter() - t0
    grid = eval_grid_ms(since, until, 1000)

    long_rows = []
    for coin in coins:
        sub = ticks.loc[ticks["base_coin"] == coin]
        if sub.empty:
            continue
        ts = sub["event_local_ts_ms"].to_numpy(dtype="int64")
        for side, col in (("long", SPREAD_LONG_COL), ("short", SPREAD_SHORT_COL)):
            p50 = rolling_tw_p50(ts, sub[col].to_numpy(dtype="float64"),
                                 window_ms=WINDOW_1M_MS, eval_ts_ms=grid)
            bars = build_5m_bucket_stats(sub, value_col=col, start_ms=since, end_ms=until)
            sma12 = causal_sma(bars["close"].to_numpy(dtype="float64"), 12)
            floor = compute_chosen_floor(sma12)[TF_SELECT_25_NAME]
            # step-hold the closed-bar floor onto the 1 Hz grid (bar_end aligned)
            bar_end = bars["bar_end_ms"].to_numpy(dtype="int64")
            idx = np.searchsorted(bar_end, grid, side="right") - 1
            fl = np.where(idx >= 0, floor[np.clip(idx, 0, len(floor) - 1)], np.nan)
            long_rows.append(pd.DataFrame({
                "ts_ms": grid, "base_coin": coin, "side": side,
                "p50_roll_1m": p50.astype("float32"),
                "floor": fl.astype("float32"),
            }))
    df = pd.concat(long_rows, ignore_index=True)
    df["base_coin"] = df["base_coin"].astype("category")
    df["side"] = df["side"].astype("category")

    out: dict = {"load_s": round(load_s, 2), "n_coins": len(coins),
                 "grid_points": int(grid.size), "long_rows": int(len(df)),
                 "layouts": {}}
    tmp = tempfile.mkdtemp(prefix="bench_out_size_")
    try:
        for comp in ("snappy", "zstd"):
            p = os.path.join(tmp, f"long_{comp}.parquet")
            pq.write_table(pa.Table.from_pandas(df, preserve_index=False), p,
                           compression=comp, use_dictionary=True)
            b = os.path.getsize(p)
            out["layouts"][f"long_f32_{comp}"] = {
                "bytes": b, "rows": int(len(df)),
                "bytes_per_row": round(b / len(df), 3),
            }
        # wide layout: one row per (ts, coin), 4 float32 + ts + coin
        w = df.pivot_table(index=["ts_ms", "base_coin"], columns="side",
                           values=["p50_roll_1m", "floor"], observed=True)
        w.columns = [f"{a}_{b}" for a, b in w.columns]
        w = w.reset_index()
        for c in w.columns:
            if c not in ("ts_ms", "base_coin"):
                w[c] = w[c].astype("float32")
        for comp in ("snappy", "zstd"):
            p = os.path.join(tmp, f"wide_{comp}.parquet")
            pq.write_table(pa.Table.from_pandas(w, preserve_index=False), p,
                           compression=comp, use_dictionary=True)
            b = os.path.getsize(p)
            out["layouts"][f"wide_f32_{comp}"] = {
                "bytes": b, "rows": int(len(w)),
                "bytes_per_row": round(b / len(w), 3),
                "bytes_per_coin_second": round(b / len(w), 3),
            }
        print(json.dumps(out, indent=2))
    finally:
        for f in os.listdir(tmp):
            os.remove(os.path.join(tmp, f))
        os.rmdir(tmp)


if __name__ == "__main__":
    main()
