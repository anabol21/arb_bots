"""Gear 2.2 cost probe: wall time for the (coin, second) p50/floor feature table.

Measurement only. No policy, no thresholds, no simulator gate. Reads local
``output/lean_ticks`` read-only and prints timings; writes nothing.

    PYTHONPATH=. python3 research/gear22_feature_cost_probe.py \
        --coins KAITO,HOME,SONY,SOL --since 2026-08-18T00:00:00Z --hours 1
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd

from research.gear22_quiet_regime_viz.candles import build_5m_bucket_stats
from research.gear22_quiet_regime_viz.floors import compute_chosen_floor
from research.gear22_quiet_regime_viz.load import load_ticks, parse_since_ms
from research.gear22_quiet_regime_viz.quantiles import (
    WINDOW_1M_MS,
    eval_grid_ms,
    rolling_tw_p50,
)

SIDES = (("long", "spread_long"), ("short", "spread_short"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default="output/lean_ticks")
    ap.add_argument("--coins", required=True)
    ap.add_argument("--since", required=True)
    ap.add_argument("--hours", type=float, default=1.0)
    ap.add_argument("--eval-step-ms", type=int, default=1000)
    args = ap.parse_args()

    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    t0 = parse_since_ms(args.since)
    t1 = t0 + int(args.hours * 3_600_000)
    load_from = t0 - WINDOW_1M_MS

    a = time.perf_counter()
    ticks = load_ticks(Path(args.data_root), coins=coins, since_ms=load_from, until_ms=t1)
    t_read = time.perf_counter() - a
    print(f"read: {t_read:.2f}s  rows={len(ticks):,}  coins={ticks.base_coin.nunique()}")

    grid = eval_grid_ms(t0, t1, args.eval_step_ms)
    print(f"eval grid: {grid.size:,} points/coin/side")

    totals = {"roll1m": 0.0, "bars": 0.0, "floor": 0.0}
    for coin in coins:
        sub = ticks.loc[ticks.base_coin == coin]
        if sub.empty:
            print(f"{coin}: no ticks")
            continue
        ts = sub["event_local_ts_ms"].to_numpy("int64")
        per = {"roll1m": 0.0, "bars": 0.0, "floor": 0.0}
        for _, col in SIDES:
            y = sub[col].to_numpy("float64")
            a = time.perf_counter()
            rolling_tw_p50(ts, y, window_ms=WINDOW_1M_MS, eval_ts_ms=grid)
            per["roll1m"] += time.perf_counter() - a

            a = time.perf_counter()
            buckets = build_5m_bucket_stats(sub, value_col=col, start_ms=t0, end_ms=t1)
            per["bars"] += time.perf_counter() - a

            close = buckets["tw_p50"].to_numpy("float64") if len(buckets) else np.array([])
            a = time.perf_counter()
            sma12 = pd.Series(close).rolling(12, min_periods=1).mean().to_numpy()
            compute_chosen_floor(sma12)
            per["floor"] += time.perf_counter() - a
        for k in totals:
            totals[k] += per[k]
        print(
            f"{coin:8s} ticks={len(sub):>9,} ({len(sub)/(args.hours*3600):6.1f}/s) "
            f"roll1m={per['roll1m']:6.2f}s bars={per['bars']:5.2f}s floor={per['floor']:5.3f}s"
        )

    print(
        f"TOTAL compute (2 sides): roll1m={totals['roll1m']:.2f}s "
        f"bars={totals['bars']:.2f}s floor={totals['floor']:.3f}s  read={t_read:.2f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
