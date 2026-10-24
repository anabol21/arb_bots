"""H1, H2, H3, H8, H9, H10 on the joint Bybit/OKX funding panel.

Stdlib only. Thresholds are fixed in METHOD.md and are not retuned here.
Window starts 2026-07-20. A row is usable when both=1 and both per-8h rates
are present. gap = bybit_rate_per_8h_pct - okx_rate_per_8h_pct.

Slices: collector (bybit_okx_universe.csv rows [0:337]), trades (unique coins
in tables/round_trips.csv), trades_inside_hold, trades_outside_hold.
An hour is inside a hold when hour_utc is inside [open_utc, close_utc] of any
round trip of that coin.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

from hypothesis_stats import mad, median, self_check, within_correlation

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PANEL = HERE / "data" / "market_funding" / "by_coin.csv"
TRIPS = HERE / "tables" / "round_trips.csv"
UNIVERSE = REPO / "bybit_okx_universe.csv"
OUT = HERE / "tables" / "funding_hypotheses.json"

WINDOW_START = "2026-07-20 00:00:00"
COLLECTOR_END = 337
MIN_N = 30
MIN_POOLED = 200
RATE_POINT = 0.01
ZERO_EPS = 1e-9


def round_gap(bybit: float, okx: float) -> float:
    return round(bybit - okx, 8)


def near(value: float, target: float) -> bool:
    return abs(value - target) <= ZERO_EPS


def load_collector_coins() -> list[str]:
    coins: list[str] = []
    with UNIVERSE.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            coins.append(row["base_coin"])
            if len(coins) >= COLLECTOR_END:
                break
    return coins


def load_trips() -> tuple[list[str], dict[str, list[tuple[str, str]]]]:
    holds: dict[str, list[tuple[str, str]]] = {}
    seen: list[str] = []
    known: set[str] = set()
    with TRIPS.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            coin = row["coin"]
            if coin not in known:
                known.add(coin)
                seen.append(coin)
            holds.setdefault(coin, []).append((row["open_utc"], row["close_utc"]))
    return seen, holds


def hour_inside(hour_utc: str, intervals: list[tuple[str, str]]) -> bool:
    for open_utc, close_utc in intervals:
        if open_utc <= hour_utc <= close_utc:
            return True
    return False


def load_panel(coins: set[str]) -> dict[str, list[tuple[str, float, float, float]]]:
    grouped: dict[str, list[tuple[str, float, float, float]]] = {coin: [] for coin in coins}
    with PANEL.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            coin = row["base_coin"]
            if coin not in grouped:
                continue
            hour_utc = row["hour_utc"]
            if hour_utc < WINDOW_START:
                continue
            if row["both"] != "1":
                continue
            bybit_text = row["bybit_rate_per_8h_pct"]
            okx_text = row["okx_rate_per_8h_pct"]
            if bybit_text == "" or okx_text == "":
                continue
            bybit = float(bybit_text)
            okx = float(okx_text)
            grouped[coin].append((hour_utc, bybit, okx, round_gap(bybit, okx)))
    for rows in grouped.values():
        rows.sort(key=lambda item: item[0])
    return grouped


def center(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": None, "median": None, "mad": None, "mean_over_mad": None}
    mean = sum(values) / len(values)
    scale = mad(values)
    ratio = None if scale in (None, 0.0) else mean / scale
    return {
        "n": len(values),
        "mean": mean,
        "median": median(values),
        "mad": scale,
        "mean_over_mad": ratio,
    }


def pairs_for(rows: list[tuple[str, float, float, float]], kind: str) -> list[tuple[float, float]]:
    if kind == "h1":
        return [(row[1], row[2]) for row in rows]
    if kind == "h10_bybit":
        return [(row[3], row[1]) for row in rows]
    if kind == "h10_okx":
        return [(row[3], row[2]) for row in rows]
    gaps = [row[3] for row in rows]
    if len(gaps) < 2:
        return []
    if kind == "h3_auto":
        return list(zip(gaps[:-1], gaps[1:]))
    if kind == "h3_delta":
        return [(gaps[i], gaps[i + 1] - gaps[i]) for i in range(len(gaps) - 1)]
    raise KeyError(kind)


def correlation_block(rows_by_coin: dict[str, list[tuple[str, float, float, float]]], kind: str) -> dict:
    pairs = {coin: pairs_for(rows, kind) for coin, rows in rows_by_coin.items()}
    return within_correlation(pairs, MIN_N, MIN_POOLED)


def slice_rows(
    panel: dict[str, list[tuple[str, float, float, float]]],
    coins: list[str],
    holds: dict[str, list[tuple[str, str]]] | None,
    mode: str,
) -> dict[str, list[tuple[str, float, float, float]]]:
    selected: dict[str, list[tuple[str, float, float, float]]] = {}
    for coin in coins:
        rows = panel.get(coin, [])
        if mode == "all":
            chosen = rows
        elif mode == "inside":
            intervals = [] if holds is None else holds.get(coin, [])
            chosen = [row for row in rows if hour_inside(row[0], intervals)]
        elif mode == "outside":
            intervals = [] if holds is None else holds.get(coin, [])
            chosen = [row for row in rows if not hour_inside(row[0], intervals)]
        else:
            raise KeyError(mode)
        if chosen:
            selected[coin] = chosen
    return selected


def summarize_slice(
    name: str,
    requested: list[str],
    rows_by_coin: dict[str, list[tuple[str, float, float, float]]],
    coins_without_series: list[str],
) -> dict:
    flat = [row for rows in rows_by_coin.values() for row in rows]
    bybit = [row[1] for row in flat]
    okx = [row[2] for row in flat]
    gaps = [row[3] for row in flat]
    n_rows = len(flat)
    n_coins = len(rows_by_coin)
    max_coin_n = max((len(rows) for rows in rows_by_coin.values()), default=0)
    estimable_pooled = n_rows >= MIN_POOLED
    estimable_per_coin = max_coin_n >= MIN_N
    label = None
    if name == "trades_inside_hold" and not (estimable_pooled or estimable_per_coin):
        label = "на сделках не измерено"
    h3_auto = correlation_block(rows_by_coin, "h3_auto")
    abs_changes: list[float] = []
    for rows in rows_by_coin.values():
        coin_gaps = [row[3] for row in rows]
        for i in range(len(coin_gaps) - 1):
            abs_changes.append(abs(coin_gaps[i + 1] - coin_gaps[i]))
    abs_gaps = [abs(value) for value in gaps]
    abs_bybit = [abs(value) for value in bybit]
    abs_okx = [abs(value) for value in okx]
    median_abs_gap = median(abs_gaps)
    n = n_rows
    return {
        "n_coins_requested": len(requested),
        "n_coins_with_rows": n_coins,
        "n_rows": n_rows,
        "coins_without_series": coins_without_series,
        "coins_absent_from_slice": [coin for coin in requested if coin not in rows_by_coin],
        "coins_with_rows": [
            {"coin": coin, "n": len(rows_by_coin[coin])}
            for coin in requested
            if coin in rows_by_coin
        ],
        "estimable_pooled": estimable_pooled,
        "estimable_per_coin": estimable_per_coin,
        "status": label,
        "H1": {
            "same_sign_share": None
            if n == 0
            else sum(
                1
                for row in flat
                if (row[1] > 0 and row[2] > 0)
                or (row[1] < 0 and row[2] < 0)
                or (row[1] == 0 and row[2] == 0)
            )
            / n,
            "correlation": correlation_block(rows_by_coin, "h1"),
        },
        "H2": {
            "bybit": center(bybit),
            "okx": center(okx),
            "gap": center(gaps),
            "description": {
                "note": "Доля строк, где ставка равна 0.01 и где gap равен 0. Описание массы в точке, не замена MAD.",
                "share_bybit_rate_eq_0_01": None if n == 0 else sum(1 for value in bybit if near(value, RATE_POINT)) / n,
                "share_okx_rate_eq_0_01": None if n == 0 else sum(1 for value in okx if near(value, RATE_POINT)) / n,
                "share_gap_eq_0": None if n == 0 else sum(1 for value in gaps if near(value, 0.0)) / n,
            },
        },
        "H3": {
            "autocorr": h3_auto,
            "corr_gap_with_next_change": correlation_block(rows_by_coin, "h3_delta"),
        },
        "H8": {
            "autocorr": h3_auto,
            "n_pairs": len(abs_changes),
            "median_abs_change": median(abs_changes),
            "description_share_abs_change_eq_0": None
            if not abs_changes
            else sum(1 for value in abs_changes if near(value, 0.0)) / len(abs_changes),
        },
        "H9": {
            "median_abs_gap": median_abs_gap,
            "median_abs_bybit": median(abs_bybit),
            "median_abs_okx": median(abs_okx),
            "compare_abs_gap_to_0_20": None
            if median_abs_gap is None
            else ("below" if median_abs_gap < 0.20 else "not_below"),
            "compare_abs_gap_to_0_30": None
            if median_abs_gap is None
            else ("at_or_above" if median_abs_gap >= 0.30 else "below"),
        },
        "H10": {
            "gap_vs_bybit": correlation_block(rows_by_coin, "h10_bybit"),
            "gap_vs_okx": correlation_block(rows_by_coin, "h10_okx"),
        },
    }


def clean(value):
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return round(value, 10)
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item) for item in value]
    return value


def main() -> int:
    check = self_check()
    if not check["ok"]:
        print(json.dumps(check), file=sys.stderr)
        return 1
    collector = load_collector_coins()
    trades, holds = load_trips()
    panel = load_panel(set(collector) | set(trades))
    missing = {
        "collector": [coin for coin in collector if not panel.get(coin)],
        "trades": [coin for coin in trades if not panel.get(coin)],
    }
    slices = {
        "collector": summarize_slice(
            "collector", collector, slice_rows(panel, collector, None, "all"), missing["collector"]
        ),
        "trades": summarize_slice(
            "trades", trades, slice_rows(panel, trades, holds, "all"), missing["trades"]
        ),
        "trades_inside_hold": summarize_slice(
            "trades_inside_hold", trades, slice_rows(panel, trades, holds, "inside"), missing["trades"]
        ),
        "trades_outside_hold": summarize_slice(
            "trades_outside_hold", trades, slice_rows(panel, trades, holds, "outside"), missing["trades"]
        ),
    }
    payload = {
        "self_check": check,
        "window_start_utc": "2026-07-20",
        "method": "research/gear23_funding/METHOD.md",
        "min_per_coin": MIN_N,
        "min_pooled": MIN_POOLED,
        "H4": {
            "status": "not_run",
            "reason": "spread_long needs backup ticks. Local parquet under research/testdata is only 2026-08-08. Backup was not copied.",
        },
        "H7": {
            "status": "not_run",
            "reason": "spread_long needs backup ticks. Local parquet under research/testdata is only 2026-08-08. Backup was not copied.",
        },
        "slices": slices,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(clean(payload), ensure_ascii=False, indent=2) + "\n")
    print(f"wrote {OUT}")
    for name, block in slices.items():
        h1 = block["H1"]["correlation"]
        h3 = block["H3"]["autocorr"]
        h3d = block["H3"]["corr_gap_with_next_change"]
        print(
            name,
            "coins",
            block["n_coins_with_rows"],
            "rows",
            block["n_rows"],
            "status",
            block["status"],
            "H1_med",
            h1["per_coin"]["median_pearson"],
            "H1_pool",
            h1["pooled"]["pearson"],
            "same",
            block["H1"]["same_sign_share"],
            "H3_med",
            h3["per_coin"]["median_pearson"],
            "H3_pool",
            h3["pooled"]["pearson"],
            "H3d_med",
            h3d["per_coin"]["median_pearson"],
            "H3d_pool",
            h3d["pooled"]["pearson"],
            "H8",
            block["H8"]["median_abs_change"],
            "H9",
            block["H9"]["median_abs_gap"],
            "gap_med",
            block["H2"]["gap"]["median"],
            "gap_mad",
            block["H2"]["gap"]["mad"],
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
