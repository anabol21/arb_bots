"""Experiment 1. One joined gear-2.2 replay, funding added only to the result.

Windows IS (2026-08-10..08-27) and OOS (2026-08-28..09-13) are one K=1 scan.
The slot is not reset on 2026-08-28. The 2026-08-19 17:15Z .. 2026-08-20 12:20Z
tick hole stays in the clock: those seconds are in the hive and are not
dropped. 2026-09-13 is a short day; the interval ends at its last row.

Entry and exit stay the frozen ridge grid. ``potential_pp`` is arm A.
Arm B is ``potential_pp + funding_pp`` on the same trades.

The scan pool drops ICX before the clock starts. Those rows never compete
for the K=1 slot. This is not a filter applied after the trade list.

Does not change policy, sweep, or the live bot.
"""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from research.gear22_backtest.params_frozen import (
    FROZEN,
    IS_DATES,
    OOS_DATES,
    RIDGE_GRID,
)
from research.gear22_backtest.policy import PolicyParams
from research.gear22_backtest.sweep import run_combo, store_from_hive
from research.gear23_funding.exp1_funding import (
    FundingBook,
    FundingResult,
    VenueSeries,
    funding_for_hold,
)

REPO = Path(__file__).resolve().parents[2]
HIVE = REPO / "output" / "gear22_backtest_features_by_date"
SETTLEMENTS = (
    REPO / "research" / "gear23_funding" / "data" / "market_funding" / "settlements.csv"
)
TABLES = REPO / "research" / "gear23_funding" / "tables"

# Tick hole documented in docs/model-data-coverage.md. Rows exist; usable collapses.
HOLE_START = int(datetime(2026, 8, 19, 17, 15, tzinfo=timezone.utc).timestamp())
HOLE_END = int(datetime(2026, 8, 20, 12, 20, tzinfo=timezone.utc).timestamp())

JOINED_DATES = list(IS_DATES) + list(OOS_DATES)
DROP_COINS = frozenset({"ICX"})


