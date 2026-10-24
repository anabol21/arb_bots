"""Download public Bybit and OKX funding rates for the joint USDT-perp market.

Read-only. Writes a local cache and three tables under
``research/gear23_funding/data/market_funding/``. Re-running skips coins
whose cache is already complete for this window.

The rate is the exchange fraction per settlement (0.0001 = 0.01% per
interval). ``rate_pct`` is that fraction times 100, the same percent unit
as the spread. ``rate_per_8h_pct`` rescales by the gap since the previous
settlement of that same series so a 1h print and an 8h print can be compared.
"""

from __future__ import annotations

import csv
import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "data" / "market_funding"
CACHE = ROOT / "cache"
START = datetime(2026, 4, 1, tzinfo=timezone.utc)
END = datetime(2026, 10, 23, tzinfo=timezone.utc)
START_MS = int(START.timestamp() * 1000)
END_MS = int(END.timestamp() * 1000)
WORKERS = 8
MAX_PAGES = 40


class Limiter:
    def __init__(self, per_sec: float) -> None:
        self.gap = 1.0 / per_sec
        self.lock = threading.Lock()
        self.nxt = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = self.nxt - now
            self.nxt = max(now, self.nxt) + self.gap
        if delay > 0:
            time.sleep(delay)


BYBIT_LIMIT = Limiter(8)
OKX_LIMIT = Limiter(8)


def http_json(url: str, limiter: Limiter) -> dict:
    last_error: Exception | None = None
    for attempt in range(6):
        limiter.wait()
        req = urllib.request.Request(url, headers={"User-Agent": "gear23-funding-market"})
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {429, 500, 502, 503, 504}:
                raise
            time.sleep(1.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"http_failed {url}") from last_error


def utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def joint_universe() -> list[dict]:
    bybit = http_json(
        "https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000",
        BYBIT_LIMIT,
    )
    bybit_rows = {}
    for row in (bybit.get("result") or {}).get("list") or []:
        if (
            str(row.get("symbol") or "").endswith("USDT")
            and row.get("status") == "Trading"
            and row.get("contractType") == "LinearPerpetual"
            and row.get("baseCoin")
        ):
            bybit_rows[str(row["baseCoin"])] = row
    okx = http_json("https://www.okx.com/api/v5/public/instruments?instType=SWAP", OKX_LIMIT)
    okx_rows = {}
    for row in okx.get("data") or []:
        inst = str(row.get("instId") or "")
        if inst.endswith("-USDT-SWAP") and row.get("state") == "live":
            okx_rows[inst[: -len("-USDT-SWAP")]] = row
    both = sorted(set(bybit_rows) & set(okx_rows))
    out = []
    for coin in both:
        b = bybit_rows[coin]
        o = okx_rows[coin]
        out.append({
            "base_coin": coin,
            "bybit_symbol": b["symbol"],
            "okx_symbol": o["instId"],
            "bybit_funding_interval_hours_now": int(b.get("fundingInterval") or 0) / 60,
            "okx_ct_val": o.get("ctVal"),
        })
    return out


def fetch_bybit(symbol: str) -> list[dict]:
    rows: dict[int, dict] = {}
    end = END_MS
    for _ in range(MAX_PAGES):
        url = (
            "https://api.bybit.com/v5/market/funding/history"
            f"?category=linear&symbol={symbol}&startTime={START_MS}&endTime={end}&limit=200"
        )
        page = http_json(url, BYBIT_LIMIT)
        if page.get("retCode") not in (0, "0"):
            raise RuntimeError(f"bybit {symbol} retCode={page.get('retCode')}")
        batch = (page.get("result") or {}).get("list") or []
        if not batch:
            break
        stamps = []
        for row in batch:
            ts = int(row["fundingRateTimestamp"])
            stamps.append(ts)
            rows[ts] = {"ts_ms": ts, "funding_rate": float(row["fundingRate"]), "realized_rate": None}
        oldest = min(stamps)
        if len(batch) < 200 or oldest <= START_MS:
            break
        if oldest >= end:
            break
        end = oldest - 1
    return [rows[k] for k in sorted(rows)]


