"""Universe liquidity screen — market cap vs turnover for Bybit∩OKX crypto perps.

Track 2 research only. Does not touch the collector, ``model.ipynb``, or live bot.

Data path
---------
1. ``bybit_okx_universe.csv`` — already the intersection of USDT swaps on both venues.
2. Equity / ETF / metal perps dropped via ``research.is_crypto``
   (denylist ``research/data/non_crypto_base_coins.txt``).
3. Market cap: CoinGecko ``/coins/markets`` snapshot (top pages). Daily
   ``market_chart`` is used only when already cached — the public chart
   endpoint 429s after a few dozen coins. CoinPaprika historical returned 402.
4. Daily volume: Bybit linear + OKX SWAP 1D quote volume (USDT), summed.
5. Disk cache under ``research/data/universe_liquidity_cache/``.

Ticker → CoinGecko id
---------------------
Explicit overrides win. Otherwise: symbol match against ``/coins/list``, then
pick the id with the highest ``market_cap`` on ``/coins/markets`` (top pages).
Unmatched symbols are reported, not silently dropped.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from research.is_crypto import (
    DEFAULT_DENYLIST_PATH,
    filter_crypto_dataframe,
    is_crypto,
    load_non_crypto_denylist,
    normalize_base_coin,
)

REPO = Path(__file__).resolve().parents[1]
DEFAULT_UNIVERSE = REPO / "bybit_okx_universe.csv"
CACHE_DIR = REPO / "research" / "data" / "universe_liquidity_cache"
METRICS_CSV = REPO / "research" / "data" / "universe_liquidity_metrics.csv"
FILTERED_CSV = REPO / "research" / "data" / "universe_liquidity_filtered.csv"
VOL_STD_CSV = REPO / "research" / "data" / "universe_spread_std_august.csv"
VOL_FILTERED_CSV = REPO / "research" / "data" / "universe_vol_quantile_filtered.csv"
JOINT_FILTERED_CSV = REPO / "research" / "data" / "universe_joint_q25_filtered.csv"
CAPSTAR_FILTERED_CSV = REPO / "research" / "data" / "universe_capstar_filtered.csv"
OVERVIEW_SUMMARY = REPO / "viz" / "data" / "overview_summary.json"
DEFAULT_LEAN_TICKS = REPO / "output" / "lean_ticks"

# Spread-opportunity calibration (not the liquidity window).
# Source: viz/data/overview_summary.json — all-tick p50/p95/p99 of
# spread_long / spread_short over compacted lean ticks.
SPREAD_WINDOW_START = "2026-07-22T11:05:00Z"
SPREAD_WINDOW_END = "2026-08-27T11:40:00Z"
ENTRY_THRESH_PCT = 0.5  # model.ipynb VARIATION thresh_open_long/short
# Just above the largest plot-set coin whose max(p99_long, p99_short) >= 0.5
# (2Z ≈ $190M). Above $200M the joined August sample has 0/76 such coins.
CAP_CUTOFF_USD = 200_000_000.0
P99_OVERFLOW = 9.0  # overview hist clips at 10.0; treat as artifact

# August-only spread СКО. Lean ticks on disk start 2026-07-22 and end
# 2026-08-27T11:40Z; the first August file is 2026-08-03T13:35Z (Aug 1–3
# morning is missing). Calendar August 31 is not on disk.
# This is computed (not a stored overview field). Overview has p50/p95/p99
# only. volume_cv from the liquidity screen is std/mean of daily USD volume
# and is NOT this metric.
VOL_WINDOW_START = "2026-08-03T13:35:00Z"
VOL_WINDOW_END = "2026-08-27T11:40:00Z"
VOL_MIN_TICKS = 10_000  # below this: sparse / missing, not "true low vol"
VOL_QUANTILE_DEFAULT = 0.20  # drop below Q20 unless a gap is marked
VOL_QUANTILES = (0.10, 0.20, 0.25, 0.50)
# Joint screen: drop quiet ∧ large. One free threshold = Q25 of std_spread
# on the std-available plot-set; cap* is the OLS inverse at that Q.
JOINT_STD_QUANTILE = 0.25
# std_spread and the §11 OLS are in **percent** (same L1 formula as overview).
STD_SPREAD_UNIT = "percent"

WINDOW_END = date(2026, 9, 3)
WINDOW_DAYS = 30
WINDOW_START = WINDOW_END - timedelta(days=WINDOW_DAYS - 1)  # 2026-08-05 inclusive
MIN_METRIC_DAYS = 20
GECKO_MARKET_PAGES = 8  # 8 * 250 = 2000 coins by market cap

GECKO_BASE = "https://api.coingecko.com/api/v3"
UA = {
    "User-Agent": "Mozilla/5.0 spread-research-universe-liquidity/0.1",
    "Accept": "application/json",
}

# Venue ticker → CoinGecko id. Reviewed 2026-09-03.
# Highest-mcap-among-symbol is usually right; these are known traps.
GECKO_ID_OVERRIDES: dict[str, str] = {
    "DOT": "polkadot",
    "MMT": "momentum-3",
    "LA": "lagrange",
    "GRAM": "gram-2",
    "ZKP": "zkpass",
    "DATA": "streamr",
    "S": "sonic-3",
    "COMP": "compound-governance-token",
    "POL": "polygon-ecosystem-token",
    "RENDER": "render-token",
    "LIT": "lighter",
    "HYPE": "hyperliquid",
    "TON": "the-open-network",
}

# Never pick these gecko ids for the ticker (name collisions).
GECKO_ID_DENY: dict[str, frozenset[str]] = {
    "GRAM": frozenset({"the-open-network", "toncoin"}),
}

POSSIBLE_DENYLIST_COLLISIONS = ("CC", "F", "W", "O")

STATUS_OK = "ok"
STATUS_UNMATCHED = "unmatched_coingecko"
STATUS_MISSING_CAP = "missing_market_cap"
STATUS_MISSING_VOL = "missing_volume"
STATUS_SPARSE = "insufficient_days"

# Back-compat alias so an older notebook cell still imports.
PAPRIKA_ID_OVERRIDES = GECKO_ID_OVERRIDES


def window_spec() -> dict[str, Any]:
    return {
        "window_start": WINDOW_START.isoformat(),
        "window_end": WINDOW_END.isoformat(),
        "n_calendar_days": WINDOW_DAYS,
        "min_metric_days": MIN_METRIC_DAYS,
        "inclusive": True,
        "source": "coingecko_markets_cap + bybit_okx_1d_quote_volume",
        "volume_def": "sum of Bybit linear + OKX SWAP 1D quote volume (USDT)",
        "cap_def": "CoinGecko /markets snapshot, or last daily market_chart cap if cached",
        "asof_note": "30 calendar days ending 2026-09-03",
    }


def _http_get_json(url: str, *, timeout: float = 40.0, retries: int = 8) -> Any:
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 404:
                raise
            sleep_s = 45.0 if exc.code == 429 else min(2**attempt * 0.6, 15.0)
            time.sleep(sleep_s)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last = exc
            time.sleep(min(2**attempt * 0.6, 15.0))
    raise RuntimeError(f"GET failed after retries: {url} ({last})")


def _cache_json_path(name: str) -> Path:
    return CACHE_DIR / name


def load_or_fetch_json(
    relpath: str,
    url: str,
    *,
    sleep_s: float = 0.0,
) -> Any:
    path = _cache_json_path(relpath)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    if sleep_s > 0:
        time.sleep(sleep_s)
    data = _http_get_json(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return data


def load_universe(path: Path = DEFAULT_UNIVERSE) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "base_coin" not in df.columns:
        raise ValueError(f"universe CSV missing base_coin: {path}")
    df = df.copy()
    df["base_coin"] = df["base_coin"].map(normalize_base_coin)
    return df


def split_crypto_equity(universe: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    crypto = filter_crypto_dataframe(universe, crypto=True)
    equity = filter_crypto_dataframe(universe, crypto=False)
    return crypto.reset_index(drop=True), equity.reset_index(drop=True)


def fetch_gecko_coins_list() -> list[dict]:
    return load_or_fetch_json("gecko_coins_list.json", f"{GECKO_BASE}/coins/list")


def fetch_gecko_markets_pages(*, n_pages: int = GECKO_MARKET_PAGES) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, n_pages + 1):
        rel = f"gecko_markets/page_{page:02d}.json"
        q = urllib.parse.urlencode(
            {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 250,
                "page": page,
                "sparkline": "false",
            }
        )
        print(f"gecko markets page {page}/{n_pages}", flush=True)
        chunk = load_or_fetch_json(rel, f"{GECKO_BASE}/coins/markets?{q}", sleep_s=0.35)
        rows.extend(chunk)
        if len(chunk) < 250:
            break
    return rows


def _ms_to_date(ms: float) -> date:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).date()


def fetch_gecko_market_chart(gecko_id: str, *, sleep_s: float = 1.2) -> dict:
    rel = f"gecko_market_chart/{gecko_id}.json"
    q = urllib.parse.urlencode(
        {"vs_currency": "usd", "days": WINDOW_DAYS, "interval": "daily"}
    )
    url = f"{GECKO_BASE}/coins/{urllib.parse.quote(gecko_id, safe='-')}/market_chart?{q}"
    return load_or_fetch_json(rel, url, sleep_s=sleep_s)



def chart_to_frame(gecko_id: str, payload: dict) -> pd.DataFrame:
    caps = {int(x[0]): x[1] for x in (payload.get("market_caps") or []) if x}
    vols = {int(x[0]): x[1] for x in (payload.get("total_volumes") or []) if x}
    keys = sorted(set(caps) | set(vols))
    if not keys:
        return pd.DataFrame(
            columns=["gecko_id", "date", "volume_usd", "market_cap_usd"]
        )
    rows = []
    for ts in keys:
        d = _ms_to_date(ts)
        if d < WINDOW_START or d > WINDOW_END:
            continue
        cap = caps.get(ts)
        vol = vols.get(ts)
        rows.append(
            {
                "gecko_id": gecko_id,
                "date": d,
                "volume_usd": float(vol) if vol is not None else np.nan,
                "market_cap_usd": float(cap) if cap is not None else np.nan,
            }
        )
    if not rows:
        return pd.DataFrame(
            columns=["gecko_id", "date", "volume_usd", "market_cap_usd"]
        )
    out = pd.DataFrame(rows)
    return out.drop_duplicates(subset=["date"], keep="last").sort_values("date").reset_index(
        drop=True
    )


def resolve_gecko_id(
    symbol: str,
    coins_by_symbol: dict[str, list[dict]],
    markets_by_id: dict[str, dict],
    markets_by_symbol: dict[str, list[dict]],
) -> dict[str, Any]:
    sym = normalize_base_coin(symbol)
    row: dict[str, Any] = {
        "base_coin": sym,
        "gecko_id": None,
        "gecko_name": None,
        "map_method": None,
        "n_candidates": 0,
        "candidate_ids": "",
        "ticker_volume_usd": None,
        "ticker_mcap_usd": None,
        "map_note": "",
    }
    deny = GECKO_ID_DENY.get(sym, frozenset())
    list_cands = [c for c in coins_by_symbol.get(sym, []) if c.get("id") not in deny]
    mkt_cands = [c for c in markets_by_symbol.get(sym, []) if c.get("id") not in deny]
    row["n_candidates"] = max(len(list_cands), len(mkt_cands))
    row["candidate_ids"] = ",".join(
        dict.fromkeys([c.get("id", "") for c in mkt_cands + list_cands])
    )[:400]

    if sym in GECKO_ID_OVERRIDES:
        gid = GECKO_ID_OVERRIDES[sym]
        row["gecko_id"] = gid
        row["map_method"] = "override"
        m = markets_by_id.get(gid)
        if m:
            row["gecko_name"] = m.get("name")
            row["ticker_mcap_usd"] = m.get("market_cap")
            row["ticker_volume_usd"] = m.get("total_volume")
        else:
            for c in list_cands:
                if c.get("id") == gid:
                    row["gecko_name"] = c.get("name")
                    break
            row["map_note"] = "override id not in top markets pages"
        return row

    if mkt_cands:
        best = max(
            mkt_cands,
            key=lambda c: (
                float(c.get("market_cap") or 0),
                float(c.get("total_volume") or 0),
            ),
        )
        row["gecko_id"] = best.get("id")
        row["gecko_name"] = best.get("name")
        row["map_method"] = "markets_max_mcap"
        row["ticker_mcap_usd"] = best.get("market_cap")
        row["ticker_volume_usd"] = best.get("total_volume")
        if len(mkt_cands) > 1:
            ranked = sorted(
                mkt_cands, key=lambda c: float(c.get("market_cap") or 0), reverse=True
            )
            a, b = float(ranked[0].get("market_cap") or 0), float(
                ranked[1].get("market_cap") or 0
            )
            if a > 0 and b > 0.25 * a:
                row["map_note"] = (
                    f"close mcap runner-up {ranked[1].get('id')} "
                    f"cap={b:.0f} vs {a:.0f}"
                )
        return row

    if len(list_cands) == 1:
        row["gecko_id"] = list_cands[0].get("id")
        row["gecko_name"] = list_cands[0].get("name")
        row["map_method"] = "unique_symbol_not_in_top_markets"
        row["map_note"] = "in coins/list but not in cached /markets pages"
        return row

    if list_cands:
        row["map_method"] = "unmatched"
        row["map_note"] = (
            f"{len(list_cands)} coins/list hits, none in top markets; not guessing"
        )
        return row

    row["map_method"] = "unmatched"
    row["map_note"] = "no CoinGecko coins/list symbol match"
    return row


def map_universe(
    crypto_df: pd.DataFrame,
    *,
    fetch: bool = True,
) -> pd.DataFrame:
    if fetch:
        coins = fetch_gecko_coins_list()
        markets = fetch_gecko_markets_pages()
    else:
        coins = (
            json.loads(_cache_json_path("gecko_coins_list.json").read_text(encoding="utf-8"))
            if _cache_json_path("gecko_coins_list.json").is_file()
            else fetch_gecko_coins_list()
        )
        markets = []
        for page in range(1, GECKO_MARKET_PAGES + 1):
            p = _cache_json_path(f"gecko_markets/page_{page:02d}.json")
            if not p.is_file():
                break
            markets.extend(json.loads(p.read_text(encoding="utf-8")))
        if not markets:
            markets = fetch_gecko_markets_pages()

    coins_by_symbol: dict[str, list[dict]] = defaultdict(list)
    for c in coins:
        coins_by_symbol[str(c.get("symbol") or "").upper()].append(c)

    markets_by_id = {m["id"]: m for m in markets if m.get("id")}
    markets_by_symbol: dict[str, list[dict]] = defaultdict(list)
    for m in markets:
        markets_by_symbol[str(m.get("symbol") or "").upper()].append(m)

    rows = [
        resolve_gecko_id(c, coins_by_symbol, markets_by_id, markets_by_symbol)
        for c in crypto_df["base_coin"].tolist()
    ]
    return crypto_df.merge(pd.DataFrame(rows), on="base_coin", how="left")


def _finite_pos(s: pd.Series) -> pd.Series:
    x = pd.to_numeric(s, errors="coerce")
    return x.where(np.isfinite(x) & (x > 0))


def metrics_from_history(hist: pd.DataFrame) -> dict[str, Any]:
    empty = {
        "n_hist_days": 0,
        "n_days_both": 0,
        "n_days_volume": 0,
        "n_days_mcap": 0,
        "market_cap_usd": np.nan,
        "median_rel_vol": np.nan,
        "mean_rel_vol": np.nan,
        "volume_cv": np.nan,
        "median_volume_usd": np.nan,
        "mean_volume_usd": np.nan,
        "hist_start": None,
        "hist_end": None,
    }
    if hist is None or hist.empty:
        return empty

    vol = _finite_pos(hist["volume_usd"])
    cap = _finite_pos(hist["market_cap_usd"])
    both = vol.notna() & cap.notna()
    rel = (vol / cap).where(both)

    out = dict(empty)
    out["n_hist_days"] = int(len(hist))
    out["n_days_volume"] = int(vol.notna().sum())
    out["n_days_mcap"] = int(cap.notna().sum())
    out["n_days_both"] = int(both.sum())
    if hist["date"].notna().any():
        out["hist_start"] = str(min(hist["date"]))
        out["hist_end"] = str(max(hist["date"]))

    cap_valid = cap.dropna()
    if len(cap_valid):
        out["market_cap_usd"] = float(cap_valid.iloc[-1])

    if rel.notna().any():
        out["median_rel_vol"] = float(rel.median())
        out["mean_rel_vol"] = float(rel.mean())

    vol_valid = vol.dropna()
    if len(vol_valid):
        out["median_volume_usd"] = float(vol_valid.median())
        out["mean_volume_usd"] = float(vol_valid.mean())
        mean_v = float(vol_valid.mean())
        if mean_v > 0 and len(vol_valid) >= 2:
            out["volume_cv"] = float(vol_valid.std(ddof=1) / mean_v)
    return out


OKX_HISTORY = "https://www.okx.com/api/v5/market/history-candles"
BYBIT_KLINE = "https://api.bybit.com/v5/market/kline"


def _okx_rows_from_payload(payload: Any) -> list[dict]:
    rows = payload.get("data") if isinstance(payload, dict) else payload
    out = []
    for r in rows or []:
        d = _ms_to_date(int(r[0]))
        if d < WINDOW_START or d > WINDOW_END:
            continue
        try:
            quote = float(r[7])
        except (TypeError, ValueError, IndexError):
            quote = np.nan
        out.append({"date": d, "okx_quote_usd": quote})
    return out


def _bybit_rows_from_payload(payload: Any) -> list[dict]:
    lst = (payload.get("result") or {}).get("list") if isinstance(payload, dict) else None
    out = []
    for r in lst or []:
        d = _ms_to_date(int(r[0]))
        if d < WINDOW_START or d > WINDOW_END:
            continue
        try:
            quote = float(r[6])
        except (TypeError, ValueError, IndexError):
            quote = np.nan
        out.append({"date": d, "bybit_quote_usd": quote})
    return out


def load_okx_daily(inst_id: str, *, fetch: bool, sleep_s: float = 0.06) -> list[dict]:
    rel = f"okx_daily/{inst_id}.json"
    path = _cache_json_path(rel)
    if path.is_file():
        return _okx_rows_from_payload(json.loads(path.read_text(encoding="utf-8")))
    if not fetch:
        return []
    q = urllib.parse.urlencode({"instId": inst_id, "bar": "1D", "limit": "40"})
    payload = load_or_fetch_json(rel, f"{OKX_HISTORY}?{q}", sleep_s=sleep_s)
    return _okx_rows_from_payload(payload)


def load_bybit_daily(symbol: str, *, fetch: bool, sleep_s: float = 0.06) -> list[dict]:
    rel = f"bybit_daily/{symbol}.json"
    path = _cache_json_path(rel)
    if path.is_file():
        return _bybit_rows_from_payload(json.loads(path.read_text(encoding="utf-8")))
    if not fetch:
        return []
    start_ms = int(
        datetime(WINDOW_START.year, WINDOW_START.month, WINDOW_START.day, tzinfo=timezone.utc).timestamp()
        * 1000
    )
    end_ms = int(
        datetime(WINDOW_END.year, WINDOW_END.month, WINDOW_END.day, tzinfo=timezone.utc).timestamp() * 1000
        + 86_400_000
    )
    q = urllib.parse.urlencode(
        {
            "category": "linear",
            "symbol": symbol,
            "interval": "D",
            "limit": 40,
            "start": start_ms,
            "end": end_ms,
        }
    )
    payload = load_or_fetch_json(rel, f"{BYBIT_KLINE}?{q}", sleep_s=sleep_s)
    return _bybit_rows_from_payload(payload)


def venue_plus_cap_history(
    *,
    okx_symbol: str,
    bybit_symbol: str,
    snapshot_cap: Optional[float],
    gecko_daily: Optional[pd.DataFrame],
    fetch: bool,
) -> pd.DataFrame:
    okx_df = pd.DataFrame(load_okx_daily(okx_symbol, fetch=fetch))
    bb_df = pd.DataFrame(load_bybit_daily(bybit_symbol, fetch=fetch))
    if okx_df.empty and bb_df.empty:
        return pd.DataFrame(
            columns=["date", "volume_usd", "market_cap_usd", "okx_quote_usd", "bybit_quote_usd"]
        )
    if okx_df.empty:
        daily = bb_df.copy()
        daily["okx_quote_usd"] = np.nan
    elif bb_df.empty:
        daily = okx_df.copy()
        daily["bybit_quote_usd"] = np.nan
    else:
        daily = okx_df.merge(bb_df, on="date", how="outer")

    daily["okx_quote_usd"] = pd.to_numeric(daily["okx_quote_usd"], errors="coerce")
    daily["bybit_quote_usd"] = pd.to_numeric(daily["bybit_quote_usd"], errors="coerce")
    daily["volume_usd"] = daily[["okx_quote_usd", "bybit_quote_usd"]].sum(axis=1, min_count=1)

    if gecko_daily is not None and not gecko_daily.empty:
        cap_map = gecko_daily.set_index("date")["market_cap_usd"]
        daily["market_cap_usd"] = daily["date"].map(cap_map)
    else:
        daily["market_cap_usd"] = np.nan
    if snapshot_cap is not None and np.isfinite(snapshot_cap) and snapshot_cap > 0:
        daily["market_cap_usd"] = daily["market_cap_usd"].fillna(float(snapshot_cap))
    return daily.sort_values("date").drop_duplicates(subset=["date"]).reset_index(drop=True)


def classify_metric_row(row: pd.Series) -> str:
    if pd.isna(row.get("gecko_id")) or not row.get("gecko_id"):
        return STATUS_UNMATCHED
    n_both = int(row.get("n_days_both") or 0)
    n_vol = int(row.get("n_days_volume") or 0)
    n_cap = int(row.get("n_days_mcap") or 0)
    if n_vol == 0:
        return STATUS_MISSING_VOL
    if n_cap == 0 or not np.isfinite(row.get("market_cap_usd") or np.nan):
        return STATUS_MISSING_CAP
    if n_both < MIN_METRIC_DAYS:
        return STATUS_SPARSE
    need = ("median_rel_vol", "mean_rel_vol", "volume_cv", "market_cap_usd")
    if any(not np.isfinite(row.get(k) or np.nan) for k in need):
        return STATUS_SPARSE
    return STATUS_OK


def build_metrics_table(
    mapped: pd.DataFrame,
    *,
    fetch: bool = True,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    histories: dict[str, pd.DataFrame] = {}
    recs = mapped.to_dict("records")

    # Daily CoinGecko cap is optional and cache-only: public market_chart 429s
    # after ~40 coins. Snapshot cap from /markets fills the rest.
    gecko_daily: dict[str, pd.DataFrame] = {}
    for rec in recs:
        gid = rec.get("gecko_id")
        if not gid or gid in gecko_daily:
            continue
        path = _cache_json_path(f"gecko_market_chart/{gid}.json")
        if path.is_file():
            gecko_daily[gid] = chart_to_frame(
                gid, json.loads(path.read_text(encoding="utf-8"))
            )

    metric_rows: list[dict[str, Any]] = []
    for i, rec in enumerate(recs, start=1):
        if i == 1 or i % 25 == 0 or i == len(recs):
            print(f"venue 1D {i}/{len(recs)} {rec.get('base_coin')}", flush=True)
        cap = rec.get("ticker_mcap_usd")
        try:
            cap_f = float(cap) if cap is not None else None
        except (TypeError, ValueError):
            cap_f = None
        hist = venue_plus_cap_history(
            okx_symbol=rec.get("okx_symbol"),
            bybit_symbol=rec.get("bybit_symbol"),
            snapshot_cap=cap_f,
            gecko_daily=gecko_daily.get(rec.get("gecko_id")),
            fetch=fetch,
        )
        key = rec.get("base_coin")
        histories[key] = hist
        metric_rows.append(metrics_from_history(hist))

    metrics = pd.DataFrame(metric_rows)
    out = pd.concat([mapped.reset_index(drop=True), metrics], axis=1)
    # aliases used by the notebook / CSV consumers
    out["paprika_id"] = out["gecko_id"]
    out["paprika_name"] = out["gecko_name"]
    out["status"] = out.apply(classify_metric_row, axis=1)
    out["in_plot_set"] = out["status"] == STATUS_OK
    out["window_start"] = WINDOW_START.isoformat()
    out["window_end"] = WINDOW_END.isoformat()
    return out, histories


def coverage_counts(df: pd.DataFrame) -> dict[str, int]:
    counts = {k: int((df["status"] == k).sum()) for k in sorted(df["status"].unique())}
    counts["total"] = int(len(df))
    counts["in_plot_set"] = int(df["in_plot_set"].sum())
    return counts


def save_metrics_csv(df: pd.DataFrame, path: Path = METRICS_CSV) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "base_coin",
        "okx_symbol",
        "bybit_symbol",
        "status",
        "in_plot_set",
        "gecko_id",
        "gecko_name",
        "map_method",
        "map_note",
        "n_candidates",
        "candidate_ids",
        "market_cap_usd",
        "median_rel_vol",
        "mean_rel_vol",
        "volume_cv",
        "median_volume_usd",
        "mean_volume_usd",
        "n_hist_days",
        "n_days_both",
        "n_days_volume",
        "n_days_mcap",
        "hist_start",
        "hist_end",
        "ticker_volume_usd",
        "ticker_mcap_usd",
        "window_start",
        "window_end",
    ]
    use = [c for c in cols if c in df.columns]
    extra = [c for c in df.columns if c not in use]
    df[use + extra].to_csv(path, index=False)
    return path


def run_screen(
    *,
    universe_path: Path = DEFAULT_UNIVERSE,
    fetch: bool = True,
    save: bool = True,
) -> dict[str, Any]:
    universe = load_universe(universe_path)
    crypto, equity = split_crypto_equity(universe)
    mapped = map_universe(crypto, fetch=fetch)
    metrics, histories = build_metrics_table(mapped, fetch=fetch)
    csv_path = save_metrics_csv(metrics) if save else None
    return {
        "universe": universe,
        "crypto": crypto,
        "equity": equity,
        "mapped": mapped,
        "metrics": metrics,
        "histories": histories,
        "coverage": coverage_counts(metrics),
        "window": window_spec(),
        "csv_path": csv_path,
        "denylist_path": str(DEFAULT_DENYLIST_PATH),
        "denylist_size": len(load_non_crypto_denylist()),
        "possible_collisions": [c for c in POSSIBLE_DENYLIST_COLLISIONS if not is_crypto(c)],
    }


def fit_loglog_ols(plot_df: pd.DataFrame) -> dict[str, Any]:
    """OLS: log(median_rel_vol) = a + b * log(market_cap). Natural log."""
    d = plot_df.loc[
        np.isfinite(plot_df["market_cap_usd"])
        & np.isfinite(plot_df["median_rel_vol"])
        & (plot_df["market_cap_usd"] > 0)
        & (plot_df["median_rel_vol"] > 0),
        ["base_coin", "market_cap_usd", "median_rel_vol"],
    ].copy()
    x = np.log(d["market_cap_usd"].to_numpy(dtype=float))
    y = np.log(d["median_rel_vol"].to_numpy(dtype=float))
    n = int(len(d))
    if n < 3:
        raise ValueError("need at least 3 finite points for log-log OLS")
    b, a = np.polyfit(x, y, 1)
    yhat = a + b * x
    resid = y - yhat
    ss_res = float(np.sum(resid**2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else np.nan
    d = d.assign(
        log_cap=x,
        log_median_rel_vol=y,
        log_fitted=yhat,
        log_residual=resid,
        fitted_median_rel_vol=np.exp(yhat),
    )
    return {
        "n": n,
        "a": float(a),
        "b": float(b),
        "r2": float(r2),
        "resid_std": float(np.std(resid, ddof=1)),
        "resid_mad": float(np.median(np.abs(resid - np.median(resid)))),
        "equation": f"log(median_rel_vol) = {a:.4f} + {b:.4f} * log(cap)",
        "power_law": f"median_rel_vol ∝ cap^{b:.4f}",
        "points": d,
    }


def fit_loglog_theil_sen(plot_df: pd.DataFrame) -> dict[str, float]:
    from scipy.stats import theilslopes

    d = plot_df.loc[
        np.isfinite(plot_df["market_cap_usd"])
        & np.isfinite(plot_df["median_rel_vol"])
        & (plot_df["market_cap_usd"] > 0)
        & (plot_df["median_rel_vol"] > 0)
    ]
    x = np.log(d["market_cap_usd"].to_numpy(dtype=float))
    y = np.log(d["median_rel_vol"].to_numpy(dtype=float))
    slope, intercept, lo, hi = theilslopes(y, x)
    return {
        "a": float(intercept),
        "b": float(slope),
        "b_lo": float(lo),
        "b_hi": float(hi),
        "equation": f"log(median_rel_vol) = {intercept:.4f} + {slope:.4f} * log(cap)  (Theil–Sen)",
    }


def fit_loglog_huber(plot_df: pd.DataFrame) -> dict[str, float]:
    from sklearn.linear_model import HuberRegressor

    d = plot_df.loc[
        np.isfinite(plot_df["market_cap_usd"])
        & np.isfinite(plot_df["median_rel_vol"])
        & (plot_df["market_cap_usd"] > 0)
        & (plot_df["median_rel_vol"] > 0)
    ]
    x = np.log(d["market_cap_usd"].to_numpy(dtype=float)).reshape(-1, 1)
    y = np.log(d["median_rel_vol"].to_numpy(dtype=float))
    est = HuberRegressor().fit(x, y)
    a = float(est.intercept_)
    b = float(est.coef_[0])
    return {
        "a": a,
        "b": b,
        "equation": f"log(median_rel_vol) = {a:.4f} + {b:.4f} * log(cap)  (Huber)",
    }


def fitted_turnover_at_cap(a: float, b: float, cap_usd: float) -> float:
    return float(np.exp(a + b * np.log(cap_usd)))


def fit_loglog_ols_xy(
    plot_df: pd.DataFrame,
    *,
    x_col: str = "market_cap_usd",
    y_col: str = "std_spread",
    y_name: str = "std_spread",
) -> dict[str, Any]:
    """OLS: log(y) = a + b * log(x). Same contract as ``fit_loglog_ols``."""
    d = plot_df.loc[
        np.isfinite(pd.to_numeric(plot_df[x_col], errors="coerce"))
        & np.isfinite(pd.to_numeric(plot_df[y_col], errors="coerce"))
        & (pd.to_numeric(plot_df[x_col], errors="coerce") > 0)
        & (pd.to_numeric(plot_df[y_col], errors="coerce") > 0),
        ["base_coin", x_col, y_col],
    ].copy()
    x = np.log(d[x_col].to_numpy(dtype=float))
    y = np.log(d[y_col].to_numpy(dtype=float))
    n = int(len(d))
    if n < 3:
        raise ValueError("need at least 3 finite points for log-log OLS")
    b, a = np.polyfit(x, y, 1)
    yhat = a + b * x
    resid = y - yhat
    ss_res = float(np.sum(resid**2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else np.nan
    d = d.assign(
        log_x=x,
        log_y=y,
        log_fitted=yhat,
        log_residual=resid,
        fitted_y=np.exp(yhat),
    )
    return {
        "n": n,
        "a": float(a),
        "b": float(b),
        "r2": float(r2),
        "resid_std": float(np.std(resid, ddof=1)),
        "resid_mad": float(np.median(np.abs(resid - np.median(resid)))),
        "equation": f"log({y_name}) = {a:.4f} + {b:.4f} * log(cap)",
        "power_law": f"{y_name} ∝ cap^{b:.4f}",
        "y_name": y_name,
        "x_col": x_col,
        "y_col": y_col,
        "points": d,
    }


def fit_loglog_theil_sen_xy(
    plot_df: pd.DataFrame,
    *,
    x_col: str = "market_cap_usd",
    y_col: str = "std_spread",
    y_name: str = "std_spread",
) -> dict[str, float]:
    from scipy.stats import theilslopes

    d = plot_df.loc[
        np.isfinite(pd.to_numeric(plot_df[x_col], errors="coerce"))
        & np.isfinite(pd.to_numeric(plot_df[y_col], errors="coerce"))
        & (pd.to_numeric(plot_df[x_col], errors="coerce") > 0)
        & (pd.to_numeric(plot_df[y_col], errors="coerce") > 0)
    ]
    x = np.log(pd.to_numeric(d[x_col], errors="coerce").to_numpy(dtype=float))
    y = np.log(pd.to_numeric(d[y_col], errors="coerce").to_numpy(dtype=float))
    slope, intercept, lo, hi = theilslopes(y, x)
    return {
        "a": float(intercept),
        "b": float(slope),
        "b_lo": float(lo),
        "b_hi": float(hi),
        "equation": f"log({y_name}) = {intercept:.4f} + {slope:.4f} * log(cap)  (Theil–Sen)",
    }


def fit_loglog_huber_xy(
    plot_df: pd.DataFrame,
    *,
    x_col: str = "market_cap_usd",
    y_col: str = "std_spread",
    y_name: str = "std_spread",
) -> dict[str, float]:
    from sklearn.linear_model import HuberRegressor

    d = plot_df.loc[
        np.isfinite(pd.to_numeric(plot_df[x_col], errors="coerce"))
        & np.isfinite(pd.to_numeric(plot_df[y_col], errors="coerce"))
        & (pd.to_numeric(plot_df[x_col], errors="coerce") > 0)
        & (pd.to_numeric(plot_df[y_col], errors="coerce") > 0)
    ]
    x = np.log(pd.to_numeric(d[x_col], errors="coerce").to_numpy(dtype=float)).reshape(-1, 1)
    y = np.log(pd.to_numeric(d[y_col], errors="coerce").to_numpy(dtype=float))
    est = HuberRegressor().fit(x, y)
    a = float(est.intercept_)
    b = float(est.coef_[0])
    return {
        "a": a,
        "b": b,
        "equation": f"log({y_name}) = {a:.4f} + {b:.4f} * log(cap)  (Huber)",
    }


def load_overview_spread_rows(path: Path = OVERVIEW_SUMMARY) -> pd.DataFrame:
    """Per-coin all-tick spread percentiles from viz overview (August-overlap)."""
    if not path.is_file():
        raise FileNotFoundError(f"overview summary not found: {path}")
    obj = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for r in obj.get("rows") or []:
        pl = r.get("pct_long") or {}
        ps = r.get("pct_short") or {}
        p99_l = pl.get("99")
        p99_s = ps.get("99")
        p95_l = pl.get("95")
        p95_s = ps.get("95")
        p99s = [x for x in (p99_l, p99_s) if x is not None]
        p95s = [x for x in (p95_l, p95_s) if x is not None]
        rows.append(
            {
                "base_coin": r.get("coin"),
                "spread_n_ticks": r.get("n_all"),
                "spread_days_with_ticks": r.get("days_with_ticks"),
                "p99_long": p99_l,
                "p99_short": p99_s,
                "p95_long": p95_l,
                "p95_short": p95_s,
                "p99_max": max(p99s) if p99s else np.nan,
                "p95_max": max(p95s) if p95s else np.nan,
            }
        )
    out = pd.DataFrame(rows)
    out["spread_window_start"] = obj.get("start") or SPREAD_WINDOW_START
    out["spread_window_end"] = obj.get("end") or SPREAD_WINDOW_END
    return out


def join_spread_overview(metrics: pd.DataFrame, *, overview_path: Path = OVERVIEW_SUMMARY) -> pd.DataFrame:
    ov = load_overview_spread_rows(overview_path)
    out = metrics.merge(ov, on="base_coin", how="left")
    out["spread_joined"] = out["p99_max"].notna()
    # Overflow bins in RunningSpreadHist clip at 10.0 — do not treat as real 10% spreads.
    out["p99_max_clean"] = out["p99_max"].where(out["p99_max"] < P99_OVERFLOW)
    out["had_entry_tail"] = out["p99_max_clean"] >= ENTRY_THRESH_PCT
    out["had_soft_tail"] = out["p99_max_clean"] >= 0.15
    return out


def attach_loglog_fit(df: pd.DataFrame, fit: dict[str, Any]) -> pd.DataFrame:
    out = df.copy()
    cap = pd.to_numeric(out["market_cap_usd"], errors="coerce").to_numpy(dtype=float)
    y = pd.to_numeric(out["median_rel_vol"], errors="coerce").to_numpy(dtype=float)
    ok = np.isfinite(cap) & np.isfinite(y) & (cap > 0) & (y > 0)
    log_cap = np.full(len(out), np.nan)
    log_y = np.full(len(out), np.nan)
    fitted = np.full(len(out), np.nan)
    resid = np.full(len(out), np.nan)
    log_cap[ok] = np.log(cap[ok])
    log_y[ok] = np.log(y[ok])
    fitted[ok] = fit["a"] + fit["b"] * log_cap[ok]
    resid[ok] = log_y[ok] - fitted[ok]
    out["log_cap"] = log_cap
    out["log_median_rel_vol"] = log_y
    out["log_fitted"] = fitted
    out["log_residual"] = resid
    out["fitted_median_rel_vol"] = np.exp(fitted)
    return out


def apply_cap_filter(
    joined: pd.DataFrame,
    *,
    cutoff_usd: float = CAP_CUTOFF_USD,
) -> pd.DataFrame:
    """Tag kept / dropped / held-out. Does not rewrite the source universe CSV."""
    out = joined.copy()
    cap = pd.to_numeric(out["market_cap_usd"], errors="coerce")
    in_plot = out.get("in_plot_set", False)
    if not isinstance(in_plot, pd.Series):
        in_plot = pd.Series(False, index=out.index)
    reason = np.array(["held_out_no_metrics"] * len(out), dtype=object)
    decision = np.array(["held_out"] * len(out), dtype=object)
    plot_ok = in_plot.fillna(False).to_numpy() & np.isfinite(cap) & (cap > 0)
    large = plot_ok & (cap > cutoff_usd)
    keep = plot_ok & (cap <= cutoff_usd)
    reason[keep] = "kept_cap_le_cutoff"
    reason[large] = "dropped_large_cap"
    decision[keep] = "kept"
    decision[large] = "dropped"
    out["cap_cutoff_usd"] = cutoff_usd
    out["filter_decision"] = decision
    out["filter_reason"] = reason
    return out


def save_filtered_csv(df: pd.DataFrame, path: Path = FILTERED_CSV) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "base_coin",
        "filter_decision",
        "filter_reason",
        "cap_cutoff_usd",
        "market_cap_usd",
        "median_rel_vol",
        "fitted_median_rel_vol",
        "log_residual",
        "p99_max",
        "p99_max_clean",
        "p95_max",
        "had_entry_tail",
        "had_soft_tail",
        "spread_n_ticks",
        "spread_joined",
        "spread_window_start",
        "spread_window_end",
        "status",
        "in_plot_set",
        "gecko_id",
        "okx_symbol",
        "bybit_symbol",
    ]
    use = [c for c in cols if c in df.columns]
    extra = [c for c in df.columns if c not in use]
    df[use + extra].to_csv(path, index=False)
    return path


def filter_counts(df: pd.DataFrame) -> dict[str, int]:
    return {
        "crypto": int(len(df)),
        "plot_set": int(df.get("in_plot_set", pd.Series(False, index=df.index)).fillna(False).sum()),
        "kept": int((df["filter_decision"] == "kept").sum()),
        "dropped_large_cap": int((df["filter_decision"] == "dropped").sum()),
        "held_out_no_metrics": int((df["filter_decision"] == "held_out").sum()),
    }


def _iso_to_ms(iso: str) -> int:
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def vol_window_spec() -> dict[str, Any]:
    return {
        "window_start": VOL_WINDOW_START,
        "window_end": VOL_WINDOW_END,
        "definition": (
            "sample std (ddof=1) of all-tick spread_long / spread_short in percent; "
            "std_spread = max(std_long, std_short). Same L1 formula as overview "
            "(no fail-closed). Not volume_cv, not gear-1.5 rolling volume sigma, "
            "not gear-2.2 quiet-window MAD/sigma0."
        ),
        "min_ticks": VOL_MIN_TICKS,
        "source": "computed from output/lean_ticks (not stored in overview)",
        "calendar_note": (
            "Wanted calendar August 2026-08-01..08-31. Disk has "
            "2026-07-22T11:05Z..2026-08-27T11:40Z; first August file is "
            "2026-08-03T13:35Z. Window is available August ticks only."
        ),
    }


_STD_READ_COLS = [
    "event_local_ts_ms",
    "base_coin",
    "okx_bid_price",
    "okx_ask_price",
    "bybit_bid_price",
    "bybit_ask_price",
]


def _merge_moments(
    n_a: int, mean_a: float, m2_a: float, n_b: int, mean_b: float, m2_b: float
) -> tuple[int, float, float]:
    """Parallel Welford merge of two one-pass moment blocks."""
    if n_b == 0:
        return n_a, mean_a, m2_a
    if n_a == 0:
        return n_b, mean_b, m2_b
    n = n_a + n_b
    delta = mean_b - mean_a
    mean = mean_a + delta * n_b / n
    m2 = m2_a + m2_b + delta * delta * n_a * n_b / n
    return n, mean, m2


def _moments_from_values(x: np.ndarray) -> tuple[int, float, float]:
    x = np.asarray(x, dtype=np.float64)
    x = x[np.isfinite(x)]
    n = int(x.size)
    if n == 0:
        return 0, 0.0, 0.0
    mean = float(x.mean())
    m2 = float(np.square(x - mean).sum())
    return n, mean, m2


def _update_std_accs(accs: dict[str, dict[str, Any]], table) -> None:
    """Add one Arrow table's L1 spreads into per-coin moment accumulators."""
    import pyarrow as pa
    import pyarrow.compute as pc

    if table is None or table.num_rows == 0:
        return
    if any(c not in table.column_names for c in _STD_READ_COLS):
        return
    ts = pc.cast(pc.floor(pc.cast(table["event_local_ts_ms"], pa.float64())), pa.int64())
    bc = table["base_coin"]
    if pa.types.is_dictionary(bc.type):
        bc = bc.dictionary_decode()
    coins = pc.utf8_upper(bc).to_numpy(zero_copy_only=False)
    ts_np = ts.to_numpy(zero_copy_only=False).astype(np.int64, copy=False)
    ob = np.asarray(table["okx_bid_price"].to_numpy(zero_copy_only=False), dtype="float64")
    oa = np.asarray(table["okx_ask_price"].to_numpy(zero_copy_only=False), dtype="float64")
    bb = np.asarray(table["bybit_bid_price"].to_numpy(zero_copy_only=False), dtype="float64")
    ba = np.asarray(table["bybit_ask_price"].to_numpy(zero_copy_only=False), dtype="float64")
    ok = (
        np.isfinite(ob)
        & np.isfinite(oa)
        & np.isfinite(bb)
        & np.isfinite(ba)
        & (ob > 0)
        & (bb > 0)
    )
    if not bool(ok.any()):
        return
    coins = coins[ok]
    ts_np = ts_np[ok]
    sl = (bb[ok] - oa[ok]) / bb[ok] * 100.0
    ss = (ob[ok] - ba[ok]) / ob[ok] * 100.0
    order = np.argsort(coins, kind="stable")
    sorted_c = coins[order]
    change = np.empty(len(sorted_c), dtype=bool)
    change[0] = True
    if len(sorted_c) > 1:
        change[1:] = sorted_c[1:] != sorted_c[:-1]
    starts = np.flatnonzero(change)
    ends = np.append(starts[1:], len(sorted_c))
    unix_days = ts_np // 86_400_000
    for a, b in zip(starts.tolist(), ends.tolist()):
        coin = str(sorted_c[a])
        idx = order[a:b]
        n_l, mean_l, m2_l = _moments_from_values(sl[idx])
        n_s, mean_s, m2_s = _moments_from_values(ss[idx])
        days = set(int(d) for d in np.unique(unix_days[idx]).tolist())
        cur = accs.get(coin)
        if cur is None:
            accs[coin] = {
                "n_l": n_l,
                "mean_l": mean_l,
                "m2_l": m2_l,
                "n_s": n_s,
                "mean_s": mean_s,
                "m2_s": m2_s,
                "days": days,
            }
            continue
        n_l, mean_l, m2_l = _merge_moments(
            cur["n_l"], cur["mean_l"], cur["m2_l"], n_l, mean_l, m2_l
        )
        n_s, mean_s, m2_s = _merge_moments(
            cur["n_s"], cur["mean_s"], cur["m2_s"], n_s, mean_s, m2_s
        )
        cur["n_l"], cur["mean_l"], cur["m2_l"] = n_l, mean_l, m2_l
        cur["n_s"], cur["mean_s"], cur["m2_s"] = n_s, mean_s, m2_s
        cur["days"].update(days)