def load_book(path: Path, coins: set[str], ts_min: int, ts_max: int) -> FundingBook:
    """Read settlement prints for ``coins`` with open/close margin of one hour."""
    lo = (ts_min - 3600) * 1000
    hi = (ts_max + 3600) * 1000
    buckets: dict[tuple[str, str], list[tuple[int, float]]] = {}
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            coin = row["base_coin"]
            if coin not in coins:
                continue
            ts_ms = int(row["ts_ms"])
            if ts_ms < lo or ts_ms > hi:
                continue
            raw = row["rate_pct"].strip()
            if raw == "":
                continue
            buckets.setdefault((row["exchange"], coin), []).append((ts_ms // 1000, float(raw)))
    bybit: dict[str, VenueSeries] = {}
    okx: dict[str, VenueSeries] = {}
    target = {"bybit": bybit, "okx": okx}
    for (exchange, coin), pairs in buckets.items():
        pairs.sort()
        target[exchange][coin] = VenueSeries(
            ts_s=np.array([p[0] for p in pairs], dtype=np.int64),
            rate_pct=np.array([p[1] for p in pairs], dtype=np.float64),
        )
    return FundingBook(bybit=bybit, okx=okx)


def _priced(result: FundingResult) -> bool:
    return not result.missing and result.funding_pp is not None


def score_run(store_coins_end: int, result, book: FundingBook) -> dict:
    """Arm A is the model total. Arm B adds priced funding and leaves gaps blank."""
    rows = []
    funding_values: list[float] = []
    for trade in result.trades:
        got = funding_for_hold(book, trade.coin, trade.side, trade.ts_open, trade.ts_close)
        funding = got.funding_pp
        with_funding = None if funding is None else trade.potential_pp + funding
        if funding is not None:
            funding_values.append(funding)
        rows.append(
            {
                "coin": trade.coin,
                "side": trade.side,
                "ts_open": trade.ts_open,
                "ts_close": trade.ts_close,
                "hold_s": trade.ts_close - trade.ts_open,
                "potential_pp": trade.potential_pp,
                "funding_pp": funding,
                "with_funding_pp": with_funding,
                "n_bybit": got.n_bybit,
                "n_okx": got.n_okx,
                "missing": got.missing,
            }
        )
    open_funding = None
    open_missing = False
    if result.open_coin is not None and result.open_ts is not None:
        got_open = funding_for_hold(
            book, result.open_coin, result.open_side, result.open_ts, store_coins_end
        )
        open_funding = got_open.funding_pp
        open_missing = got_open.missing
    sum_funding = float(sum(funding_values))
    worst = min(funding_values) if funding_values else None
    sum_ex_worst = None if worst is None else sum_funding - worst
    n_flip = sum(
        1
        for row in rows
        if row["with_funding_pp"] is not None and row["with_funding_pp"] < 0
    )
    open_add = 0.0 if open_funding is None else open_funding
    return {
        "n_closed": result.n_closed,
        "n_priced": len(funding_values),
        "n_missing": sum(1 for row in rows if row["missing"]),
        "n_funding_flip_negative": n_flip,
        "sum_potential_pp": result.sum_closed_pp,
        "sum_funding_pp": sum_funding,
        "sum_with_funding_pp": result.sum_closed_pp + sum_funding,
        "sum_funding_ex_worst_pp": sum_ex_worst,
        "worst_funding_pp": worst,
        "mtm_pp": result.mtm_pp,
        "open_coin": result.open_coin,
        "open_side": result.open_side,
        "open_funding_pp": open_funding,
        "open_funding_missing": open_missing,
        "total_pp": result.total_pp,
        "total_with_funding_pp": result.total_pp + sum_funding + open_add,
        "exposure_s": result.exposure_s,
        "trades": rows,
    }


def hole_usable(store) -> dict:
    """Share of hive rows inside the documented tick hole that are usable."""
    mask = (store.ts >= HOLE_START) & (store.ts < HOLE_END)
    n = int(mask.sum())
    if n == 0:
        return {"rows": 0, "usable_either": None}
    either = store.usable_long[mask] | store.usable_short[mask]
    return {"rows": n, "usable_either": float(np.mean(either))}


def pool_coins() -> list[str]:
    """Canary list with ICX removed. The hive still contains ICX rows."""
    manifest = json.loads((HIVE / "MANIFEST.json").read_text())
    coins = [str(c) for c in manifest["coins"] if str(c) not in DROP_COINS]
    dropped = sorted(set(manifest["coins"]) - set(coins))
    if dropped != sorted(DROP_COINS):
        raise RuntimeError(f"expected to drop {sorted(DROP_COINS)}, dropped {dropped}")
    return coins


def main() -> None:
    dates = JOINED_DATES
    coins = pool_coins()
    print(
        f"joined dates {dates[0]} .. {dates[-1]} n={len(dates)} "
        f"pool={len(coins)} dropped={sorted(DROP_COINS)}",
        flush=True,
    )
    t0 = time.perf_counter()
    store = store_from_hive(HIVE, dates=dates, coins=coins)
    if set(store.coins) & DROP_COINS:
        raise RuntimeError(f"dropped coins still in store: {set(store.coins) & DROP_COINS}")
    load_s = time.perf_counter() - t0
    end_ts = int(store.ts[-1])
    print(
        f"store rows={store.n_rows} coins={len(store.coins)} span_s={store.span_s} "
        f"first={int(store.ts[0])} last={end_ts} load_s={load_s:.1f}",
        flush=True,
    )
    hole = hole_usable(store)
    print(f"hole rows={hole['rows']} usable_either={hole['usable_either']}", flush=True)
    book = load_book(SETTLEMENTS, set(store.coins), int(store.ts[0]), end_ts)
    missing_coins = sorted(
        c for c in store.coins if c not in book.bybit or c not in book.okx
    )
    print(f"funding missing coins={missing_coins}", flush=True)

    combos = [
        PolicyParams(
            theta_open=th,
            p50_open=p50,
            min_profit_pp=prof,
            fee_round_trip_pp=0.30,
            min_spread_open=None,
            min_theta_close=theta_cl,
        )
        for th in RIDGE_GRID["theta_open"]
        for p50 in RIDGE_GRID["p50_open"]
        for prof in RIDGE_GRID["min_profit_pp"]
        for theta_cl in RIDGE_GRID["min_theta_close"]
    ]
    grid_rows = []
    frozen_trades = None
    for i, params in enumerate(combos, start=1):
        t1 = time.perf_counter()
        run = run_combo(store, params)
        scored = score_run(end_ts, run, book)
        trades = scored.pop("trades")
        is_frozen = (
            params.theta_open == FROZEN.theta_open
            and params.p50_open == FROZEN.p50_open
            and params.min_profit_pp == FROZEN.min_profit_pp
            and params.min_theta_close == FROZEN.min_theta_close
            and params.min_spread_open == FROZEN.min_spread_open
            and params.fee_round_trip_pp == FROZEN.fee_round_trip_pp
        )
        if is_frozen:
            frozen_trades = trades
        duty = scored["exposure_s"] / store.span_s if store.span_s else float("nan")
        grid_rows.append(
            {
                "frozen": is_frozen,
                "theta_open": params.theta_open,
                "p50_open": params.p50_open,
                "min_profit_pp": params.min_profit_pp,
                "min_theta_close": params.min_theta_close,
                "duty_cycle": duty,
                "combo_s": time.perf_counter() - t1,
                **scored,
            }
        )
        if i == 1 or i % 12 == 0 or i == len(combos):
            print(
                f"combo {i}/{len(combos)} {params.theta_open} {params.p50_open} "
                f"{params.min_profit_pp} {params.min_theta_close} "
                f"n={scored['n_closed']} A={scored['total_pp']:.2f} "
                f"B={scored['total_with_funding_pp']:.2f} "
                f"s={grid_rows[-1]['combo_s']:.1f}",
                flush=True,
            )

    TABLES.mkdir(parents=True, exist_ok=True)
    grid_path = TABLES / "exp1_no_icx_grid.csv"
    trades_path = TABLES / "exp1_no_icx_frozen_trades.csv"
    summary_path = TABLES / "exp1_no_icx_summary.json"
    pd.DataFrame(grid_rows).to_csv(grid_path, index=False)
    pd.DataFrame(frozen_trades or []).to_csv(trades_path, index=False)
    frozen_row = next(row for row in grid_rows if row["frozen"])
    summary = {
        "experiment": "exp1_posthoc_funding_no_icx",
        "dropped_coins": sorted(DROP_COINS),
        "pool_coins": store.coins,
        "dates": [dates[0], dates[-1]],
        "n_dates": len(dates),
        "hive": str(HIVE),
        "settlements": str(SETTLEMENTS),
        "span_s": store.span_s,
        "first_ts": int(store.ts[0]),
        "last_ts": end_ts,
        "hole": {
            "start_utc": "2026-08-19T17:15:00Z",
            "end_utc": "2026-08-20T12:20:00Z",
            **hole,
        },
        "missing_coins": missing_coins,
        "boundary": "ts_open < settlement_second <= ts_close",
        "sign": "long = short Bybit + long OKX; funding_pp = bybit_rate_pct - okx_rate_pct",
        "n_combos": len(grid_rows),
        "frozen": frozen_row,
        "grid_csv": str(grid_path),
        "trades_csv": str(trades_path),
    }
    summary_path.write_text(json.dumps(summary, indent=2, default=str) + "\n")
    print(f"wrote {grid_path}", flush=True)
    print(f"wrote {trades_path}", flush=True)
    print(f"wrote {summary_path}", flush=True)


if __name__ == "__main__":
    main()
