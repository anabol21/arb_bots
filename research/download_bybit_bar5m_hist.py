"""Download Bybit linear 5m history (OHLC + volume) for vacation-dump coins.

Layout (mirrors OKX hist dump):

  output/bybit_bar5m_hist_regime/
    base_coin=<COIN>/event_date=<YYYY-MM-DD>/part.parquet

Columns (aligned with OKX hist for cross-exchange regime work):
  bar_start_ts_ms, bar_end_ts_ms, base_coin, ref_exchange,
  open, high, low, close, volume, amp_ohlc, ret_close

volume = Bybit kline base-coin volume (linear USDT).
ref_exchange = bybit.

Idempotent by default: existing `part.parquet` day files are skipped
unless `--force` is set. Re-runs that extend the window only fetch missing days.

Default window (~1 calendar month ending at vacation dump):
  [2026-07-08, 2026-08-08)

Together with output/okx_bar5m_hist_regime this supports:
  - one-exchange volume spike vs both-exchange stress
  - price amplitude from OHLC on each venue

Example:
  python3 research/download_bybit_bar5m_hist.py
  python3 research/download_bybit_bar5m_hist.py --start 2026-07-08 --end 2026-08-08 --workers 3
  python3 research/download_bybit_bar5m_hist.py --limit-coins 10
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[1]
DEFAULT_TICK = REPO / "output" / "vacation_return_20260810" / "ticks"
DEFAULT_UNIVERSE = REPO / "bybit_okx_universe.csv"
DEFAULT_OUT = REPO / "output" / "bybit_bar5m_hist_regime"

BYBIT_KLINE = "https://api.bybit.com/v5/market/kline"
BAR_MS = 300_000
LIMIT = 1000
# ~1 calendar month ending at vacation window (end exclusive).
DEFAULT_START = "2026-07-08"
DEFAULT_END = "2026-08-08"


def parse_day(s: str) -> date:
    return date.fromisoformat(s)


def day_bounds_ms(d: date) -> tuple[int, int]:
    start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def iter_days(start_d: date, end_d: date) -> list[date]:
    days: list[date] = []
    d = start_d
    while d < end_d:
        days.append(d)
        d += timedelta(days=1)
    return days


def day_part_path(out_root: Path, base_coin: str, day: date) -> Path:
    return out_root / f"base_coin={base_coin}" / f"event_date={day.isoformat()}" / "part.parquet"


def coalesce_ranges(days: list[date]) -> list[tuple[date, date]]:
    """Merge consecutive missing days into [start, end) ranges."""
    if not days:
        return []
    ranges: list[tuple[date, date]] = []
    run_start = days[0]
    prev = days[0]
    for d in days[1:]:
        if d == prev + timedelta(days=1):
            prev = d
            continue
        ranges.append((run_start, prev + timedelta(days=1)))
        run_start = d
        prev = d
    ranges.append((run_start, prev + timedelta(days=1)))
    return ranges


def list_dump_coins(tick_dir: Path) -> list[str]:
    files = sorted(tick_dir.glob("spread_*.parquet"))
    if not files:
        raise FileNotFoundError(f"no tick parquet in {tick_dir}")
    table = pq.read_table(files[len(files) // 2], columns=["base_coin"])
    return sorted(set(table.column(0).to_pylist()))


def load_bybit_map(universe_csv: Path, coins: Iterable[str]) -> dict[str, str]:
    df = pd.read_csv(universe_csv)
    want = set(coins)
    sub = df[df["base_coin"].isin(want)]
    missing = want - set(sub["base_coin"])
    if missing:
        raise ValueError(f"coins missing from universe csv: {sorted(missing)[:20]}")
    return dict(zip(sub["base_coin"], sub["bybit_symbol"]))


def bybit_get(params: dict, *, timeout: float = 30.0, retries: int = 5) -> list:
    qs = urllib.parse.urlencode(params)
    url = f"{BYBIT_KLINE}?{qs}"
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "spread-regime-hist/0.1"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if str(payload.get("retCode")) != "0":
                raise RuntimeError(
                    f"Bybit error retCode={payload.get('retCode')} "
                    f"retMsg={payload.get('retMsg')}"
                )
            result = payload.get("result") or {}
            return result.get("list") or []
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError) as exc:
            last_err = exc
            time.sleep(min(2**attempt * 0.25, 8.0))
    raise RuntimeError(f"Bybit request failed after retries: {last_err}")


def fetch_klines(
    symbol: str,
    start_ms: int,
    end_ms: int,
    *,
    sleep_s: float = 0.08,
) -> list[list[str]]:
    """Fetch [start_ms, end_ms) 5m klines; walk backward via `end`."""
    out: list[list[str]] = []
    cursor_end = end_ms
    guard = 0
    while guard < 10_000:
        guard += 1
        batch = bybit_get(
            {
                "category": "linear",
                "symbol": symbol,
                "interval": "5",
                "start": str(start_ms),
                "end": str(cursor_end),
                "limit": str(LIMIT),
            }
        )
        if sleep_s > 0:
            time.sleep(sleep_s)
        if not batch:
            break
        # Bybit returns newest first: [start, open, high, low, close, volume, turnover]
        stop = False
        for row in batch:
            ts = int(row[0])
            if ts >= end_ms:
                continue
            if ts < start_ms:
                stop = True
                continue
            out.append(row)
        oldest = int(batch[-1][0])
        if stop or oldest <= start_ms:
            break
        if oldest >= cursor_end:
            break
        cursor_end = oldest
        if len(batch) < LIMIT:
            break
    by_ts = {int(r[0]): r for r in out}
    return [by_ts[k] for k in sorted(by_ts)]


def rows_to_frame(base_coin: str, rows: list[list[str]]) -> pd.DataFrame:
    cols = [
        "bar_start_ts_ms",
        "bar_end_ts_ms",
        "base_coin",
        "ref_exchange",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amp_ohlc",
        "ret_close",
    ]
    if not rows:
        return pd.DataFrame(columns=cols)
    recs = []
    for r in rows:
        ts = int(r[0])
        o, h, l, c = float(r[1]), float(r[2]), float(r[3]), float(r[4])
        vol = float(r[5])
        amp = (h - l) / o if o else float("nan")
        ret = (c / o - 1.0) if o else float("nan")
        recs.append(
            {
                "bar_start_ts_ms": ts,
                "bar_end_ts_ms": ts + BAR_MS,
                "base_coin": base_coin,
                "ref_exchange": "bybit",
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": vol,
                "amp_ohlc": amp,
                "ret_close": ret,
            }
        )
    return pd.DataFrame.from_records(recs)


def write_coin_days(out_root: Path, df: pd.DataFrame, *, force: bool) -> tuple[int, int]:
    """Write day parts. Returns (n_written, n_skipped_existing)."""
    if df.empty:
        return 0, 0
    n_written = 0
    n_skipped = 0
    df = df.copy()
    df["event_date"] = pd.to_datetime(df["bar_start_ts_ms"], unit="ms", utc=True).dt.strftime(
        "%Y-%m-%d"
    )
    for (coin, day), part in df.groupby(["base_coin", "event_date"], sort=True):
        dest = out_root / f"base_coin={coin}" / f"event_date={day}"
        path = dest / "part.parquet"
        if path.exists() and not force:
            n_skipped += 1
            continue
        dest.mkdir(parents=True, exist_ok=True)
        part = part.drop(columns=["event_date"]).sort_values("bar_start_ts_ms")
        part.to_parquet(path, index=False)
        n_written += 1
    return n_written, n_skipped


def download_one(
    base_coin: str,
    symbol: str,
    days: list[date],
    out_root: Path,
    sleep_s: float,
    *,
    force: bool,
) -> dict:
    if force:
        missing = list(days)
    else:
        missing = [d for d in days if not day_part_path(out_root, base_coin, d).exists()]

    n_skipped_days = len(days) - len(missing)
    if not missing:
        return {
            "base_coin": base_coin,
            "symbol": symbol,
            "n_bars": 0,
            "n_parts": 0,
            "n_skipped_days": n_skipped_days,
            "n_missing_days": 0,
            "ok": True,
            "skipped_all": True,
        }

    frames: list[pd.DataFrame] = []
    for range_start, range_end in coalesce_ranges(missing):
        start_ms, _ = day_bounds_ms(range_start)
        end_ms, _ = day_bounds_ms(range_end)
        rows = fetch_klines(symbol, start_ms, end_ms, sleep_s=sleep_s)
        frames.append(rows_to_frame(base_coin, rows))

    frame = (
        pd.concat(frames, ignore_index=True)
        if frames
        else rows_to_frame(base_coin, [])
    )
    if not frame.empty:
        frame = frame.sort_values("bar_start_ts_ms", kind="mergesort").drop_duplicates(
            subset=["bar_start_ts_ms"], keep="last"
        )
    n_parts, n_write_skipped = write_coin_days(out_root, frame, force=force)
    return {
        "base_coin": base_coin,
        "symbol": symbol,
        "n_bars": int(len(frame)),
        "n_parts": n_parts,
        "n_skipped_days": n_skipped_days + n_write_skipped,
        "n_missing_days": len(missing),
        "ok": True,
        "skipped_all": False,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tick-dir", type=Path, default=DEFAULT_TICK)
    ap.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--start", type=str, default=DEFAULT_START)
    ap.add_argument("--end", type=str, default=DEFAULT_END)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--sleep", type=float, default=0.08)
    ap.add_argument("--limit-coins", type=int, default=0)
    ap.add_argument(
        "--coins",
        type=str,
        default="",
        help="comma-separated base_coin subset (default: full dump universe)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="re-download and overwrite existing day parts",
    )
    args = ap.parse_args()

    start_d = parse_day(args.start)
    end_d = parse_day(args.end)
    if end_d <= start_d:
        raise SystemExit("end must be after start")
    start_ms, _ = day_bounds_ms(start_d)
    end_ms, _ = day_bounds_ms(end_d)
    days = iter_days(start_d, end_d)

    if args.coins.strip():
        coins = [c.strip() for c in args.coins.split(",") if c.strip()]
    else:
        coins = list_dump_coins(args.tick_dir)
    if args.limit_coins > 0:
        coins = coins[: args.limit_coins]
    mapping = load_bybit_map(args.universe, coins)
    args.out.mkdir(parents=True, exist_ok=True)

    meta = {
        "exchange": "bybit",
        "start": args.start,
        "end": args.end,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "n_days": len(days),
        "n_coins": len(coins),
        "out": str(args.out),
        "force": bool(args.force),
        "idempotent_skip_existing_days": not bool(args.force),
        "source": "Bybit GET /v5/market/kline category=linear interval=5 volume=base",
        "purpose": "cross-exchange regime calibration (vs OKX hist)",
        "schema_align": "same columns as output/okx_bar5m_hist_regime",
    }
    (args.out / "_meta_request.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (args.out / "_coins.txt").write_text("\n".join(coins) + "\n", encoding="utf-8")

    results = []
    errors = []
    print(
        f"coins={len(coins)} window=[{args.start},{args.end}) days={len(days)} "
        f"force={args.force} out={args.out}",
        flush=True,
    )

    def job(coin: str) -> dict:
        return download_one(
            coin, mapping[coin], days, args.out, args.sleep, force=args.force
        )

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futs = {pool.submit(job, c): c for c in coins}
        done = 0
        for fut in as_completed(futs):
            coin = futs[fut]
            done += 1
            try:
                res = fut.result()
                results.append(res)
                if done % 20 == 0 or done == len(coins):
                    print(
                        f"[{done}/{len(coins)}] {coin} bars={res['n_bars']} "
                        f"parts={res['n_parts']} skipped_days={res['n_skipped_days']}",
                        flush=True,
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append({"base_coin": coin, "error": str(exc)})
                print(f"[{done}/{len(coins)}] FAIL {coin}: {exc}", flush=True)

    summary = {
        "meta": meta,
        "n_ok": len(results),
        "n_fail": len(errors),
        "total_bars": int(sum(r["n_bars"] for r in results)),
        "total_parts_written": int(sum(r["n_parts"] for r in results)),
        "total_skipped_days": int(sum(r["n_skipped_days"] for r in results)),
        "n_skipped_all": int(sum(1 for r in results if r.get("skipped_all"))),
        "errors": errors,
        "results_sample": results[:5],
    }
    (args.out / "_download_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "n_ok",
                    "n_fail",
                    "total_bars",
                    "total_parts_written",
                    "total_skipped_days",
                    "n_skipped_all",
                )
            },
            indent=2,
        )
    )
    if errors:
        print(f"failures: {len(errors)} (see _download_summary.json)", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