def fetch_okx(inst_id: str) -> list[dict]:
    rows: dict[int, dict] = {}
    after = ""
    for _ in range(MAX_PAGES):
        url = f"https://www.okx.com/api/v5/public/funding-rate-history?instId={inst_id}&limit=100"
        if after:
            url += f"&after={after}"
        page = http_json(url, OKX_LIMIT)
        if str(page.get("code")) != "0":
            raise RuntimeError(f"okx {inst_id} code={page.get('code')}")
        batch = page.get("data") or []
        if not batch:
            break
        stamps = []
        stop = False
        for row in batch:
            ts = int(row["fundingTime"])
            if ts < START_MS:
                stop = True
                continue
            if ts > END_MS:
                continue
            stamps.append(ts)
            realized = row.get("realizedRate")
            rows[ts] = {
                "ts_ms": ts,
                "funding_rate": float(row["fundingRate"]),
                "realized_rate": None if realized in (None, "") else float(realized),
            }
        if not stamps or len(batch) < 100 or stop:
            break
        oldest = min(int(row["fundingTime"]) for row in batch)
        if after and int(after) <= oldest:
            break
        after = str(oldest)
    return [rows[k] for k in sorted(rows)]


def cache_path(exchange: str, coin: str) -> Path:
    return CACHE / exchange / f"{coin}.json"


def load_cached(exchange: str, coin: str) -> dict | None:
    path = cache_path(exchange, coin)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    if payload.get("complete") and payload.get("start_ms") == START_MS and payload.get("end_ms") == END_MS:
        return payload
    return None


def save_cache(exchange: str, coin: str, symbol: str, rows: list[dict]) -> None:
    path = cache_path(exchange, coin)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "exchange": exchange,
        "base_coin": coin,
        "symbol": symbol,
        "start_ms": START_MS,
        "end_ms": END_MS,
        "complete": True,
        "n": len(rows),
        "rows": rows,
    }))


def download_one(spec: dict, exchange: str) -> tuple[str, str, int | str]:
    coin = spec["base_coin"]
    cached = load_cached(exchange, coin)
    if cached is not None:
        return exchange, coin, int(cached["n"])
    symbol = spec["bybit_symbol"] if exchange == "bybit" else spec["okx_symbol"]
    try:
        rows = fetch_bybit(symbol) if exchange == "bybit" else fetch_okx(symbol)
    except Exception as exc:
        return exchange, coin, f"error:{type(exc).__name__}"
    save_cache(exchange, coin, symbol, rows)
    return exchange, coin, len(rows)


def with_interval(rows: list[dict]) -> list[dict]:
    out = []
    prev = None
    for row in rows:
        gap = None if prev is None else (row["ts_ms"] - prev) / 3_600_000
        settled = row["realized_rate"] if row.get("realized_rate") is not None else row["funding_rate"]
        per_8h = None
        if gap and gap >= 0.5:
            per_8h = settled * (8.0 / gap)
        out.append({
            **row,
            "settled_rate": settled,
            "interval_hours": None if gap is None else round(gap, 4),
            "rate_pct": settled * 100,
            "rate_per_8h_pct": None if per_8h is None else per_8h * 100,
        })
        prev = row["ts_ms"]
    return out


