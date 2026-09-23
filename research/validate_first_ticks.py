#!/usr/bin/env python3
"""Validate local backup lean ticks + hist bars for Gear-2 crypto tests.

Focus: first ticks after a coverage gap must not be a stale-cross print
(one exchange book still old). Collector fail-closed
(app/utils/tick_validity.py) does not write when
  |okx_ts − bybit_ts| > 2000 ms  (skew)
  or calc_local − leg_recv > 2000 ms  (age).
Generation suppress is not stored in lean parquet; skew/age are the proxy.

Date holes are allowed. This script does not rewrite parquet.
"""

from __future__ import annotations

import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.schema.lean_event import LEAN_TICK_BODY_COLS
from app.utils.tick_validity import DEFAULT_AGE_MAX_MS, DEFAULT_SKEW_MAX_MS
from research.is_crypto import DEFAULT_CRYPTO_ALLOWLIST, load_non_crypto_denylist

TICKS_DIR = REPO / "output" / "lean_ticks"
OKX_BARS = REPO / "output" / "okx_bar5m_hist_regime"
BYBIT_BARS = REPO / "output" / "bybit_bar5m_hist_regime"
REPORT = REPO / "output" / "_first_tick_validity.json"

COLS = [
    "event_local_ts_ms",
    "base_coin",
    "okx_ts_ms",
    "bybit_ts_ms",
    "okx_local_recv_ts_ms",
    "bybit_local_recv_ts_ms",
    "calc_local_ts_ms",
    "okx_bid_price",
    "okx_ask_price",
    "bybit_bid_price",
    "bybit_ask_price",
]
WINDOW_RE = re.compile(
    r"spread_(\d{8}T\d{6}Z)_(\d{8}T\d{6}Z)\.parquet$"
)
INTERVAL_MS = 300_000
SPREAD_ALERT = 2.0
FAIL_CLOSED_DAY = "20260815"
N_EXAMPLES = 15


def _parse_window(name: str) -> tuple[int, int] | None:
    m = WINDOW_RE.match(name)
    if not m:
        return None
    fmt = "%Y%m%dT%H%M%SZ"

    def to_ms(s: str) -> int:
        dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)

    return to_ms(m.group(1)), to_ms(m.group(2))


def _iso(ms: float) -> str:
    if ms != ms:
        return ""
    return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def _hive_bar_index(root: Path) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    if not root.exists():
        return {}
    for part in root.glob("base_coin=*/event_date=*/part.parquet"):
        coin = part.parts[-3].split("=", 1)[1]
        day = part.parts[-2].split("=", 1)[1]
        out[coin].add(day)
    return dict(out)


def _crypto_mask_arrow(coin_col: pa.Array, deny_arr: pa.Array, allow_arr: pa.Array) -> pa.BooleanArray:
    upper = pc.utf8_upper(coin_col)
    return pc.or_(
        pc.is_in(upper, value_set=allow_arr),
        pc.invert(pc.is_in(upper, value_set=deny_arr)),
    )


def _spreads(batch: dict) -> tuple[np.ndarray, np.ndarray]:
    bb = np.asarray(batch["bybit_bid_price"], dtype="float64")
    oa = np.asarray(batch["okx_ask_price"], dtype="float64")
    ob = np.asarray(batch["okx_bid_price"], dtype="float64")
    ba = np.asarray(batch["bybit_ask_price"], dtype="float64")
    sl = np.full(bb.shape, np.nan)
    ss = np.full(ob.shape, np.nan)
    ok_l = np.isfinite(bb) & np.isfinite(oa) & (bb != 0)
    ok_s = np.isfinite(ob) & np.isfinite(ba) & (ob != 0)
    sl[ok_l] = (bb[ok_l] - oa[ok_l]) * 100.0 / bb[ok_l]
    ss[ok_s] = (ob[ok_s] - ba[ok_s]) * 100.0 / ob[ok_s]
    return sl, ss


