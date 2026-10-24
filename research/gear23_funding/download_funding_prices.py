"""Download public Bybit and OKX 1h candles for gear 2.3 H5/H6.

Stdlib urllib only. Read-only. No secrets are printed.

Window of settlement timestamps: 2026-07-20 inclusive through 2026-10-23
exclusive. The price at a settlement timestamp is the close of the 1h candle
that ends at that timestamp, so the cache starts one hour earlier.

A rerun skips coins whose cache is already complete for this window.
Errors are recorded and do not stop the other coins.
Rate limit is about 8 requests/second per host.
"""

from __future__ import annotations

import csv
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
ROOT = HERE / "data" / "market_funding"
PRICE_ROOT = ROOT / "prices"
UNIVERSE_JOINT = ROOT / "universe_joint.csv"
COLLECTOR = REPO / "bybit_okx_universe.csv"
TRIPS = HERE / "tables" / "round_trips.csv"

START = datetime(2026, 7, 20, tzinfo=timezone.utc)
END = datetime(2026, 10, 23, tzinfo=timezone.utc)
START_MS = int(START.timestamp() * 1000)
END_MS = int(END.timestamp() * 1000)
CANDLE_START_MS = START_MS - 3_600_000
HOUR_MS = 3_600_000
WORKERS = 16
BYBIT_PAGES = 12
OKX_PAGES = 40


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
        req = urllib.request.Request(url, headers={"User-Agent": "gear23-funding-prices"})
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {429, 500, 502, 503, 504}:
                raise
            time.sleep(1.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"http_failed {type(last_error).__name__}") from last_error


def cache_path(exchange: str, coin: str) -> Path:
    return PRICE_ROOT / exchange / f"{coin}.json"


def load_complete(exchange: str, coin: str) -> dict | None:
    path = cache_path(exchange, coin)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    if (
        payload.get("complete")
        and payload.get("start_ms") == CANDLE_START_MS
        and payload.get("end_ms") == END_MS
    ):
        return payload
    return None


def save_cache(exchange: str, coin: str, symbol: str, rows: dict[int, float]) -> None:
    path = cache_path(exchange, coin)
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = [{"ts_ms": ts, "close": rows[ts]} for ts in sorted(rows)]
    path.write_text(json.dumps({
        "exchange": exchange,
        "base_coin": coin,
        "symbol": symbol,
        "start_ms": CANDLE_START_MS,
        "end_ms": END_MS,
        "complete": True,
        "n": len(ordered),
        "rows": ordered,
    }))


def fetch_bybit(symbol: str) -> dict[int, float]:
    rows: dict[int, float] = {}
    end = END_MS - 1
    now_ms = int(time.time() * 1000)
    symbol_q = urllib.parse.quote(symbol)
    for _ in range(BYBIT_PAGES):
        url = (
            "https://api.bybit.com/v5/market/kline"
            f"?category=linear&symbol={symbol_q}&interval=60"
            f"&start={CANDLE_START_MS}&end={end}&limit=1000"
        )
        page = http_json(url, BYBIT_LIMIT)
        if page.get("retCode") not in (0, "0"):
            raise RuntimeError(f"bybit retCode={page.get('retCode')}")
        batch = (page.get("result") or {}).get("list") or []
        if not batch:
            break
        oldest = None
        for row in batch:
            ts = int(row[0])
            oldest = ts if oldest is None else min(oldest, ts)
            if ts < CANDLE_START_MS or ts >= END_MS:
                continue
            if ts + HOUR_MS > now_ms:
                continue
            rows[ts] = float(row[4])
        if oldest is None or len(batch) < 1000 or oldest <= CANDLE_START_MS:
            break
        if oldest >= end:
            break
        end = oldest - 1
    return rows


