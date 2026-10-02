#!/usr/bin/env python3
"""Daily ~10:00 MSK: expand-only +extras from top10_volatile_spread.json.

Keep base-29 forever. Accumulate extras: add new top10 coins not yet in the
pool; never drop / never rewrite hot_add_drop.csv with removals (header-only).
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DATA = Path("/data/bbot-would-send-prod")
UNIVERSE = DATA / "bybit_okx_universe.csv"
DELTA = DATA / "hot_add_delta.csv"
DROP = DATA / "hot_add_drop.csv"
STATE = DATA / "driver" / "rotate_state.json"
LOG = DATA / "driver" / "rotate.log"
METRICS = DATA / "driver" / "rotate_metrics.jsonl"
UNIT = "spread-bbot-would-send-prod"
DIGEST_CANDIDATES = [
    Path("/var/log/spread/top10_volatile_spread.json"),
    Path("/tmp/ops_digest_charts/top10_volatile_spread.json"),
]

BASE = {
    "KAITO", "HOME", "WAL", "RVN", "ONT", "2Z", "BICO", "HMSTR", "CAP", "BLEND",
    "EDEN", "KMNO", "GPS", "ME", "ZBT", "MOVE", "COAI", "AZTEC", "APR", "YB",
    "AT", "H", "MUBARAK", "ACU", "LA", "BEAT", "PARTI", "SIGN", "GIGGLE",
}

DELTA_FIELDS = [
    "base_coin", "okx_symbol", "bybit_symbol",
    "okx_tick_size", "okx_lot_size", "okx_min_size",
    "bybit_tick_size", "bybit_qty_step", "bybit_min_order_qty",
    "bybit_min_notional_value", "discovered_at_utc",
]

# Expand-only: never emit removals to the poller.
EXPAND_ONLY = True


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dlog(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{now_iso()} | {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def metric(event: str, **fields) -> None:
    METRICS.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": now_iso(), "event": event, **fields}
    with METRICS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"current_extras": [], "ever_extras": [], "last_digest_stamp": None}


def save_state(obj: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE)


def pick_digest() -> Path:
    for p in DIGEST_CANDIDATES:
        if p.is_file():
            return p
    raise FileNotFoundError(f"no digest JSON in {DIGEST_CANDIDATES}")


def load_top10(path: Path) -> tuple[list[str], dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    coins = [str(x["base_coin"]).strip().upper() for x in data.get("top10") or []]
    if len(coins) < 1:
        raise RuntimeError(f"empty top10 in {path}")
    return coins, data


def _http_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "bbot-top10-canary/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def fetch_instrument_row(coin: str) -> dict[str, str]:
    coin_u = coin.upper()
    bybit = None
    cursor = None
    while bybit is None:
        url = "https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000"
        if cursor:
            url += f"&cursor={cursor}"
        data = _http_json(url)
        for row in data.get("result", {}).get("list", []):
            if row.get("symbol") == f"{coin_u}USDT":
                bybit = row
                break
        cursor = data.get("result", {}).get("nextPageCursor") or None
        if bybit is not None or not cursor:
            break
    data = _http_json("https://www.okx.com/api/v5/public/instruments?instType=SWAP")
    okx = None
    for row in data.get("data", []):
        if row.get("instId") == f"{coin_u}-USDT-SWAP":
            okx = row
            break
    if bybit is None or okx is None:
        raise RuntimeError(
            f"REST instruments missing for {coin_u} bybit={bybit is not None} okx={okx is not None}"
        )
    lot = bybit.get("lotSizeFilter") or {}
    price = bybit.get("priceFilter") or {}
    return {
        "base_coin": coin_u,
        "okx_symbol": okx["instId"],
        "bybit_symbol": bybit["symbol"],
        "okx_tick_size": str(okx.get("tickSz")),
        "okx_lot_size": str(okx.get("lotSz")),
        "okx_min_size": str(okx.get("minSz")),
        "bybit_tick_size": str(price.get("tickSize")),
        "bybit_qty_step": str(lot.get("qtyStep")),
        "bybit_min_order_qty": str(lot.get("minOrderQty")),
        "bybit_min_notional_value": str(lot.get("minNotionalValue") or "5"),
    }


def ensure_universe_rows(coins: list[str]) -> dict[str, dict[str, str]]:
    need = {c.upper() for c in coins}
    out: dict[str, dict[str, str]] = {}
    with UNIVERSE.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            coin = str(row.get("base_coin", "")).strip().upper()
            if coin not in need:
                continue
            out[coin] = {
                k: str(row.get(k, "")).strip()
                for k in DELTA_FIELDS
                if k != "discovered_at_utc"
            }
            out[coin]["base_coin"] = coin
    for coin in sorted(need - set(out)):
        dlog(f"universe_rest_fetch coin={coin}")
        row = fetch_instrument_row(coin)
        out[coin] = row
        line = ",".join(
            [
                row["base_coin"],
                row["okx_symbol"],
                row["bybit_symbol"],
                row["okx_tick_size"],
                row["okx_lot_size"],
                row["okx_min_size"],
                row["bybit_tick_size"],
                row["bybit_qty_step"],
                row["bybit_min_order_qty"],
                row["bybit_min_notional_value"],
                "yes",
            ]
        )
        with UNIVERSE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        metric("universe_rest_appended", coin=coin)
    return out


def write_delta(rows: list[dict[str, str]]) -> float:
    tmp = DELTA.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=DELTA_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, DELTA)
    return DELTA.stat().st_mtime


def write_drop_empty() -> float:
    """Always write header-only drop file (disable removals)."""
    tmp = DROP.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["base_coin"])
        w.writeheader()
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, DROP)
    return DROP.stat().st_mtime


def sighup() -> None:
    try:
        subprocess.check_call(["systemctl", "kill", "-s", "SIGHUP", UNIT])
    except Exception as exc:
        dlog(f"sighup_failed err={exc}")


def maybe_seed_history(new_coins: list[str]) -> None:
    if not new_coins:
        return
    hist = Path("/data/bbot-would-send-prod-history/floor")
    need_seed = []
    for c in new_coins:
        found = False
        for day in sorted(hist.glob("event_date=*"))[-2:]:
            p = day / "metrics.jsonl"
            if not p.exists():
                continue
            try:
                with p.open("rb") as fh:
                    fh.seek(0, 2)
                    size = fh.tell()
                    fh.seek(max(0, size - 2_000_000))
                    blob = fh.read().decode("utf-8", errors="replace")
                if f'"base_coin":"{c}"' in blob:
                    found = True
                    break
            except OSError:
                pass
        if not found:
            need_seed.append(c)
    if not need_seed:
        dlog(f"history_ok coins={new_coins}")
        return
    dlog(f"history_seed_start coins={need_seed}")
    rc = subprocess.call(
        ["/root/venv/bin/python", "/usr/local/sbin/seed_top10_hotadd_history.py", *need_seed],
        env={**os.environ, "PYTHONPATH": "/root/spread_bbot_theta_top10_canary"},
    )
    dlog(f"history_seed_done rc={rc} coins={need_seed}")
    metric("history_seed", coins=need_seed, rc=rc)


def main() -> int:
    dlog("rotate_start expand_only=1")
    digest_path = pick_digest()
    top10, meta = load_top10(digest_path)
    today_new = [c for c in top10 if c not in BASE]
    seen: set[str] = set()
    today_u: list[str] = []
    for c in today_new:
        if c not in seen:
            seen.add(c)
            today_u.append(c)

    state = load_state()
    prev = [str(c).upper() for c in state.get("current_extras") or []]
    # Expand-only pool: keep all prior extras, append newly seen top10 extras.
    pool: list[str] = []
    pool_seen: set[str] = set()
    for c in prev + today_u:
        cu = str(c).upper()
        if cu in BASE or cu in pool_seen:
            continue
        pool_seen.add(cu)
        pool.append(cu)

    newly_added = [c for c in pool if c not in set(prev)]
    ever = set(str(c).upper() for c in state.get("ever_extras") or []) | set(pool)
    drop_list: list[str] = []  # hard-disabled

    # Seed / universe only for coins we still need rows for (whole pool for delta).
    maybe_seed_history(newly_added)
    universe = ensure_universe_rows(pool) if pool else {}
    rows = []
    for c in pool:
        row = dict(universe[c])
        row["discovered_at_utc"] = utc_iso()
        rows.append(row)

    m_delta = write_delta(rows)
    m_drop = write_drop_empty()
    sighup()

    state = {
        "current_extras": pool,
        "ever_extras": sorted(ever),
        "expand_only": True,
        "last_digest_stamp": meta.get("stamp"),
        "last_digest_path": str(digest_path),
        "last_window_end_msk": meta.get("window_end_msk"),
        "top10": top10,
        "desired_extras": today_u,
        "newly_added": newly_added,
        "drop_list": drop_list,
        "rotated_at": now_iso(),
    }
    save_state(state)
    metric(
        "rotate_applied",
        expand_only=True,
        top10=top10,
        desired_extras=today_u,
        pool_extras=pool,
        newly_added=newly_added,
        drop_list=drop_list,
        prev_extras=prev,
        digest=str(digest_path),
        stamp=meta.get("stamp"),
        delta_mtime=m_delta,
        drop_mtime=m_drop,
    )
    dlog(
        f"rotate_applied expand_only=1 pool={pool} newly_added={newly_added} "
        f"drop=[] stamp={meta.get('stamp')} digest={digest_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