def median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fields: list[str] = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def build(universe: list[dict]) -> dict:
    settlements: list[dict] = []
    coverage = []
    by_coin_hour: dict[tuple[str, int], dict] = {}
    exchange_buckets: dict[tuple[str, int], list[float]] = {}
    for spec in universe:
        coin = spec["base_coin"]
        for exchange, symbol in (("bybit", spec["bybit_symbol"]), ("okx", spec["okx_symbol"])):
            payload = load_cached(exchange, coin)
            rows = with_interval(payload["rows"]) if payload else []
            stamps = [row["ts_ms"] for row in rows]
            gaps = [row["interval_hours"] for row in rows if row["interval_hours"] is not None]
            coverage.append({
                "exchange": exchange,
                "base_coin": coin,
                "symbol": symbol,
                "n": len(rows),
                "first_utc": utc(min(stamps)) if stamps else "",
                "last_utc": utc(max(stamps)) if stamps else "",
                "median_interval_hours": None if not gaps else round(median(gaps) or 0, 4),
                "current_bybit_interval_hours": spec["bybit_funding_interval_hours_now"],
            })
            for row in rows:
                hour = row["ts_ms"] // 3_600_000 * 3_600_000
                settlements.append({
                    "exchange": exchange,
                    "base_coin": coin,
                    "symbol": symbol,
                    "ts_ms": row["ts_ms"],
                    "ts_utc": utc(row["ts_ms"]),
                    "hour_utc": utc(hour),
                    "funding_rate": row["funding_rate"],
                    "realized_rate": row["realized_rate"] if row["realized_rate"] is not None else "",
                    "settled_rate": row["settled_rate"],
                    "rate_pct": round(row["rate_pct"], 8),
                    "interval_hours": "" if row["interval_hours"] is None else row["interval_hours"],
                    "rate_per_8h_pct": "" if row["rate_per_8h_pct"] is None else round(row["rate_per_8h_pct"], 8),
                })
                slot = by_coin_hour.setdefault((coin, hour), {
                    "base_coin": coin,
                    "hour_ms": hour,
                    "hour_utc": utc(hour),
                })
                prefix = exchange
                slot[f"{prefix}_rate_pct"] = round(row["rate_pct"], 8)
                slot[f"{prefix}_rate_per_8h_pct"] = (
                    "" if row["rate_per_8h_pct"] is None else round(row["rate_per_8h_pct"], 8)
                )
                slot[f"{prefix}_interval_hours"] = (
                    "" if row["interval_hours"] is None else row["interval_hours"]
                )
                if row["rate_pct"] is not None:
                    exchange_buckets.setdefault((exchange, hour), []).append(row["rate_pct"])
    by_coin = []
    for (coin, hour), slot in sorted(by_coin_hour.items(), key=lambda item: (item[0][0], item[0][1])):
        b = slot.get("bybit_rate_pct")
        o = slot.get("okx_rate_pct")
        slot["both"] = int(b is not None and o is not None)
        slot["rate_diff_pct"] = "" if b is None or o is None else round(b - o, 8)
        b8 = slot.get("bybit_rate_per_8h_pct")
        o8 = slot.get("okx_rate_per_8h_pct")
        slot["rate_per_8h_diff_pct"] = "" if b8 in (None, "") or o8 in (None, "") else round(float(b8) - float(o8), 8)
        by_coin.append(slot)
    by_exchange = []
    for (exchange, hour), values in sorted(exchange_buckets.items(), key=lambda item: (item[0][0], item[0][1])):
        by_exchange.append({
            "exchange": exchange,
            "hour_utc": utc(hour),
            "hour_ms": hour,
            "n_coins": len(values),
            "mean_rate_pct": round(sum(values) / len(values), 8),
            "median_rate_pct": round(median(values) or 0, 8),
        })
    write_csv(ROOT / "settlements.csv", settlements)
    write_csv(ROOT / "by_coin.csv", by_coin)
    write_csv(ROOT / "by_exchange.csv", by_exchange)
    write_csv(ROOT / "coverage.csv", coverage)
    return {
        "settlements": len(settlements),
        "by_coin_hours": len(by_coin),
        "by_coin_both": sum(row["both"] for row in by_coin),
        "by_exchange_hours": len(by_exchange),
        "coins": len(universe),
    }


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    universe = joint_universe()
    write_csv(ROOT / "universe_joint.csv", universe)
    print(f"joint_coins {len(universe)} window {START.date()} .. {END.date()} exclusive end", flush=True)
    errors = []
    done = 0
    total = len(universe) * 2
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = []
        for spec in universe:
            futures.append(pool.submit(download_one, spec, "bybit"))
            futures.append(pool.submit(download_one, spec, "okx"))
        for fut in as_completed(futures):
            exchange, coin, result = fut.result()
            done += 1
            if isinstance(result, str):
                errors.append({"exchange": exchange, "base_coin": coin, "error": result})
            if done % 50 == 0 or done == total:
                print(f"progress {done}/{total} errors {len(errors)}", flush=True)
    write_csv(ROOT / "errors.csv", errors)
    summary = build(universe)
    summary["errors"] = len(errors)
    summary["window_start_utc"] = START.strftime("%Y-%m-%d")
    summary["window_end_utc_exclusive"] = END.strftime("%Y-%m-%d")
    (ROOT / "meta.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
