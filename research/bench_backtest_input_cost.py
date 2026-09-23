"""Read-only cost benchmark: 5m bars + floor and 1 Hz TW-p50 for backtest input.

Measures wall clock only; changes no locked formula. Run from repo root:

    python3 -m research.bench_backtest_input_cost --coins KAITO,AZTEC \
        --since 2026-08-12T00:00:00Z --until 2026-08-12T03:00:00Z
"""

from __future__ import annotations

import argparse
import json
import time

import numpy as np

from research.gear22_quiet_regime_viz.candles import (
    SPREAD_LONG_COL,
    SPREAD_SHORT_COL,
    build_5m_bucket_stats,
    causal_sma,
)
from research.gear22_quiet_regime_viz.floors import compute_chosen_floor
from research.gear22_quiet_regime_viz.load import load_ticks, parse_since_ms
from research.gear22_quiet_regime_viz.quantiles import (
    WINDOW_1M_MS,
    eval_grid_ms,
    rolling_tw_p50,
)

MASS_FRAC = 0.20


def coverage_frac(ts_ms: np.ndarray, grid: np.ndarray, window_ms: int = WINDOW_1M_MS):
    """Fraction of grid points where the locked p50 mass/tick rule can pass.

    mass = t - ts[first in window] under hold->next, so mass >= 0.2*W means a
    tick exists in [t-W, t-0.8*W]; also require >= 2 ticks in [t-W, t].
    """
    ts = np.sort(np.asarray(ts_ms, dtype="int64"))
    left = np.searchsorted(ts, grid - window_ms, side="left")
    right = np.searchsorted(ts, grid, side="right")
    # mass = t - ts[left] under hold->next, so require ts[left] <= t - 0.2*W.
    early = np.searchsorted(ts, grid - int(MASS_FRAC * window_ms), side="right")
    ok = (right - left >= 2) & (early - left >= 1)
    return float(ok.mean()), int(ok.size)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default="output/lean_ticks")
    ap.add_argument("--coins", required=True)
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--step-ms", type=int, default=1000)
    args = ap.parse_args()

    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    since = parse_since_ms(args.since)
    until = parse_since_ms(args.until)

    t0 = time.perf_counter()
    ticks = load_ticks(args.data_root, coins=coins, since_ms=since, until_ms=until)
    t_load = time.perf_counter() - t0

    grid = eval_grid_ms(since, until, args.step_ms)
    report = {
        "window_h": (until - since) / 3_600_000,
        "n_coins": len(coins),
        "load_s": round(t_load, 3),
        "grid_points": int(grid.size),
        "coins": {},
    }
    for coin in coins:
        sub = ticks.loc[ticks["base_coin"] == coin]
        if sub.empty:
            report["coins"][coin] = {"ticks": 0, "note": "absent"}
            continue
        rec: dict = {"ticks": int(len(sub))}
        rec["ticks_per_s"] = round(len(sub) / ((until - since) / 1000), 2)

        t0 = time.perf_counter()
        for col in (SPREAD_LONG_COL, SPREAD_SHORT_COL):
            b = build_5m_bucket_stats(sub, value_col=col, start_ms=since, end_ms=until)
            close = b["close"].to_numpy(dtype="float64")
            sma12 = causal_sma(close, 12)
            compute_chosen_floor(sma12)
        rec["bars_floor_both_sides_s"] = round(time.perf_counter() - t0, 3)

        ts = sub["event_local_ts_ms"].to_numpy(dtype="int64")
        for side, col in (("long", SPREAD_LONG_COL), ("short", SPREAD_SHORT_COL)):
            t0 = time.perf_counter()
            out = rolling_tw_p50(
                ts, sub[col].to_numpy(dtype="float64"),
                window_ms=WINDOW_1M_MS, eval_ts_ms=grid,
            )
            rec[f"p50_1m_{side}_s"] = round(time.perf_counter() - t0, 3)
            rec[f"p50_1m_{side}_finite_frac"] = round(
                float(np.isfinite(out).mean()), 4
            )
        cov, n = coverage_frac(ts, grid)
        rec["coverage_frac_predicted"] = round(cov, 4)
        report["coins"][coin] = rec

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
