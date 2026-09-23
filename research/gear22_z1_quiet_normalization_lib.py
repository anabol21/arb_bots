"""Gear 2.2 experiment Z1 - transferability of quiet-spread normalization.

Track M research only. Reads ``output/lean_ticks`` read-only. Does not touch the
canonical simulator, VARIATION, HYPER, Trade_Lat, fees, model_gear2 or
gear2_backtest. No PnL is computed anywhere in this module.

Stage 1 (this file, first half) freezes the manifest and audits data validity.
Stage 2 computes the window statistics, cross-application, LOCO calibration,
distances, ICC and the verdict.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from research.lean_ticks_io import (
    GEAR2_LEAN_COLS_NO_SIZE,
    list_lean_files_overlapping,
    parse_lean_file_window,
    parse_ts_ms,
    read_and_prepare_lean_ticks,
)

REPO = Path(__file__).resolve().parents[1]
LEAN = REPO / "output" / "lean_ticks"
OUT = REPO / "research" / "output" / "gear22_z1_quiet_normalization"
# v1 pilot artifacts are frozen under OUT/v1; the manifest-v2 run writes here.
OUT_V2 = OUT / "v2"
# The tick cache is shared across manifest versions: a window is keyed by
# coin + start, so a cached frame stays valid when the manifest changes.
CACHE = OUT / "window_cache"

FILE_INTERVAL_MS = 300_000
WINDOW_MS = 12 * 3_600_000
HALF_MS = 6 * 3_600_000

# --- frozen data-quality thresholds (declared before any r/z is inspected) ---
MAX_MISSING_FILE_FRAC = 0.02  # >2% missing 5-min files -> contaminated
MAX_INTRA_GAP_MS = 120_000  # single per-coin tick gap above this -> contaminated
MAX_GAP_TIME_FRAC = 0.02  # total time in gaps > 60 s above this -> contaminated
MIN_TICKS_PER_HALF = 2_000  # each half must carry this many usable ticks
GAP_COUNT_MS = 60_000  # gaps longer than this accumulate into gap-time

# --- frozen liquidity labels (set before any z is computed) ---
LIQUIDITY_CLASS = {
    "BTC": "major",
    "ETH": "major",
    "SOL": "major",
    "2Z": "alt",
    "BICO": "alt",
    "CAP": "alt",
    "ALLO": "alt",
    "EDEN": "alt",
    "JELLYJELLY": "alt",
    "BIO": "alt",
}

# --- frozen effective-resolution contract -----------------------------------
# spread_long  = (bybit_bid - okx_ask) / bybit_bid * 100
# spread_short = (okx_bid  - bybit_ask) / okx_bid  * 100
#
# A one-tick move of a single leg shifts the spread by
#   long : okx_ask   -> d = tick_okx   / bybit_bid * 100
#          bybit_bid -> d = tick_bybit * okx_ask / bybit_bid**2 * 100
#   short: bybit_ask -> d = tick_bybit / okx_bid * 100
#          okx_bid   -> d = tick_okx  * bybit_ask / okx_bid**2 * 100
# The finest achievable step is the minimum over the two legs:
#   delta_s(t) = min(leg steps)
# Exchange tick sizes are not stored in the lean parquet, so each leg tick is
# estimated from the observed price grid inside the window: the smallest
# non-zero |price increment| that recurs at least TICK_MIN_COUNT times.
TICK_MIN_COUNT = 10
TICK_ROUND_DECIMALS = 12

# Manifest v2 (user-declared, 30 windows x 12 h). Differences vs the v1 pilot:
#   2Z   18.08 02:00 -> 13.08 02:00
#   ETH  25.08 04:00 -> 15.08 02:00
#   ALLO 16.08 00:00 -> 22.08 12:00
#   SOL  07.08 00:00 -> 15.08 12:00 and 19.08 00:00 -> 23.08 18:00
#   BIO  18.08 12:00 -> 24.08 02:00
# BIO 15.08 00:00 is retained by explicit user decision although the v1 run
# found sigma0 == 0 on its long side.
USER_WINDOWS: list[tuple[str, str, str]] = [
    ("2Z", "2026-08-11T00:00:00Z", "2026-08-11T12:00:00Z"),
    ("2Z", "2026-08-06T00:00:00Z", "2026-08-06T12:00:00Z"),
    ("2Z", "2026-08-13T02:00:00Z", "2026-08-13T14:00:00Z"),
    ("BTC", "2026-08-09T02:00:00Z", "2026-08-09T14:00:00Z"),
    ("BTC", "2026-08-26T04:00:00Z", "2026-08-26T16:00:00Z"),
    ("BTC", "2026-08-15T12:00:00Z", "2026-08-16T00:00:00Z"),
    ("ETH", "2026-08-08T02:00:00Z", "2026-08-08T14:00:00Z"),
    ("ETH", "2026-08-13T12:00:00Z", "2026-08-14T00:00:00Z"),
    ("ETH", "2026-08-15T02:00:00Z", "2026-08-15T14:00:00Z"),
    ("BICO", "2026-08-12T00:00:00Z", "2026-08-12T12:00:00Z"),
    ("BICO", "2026-08-14T00:00:00Z", "2026-08-14T12:00:00Z"),
    ("BICO", "2026-08-23T10:00:00Z", "2026-08-23T22:00:00Z"),
    ("SOL", "2026-08-26T00:00:00Z", "2026-08-26T12:00:00Z"),
    ("SOL", "2026-08-15T12:00:00Z", "2026-08-16T00:00:00Z"),
    ("SOL", "2026-08-23T18:00:00Z", "2026-08-24T06:00:00Z"),
    ("CAP", "2026-08-08T00:00:00Z", "2026-08-08T12:00:00Z"),
    ("CAP", "2026-08-25T00:00:00Z", "2026-08-25T12:00:00Z"),
    ("CAP", "2026-08-03T15:00:00Z", "2026-08-04T03:00:00Z"),
    ("ALLO", "2026-08-13T00:00:00Z", "2026-08-13T12:00:00Z"),
    ("ALLO", "2026-08-25T00:00:00Z", "2026-08-25T12:00:00Z"),
    ("ALLO", "2026-08-22T12:00:00Z", "2026-08-23T00:00:00Z"),
    ("EDEN", "2026-08-23T00:00:00Z", "2026-08-23T12:00:00Z"),
    ("EDEN", "2026-08-08T00:00:00Z", "2026-08-08T12:00:00Z"),
    ("EDEN", "2026-08-08T12:00:00Z", "2026-08-09T00:00:00Z"),
    ("JELLYJELLY", "2026-08-07T00:00:00Z", "2026-08-07T12:00:00Z"),
    ("JELLYJELLY", "2026-08-25T00:00:00Z", "2026-08-25T12:00:00Z"),
    ("JELLYJELLY", "2026-08-06T00:00:00Z", "2026-08-06T12:00:00Z"),
    ("BIO", "2026-08-26T00:00:00Z", "2026-08-26T12:00:00Z"),
    ("BIO", "2026-08-15T00:00:00Z", "2026-08-15T12:00:00Z"),
    ("BIO", "2026-08-24T02:00:00Z", "2026-08-24T14:00:00Z"),
]

PRICE_COLS = [
    "okx_ask_price",
    "okx_bid_price",
    "bybit_ask_price",
    "bybit_bid_price",
]
# The frozen fail-closed contract needs every timestamp column; reuse the
# canonical gear-2 column set rather than a hand-written subset.
READ_COLS = list(GEAR2_LEAN_COLS_NO_SIZE)


def build_manifest() -> pd.DataFrame:
    """Materialize the manifest skeleton from the user-declared windows."""
    rows = []
    for coin, a, b in USER_WINDOWS:
        s, e = parse_ts_ms(a), parse_ts_ms(b)
        if e - s != WINDOW_MS:
            raise ValueError(f"{coin} {a}->{b} is not exactly 12 h")
        rows.append(
            {
                "quiet_id": f"{coin}_{a[2:10].replace('-', '')}_{a[11:13]}",
                "base_coin": coin,
                "liquidity_class_pre": LIQUIDITY_CLASS[coin],
                "window_start_ts_utc": a,
                "window_end_ts_utc": b,
                "start_ms": s,
                "end_ms": e,
            }
        )
    df = pd.DataFrame(rows)
    # quiet_regime_id: windows of the same coin that touch each other belong to
    # one continuous quiet regime and are not independent.
    df = df.sort_values(["base_coin", "start_ms"]).reset_index(drop=True)
    regime = []
    for coin, grp in df.groupby("base_coin", sort=False):
        prev_end = None
        idx = 0
        for _, r in grp.iterrows():
            if prev_end is not None and r["start_ms"] > prev_end:
                idx += 1
            prev_end = max(prev_end or 0, r["end_ms"])
            regime.append(f"{coin}_R{idx}")
    df["quiet_regime_id"] = regime
    # calendar_cluster_id: UTC-overlapping windows across coins share market context.
    order = df.sort_values("start_ms").index
    cluster = pd.Series(index=df.index, dtype=object)
    cid = 0
    cur_end = None
    for i in order:
        if cur_end is None or df.at[i, "start_ms"] >= cur_end:
            cid += 1
            cur_end = df.at[i, "end_ms"]
        else:
            cur_end = max(cur_end, df.at[i, "end_ms"])
        cluster[i] = f"C{cid:02d}"
    df["calendar_cluster_id"] = cluster
    return df


def _file_holes(start_ms: int, end_ms: int) -> tuple[int, int]:
    files = list_lean_files_overlapping(LEAN, start_ms, end_ms)
    have = set()
    for p in files:
        w = parse_lean_file_window(p)
        if w:
            have.add(w[0])
    expected = list(range(start_ms, end_ms, FILE_INTERVAL_MS))
    missing = [t for t in expected if t not in have]
    return len(expected), len(missing)


def estimate_leg_ticks(df: pd.DataFrame) -> dict[str, float]:
    """Smallest recurring non-zero price increment per leg, in price units."""
    out: dict[str, float] = {}
    for col in PRICE_COLS:
        v = df[col].to_numpy(dtype=float)
        d = np.abs(np.diff(v))
        d = d[np.isfinite(d) & (d > 0)]
        if d.size == 0:
            out[col] = float("nan")
            continue
        d = np.round(d, TICK_ROUND_DECIMALS)
        vals, cnt = np.unique(d, return_counts=True)
        ok = vals[cnt >= TICK_MIN_COUNT]
        out[col] = float(ok.min()) if ok.size else float(vals.min())
    return out


def _cache_meta_path(quiet_id: str) -> Path:
    return CACHE / f"{quiet_id}.meta.json"


def _cached_leg_ticks(quiet_id: str) -> tuple[float, float] | None:
    """Leg tick sizes recorded next to a cached window, or carried over from a
    previous manifest run. They are estimated from the raw price grid and can
    therefore not be recovered from the cached spread frame itself."""
    p = _cache_meta_path(quiet_id)
    if p.is_file():
        m = json.loads(p.read_text())
        return float(m["tick_okx"]), float(m["tick_bybit"])
    for prior in (OUT / "v1" / "manifest_audit.csv", OUT / "manifest_audit.csv"):
        if not prior.is_file():
            continue
        a = pd.read_csv(prior)
        hit = a[a["quiet_id"] == quiet_id]
        if not hit.empty and np.isfinite(hit.iloc[0]["tick_okx"]):
            return float(hit.iloc[0]["tick_okx"]), float(hit.iloc[0]["tick_bybit"])
    return None


def _read_window_fresh(row: pd.Series) -> pd.DataFrame | None:
    """Read the lean files for one window and (re)write its cache frame."""
    s, e = int(row["start_ms"]), int(row["end_ms"])
    coin = row["base_coin"]
    df, _ = read_and_prepare_lean_ticks(
        LEAN, s, e, coins={coin}, columns=READ_COLS, workers=4
    )
    if df.empty:
        return None
    ticks = estimate_leg_ticks(df)
    tick_okx = float(min(ticks["okx_ask_price"], ticks["okx_bid_price"]))
    tick_bybit = float(min(ticks["bybit_ask_price"], ticks["bybit_bid_price"]))

    df = df.sort_values("event_local_ts_ms")
    bb = df["bybit_bid_price"].to_numpy(dtype=float)
    ba = df["bybit_ask_price"].to_numpy(dtype=float)
    ob = df["okx_bid_price"].to_numpy(dtype=float)
    oa = df["okx_ask_price"].to_numpy(dtype=float)
    # frozen per-tick effective resolution of the executable spread
    d_long = np.minimum(tick_okx / bb, tick_bybit * oa / bb**2) * 100.0
    d_short = np.minimum(tick_bybit / ob, tick_okx * ba / ob**2) * 100.0

    CACHE.mkdir(parents=True, exist_ok=True)
    cached = pd.DataFrame(
        {
            "ts": df["event_local_ts_ms"].to_numpy(dtype=np.int64),
            "spread_long": df["spread_long"].to_numpy(dtype=float),
            "spread_short": df["spread_short"].to_numpy(dtype=float),
            "res_long": d_long.astype(np.float32),
            "res_short": d_short.astype(np.float32),
        }
    )
    cached.to_parquet(CACHE / f"{row['quiet_id']}.parquet", index=False)
    _cache_meta_path(row["quiet_id"]).write_text(
        json.dumps({"tick_okx": tick_okx, "tick_bybit": tick_bybit}, indent=2)
    )
    return cached


def audit_window(row: pd.Series) -> dict:
    """Evaluate the frozen data-validity contract for one window.

    File-presence counters are always recomputed from the lean directory. The
    tick frame is reused from ``window_cache`` when it is already there, so a
    manifest revision only pays for its genuinely new windows.
    """
    s, e = int(row["start_ms"]), int(row["end_ms"])
    coin = row["base_coin"]
    n_exp, n_missing = _file_holes(s, e)

    rec: dict = {
        "quiet_id": row["quiet_id"],
        "base_coin": coin,
        "window_start_ts_utc": row["window_start_ts_utc"],
        "n_files_expected": n_exp,
        "n_files_missing": n_missing,
        "missing_file_frac": n_missing / max(n_exp, 1),
    }

    cache_path = CACHE / f"{row['quiet_id']}.parquet"
    legs = _cached_leg_ticks(row["quiet_id"]) if cache_path.is_file() else None
    if legs is not None:
        df = pd.read_parquet(cache_path)
        tick_okx, tick_bybit = legs
        rec["cache_hit"] = True
    else:
        df = _read_window_fresh(row)
        rec["cache_hit"] = False
        if df is not None:
            legs = _cached_leg_ticks(row["quiet_id"])
            tick_okx, tick_bybit = legs if legs else (float("nan"), float("nan"))

    if df is None or df.empty:
        rec.update(
            n_ticks=0,
            n_ticks_cal=0,
            n_ticks_eval=0,
            max_gap_ms=-1,
            gap_time_frac=1.0,
            data_valid=False,
            contaminated=True,
            rejection_reason="no_ticks",
        )
        return rec

    t = df["ts"].to_numpy(dtype=np.int64)
    t.sort()
    gaps = np.diff(t)
    long_gaps = gaps[gaps > GAP_COUNT_MS]
    mid = s + HALF_MS
    n_cal = int(np.searchsorted(t, mid, side="left"))
    n_eval = int(t.size - n_cal)
    d_long = df["res_long"].to_numpy(dtype=float)
    d_short = df["res_short"].to_numpy(dtype=float)

    rec.update(
        n_ticks=int(t.size),
        n_ticks_cal=n_cal,
        n_ticks_eval=n_eval,
        max_gap_ms=int(gaps.max()) if gaps.size else 0,
        n_gaps_gt_60s=int(long_gaps.size),
        gap_time_frac=float(long_gaps.sum() / (e - s)) if long_gaps.size else 0.0,
        tick_okx=tick_okx,
        tick_bybit=tick_bybit,
        eff_res_long_pct=float(np.median(d_long)),
        eff_res_short_pct=float(np.median(d_short)),
    )

    # These per-gap flags are the first-audit diagnostic only. The binding
    # contamination rule is the unknown-time union applied in the freeze step;
    # a single long gap with every 5-minute file present is a per-coin feed
    # outage or an unchanged L1, and the latter is normal in a quiet regime.
    reasons = []
    if rec["missing_file_frac"] > MAX_MISSING_FILE_FRAC:
        reasons.append(f"file_holes={n_missing}/{n_exp}")
    if rec["max_gap_ms"] > MAX_INTRA_GAP_MS:
        reasons.append(f"max_gap_ms={rec['max_gap_ms']}")
    if rec["gap_time_frac"] > MAX_GAP_TIME_FRAC:
        reasons.append(f"gap_time_frac={rec['gap_time_frac']:.3f}")
    if min(n_cal, n_eval) < MIN_TICKS_PER_HALF:
        reasons.append(f"thin_half cal={n_cal} eval={n_eval}")
    rec["data_valid"] = min(n_cal, n_eval) >= MIN_TICKS_PER_HALF
    rec["contaminated_pergap_diagnostic"] = bool(reasons)
    rec["rejection_reason_pergap_diagnostic"] = ";".join(reasons)
    return rec


def run_audit(out_dir: Path = OUT_V2) -> pd.DataFrame:
    out_dir.mkdir(parents=True, exist_ok=True)
    man = build_manifest()
    recs = [audit_window(r) for _, r in man.iterrows()]
    audit = pd.DataFrame(recs)
    full = man.merge(audit.drop(columns=["base_coin", "window_start_ts_utc"]), on="quiet_id")
    full["quiet_visual_verified"] = True  # user-declared quiet selection
    full["selection_notes"] = "user-declared quiet interval, chosen before any r/z computation"
    full.to_csv(out_dir / "manifest_audit.csv", index=False)
    meta = {
        "manifest_version": "v2",
        "liquidity_class_pre": LIQUIDITY_CLASS,
        "pergap_diagnostic_thresholds": {
            "MAX_MISSING_FILE_FRAC": MAX_MISSING_FILE_FRAC,
            "MAX_INTRA_GAP_MS": MAX_INTRA_GAP_MS,
            "MAX_GAP_TIME_FRAC": MAX_GAP_TIME_FRAC,
            "MIN_TICKS_PER_HALF": MIN_TICKS_PER_HALF,
        },
        "binding_contamination_rule": "unknown-time union, applied in the freeze step",
        "n_windows": int(len(full)),
        "n_cache_hits": int(full["cache_hit"].sum()),
        "n_fresh_reads": int((~full["cache_hit"]).sum()),
    }
    (out_dir / "manifest_audit_meta.json").write_text(json.dumps(meta, indent=2))
    return full


if __name__ == "__main__":
    df = run_audit()
    cols = [
        "quiet_id",
        "liquidity_class_pre",
        "n_ticks",
        "n_ticks_cal",
        "n_ticks_eval",
        "n_files_missing",
        "max_gap_ms",
        "n_gaps_gt_60s",
        "eff_res_long_pct",
        "cache_hit",
        "rejection_reason_pergap_diagnostic",
    ]
    with pd.option_context("display.width", 200, "display.max_columns", 50):
        print(df[cols].to_string(index=False))
