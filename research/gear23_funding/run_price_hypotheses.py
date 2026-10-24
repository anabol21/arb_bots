"""H5 and H6: funding changes and printed rates against each venue's own price.

Stdlib only. Bybit rates are joined only to Bybit closes, OKX rates only to
OKX closes. Price at a settlement is the close of the 1h candle that ends at
that timestamp. Aggregation matches H1: per-coin correlations at n>=30 pairs,
pooled correlation after subtracting each coin's mean at n>=200.

H4 and H7 are not computed here.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from hypothesis_stats import self_check, within_correlation

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
ROOT = HERE / "data" / "market_funding"
FUNDING_CACHE = ROOT / "cache"
PRICE_ROOT = ROOT / "prices"
TRIPS = HERE / "tables" / "round_trips.csv"
COLLECTOR = REPO / "bybit_okx_universe.csv"
OUT = HERE / "tables" / "price_hypotheses.json"

START = datetime(2026, 7, 20, tzinfo=timezone.utc)
END = datetime(2026, 10, 23, tzinfo=timezone.utc)
START_MS = int(START.timestamp() * 1000)
END_MS = int(END.timestamp() * 1000)
CANDLE_START_MS = START_MS - 3_600_000
HOUR_MS = 3_600_000
MIN_N = 30
MIN_POOLED = 200
MIN_GAP_HOURS = 0.5


def hour_utc(ts_ms: int) -> str:
    hour = ts_ms // HOUR_MS * HOUR_MS
    return datetime.fromtimestamp(hour / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def collector_coins() -> list[str]:
    coins: list[str] = []
    with COLLECTOR.open(newline="") as handle:
        for row in csv.DictReader(handle):
            coins.append(row["base_coin"])
            if len(coins) >= 337:
                break
    return coins


def load_trips() -> tuple[list[str], dict[str, list[tuple[str, str]]]]:
    holds: dict[str, list[tuple[str, str]]] = {}
    coins: list[str] = []
    seen: set[str] = set()
    with TRIPS.open(newline="") as handle:
        for row in csv.DictReader(handle):
            coin = row["coin"]
            if coin not in seen:
                seen.add(coin)
                coins.append(coin)
            holds.setdefault(coin, []).append((row["open_utc"], row["close_utc"]))
    return coins, holds


def hour_inside(hour: str, intervals: list[tuple[str, str]]) -> bool:
    return any(open_utc <= hour <= close_utc for open_utc, close_utc in intervals)


def load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def price_map(exchange: str, coin: str) -> dict[int, float] | None:
    payload = load_json(PRICE_ROOT / exchange / f"{coin}.json")
    if not payload or not payload.get("complete"):
        return None
    if payload.get("start_ms") != CANDLE_START_MS or payload.get("end_ms") != END_MS:
        return None
    return {int(row["ts_ms"]): float(row["close"]) for row in payload.get("rows") or []}


def rate_series(exchange: str, coin: str) -> list[dict] | None:
    payload = load_json(FUNDING_CACHE / exchange / f"{coin}.json")
    if not payload or not payload.get("rows"):
        return None
    out: list[dict] = []
    prev = None
    for row in payload["rows"]:
        ts = int(row["ts_ms"])
        gap = None if prev is None else (ts - prev) / HOUR_MS
        settled = row["realized_rate"] if row.get("realized_rate") is not None else row.get("funding_rate")
        rate = None
        if gap is not None and gap >= MIN_GAP_HOURS and settled is not None:
            rate = float(settled) * 100.0 * (8.0 / gap)
        out.append({"ts": ts, "rate": rate, "gap": gap})
        prev = ts
    out.sort(key=lambda item: item["ts"])
    return out


def price_at(prices: dict[int, float], ts_ms: int) -> float | None:
    return prices.get(ts_ms - HOUR_MS)


def scaled_return(p_prev: float, p_now: float, interval_hours: float) -> float | None:
    if p_prev <= 0.0 or interval_hours <= 0.0:
        return None
    return ((p_now / p_prev) - 1.0) * 100.0 * (8.0 / interval_hours)


def build_pairs(exchange: str, coins: list[str]) -> dict:
    """Return H5/H6 pairs and coverage counts for one exchange."""
    h5: list[dict] = []
    h6: list[dict] = []
    coverage = {
        "coins_requested": len(coins),
        "coins_with_funding": 0,
        "coins_with_price": 0,
        "coins_with_both": 0,
        "settlements_in_window": 0,
        "settlements_with_rate": 0,
        "settlements_with_price": 0,
        "missing_price": [],
        "missing_funding": [],
    }
    for coin in coins:
        rates = rate_series(exchange, coin)
        prices = price_map(exchange, coin)
        if rates is not None:
            coverage["coins_with_funding"] += 1
        else:
            coverage["missing_funding"].append(coin)
        if prices is not None:
            coverage["coins_with_price"] += 1
        else:
            coverage["missing_price"].append(coin)
        if rates is None or prices is None:
            continue
        coverage["coins_with_both"] += 1
        for row in rates:
            if START_MS <= row["ts"] < END_MS:
                coverage["settlements_in_window"] += 1
                if row["rate"] is not None:
                    coverage["settlements_with_rate"] += 1
                if price_at(prices, row["ts"]) is not None:
                    coverage["settlements_with_price"] += 1
        for prev, cur in zip(rates, rates[1:]):
            if not (START_MS <= cur["ts"] < END_MS):
                continue
            interval = (cur["ts"] - prev["ts"]) / HOUR_MS
            if prev["rate"] is None or cur["rate"] is None or interval < MIN_GAP_HOURS:
                continue
            p_prev = price_at(prices, prev["ts"])
            p_cur = price_at(prices, cur["ts"])
            if p_prev is None or p_cur is None:
                continue
            y = scaled_return(p_prev, p_cur, interval)
            if y is None:
                continue
            h5.append({
                "coin": coin,
                "hour": hour_utc(cur["ts"]),
                "x": cur["rate"] - prev["rate"],
                "y": y,
            })
        for cur, nxt in zip(rates, rates[1:]):
            if not (START_MS <= cur["ts"] < END_MS):
                continue
            interval = (nxt["ts"] - cur["ts"]) / HOUR_MS
            if cur["rate"] is None or interval < MIN_GAP_HOURS:
                continue
            p_cur = price_at(prices, cur["ts"])
            p_nxt = price_at(prices, nxt["ts"])
            if p_cur is None or p_nxt is None:
                continue
            y = scaled_return(p_cur, p_nxt, interval)
            if y is None:
                continue
            h6.append({
                "coin": coin,
                "hour": hour_utc(cur["ts"]),
                "x": cur["rate"],
                "y": y,
            })
    return {"H5": h5, "H6": h6, "coverage": coverage}


def select_pairs(
    pairs: list[dict],
    coins: set[str],
    holds: dict[str, list[tuple[str, str]]],
    mode: str,
) -> dict[str, list[tuple[float, float]]]:
    grouped: dict[str, list[tuple[float, float]]] = {}
    for pair in pairs:
        coin = pair["coin"]
        if coin not in coins:
            continue
        inside = hour_inside(pair["hour"], holds.get(coin, []))
        if mode == "inside" and not inside:
            continue
        if mode == "outside" and inside:
            continue
        grouped.setdefault(coin, []).append((pair["x"], pair["y"]))
    return grouped


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


def larger(h5: float | None, h6: float | None) -> str | None:
    if h5 is None or h6 is None:
        return None
    a5 = abs(h5)
    a6 = abs(h6)
    if a5 == a6:
        return "tie"
    return "H5" if a5 > a6 else "H6"


def main() -> int:
    check = self_check()
    if not check["ok"]:
        print(json.dumps(check), file=sys.stderr)
        return 1
    collector = collector_coins()
    trades, holds = load_trips()
    collector_set = set(collector)
    trade_set = set(trades)
    # Price files exist only for the joint universe. Coverage is counted on the
    # coins that can have a venue series: collector members of that download
    # plus the trade coins. ICX and LRC stay in the requested collector list.
    download_coins: list[str] = []
    seen: set[str] = set()
    price_coins = set()
    for exchange in ("bybit", "okx"):
        folder = PRICE_ROOT / exchange
        if folder.is_dir():
            for path in folder.glob("*.json"):
                price_coins.add(path.stem)
    funding_coins = set()
    for exchange in ("bybit", "okx"):
        folder = FUNDING_CACHE / exchange
        if folder.is_dir():
            for path in folder.glob("*.json"):
                funding_coins.add(path.stem)
    for coin in collector + trades:
        if coin in funding_coins or coin in price_coins:
            if coin not in seen:
                seen.add(coin)
                download_coins.append(coin)
    by_exchange = {}
    for exchange in ("bybit", "okx"):
        built = build_pairs(exchange, download_coins)
        slices = {}
        for slice_name, coin_set, mode in (
            ("collector", collector_set, "all"),
            ("trades", trade_set, "all"),
            ("trades_inside_hold", trade_set, "inside"),
            ("trades_outside_hold", trade_set, "outside"),
        ):
            block = {}
            for hyp in ("H5", "H6"):
                grouped = select_pairs(built[hyp], coin_set, holds, mode)
                stats = within_correlation(grouped, MIN_N, MIN_POOLED)
                n_coins_ge = stats["per_coin"]["n_coins"]
                estimable = stats["pooled"]["estimable"] or n_coins_ge > 0
                status = None
                if slice_name == "trades_inside_hold" and not stats["pooled"]["estimable"] and n_coins_ge == 0:
                    status = "на сделках не измерено"
                block[hyp] = {
                    "status": status,
                    "n_coins_with_pairs": stats["n_coins_with_pairs"],
                    "correlation": stats,
                }
            slices[slice_name] = block
        by_exchange[exchange] = {"coverage": built["coverage"], "slices": slices}

    comparison = {}
    ordering = {}
    for slice_name in ("collector", "trades"):
        comparison[slice_name] = {}
        for exchange in ("bybit", "okx"):
            h5 = by_exchange[exchange]["slices"][slice_name]["H5"]["correlation"]["pooled"]["pearson"]
            h6 = by_exchange[exchange]["slices"][slice_name]["H6"]["correlation"]["pooled"]["pearson"]
            comparison[slice_name][exchange] = {
                "H5_pooled_pearson": h5,
                "H6_pooled_pearson": h6,
                "abs_H5": None if h5 is None else abs(h5),
                "abs_H6": None if h6 is None else abs(h6),
                "larger_abs_pooled_pearson": larger(h5, h6),
            }
    for exchange in ("bybit", "okx"):
        left = comparison["collector"][exchange]["larger_abs_pooled_pearson"]
        right = comparison["trades"][exchange]["larger_abs_pooled_pearson"]
        ordering[exchange] = None if left is None or right is None else left == right

    errors = []
    error_path = PRICE_ROOT / "errors.csv"
    if error_path.is_file():
        with error_path.open(newline="") as handle:
            errors = list(csv.DictReader(handle))
    coverage_complete = all(
        not by_exchange[exchange]["coverage"]["missing_price"]
        and not by_exchange[exchange]["coverage"]["missing_funding"]
        for exchange in ("bybit", "okx")
    )
    payload = {
        "self_check": check,
        "window_start_utc": "2026-07-20",
        "window_end_utc_exclusive": "2026-10-23",
        "price_rule": "close of the 1h candle that ends at the settlement timestamp",
        "H5": "corr(rate_per_8h_t - rate_per_8h_prev, price return over that same interval, scaled to 8h)",
        "H6": "corr(rate_per_8h printed at t, price return from t to the next settlement, scaled to 8h)",
        "coverage_complete_for_coins_with_funding_or_price_files": coverage_complete,
        "download_errors": errors,
        "comparison_abs_pooled_pearson": comparison,
        "ordering_same_on_collector_and_trades": ordering,
        "exchanges": by_exchange,
        "H4": {"status": "not_run"},
        "H7": {"status": "not_run"},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(clean(payload), ensure_ascii=False, indent=2) + "\n")
    print(f"wrote {OUT}")
    for exchange, block in by_exchange.items():
        cov = block["coverage"]
        print(
            exchange,
            "funding",
            cov["coins_with_funding"],
            "price",
            cov["coins_with_price"],
            "both",
            cov["coins_with_both"],
            "missing_price",
            len(cov["missing_price"]),
            "settlements_rate",
            cov["settlements_with_rate"],
            "settlements_price",
            cov["settlements_with_price"],
        )
        for slice_name, hyps in block["slices"].items():
            for hyp, body in hyps.items():
                pooled = body["correlation"]["pooled"]
                per = body["correlation"]["per_coin"]
                print(
                    " ",
                    slice_name,
                    hyp,
                    "status",
                    body["status"],
                    "points",
                    pooled["n"],
                    "med",
                    per["median_pearson"],
                    "pool",
                    pooled["pearson"],
                )
    print("ordering", ordering)
    return 0


if __name__ == "__main__":
    sys.exit(main())