def fetch_okx(inst_id: str) -> dict[int, float]:
    rows: dict[int, float] = {}
    after = str(END_MS)
    inst_q = urllib.parse.quote(inst_id)
    for _ in range(OKX_PAGES):
        url = (
            "https://www.okx.com/api/v5/market/history-candles"
            f"?instId={inst_q}&bar=1H&after={after}&limit=100"
        )
        page = http_json(url, OKX_LIMIT)
        if str(page.get("code")) != "0":
            raise RuntimeError(f"okx code={page.get('code')}")
        batch = page.get("data") or []
        if not batch:
            break
        oldest = min(int(row[0]) for row in batch)
        stop = False
        for row in batch:
            ts = int(row[0])
            if ts < CANDLE_START_MS:
                stop = True
                continue
            if ts >= END_MS:
                continue
            if len(row) <= 8 or str(row[8]) != "1":
                continue
            rows[ts] = float(row[4])
        if stop or len(batch) < 100:
            break
        if oldest >= int(after):
            break
        after = str(oldest)
    return rows


def collector_coins() -> list[str]:
    coins: list[str] = []
    with COLLECTOR.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            coins.append(row["base_coin"])
            if len(coins) >= 337:
                break
    return coins


def trade_coins() -> list[str]:
    coins: list[str] = []
    seen: set[str] = set()
    with TRIPS.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            coin = row["coin"]
            if coin not in seen:
                seen.add(coin)
                coins.append(coin)
    return coins


def joint_symbols() -> dict[str, dict]:
    out: dict[str, dict] = {}
    with UNIVERSE_JOINT.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            out[row["base_coin"]] = row
    return out


def target_coins() -> tuple[list[dict], list[str]]:
    joint = joint_symbols()
    wanted: list[str] = []
    seen: set[str] = set()
    for coin in collector_coins():
        if coin in joint and coin not in seen:
            seen.add(coin)
            wanted.append(coin)
    for coin in trade_coins():
        if coin in joint and coin not in seen:
            seen.add(coin)
            wanted.append(coin)
    missing_symbol = [coin for coin in trade_coins() if coin not in joint]
    specs = [joint[coin] for coin in wanted]
    return specs, missing_symbol


def download_one(spec: dict, exchange: str) -> tuple[str, str, int | str]:
    coin = spec["base_coin"]
    cached = load_complete(exchange, coin)
    if cached is not None:
        return exchange, coin, int(cached["n"])
    symbol = spec["bybit_symbol"] if exchange == "bybit" else spec["okx_symbol"]
    try:
        rows = fetch_bybit(symbol) if exchange == "bybit" else fetch_okx(symbol)
    except Exception as exc:
        return exchange, coin, f"error:{type(exc).__name__}"
    save_cache(exchange, coin, symbol, rows)
    return exchange, coin, len(rows)


def main() -> None:
    PRICE_ROOT.mkdir(parents=True, exist_ok=True)
    specs, missing_symbol = target_coins()
    joint = joint_symbols()
    collector_not_in_joint = [coin for coin in collector_coins() if coin not in joint]
    print(
        f"price_coins {len(specs)} missing_symbol {len(missing_symbol)} "
        f"collector_not_in_joint {len(collector_not_in_joint)} "
        f"window {START.date()} .. {END.date()} exclusive end",
        flush=True,
    )
    errors = [{"exchange": "", "base_coin": coin, "error": "missing_symbol"} for coin in missing_symbol]
    done = 0
    total = len(specs) * 2
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = []
        for spec in specs:
            futures.append(pool.submit(download_one, spec, "bybit"))
            futures.append(pool.submit(download_one, spec, "okx"))
        for fut in as_completed(futures):
            exchange, coin, result = fut.result()
            done += 1
            if isinstance(result, str):
                errors.append({"exchange": exchange, "base_coin": coin, "error": result})
            if done % 20 == 0 or done == total:
                print(f"progress {done}/{total} errors {len(errors)}", flush=True)
    error_path = PRICE_ROOT / "errors.csv"
    with error_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["exchange", "base_coin", "error"])
        writer.writeheader()
        writer.writerows(errors)
    meta = {
        "coins": len(specs),
        "missing_symbol": missing_symbol,
        "collector_not_in_joint": collector_not_in_joint,
        "errors": len(errors),
        "window_start_utc": START.strftime("%Y-%m-%d"),
        "window_end_utc_exclusive": END.strftime("%Y-%m-%d"),
        "candle_start_ms": CANDLE_START_MS,
        "end_ms": END_MS,
    }
    (PRICE_ROOT / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta), flush=True)


if __name__ == "__main__":
    main()