def _first_indices(coins: np.ndarray) -> np.ndarray:
    """Index of first occurrence of each coin in this array (stable)."""
    _, idx = np.unique(coins, return_index=True)
    return np.sort(idx)


def main() -> int:
    files = sorted(TICKS_DIR.glob("spread_*.parquet"))
    print(f"lean_ticks files: {len(files)}  dir={TICKS_DIR}", flush=True)
    if not files:
        print("NO TICK FILES")
        return 1

    deny = set(load_non_crypto_denylist())
    allow = set(DEFAULT_CRYPTO_ALLOWLIST)
    deny_arr = pa.array(sorted(deny), type=pa.string())
    allow_arr = pa.array(sorted(allow), type=pa.string())

    starts: dict[int, Path] = {}
    for path in files:
        win = _parse_window(path.name)
        if win:
            starts[win[0]] = path
    start_set = set(starts)
    resume_files: set[str] = set()
    for start, path in starts.items():
        if (start - INTERVAL_MS) not in start_set:
            resume_files.add(path.name)
    print(f"gap-resume 5m files (no previous window): {len(resume_files)}", flush=True)

    t0 = time.time()
    schema_ok = 0
    schema_bad: list[tuple[str, str]] = []
    unreadable: list[tuple[str, str]] = []
    empty_files = 0
    crypto_rows = 0
    non_crypto_rows = 0
    skew_n = 0
    age_n = 0
    either_n = 0
    first_n = 0
    first_skew = 0
    first_age = 0
    first_either = 0
    first_spread_hi = 0
    first_spread_hi_and_stale = 0
    coins_seen: set[str] = set()
    examples: list[dict] = []
    max_skew = 0.0
    max_age = 0.0
    max_first_abs_spread = 0.0

    by_day: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "files": 0,
            "crypto_rows": 0,
            "skew": 0,
            "age": 0,
            "either": 0,
            "first": 0,
            "first_stale": 0,
            "first_spread_hi": 0,
        }
    )

    for i, path in enumerate(files, 1):
        day = path.name[7:15]
        by_day[day]["files"] += 1
        try:
            names = list(pq.read_schema(path).names)
        except Exception as exc:
            unreadable.append((path.name, str(exc)))
            continue
        if names != list(LEAN_TICK_BODY_COLS):
            schema_bad.append((path.name, f"{len(names)} cols"))
            continue
        schema_ok += 1
        is_resume = path.name in resume_files
        try:
            pf = pq.ParquetFile(path)
            if pf.metadata.num_rows == 0:
                empty_files += 1
                continue
            file_coins: list[np.ndarray] = []
            file_ts: list[np.ndarray] = []
            file_skew: list[np.ndarray] = []
            file_age: list[np.ndarray] = []
            file_sl: list[np.ndarray] = []
            file_ss: list[np.ndarray] = []
            file_stale: list[np.ndarray] = []
            file_crypto: list[np.ndarray] = []
            for batch in pf.iter_batches(columns=COLS, batch_size=65_536):
                crypto_pa = _crypto_mask_arrow(
                    batch.column("base_coin"), deny_arr, allow_arr
                )
                crypto = np.asarray(crypto_pa)
                n_c = int(crypto.sum())
                n_nc = int(len(crypto) - n_c)
                crypto_rows += n_c
                non_crypto_rows += n_nc
                by_day[day]["crypto_rows"] += n_c
                if n_c:
                    uniq = pc.unique(
                        pc.utf8_upper(batch.column("base_coin").filter(crypto_pa))
                    )
                    coins_seen.update(str(x) for x in uniq.to_pylist())

                okx_ts = np.asarray(batch.column("okx_ts_ms"), dtype="float64")
                bybit_ts = np.asarray(batch.column("bybit_ts_ms"), dtype="float64")
                calc = np.asarray(batch.column("calc_local_ts_ms"), dtype="float64")
                okx_recv = np.asarray(batch.column("okx_local_recv_ts_ms"), dtype="float64")
                bybit_recv = np.asarray(batch.column("bybit_local_recv_ts_ms"), dtype="float64")
                skew = np.abs(okx_ts - bybit_ts)
                age = np.maximum(calc - okx_recv, calc - bybit_recv)
                bad_skew = crypto & np.isfinite(skew) & (skew > DEFAULT_SKEW_MAX_MS)
                bad_age = crypto & np.isfinite(age) & (age > DEFAULT_AGE_MAX_MS)
                stale = bad_skew | bad_age
                ns = int(bad_skew.sum())
                na = int(bad_age.sum())
                ne = int(stale.sum())
                skew_n += ns
                age_n += na
                either_n += ne
                by_day[day]["skew"] += ns
                by_day[day]["age"] += na
                by_day[day]["either"] += ne
                if n_c:
                    c_skew = skew[crypto]
                    c_age = age[crypto]
                    if c_skew.size:
                        max_skew = max(max_skew, float(np.nanmax(c_skew)))
                    if c_age.size:
                        max_age = max(max_age, float(np.nanmax(c_age)))

                if is_resume:
                    coins = np.asarray(
                        batch.column("base_coin").to_numpy(zero_copy_only=False),
                        dtype=object,
                    )
                    file_coins.append(coins)
                    file_ts.append(
                        np.asarray(batch.column("event_local_ts_ms"), dtype="float64")
                    )
                    file_skew.append(skew)
                    file_age.append(age)
                    sl, ss = _spreads(
                        {
                            "bybit_bid_price": np.asarray(
                                batch.column("bybit_bid_price"), dtype="float64"
                            ),
                            "okx_ask_price": np.asarray(
                                batch.column("okx_ask_price"), dtype="float64"
                            ),
                            "okx_bid_price": np.asarray(
                                batch.column("okx_bid_price"), dtype="float64"
                            ),
                            "bybit_ask_price": np.asarray(
                                batch.column("bybit_ask_price"), dtype="float64"
                            ),
                        }
                    )
                    file_sl.append(sl)
                    file_ss.append(ss)
                    file_stale.append(stale)
                    file_crypto.append(crypto)

            if is_resume and file_coins:
                coins_a = np.concatenate(file_coins)
                crypto_a = np.concatenate(file_crypto)
                ts_a = np.concatenate(file_ts)
                skew_a = np.concatenate(file_skew)
                age_a = np.concatenate(file_age)
                sl_a = np.concatenate(file_sl)
                ss_a = np.concatenate(file_ss)
                stale_a = np.concatenate(file_stale)
                order = np.argsort(ts_a, kind="mergesort")
                coins_s = coins_a[order]
                crypto_s = crypto_a[order]
                keep = crypto_s
                if keep.any():
                    first_idx = _first_indices(coins_s[keep].astype(str))
                    # first_idx is relative to coins_s[keep]; map back
                    crypto_pos = np.flatnonzero(keep)
                    pick = crypto_pos[first_idx]
                    first_n += int(pick.size)
                    by_day[day]["first"] += int(pick.size)
                    p_skew = skew_a[order][pick]
                    p_age = age_a[order][pick]
                    p_stale = stale_a[order][pick]
                    p_sl = sl_a[order][pick]
                    p_ss = ss_a[order][pick]
                    p_coins = coins_s[pick]
                    p_ts = ts_a[order][pick]
                    fs = int((np.isfinite(p_skew) & (p_skew > DEFAULT_SKEW_MAX_MS)).sum())
                    fa = int((np.isfinite(p_age) & (p_age > DEFAULT_AGE_MAX_MS)).sum())
                    fe = int(p_stale.sum())
                    first_skew += fs
                    first_age += fa
                    first_either += fe
                    by_day[day]["first_stale"] += fe
                    abs_sp = np.nanmax(
                        np.vstack(
                            [np.abs(p_sl), np.abs(p_ss)]
                        ),
                        axis=0,
                    )
                    if abs_sp.size:
                        max_first_abs_spread = max(
                            max_first_abs_spread, float(np.nanmax(abs_sp))
                        )
                    hi = np.isfinite(abs_sp) & (abs_sp >= SPREAD_ALERT)
                    first_spread_hi += int(hi.sum())
                    by_day[day]["first_spread_hi"] += int(hi.sum())
                    first_spread_hi_and_stale += int((hi & p_stale).sum())
                    interesting = np.flatnonzero(p_stale | hi)
                    for j in interesting[:8]:
                        if len(examples) >= N_EXAMPLES * 8:
                            break
                        examples.append(
                            {
                                "file": path.name,
                                "coin": str(p_coins[j]),
                                "ts": _iso(float(p_ts[j])),
                                "skew_ms": round(float(p_skew[j]), 1)
                                if np.isfinite(p_skew[j])
                                else None,
                                "age_ms": round(float(p_age[j]), 1)
                                if np.isfinite(p_age[j])
                                else None,
                                "spread_long": round(float(p_sl[j]), 4)
                                if np.isfinite(p_sl[j])
                                else None,
                                "spread_short": round(float(p_ss[j]), 4)
                                if np.isfinite(p_ss[j])
                                else None,
                                "stale_cross": bool(p_stale[j]),
                                "high_spread": bool(hi[j]),
                            }
                        )
        except Exception as exc:
            unreadable.append((path.name, str(exc)))

        if i % 200 == 0 or i == len(files):
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed else 0
            print(
                f"  [{i}/{len(files)}] crypto_rows={crypto_rows:,} stale={either_n:,} "
                f"first={first_n:,} first_stale={first_either:,} "
                f"{elapsed:.0f}s ({rate:.1f} files/s)",
                flush=True,
            )

    examples.sort(
        key=lambda r: (
            not r["stale_cross"],
            -(abs(r["spread_long"] or 0) + abs(r["spread_short"] or 0)),
        )
    )
    examples = examples[:N_EXAMPLES]

    tick_days = sorted(by_day.keys())

    def _rate(days: list[str]) -> dict:
        rows = sum(by_day[d]["crypto_rows"] for d in days)
        stale = sum(by_day[d]["either"] for d in days)
        first = sum(by_day[d]["first"] for d in days)
        first_stale = sum(by_day[d]["first_stale"] for d in days)
        return {
            "n_days": len(days),
            "crypto_rows": rows,
            "stale_rows": stale,
            "stale_pct": round(100.0 * stale / rows, 4) if rows else None,
            "first_ticks": first,
            "first_stale": first_stale,
            "first_stale_pct": round(100.0 * first_stale / first, 4) if first else None,
        }

    pre_days = [d for d in tick_days if d < FAIL_CLOSED_DAY]
    post_days = [d for d in tick_days if d >= FAIL_CLOSED_DAY]
    pre_s = _rate(pre_days)
    post_s = _rate(post_days)

    def _is_crypto_coin(c: str) -> bool:
        u = c.upper()
        return u in DEFAULT_CRYPTO_ALLOWLIST or u not in set(load_non_crypto_denylist())

    okx_bars = _hive_bar_index(OKX_BARS)
    bybit_bars = _hive_bar_index(BYBIT_BARS)
    crypto_bar_okx = {c: ds for c, ds in okx_bars.items() if _is_crypto_coin(c)}
    crypto_bar_bybit = {c: ds for c, ds in bybit_bars.items() if _is_crypto_coin(c)}
    tick_day_iso = {f"{d[:4]}-{d[4:6]}-{d[6:8]}" for d in tick_days}
    bars_okx_days = set().union(*crypto_bar_okx.values()) if crypto_bar_okx else set()
    bars_bybit_days = set().union(*crypto_bar_bybit.values()) if crypto_bar_bybit else set()
    tick_days_missing_okx_bars = sorted(tick_day_iso - bars_okx_days)
    tick_days_missing_bybit_bars = sorted(tick_day_iso - bars_bybit_days)
    coins_ticks_no_okx_bar = sorted(c for c in coins_seen if c not in crypto_bar_okx)
    coins_ticks_no_bybit_bar = sorted(c for c in coins_seen if c not in crypto_bar_bybit)

    print("\n======== TICK FILES ========")
    print(
        f"files total={len(files)} schema_ok={schema_ok} schema_bad={len(schema_bad)} "
        f"unreadable={len(unreadable)} empty={empty_files} resume_files={len(resume_files)}"
    )
    print(
        f"crypto rows={crypto_rows:,}  non-crypto skipped={non_crypto_rows:,}  "
        f"coins={len(coins_seen)}"
    )
    stale_pct = 100.0 * either_n / crypto_rows if crypto_rows else 0.0
    print(
        f"ALL crypto ticks  skew>{DEFAULT_SKEW_MAX_MS}ms={skew_n:,}  "
        f"age>{DEFAULT_AGE_MAX_MS}ms={age_n:,}  either={either_n:,}  ({stale_pct:.4f}%)"
    )
    first_pct = 100.0 * first_either / first_n if first_n else 0.0
    print(
        f"FIRST ticks after a missing 5m window: n={first_n:,}  stale={first_either:,}  "
        f"({first_pct:.4f}%)  |spread|>={SPREAD_ALERT}%={first_spread_hi:,}  "
        f"high+stale={first_spread_hi_and_stale:,}"
    )
    print(
        f"max skew={max_skew:.0f} ms  max age={max_age:.0f} ms  "
        f"max |first spread|={max_first_abs_spread:.3f}%"
    )
    print(f"date range {tick_days[0]} .. {tick_days[-1]}  ({len(tick_days)} distinct UTC days)")

    print("\nDaily crypto coverage (windows/288) and stale-cross rate:")
    for d in tick_days:
        row = by_day[d]
        nwin = row["files"]
        pct = 100.0 * nwin / 288.0
        sp = 100.0 * row["either"] / row["crypto_rows"] if row["crypto_rows"] else 0.0
        fp = 100.0 * row["first_stale"] / row["first"] if row["first"] else 0.0
        mark = "  << pre fail-closed" if d < FAIL_CLOSED_DAY else ""
        print(
            f"  {d}: windows={nwin:3d}/288 ({pct:5.1f}%)  "
            f"rows={row['crypto_rows']:11,}  stale={sp:7.4f}%  "
            f"first_stale={row['first_stale']:4d}/{row['first']:4d} ({fp:6.2f}%){mark}"
        )

    print(
        f"\nPRE  {FAIL_CLOSED_DAY}  stale_pct={pre_s['stale_pct']}%  "
        f"first_stale_pct={pre_s['first_stale_pct']}%  rows={pre_s['crypto_rows']:,}"
    )
    print(
        f"POST {FAIL_CLOSED_DAY}  stale_pct={post_s['stale_pct']}%  "
        f"first_stale_pct={post_s['first_stale_pct']}%  rows={post_s['crypto_rows']:,}"
    )

    if examples:
        print("\nWorst first-after-gap examples (stale-cross first):")
        for ex in examples:
            print(
                f"  {ex['ts']} {ex['coin']:8s} skew={ex['skew_ms']} age={ex['age_ms']} "
                f"L={ex['spread_long']} S={ex['spread_short']} "
                f"stale={ex['stale_cross']} {ex['file']}"
            )

    print("\n======== BARS (hist REST, crypto) ========")
    print(
        f"OKX   coins={len(crypto_bar_okx)} days={len(bars_okx_days)} "
        f"{min(bars_okx_days) if bars_okx_days else '—'} .. "
        f"{max(bars_okx_days) if bars_okx_days else '—'}"
    )
    print(
        f"Bybit coins={len(crypto_bar_bybit)} days={len(bars_bybit_days)} "
        f"{min(bars_bybit_days) if bars_bybit_days else '—'} .. "
        f"{max(bars_bybit_days) if bars_bybit_days else '—'}"
    )
    print(f"tick coins without OKX hist bar:   {len(coins_ticks_no_okx_bar)} {coins_ticks_no_okx_bar[:20]}")
    print(f"tick coins without Bybit hist bar: {len(coins_ticks_no_bybit_bar)} {coins_ticks_no_bybit_bar[:20]}")
    print(f"tick UTC days missing OKX bars:    {tick_days_missing_okx_bars}")
    print(f"tick UTC days missing Bybit bars:  {tick_days_missing_bybit_bars}")

    if schema_bad[:8]:
        print("\nSCHEMA BAD:")
        for name, err in schema_bad[:8]:
            print(f"  {name}: {err}")
    if unreadable[:8]:
        print("\nUNREADABLE:")
        for name, err in unreadable[:8]:
            print(f"  {name}: {err}")

    report = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "thresholds_ms": {
            "skew": DEFAULT_SKEW_MAX_MS,
            "age": DEFAULT_AGE_MAX_MS,
            "window": INTERVAL_MS,
        },
        "fail_closed_from_utc_day": FAIL_CLOSED_DAY,
        "files": {
            "n": len(files),
            "schema_ok": schema_ok,
            "schema_bad": len(schema_bad),
            "unreadable": len(unreadable),
            "empty": empty_files,
            "resume_after_gap": len(resume_files),
            "schema_bad_names": [n for n, _ in schema_bad[:30]],
            "unreadable_names": [n for n, _ in unreadable[:30]],
        },
        "crypto": {
            "rows": crypto_rows,
            "non_crypto_rows_skipped": non_crypto_rows,
            "n_coins": len(coins_seen),
            "skew_rows": skew_n,
            "age_rows": age_n,
            "stale_rows": either_n,
            "stale_pct": round(stale_pct, 6),
            "first_ticks_after_gap": first_n,
            "first_skew": first_skew,
            "first_age": first_age,
            "first_stale": first_either,
            "first_stale_pct": round(first_pct, 6) if first_n else None,
            "first_spread_ge_2pct": first_spread_hi,
            "first_high_spread_and_stale": first_spread_hi_and_stale,
            "max_skew_ms": max_skew,
            "max_age_ms": max_age,
            "max_first_abs_spread_pct": max_first_abs_spread,
        },
        "pre_fail_closed": pre_s,
        "post_fail_closed": post_s,
        "by_day": {d: dict(by_day[d]) for d in tick_days},
        "examples_first_after_gap": examples,
        "bars": {
            "okx_crypto_coins": len(crypto_bar_okx),
            "bybit_crypto_coins": len(crypto_bar_bybit),
            "okx_day_min": min(bars_okx_days) if bars_okx_days else None,
            "okx_day_max": max(bars_okx_days) if bars_okx_days else None,
            "bybit_day_min": min(bars_bybit_days) if bars_bybit_days else None,
            "bybit_day_max": max(bars_bybit_days) if bars_bybit_days else None,
            "tick_days_missing_okx_bars": tick_days_missing_okx_bars,
            "tick_days_missing_bybit_bars": tick_days_missing_bybit_bars,
            "tick_coins_missing_okx_bar": coins_ticks_no_okx_bar,
            "tick_coins_missing_bybit_bar": coins_ticks_no_bybit_bar,
        },
        "note": (
            "Generation suppress cannot be recovered from lean parquet. "
            "First ticks = first crypto row per coin in a 5m file that has no previous window. "
            "Date holes are expected. Not live-fill proof."
        ),
    }
    REPORT.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nWrote {REPORT}")

    blocked = bool(schema_bad or unreadable)
    post_stale = post_s["stale_pct"] if post_s["stale_pct"] is not None else 0.0
    print("\n======== VERDICT ========")
    if blocked:
        print("blocked: unreadable or non-lean files remain")
        return 2
    if post_stale > 0.05:
        print(f"partial: post-{FAIL_CLOSED_DAY} still has {post_stale}% stale-cross rows in parquet")
        return 0
    if first_spread_hi_and_stale:
        print("partial: after-gap first ticks still include high-spread stale-cross prints")
        return 0
    print(
        "lean readable; post-fail-closed stale-cross near zero. "
        "Pre-gate days must be reader-gated or excluded for honest backtests."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