def compute_august_spread_std(
    *,
    ticks_dir: Path = DEFAULT_LEAN_TICKS,
    start: str = VOL_WINDOW_START,
    end: str = VOL_WINDOW_END,
    cache_path: Path = VOL_STD_CSV,
    force: bool = False,
    workers: int = 8,
) -> pd.DataFrame:
    """All-tick sample std of spread_long/short per coin over the August window.

    Streams lean parquet (same L1 spread formula as viz overview). Caches CSV
    so the notebook does not rescan tens of GB. Recompute with force=True.
    """
    if cache_path.is_file() and not force:
        return load_august_spread_std(cache_path)

    ticks_dir = Path(ticks_dir)
    if not ticks_dir.is_dir():
        raise FileNotFoundError(f"lean ticks missing: {ticks_dir}")

    from research.lean_ticks_io import iter_lean_tables, list_lean_files_overlapping

    start_ms = _iso_to_ms(start)
    end_ms = _iso_to_ms(end)
    files = list_lean_files_overlapping(ticks_dir, start_ms, end_ms)
    if not files:
        raise FileNotFoundError(f"no lean files overlapping {start} … {end}")

    accs: dict[str, dict[str, Any]] = {}
    n_tables = 0
    for _path, table in iter_lean_tables(
        ticks_dir,
        start_ms,
        end_ms,
        workers=workers,
        columns=_STD_READ_COLS,
    ):
        _update_std_accs(accs, table)
        n_tables += 1

    rows = []
    for coin, cur in accs.items():
        n = int(cur["n_l"])
        std_l = float(np.sqrt(cur["m2_l"] / (n - 1))) if n > 1 else np.nan
        std_s = float(np.sqrt(cur["m2_s"] / (cur["n_s"] - 1))) if cur["n_s"] > 1 else np.nan
        rows.append(
            {
                "base_coin": coin,
                "n_ticks": n,
                "std_spread_long": std_l,
                "std_spread_short": std_s,
                "mean_spread_long": float(cur["mean_l"]) if n else np.nan,
                "mean_spread_short": float(cur["mean_s"]) if cur["n_s"] else np.nan,
                "days_with_ticks": int(len(cur["days"])),
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        raise ValueError(f"no L1 ticks in {start} … {end}")
    out["std_spread"] = out[["std_spread_long", "std_spread_short"]].max(axis=1)
    out["std_status"] = np.where(
        (out["n_ticks"] >= VOL_MIN_TICKS) & np.isfinite(out["std_spread"]),
        "ok",
        np.where(out["n_ticks"] > 1, "sparse", "missing"),
    )
    out["window_start"] = start
    out["window_end"] = end
    out["n_files"] = len(files)
    out["n_tables"] = n_tables
    out = out.sort_values("base_coin").reset_index(drop=True)
    cache_path = Path(cache_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(cache_path, index=False)
    return out


def load_august_spread_std(path: Path = VOL_STD_CSV) -> pd.DataFrame:
    if not Path(path).is_file():
        raise FileNotFoundError(f"August spread std cache missing: {path}")
    df = pd.read_csv(path)
    df["base_coin"] = df["base_coin"].astype(str).str.upper()
    return df


def join_august_spread_std(
    metrics: pd.DataFrame,
    std_df: Optional[pd.DataFrame] = None,
    *,
    std_path: Path = VOL_STD_CSV,
) -> pd.DataFrame:
    std_df = load_august_spread_std(std_path) if std_df is None else std_df
    keep = [
        "base_coin",
        "n_ticks",
        "std_spread_long",
        "std_spread_short",
        "std_spread",
        "mean_spread_long",
        "mean_spread_short",
        "days_with_ticks",
        "std_status",
        "window_start",
        "window_end",
    ]
    use = [c for c in keep if c in std_df.columns]
    std_part = std_df[use].copy()
    # filtered/metrics already have liquidity window_start; do not let that
    # overwrite the August std window.
    std_part = std_part.rename(
        columns={"window_start": "vol_window_start", "window_end": "vol_window_end"}
    )
    out = metrics.merge(std_part, on="base_coin", how="left")
    out["std_status"] = out["std_status"].fillna("missing")
    out["std_present"] = out["std_spread"].notna() & np.isfinite(out["std_spread"])
    out["std_usable"] = (out["std_status"] == "ok") & out["std_present"]
    out["vol_window_start"] = out["vol_window_start"].fillna(VOL_WINDOW_START)
    out["vol_window_end"] = out["vol_window_end"].fillna(VOL_WINDOW_END)
    return out


def std_quantiles(std_ok: pd.Series, qs: tuple[float, ...] = VOL_QUANTILES) -> dict[str, float]:
    s = pd.to_numeric(std_ok, errors="coerce")
    s = s[np.isfinite(s)]
    return {f"q{int(q * 100)}": float(s.quantile(q)) for q in qs}


def apply_vol_quantile_filter(
    joined: pd.DataFrame,
    *,
    q: float = VOL_QUANTILE_DEFAULT,
    cutoff: Optional[float] = None,
    min_ticks: int = VOL_MIN_TICKS,
) -> pd.DataFrame:
    """Drop the low-std tail. Missing/sparse std is held_out, not low-vol."""
    out = joined.copy()
    std = pd.to_numeric(out.get("std_spread"), errors="coerce")
    n = pd.to_numeric(out.get("n_ticks"), errors="coerce")
    usable = np.isfinite(std) & n.fillna(0).ge(min_ticks)
    if cutoff is None:
        if int(usable.sum()) < 5:
            raise ValueError("not enough usable std values to set a quantile cutoff")
        cutoff = float(std[usable].quantile(q))
    in_plot = out.get("in_plot_set", False)
    if not isinstance(in_plot, pd.Series):
        in_plot = pd.Series(False, index=out.index)
    else:
        in_plot = in_plot.replace({"True": True, "False": False, "true": True, "false": False})
        if in_plot.dtype == object:
            in_plot = in_plot.map(lambda x: bool(x) if pd.notna(x) else False)
    plot_ok = in_plot.fillna(False).astype(bool).to_numpy()

    reason = np.array(["held_out_no_std"] * len(out), dtype=object)
    decision = np.array(["held_out"] * len(out), dtype=object)
    low = usable.to_numpy() & plot_ok & (std.to_numpy() < cutoff)
    keep = usable.to_numpy() & plot_ok & (std.to_numpy() >= cutoff)
    reason[keep] = "kept_std_ge_cutoff"
    reason[low] = "dropped_low_vol"
    decision[keep] = "kept"
    decision[low] = "dropped"
    # plot-set but sparse/missing std
    sparse = plot_ok & ~usable.to_numpy()
    reason[sparse] = "held_out_sparse_or_missing_std"
    decision[sparse] = "held_out"

    out["vol_cutoff"] = float(cutoff)
    out["vol_quantile"] = float(q)
    out["vol_min_ticks"] = int(min_ticks)
    out["vol_filter_decision"] = decision
    out["vol_filter_reason"] = reason
    return out


def vol_cap_venn(df: pd.DataFrame) -> dict[str, Any]:
    """Overlap of cap>$200M drop and low-std drop on the plot-set."""
    in_plot = df.get("in_plot_set", pd.Series(False, index=df.index)).fillna(False)
    cap_drop = in_plot & (df.get("filter_decision") == "dropped")
    cap_keep = in_plot & (df.get("filter_decision") == "kept")
    vol_drop = in_plot & (df.get("vol_filter_decision") == "dropped")
    vol_keep = in_plot & (df.get("vol_filter_decision") == "kept")
    vol_hold = in_plot & (df.get("vol_filter_decision") == "held_out")
    both_keep = cap_keep & vol_keep
    return {
        "plot_set": int(in_plot.sum()),
        "dropped_cap_only": int((cap_drop & vol_keep).sum()),
        "dropped_vol_only": int((cap_keep & vol_drop).sum()),
        "dropped_both": int((cap_drop & vol_drop).sum()),
        "kept_both": int(both_keep.sum()),
        "held_out_std_on_plot_set": int(vol_hold.sum()),
        "cap_dropped": int(cap_drop.sum()),
        "vol_dropped": int(vol_drop.sum()),
    }


def save_vol_filtered_csv(df: pd.DataFrame, path: Path = VOL_FILTERED_CSV) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "base_coin",
        "filter_decision",
        "filter_reason",
        "vol_filter_decision",
        "vol_filter_reason",
        "vol_cutoff",
        "vol_quantile",
        "std_spread",
        "std_spread_long",
        "std_spread_short",
        "std_status",
        "n_ticks",
        "days_with_ticks",
        "vol_window_start",
        "vol_window_end",
        "market_cap_usd",
        "median_rel_vol",
        "p99_max_clean",
        "had_entry_tail",
        "status",
        "in_plot_set",
        "gecko_id",
        "okx_symbol",
        "bybit_symbol",
    ]
    use = [c for c in cols if c in df.columns]
    extra = [c for c in df.columns if c not in use]
    df[use + extra].to_csv(path, index=False)
    return path


def invert_cap_at_std(a: float, b: float, std_level: float) -> float:
    """cap* such that OLS predicts ``std_level``: ln(std)=a+b·ln(cap).

    ``std_level`` must be in the same units as the fitted y (percent).
    """
    if not np.isfinite(a) or not np.isfinite(b) or b == 0:
        raise ValueError("OLS a,b must be finite with b≠0")
    if not np.isfinite(std_level) or std_level <= 0:
        raise ValueError("std_level must be a positive finite value")
    return float(np.exp((a - np.log(std_level)) / (-b)))


def joint_threshold_from_ols(
    cloud: pd.DataFrame,
    *,
    q: float = JOINT_STD_QUANTILE,
    ols: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Q of std_spread on ``cloud`` + cap* from the same-n OLS inverse."""
    if ols is None:
        ols = fit_loglog_ols_xy(cloud, y_col="std_spread", y_name="std_spread")
    std = pd.to_numeric(cloud["std_spread"], errors="coerce")
    std = std[np.isfinite(std) & (std > 0)]
    q_val = float(std.quantile(q))
    cap_star = invert_cap_at_std(ols["a"], ols["b"], q_val)
    pred_at_star = float(np.exp(ols["a"] + ols["b"] * np.log(cap_star)))
    return {
        "q": float(q),
        "q_std": q_val,
        "std_unit": STD_SPREAD_UNIT,
        "cap_star_usd": cap_star,
        "a": float(ols["a"]),
        "b": float(ols["b"]),
        "r2": float(ols["r2"]),
        "ols_n": int(ols["n"]),
        "pred_std_at_cap_star": pred_at_star,
        "equation": ols["equation"],
    }


def joint_sensitivity_table(
    cloud: pd.DataFrame,
    *,
    qs: tuple[float, ...] = (0.20, 0.25, 0.30),
    ols: Optional[dict[str, Any]] = None,
) -> pd.DataFrame:
    if ols is None:
        ols = fit_loglog_ols_xy(cloud, y_col="std_spread", y_name="std_spread")
    rows = []
    std = pd.to_numeric(cloud["std_spread"], errors="coerce")
    cap = pd.to_numeric(cloud["market_cap_usd"], errors="coerce")
    for q in qs:
        spec = joint_threshold_from_ols(cloud, q=q, ols=ols)
        quiet = np.isfinite(std) & (std < spec["q_std"])
        large = np.isfinite(cap) & (cap > spec["cap_star_usd"])
        rows.append(
            {
                "q": q,
                "q_std_pct": spec["q_std"],
                "cap_star_usd": spec["cap_star_usd"],
                "n_quiet": int(quiet.sum()),
                "n_large": int(large.sum()),
                "n_dropped": int((quiet & large).sum()),
            }
        )
    return pd.DataFrame(rows)


def apply_joint_q25_filter(
    joined: pd.DataFrame,
    *,
    spec: dict[str, Any],
) -> pd.DataFrame:
    """Drop iff std < Q and cap > cap*. Missing std is held_out, not quiet."""
    out = joined.copy()
    std = pd.to_numeric(out.get("std_spread"), errors="coerce")
    cap = pd.to_numeric(out.get("market_cap_usd"), errors="coerce")
    in_plot = out.get("in_plot_set", False)
    if not isinstance(in_plot, pd.Series):
        in_plot = pd.Series(False, index=out.index)
    else:
        in_plot = in_plot.replace({"True": True, "False": False, "true": True, "false": False})
        if in_plot.dtype == object:
            in_plot = in_plot.map(lambda x: bool(x) if pd.notna(x) else False)
    plot_ok = in_plot.fillna(False).astype(bool)
    usable_col = out.get("std_usable", False)
    if not isinstance(usable_col, pd.Series):
        usable_col = pd.Series(False, index=out.index)
    usable = plot_ok & usable_col.fillna(False).astype(bool)
    usable = usable & np.isfinite(std) & (std > 0) & np.isfinite(cap) & (cap > 0)

    q_std = float(spec["q_std"])
    cap_star = float(spec["cap_star_usd"])
    quiet = usable & (std < q_std)
    large = usable & (cap > cap_star)
    drop = quiet & large
    keep = usable & ~drop

    reason = np.array(["held_out_no_std"] * len(out), dtype=object)
    decision = np.array(["held_out"] * len(out), dtype=object)
    reason[keep.to_numpy()] = np.where(
        quiet[keep].to_numpy(),
        "kept_quiet_but_small",
        np.where(large[keep].to_numpy(), "kept_large_but_noisy", "kept_joint"),
    )
    decision[keep.to_numpy()] = "kept"
    reason[drop.to_numpy()] = "dropped_quiet_and_large"
    decision[drop.to_numpy()] = "dropped"
    sparse = plot_ok.to_numpy() & ~usable.to_numpy()
    reason[sparse] = "held_out_sparse_or_missing_std"
    decision[sparse] = "held_out"

    out["is_quiet"] = quiet
    out["is_large"] = large
    out["joint_q"] = float(spec["q"])
    out["joint_q_std"] = q_std
    out["joint_cap_star_usd"] = cap_star
    out["joint_ols_a"] = float(spec["a"])
    out["joint_ols_b"] = float(spec["b"])
    out["joint_std_unit"] = spec.get("std_unit", STD_SPREAD_UNIT)
    out["joint_filter_decision"] = decision
    out["joint_filter_reason"] = reason
    return out


def joint_filter_counts(df: pd.DataFrame) -> dict[str, int]:
    in_plot = df.get("in_plot_set", pd.Series(False, index=df.index))
    if not isinstance(in_plot, pd.Series):
        in_plot = pd.Series(False, index=df.index)
    in_plot = in_plot.fillna(False).astype(bool)
    dec = df.get("joint_filter_decision", pd.Series("", index=df.index))
    return {
        "crypto": int(len(df)),
        "plot_set": int(in_plot.sum()),
        "std_usable": int(
            (
                in_plot
                & df.get("std_usable", pd.Series(False, index=df.index)).fillna(False).astype(bool)
            ).sum()
        ),
        "n_quiet": int(df.get("is_quiet", False).fillna(False).astype(bool).sum())
        if "is_quiet" in df.columns
        else 0,
        "n_large": int(df.get("is_large", False).fillna(False).astype(bool).sum())
        if "is_large" in df.columns
        else 0,
        "dropped": int((dec == "dropped").sum()),
        "kept": int((dec == "kept").sum()),
        "held_out": int((dec == "held_out").sum()),
        "held_out_on_plot_set": int(((dec == "held_out") & in_plot).sum()),
    }


def joint_vs_old_screens(df: pd.DataFrame) -> dict[str, int]:
    """Counts-only overlap of joint drop vs cap>$200M and std<Q20."""
    in_plot = df.get("in_plot_set", pd.Series(False, index=df.index)).fillna(False).astype(bool)
    joint = in_plot & (df.get("joint_filter_decision") == "dropped")
    cap200 = in_plot & (df.get("filter_decision") == "dropped")
    vol_q20 = in_plot & (df.get("vol_filter_decision") == "dropped")
    return {
        "joint_dropped": int(joint.sum()),
        "cap200_dropped": int(cap200.sum()),
        "vol_q20_dropped": int(vol_q20.sum()),
        "joint_and_cap200": int((joint & cap200).sum()),
        "joint_only_vs_cap200": int((joint & ~cap200).sum()),
        "cap200_only_vs_joint": int((cap200 & ~joint).sum()),
        "joint_and_vol_q20": int((joint & vol_q20).sum()),
        "joint_only_vs_vol_q20": int((joint & ~vol_q20).sum()),
        "vol_q20_only_vs_joint": int((vol_q20 & ~joint).sum()),
    }


def save_joint_filtered_csv(df: pd.DataFrame, path: Path = JOINT_FILTERED_CSV) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "base_coin",
        "joint_filter_decision",
        "joint_filter_reason",
        "is_quiet",
        "is_large",
        "joint_q",
        "joint_q_std",
        "joint_cap_star_usd",
        "joint_ols_a",
        "joint_ols_b",
        "joint_std_unit",
        "std_spread",
        "std_status",
        "market_cap_usd",
        "filter_decision",
        "vol_filter_decision",
        "p99_max_clean",
        "had_entry_tail",
        "status",
        "in_plot_set",
        "gecko_id",
        "okx_symbol",
        "bybit_symbol",
        "vol_window_start",
        "vol_window_end",
    ]
    use = [c for c in cols if c in df.columns]
    extra = [c for c in df.columns if c not in use]
    df[use + extra].to_csv(path, index=False)
    return path


def _plot_ok_mask(df: pd.DataFrame) -> pd.Series:
    in_plot = df.get("in_plot_set", False)
    if not isinstance(in_plot, pd.Series):
        in_plot = pd.Series(False, index=df.index)
    else:
        in_plot = in_plot.replace({"True": True, "False": False, "true": True, "false": False})
        if in_plot.dtype == object:
            in_plot = in_plot.map(lambda x: bool(x) if pd.notna(x) else False)
    return in_plot.fillna(False).astype(bool)


def apply_capstar_filter(
    joined: pd.DataFrame,
    *,
    spec: dict[str, Any],
) -> pd.DataFrame:
    """Hard cut: drop plot-set coins with usable std and cap > cap*.

    cap* still comes from Q25 via OLS inverse. This is not an AND on std.
    Missing std stays held_out even if cap is large.
    """
    out = joined.copy()
    std = pd.to_numeric(out.get("std_spread"), errors="coerce")
    cap = pd.to_numeric(out.get("market_cap_usd"), errors="coerce")
    plot_ok = _plot_ok_mask(out)
    usable_col = out.get("std_usable", False)
    if not isinstance(usable_col, pd.Series):
        usable_col = pd.Series(False, index=out.index)
    usable = plot_ok & usable_col.fillna(False).astype(bool)
    usable = usable & np.isfinite(std) & (std > 0) & np.isfinite(cap) & (cap > 0)

    q_std = float(spec["q_std"])
    cap_star = float(spec["cap_star_usd"])
    quiet = usable & (std < q_std)
    large = usable & (cap > cap_star)
    drop = large
    keep = usable & ~drop

    reason = np.array(["held_out_no_std"] * len(out), dtype=object)
    decision = np.array(["held_out"] * len(out), dtype=object)
    reason[keep.to_numpy()] = np.where(
        quiet[keep].to_numpy(),
        "kept_quiet_but_small",
        "kept_cap_le_capstar",
    )
    decision[keep.to_numpy()] = "kept"
    reason[drop.to_numpy()] = np.where(
        quiet[drop].to_numpy(),
        "dropped_quiet_large",
        "dropped_large_noisy",
    )
    decision[drop.to_numpy()] = "dropped"
    sparse = plot_ok.to_numpy() & ~usable.to_numpy()
    reason[sparse] = "held_out_sparse_or_missing_std"
    decision[sparse] = "held_out"

    out["is_quiet"] = quiet
    out["is_large"] = large
    out["capstar_q"] = float(spec["q"])
    out["capstar_q_std"] = q_std
    out["capstar_usd"] = cap_star
    out["capstar_ols_a"] = float(spec["a"])
    out["capstar_ols_b"] = float(spec["b"])
    out["capstar_std_unit"] = spec.get("std_unit", STD_SPREAD_UNIT)
    out["capstar_filter_decision"] = decision
    out["capstar_filter_reason"] = reason
    return out


def capstar_filter_counts(df: pd.DataFrame) -> dict[str, int]:
    in_plot = _plot_ok_mask(df)
    dec = df.get("capstar_filter_decision", pd.Series("", index=df.index))
    reason = df.get("capstar_filter_reason", pd.Series("", index=df.index))
    return {
        "crypto": int(len(df)),
        "plot_set": int(in_plot.sum()),
        "std_usable": int(
            (
                in_plot
                & df.get("std_usable", pd.Series(False, index=df.index)).fillna(False).astype(bool)
            ).sum()
        ),
        "n_quiet": int(df.get("is_quiet", False).fillna(False).astype(bool).sum())
        if "is_quiet" in df.columns
        else 0,
        "n_large": int(df.get("is_large", False).fillna(False).astype(bool).sum())
        if "is_large" in df.columns
        else 0,
        "dropped": int((dec == "dropped").sum()),
        "dropped_quiet_large": int((reason == "dropped_quiet_large").sum()),
        "dropped_large_noisy": int((reason == "dropped_large_noisy").sum()),
        "kept": int((dec == "kept").sum()),
        "kept_quiet_but_small": int((reason == "kept_quiet_but_small").sum()),
        "held_out": int((dec == "held_out").sum()),
        "held_out_on_plot_set": int(((dec == "held_out") & in_plot).sum()),
    }


def save_capstar_filtered_csv(df: pd.DataFrame, path: Path = CAPSTAR_FILTERED_CSV) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "base_coin",
        "capstar_filter_decision",
        "capstar_filter_reason",
        "is_quiet",
        "is_large",
        "capstar_q",
        "capstar_q_std",
        "capstar_usd",
        "capstar_ols_a",
        "capstar_ols_b",
        "capstar_std_unit",
        "std_spread",
        "std_status",
        "market_cap_usd",
        "filter_decision",
        "joint_filter_decision",
        "joint_filter_reason",
        "p99_max_clean",
        "had_entry_tail",
        "status",
        "in_plot_set",
        "gecko_id",
        "okx_symbol",
        "bybit_symbol",
        "vol_window_start",
        "vol_window_end",
    ]
    use = [c for c in cols if c in df.columns]
    extra = [c for c in df.columns if c not in use]
    df[use + extra].to_csv(path, index=False)
    return path


def assign_take_yes_no(
    universe: pd.DataFrame,
    *,
    cap_star_usd: float,
    cap_by_coin: dict[str, float],
) -> pd.Series:
    """Canonical take: no if equity OR no cap OR cap > cap*; else yes."""
    flags: list[str] = []
    for coin in universe["base_coin"].map(normalize_base_coin):
        if not is_crypto(coin):
            flags.append("no")
            continue
        cap = cap_by_coin.get(coin)
        if cap is None or not np.isfinite(cap) or cap <= 0:
            flags.append("no")
            continue
        flags.append("no" if cap > cap_star_usd else "yes")
    return pd.Series(flags, index=universe.index, name="take")


def write_universe_take_column(
    take_by_coin: dict[str, str],
    *,
    path: Path = DEFAULT_UNIVERSE,
) -> dict[str, int]:
    """Append ``take`` last. Does not rewrite other fields or row order."""
    import csv

    path = Path(path)
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise ValueError(f"empty universe CSV: {path}")
        fieldnames = list(reader.fieldnames)
        rows = list(reader)
    if "take" in fieldnames:
        fieldnames = [c for c in fieldnames if c != "take"] + ["take"]
    else:
        fieldnames = fieldnames + ["take"]
    n_yes = n_no = 0
    out_rows = []
    for row in rows:
        coin = normalize_base_coin(row.get("base_coin", ""))
        flag = take_by_coin.get(coin)
        if flag not in ("yes", "no"):
            raise KeyError(f"take missing for {coin}")
        row = dict(row)
        row["take"] = flag
        out_rows.append(row)
        if flag == "yes":
            n_yes += 1
        else:
            n_no += 1
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(out_rows)
    return {"n": len(out_rows), "yes": n_yes, "no": n_no, "ncols": len(fieldnames)}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fetch/cache universe liquidity metrics")
    p.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE)
    p.add_argument("--no-fetch", action="store_true", help="Use cache only")
    p.add_argument("--no-save", action="store_true")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    t0 = time.time()
    result = run_screen(
        universe_path=args.universe,
        fetch=not args.no_fetch,
        save=not args.no_save,
    )
    print(
        json.dumps(
            {
                "window": result["window"],
                "n_universe": int(len(result["universe"])),
                "n_crypto": int(len(result["crypto"])),
                "n_equity_dropped": int(len(result["equity"])),
                "equity_dropped": result["equity"]["base_coin"].tolist(),
                "coverage": result["coverage"],
                "unmatched": result["metrics"]
                .loc[result["metrics"]["status"] == STATUS_UNMATCHED, "base_coin"]
                .tolist(),
                "csv": str(result["csv_path"]) if result["csv_path"] else None,
                "elapsed_s": round(time.time() - t0, 1),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
