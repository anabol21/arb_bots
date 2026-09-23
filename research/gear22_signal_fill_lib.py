"""Gear 2.2 research: signal→fill spread response (scratch; not canon).

Pre-registered quiet floor F_t: past-only rolling median over LOOKBACK_MS.
Does not retune VARIATION/HYPER or touch model_gear2 / gear2_backtest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd

from research.gap_fill import DEFAULT_GAP_FILL_SLACK_MS
from research.is_crypto import is_crypto
from research.lean_ticks_io import (
    list_lean_files_overlapping,
    parse_ts_ms,
    prepare_lean_ticks,
    read_lean_raw,
)

REPO = Path(__file__).resolve().parents[1]
LEAN_TICKS = REPO / "output" / "lean_ticks"
OUT_DIR = REPO / "research" / "output" / "gear22_signal_fill"

# --- Pre-registered constants (not searched) ---
LOOKBACK_MS = 300_000  # 5 min past window for F_t / sigma
MIN_PAST = 30
SIGMA_MIN = 0.02  # % spread units
GAP_SLACK_MS = DEFAULT_GAP_FILL_SLACK_MS  # 1000
LATENCIES_MS = (50, 100, 200)
PRIMARY_L = 100
# Temporal grid for candidate states (not IID; block bootstrap handles dependence)
STORE_STRIDE_MS = 15_000
# Always keep large |x|/|z|; keep quiet baseline ≤1 / BASELINE_MS
BASELINE_MS = 60_000
X_KEEP = 0.05
Z_KEEP = 1.0
STALE_LEG_MS = 200
BLOCK_MS = 300_000  # 5-min blocks for cluster bootstrap
X_ABS_MIN_FOR_R = 0.05
BOOT_N = 200
BOOT_SEED = 22
QUIET_MAD_SCALE = 1.4826

# August continuous segments from docs/model-data-coverage.md (UTC, END exclusive).
# Internal 5-min holes on 16–19: fills across holes rejected via L+slack.
AUGUST_SEGMENTS: list[tuple[str, str]] = [
    ("2026-08-03T13:35:00Z", "2026-08-04T00:00:00Z"),
    ("2026-08-04T00:00:00Z", "2026-08-04T11:45:00Z"),
    ("2026-08-05T11:50:00Z", "2026-08-06T00:00:00Z"),
    ("2026-08-06T00:00:00Z", "2026-08-10T00:00:00Z"),
    ("2026-08-10T00:00:00Z", "2026-08-10T05:35:00Z"),
    ("2026-08-10T12:55:00Z", "2026-08-10T13:45:00Z"),
    ("2026-08-10T16:25:00Z", "2026-08-10T17:05:00Z"),
    ("2026-08-10T19:00:00Z", "2026-08-10T19:35:00Z"),
    ("2026-08-10T20:40:00Z", "2026-08-11T00:00:00Z"),
    ("2026-08-11T00:00:00Z", "2026-08-14T00:00:00Z"),
    ("2026-08-14T00:00:00Z", "2026-08-14T12:25:00Z"),
    ("2026-08-14T12:30:00Z", "2026-08-16T00:00:00Z"),
    ("2026-08-16T00:00:00Z", "2026-08-19T12:00:00Z"),
]


@dataclass
class RunConfig:
    lean_ticks: Path = LEAN_TICKS
    out_dir: Path = OUT_DIR
    latencies_ms: tuple[int, ...] = LATENCIES_MS
    lookback_ms: int = LOOKBACK_MS
    min_past: int = MIN_PAST
    sigma_min: float = SIGMA_MIN
    gap_slack_ms: float = GAP_SLACK_MS
    store_stride_ms: int = STORE_STRIDE_MS
    chunk_ms: int = 3_600_000
    workers: int = 4
    max_coins: Optional[int] = None
    segments: Optional[list[tuple[str, str]]] = None


def _floor_at(
    s: np.ndarray, ts: np.ndarray, idxs: np.ndarray, lookback_ms: int, min_past: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Past-only median/MAD only at candidate indices (sparse)."""
    F = np.full(idxs.size, np.nan, dtype=np.float64)
    sig = np.full(idxs.size, np.nan, dtype=np.float64)
    n_past = np.zeros(idxs.size, dtype=np.int32)
    for k, i in enumerate(idxs.tolist()):
        left = int(np.searchsorted(ts, ts[i] - lookback_ms, side="left"))
        if i - left < min_past:
            continue
        window = s[left:i]
        med = float(np.median(window))
        F[k] = med
        mad = float(np.median(np.abs(window - med)))
        sig[k] = QUIET_MAD_SCALE * mad
        n_past[k] = i - left
    return F, sig, n_past


def _coin_side_events(
    g: pd.DataFrame,
    *,
    side: str,
    latencies_ms: Sequence[int],
    lookback_ms: int,
    min_past: int,
    sigma_min: float,
    gap_slack_ms: float,
    store_stride_ms: int,
    block_ms: int = BLOCK_MS,
) -> pd.DataFrame:
    spread_col = "spread_long" if side == "long" else "spread_short"
    g = g.sort_values("event_local_ts_ms", kind="mergesort")
    ts = g["event_local_ts_ms"].to_numpy(dtype=np.int64)
    s = g[spread_col].to_numpy(dtype=np.float64)
    n = len(ts)
    if n < min_past + 2:
        return pd.DataFrame()

    dt = np.empty(n, dtype=np.float64)
    dt[0] = float(store_stride_ms)
    dt[1:] = np.diff(ts).astype(np.float64)
    dt = np.clip(dt, 1.0, float(BASELINE_MS))

    bucket = ts // int(store_stride_ms)
    cand = np.ones(n, dtype=bool)
    cand[1:] = bucket[1:] != bucket[:-1]
    cand_idx = np.flatnonzero(cand)
    if cand_idx.size == 0:
        return pd.DataFrame()

    F_c, sig_c, n_past = _floor_at(s, ts, cand_idx, lookback_ms, min_past)
    ok = np.isfinite(F_c) & (n_past >= min_past)
    cand_idx = cand_idx[ok]
    F_c = F_c[ok]
    sig_c = sig_c[ok]
    if cand_idx.size == 0:
        return pd.DataFrame()

    x_c = s[cand_idx] - F_c
    z_c = x_c / np.maximum(sig_c, sigma_min)
    # stratified keep: large deviation OR ≤1 baseline per BASELINE_MS
    base_bucket = ts[cand_idx] // int(BASELINE_MS)
    is_base = np.ones(cand_idx.size, dtype=bool)
    is_base[1:] = base_bucket[1:] != base_bucket[:-1]
    keep_m = (np.abs(x_c) >= X_KEEP) | (np.abs(z_c) >= Z_KEEP) | is_base
    keep_idx = cand_idx[keep_m]
    F_k = F_c[keep_m]
    sig_k = sig_c[keep_m]
    if keep_idx.size == 0:
        return pd.DataFrame()

    coin = str(g["base_coin"].iloc[0])
    trigger = (
        g["trigger"].astype(str).to_numpy()
        if "trigger" in g.columns
        else np.full(n, "?", dtype=object)
    )
    ofr = (
        g["okx_freshness_ms"].to_numpy(dtype=np.float64)
        if "okx_freshness_ms" in g.columns
        else np.full(n, np.nan)
    )
    bfr = (
        g["bybit_freshness_ms"].to_numpy(dtype=np.float64)
        if "bybit_freshness_ms" in g.columns
        else np.full(n, np.nan)
    )
    ob = g["okx_bid_price"].to_numpy(dtype=np.float64)
    oa = g["okx_ask_price"].to_numpy(dtype=np.float64)
    bb = g["bybit_bid_price"].to_numpy(dtype=np.float64)
    ba = g["bybit_ask_price"].to_numpy(dtype=np.float64)

    frames: list[pd.DataFrame] = []
    ts_f = ts.astype(np.float64)
    for L in latencies_ms:
        targets = ts_f[keep_idx] + float(L)
        fill_idx = np.searchsorted(ts_f, targets, side="left")
        valid = fill_idx < n
        ii = keep_idx[valid]
        jj = fill_idx[valid]
        Ft = F_k[valid]
        sg = sig_k[valid]
        eff = (ts[jj] - ts[ii]).astype(np.float64)
        ok_gap = eff <= (float(L) + float(gap_slack_ms))
        ii, jj, Ft, sg, eff = ii[ok_gap], jj[ok_gap], Ft[ok_gap], sg[ok_gap], eff[ok_gap]
        if ii.size == 0:
            continue

        s0 = s[ii]
        s1 = s[jj]
        x = s0 - Ft
        z = x / np.maximum(sg, sigma_min)
        delta = s1 - s0
        correction = -np.sign(x) * delta
        abs_x = np.abs(x)
        with np.errstate(divide="ignore", invalid="ignore"):
            R = np.where(abs_x >= X_ABS_MIN_FOR_R, (s1 - Ft) / x, np.nan)
        fres_ok = np.isfinite(ofr[ii]) & np.isfinite(bfr[ii])
        fres_diff = np.where(fres_ok, ofr[ii] - bfr[ii], np.nan)
        stale_okx = fres_ok & ((ofr[ii] - bfr[ii]) > STALE_LEG_MS)
        stale_bybit = fres_ok & ((bfr[ii] - ofr[ii]) > STALE_LEG_MS)

        frames.append(
            pd.DataFrame(
                {
                    "base_coin": coin,
                    "side": side,
                    "L_ms": int(L),
                    "signal_ts_ms": ts[ii],
                    "fill_ts_ms": ts[jj],
                    "effective_latency_ms": eff,
                    "signal_spread": s0,
                    "fill_spread": s1,
                    "F": Ft,
                    "sigma": sg,
                    "x": x,
                    "z": z,
                    "delta": delta,
                    "abs_x": abs_x,
                    "abs_delta": np.abs(delta),
                    "correction": correction,
                    "R": R,
                    "w_time": dt[ii],
                    "block_id": (ts[ii] // block_ms).astype(np.int64),
                    "trigger": trigger[ii],
                    "okx_freshness_ms": ofr[ii],
                    "bybit_freshness_ms": bfr[ii],
                    "fresh_diff_okx_minus_bybit": fres_diff,
                    "stale_okx": stale_okx,
                    "stale_bybit": stale_bybit,
                    "d_okx_bid": ob[jj] - ob[ii],
                    "d_okx_ask": oa[jj] - oa[ii],
                    "d_bybit_bid": bb[jj] - bb[ii],
                    "d_bybit_ask": ba[jj] - ba[ii],
                    "d_okx_mid": 0.5 * ((ob[jj] + oa[jj]) - (ob[ii] + oa[ii])),
                    "d_bybit_mid": 0.5 * ((bb[jj] + ba[jj]) - (bb[ii] + ba[ii])),
                }
            )
        )
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _iter_chunks(start_ms: int, end_ms: int, chunk_ms: int) -> Iterable[tuple[int, int]]:
    t = start_ms
    while t < end_ms:
        u = min(t + chunk_ms, end_ms)
        yield t, u
        t = u


def process_window(start_ms: int, end_ms: int, cfg: RunConfig) -> pd.DataFrame:
    if end_ms <= start_ms:
        return pd.DataFrame()
    files = list_lean_files_overlapping(cfg.lean_ticks, start_ms, end_ms)
    if not files:
        return pd.DataFrame()
    raw, _ = read_lean_raw(cfg.lean_ticks, start_ms, end_ms, workers=cfg.workers)
    df = prepare_lean_ticks(raw)
    del raw
    df = df.loc[df["base_coin"].map(lambda c: bool(is_crypto(str(c))))].copy()
    if cfg.max_coins is not None:
        coins = sorted(df["base_coin"].unique())[: cfg.max_coins]
        df = df.loc[df["base_coin"].isin(coins)]
    parts: list[pd.DataFrame] = []
    for _, g in df.groupby("base_coin", sort=False):
        for side in ("long", "short"):
            ev = _coin_side_events(
                g,
                side=side,
                latencies_ms=cfg.latencies_ms,
                lookback_ms=cfg.lookback_ms,
                min_past=cfg.min_past,
                sigma_min=cfg.sigma_min,
                gap_slack_ms=cfg.gap_slack_ms,
                store_stride_ms=cfg.store_stride_ms,
            )
            if len(ev):
                parts.append(ev)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def process_segment(start_iso: str, end_iso: str, cfg: RunConfig, parts_dir: Optional[Path] = None, part_start: int = 0) -> tuple[pd.DataFrame, int]:
    """Process segment; if parts_dir set, write each chunk as part and return empty frame + next index."""
    start_ms = parse_ts_ms(start_iso)
    end_ms = parse_ts_ms(end_iso)
    lookahead_ms = int(max(cfg.latencies_ms) + cfg.gap_slack_ms + 50)
    parts: list[pd.DataFrame] = []
    part_i = part_start
    for a, b in _iter_chunks(start_ms, end_ms, cfg.chunk_ms):
        load_a = a - cfg.lookback_ms
        load_b = min(b + lookahead_ms, end_ms + lookahead_ms)
        print(
            f"  chunk {datetime.fromtimestamp(a / 1000, tz=timezone.utc).isoformat()} "
            f"→ {datetime.fromtimestamp(b / 1000, tz=timezone.utc).isoformat()}",
            flush=True,
        )
        try:
            ev = process_window(load_a, load_b, cfg)
        except FileNotFoundError:
            continue
        except ValueError as exc:
            print(f"    skip: {exc}", flush=True)
            continue
        if len(ev):
            ev = ev.loc[(ev["signal_ts_ms"] >= a) & (ev["signal_ts_ms"] < b)]
            if not len(ev):
                continue
            print(f"    events={len(ev)}", flush=True)
            if parts_dir is not None:
                path = parts_dir / f"part_{part_i:04d}.parquet"
                ev.to_parquet(path, index=False)
                part_i += 1
            else:
                parts.append(ev)
    if parts_dir is not None:
        return pd.DataFrame(), part_i
    if not parts:
        return pd.DataFrame(), part_i
    return pd.concat(parts, ignore_index=True), part_i


def _append_parquet(path: Path, ev: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        ev.to_parquet(path, index=False)
        return
    prev = pd.read_parquet(path)
    pd.concat([prev, ev], ignore_index=True).to_parquet(path, index=False)


def run_build_events(cfg: Optional[RunConfig] = None) -> Path:
    cfg = cfg or RunConfig()
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    parts_dir = cfg.out_dir / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    for old in parts_dir.glob("part_*.parquet"):
        old.unlink()
    segs = cfg.segments or AUGUST_SEGMENTS
    meta = {
        "hypothesis": "corr(x, delta_100)<0 directed mean reversion toward quiet floor",
        "lookback_ms": cfg.lookback_ms,
        "min_past": cfg.min_past,
        "sigma_min": cfg.sigma_min,
        "latencies_ms": list(cfg.latencies_ms),
        "gap_slack_ms": cfg.gap_slack_ms,
        "store_stride_ms": cfg.store_stride_ms,
        "baseline_ms": BASELINE_MS,
        "x_keep": X_KEEP,
        "z_keep": Z_KEEP,
        "segments": segs,
        "floor_method": (
            "past-only rolling median on sparse candidates; "
            "sigma=1.4826*MAD; no result-tuned params"
        ),
        "sampling_note": (
            f"candidates ≤1/{cfg.store_stride_ms}ms per coin/side; keep if "
            f"|x|>={X_KEEP} or |z|>={Z_KEEP} or ≤1/{BASELINE_MS}ms baseline; "
            "w_time=inter-arrival; independence via 5m block bootstrap"
        ),
        "coverage_source": "docs/model-data-coverage.md",
        "exclusions": [
            "fill across gap (delay > L + slack)",
            "invalid L1 / fail-closed stale-cross",
            "missing both legs",
            "insufficient past window for F_t",
        ],
        "not_claiming": ["arb bots as cause", "alpha / PnL", "live readiness"],
    }
    (cfg.out_dir / "run_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    out_path = cfg.out_dir / "events.parquet"
    if out_path.exists():
        out_path.unlink()
    part_i = 0
    wrote = False
    for start_iso, end_iso in segs:
        print(f"SEGMENT {start_iso} → {end_iso}", flush=True)
        _, part_i = process_segment(start_iso, end_iso, cfg, parts_dir=parts_dir, part_start=part_i)
        if part_i > 0:
            wrote = True
            print(f"  parts so far: {part_i}", flush=True)
    if not wrote:
        raise RuntimeError("no events produced")
    parts = sorted(parts_dir.glob("part_*.parquet"))
    print(f"concat {len(parts)} parts → {out_path}", flush=True)
    # concat in batches to limit RAM
    frames = []
    batch = []
    for i, p in enumerate(parts):
        batch.append(pd.read_parquet(p))
        if len(batch) >= 24:
            frames.append(pd.concat(batch, ignore_index=True))
            batch = []
    if batch:
        frames.append(pd.concat(batch, ignore_index=True))
    pd.concat(frames, ignore_index=True).to_parquet(out_path, index=False)
    return out_path


def weighted_corr(x: np.ndarray, y: np.ndarray, w: np.ndarray) -> float:
    m = np.isfinite(x) & np.isfinite(y) & np.isfinite(w) & (w > 0)
    if int(m.sum()) < 3:
        return float("nan")
    x, y, w = x[m], y[m], w[m]
    w = w / w.sum()
    mx = np.sum(w * x)
    my = np.sum(w * y)
    cx, cy = x - mx, y - my
    num = np.sum(w * cx * cy)
    den = np.sqrt(np.sum(w * cx * cx) * np.sum(w * cy * cy))
    return float(num / den) if den > 0 else float("nan")


def spearman_corr(x: np.ndarray, y: np.ndarray, w: Optional[np.ndarray] = None) -> float:
    m = np.isfinite(x) & np.isfinite(y)
    if w is not None:
        m &= np.isfinite(w) & (w > 0)
    if int(m.sum()) < 3:
        return float("nan")
    rx = pd.Series(x[m]).rank().to_numpy(dtype=np.float64)
    ry = pd.Series(y[m]).rank().to_numpy(dtype=np.float64)
    if w is None:
        return float(np.corrcoef(rx, ry)[0, 1])
    return weighted_corr(rx, ry, w[m])


def block_bootstrap_corr(
    df: pd.DataFrame,
    xcol: str,
    ycol: str,
    *,
    n_boot: int = BOOT_N,
    seed: int = BOOT_SEED,
    method: str = "spearman",
) -> dict:
    if df.empty:
        return {"point": float("nan"), "ci_low": float("nan"), "ci_high": float("nan"), "n_blocks": 0}
    # Pre-extract arrays; resample by concatenating index lists (no DataFrame concat).
    x_all = df[xcol].to_numpy(dtype=np.float64)
    y_all = df[ycol].to_numpy(dtype=np.float64)
    w_all = df["w_time"].to_numpy(dtype=np.float64)
    block_ids = df["block_id"].to_numpy()
    uniq, inverse = np.unique(block_ids, return_inverse=True)
    n_blocks = int(uniq.size)
    if n_blocks < 3:
        return {
            "point": float("nan"),
            "ci_low": float("nan"),
            "ci_high": float("nan"),
            "n_blocks": n_blocks,
        }
    groups = [np.flatnonzero(inverse == b) for b in range(n_blocks)]

    def _corr_idx(idx: np.ndarray) -> float:
        if method == "pearson":
            return weighted_corr(x_all[idx], y_all[idx], w_all[idx])
        return spearman_corr(x_all[idx], y_all[idx], w_all[idx])

    point = _corr_idx(np.arange(len(df)))
    rng = np.random.default_rng(seed)
    stats = np.empty(n_boot, dtype=np.float64)
    for b in range(n_boot):
        pick = rng.integers(0, n_blocks, size=n_blocks)
        idx = np.concatenate([groups[i] for i in pick])
        stats[b] = _corr_idx(idx)
    lo, hi = np.nanpercentile(stats, [2.5, 97.5])
    return {
        "point": float(point),
        "ci_low": float(lo),
        "ci_high": float(hi),
        "n_blocks": int(n_blocks),
        "n_obs": int(len(df)),
    }


def equal_group_corr(
    df: pd.DataFrame, xcol: str, ycol: str, group: str, method: str = "spearman"
) -> float:
    if df.empty:
        return float("nan")
    vals = []
    for _, g in df.groupby(group):
        if len(g) < 5:
            continue
        x = g[xcol].to_numpy(dtype=np.float64)
        y = g[ycol].to_numpy(dtype=np.float64)
        w = g["w_time"].to_numpy(dtype=np.float64)
        c = spearman_corr(x, y, w) if method == "spearman" else weighted_corr(x, y, w)
        if np.isfinite(c):
            vals.append(c)
    return float(np.mean(vals)) if vals else float("nan")


def conditional_bins(df: pd.DataFrame, col: str, n_bins: int = 8) -> pd.DataFrame:
    d = df.loc[np.isfinite(df[col]) & np.isfinite(df["delta"])].copy()
    if len(d) < n_bins * 20:
        return pd.DataFrame()
    d["bin"] = pd.qcut(d[col], n_bins, duplicates="drop")
    rows = []
    for b, g in d.groupby("bin", observed=True):
        x_pos = g.loc[g["x"] > 0, "delta"]
        rows.append(
            {
                "bin": str(b),
                "n": int(len(g)),
                "n_blocks": int(g["block_id"].nunique()),
                "median_x": float(g["x"].median()),
                "median_delta": float(g["delta"].median()),
                "q10_delta": float(g["delta"].quantile(0.10)),
                "q90_delta": float(g["delta"].quantile(0.90)),
                "median_correction": float(g["correction"].median()),
                "median_R": float(g["R"].median()) if g["R"].notna().any() else float("nan"),
                "p_delta_neg_given_x_pos": float((x_pos < 0).mean()) if len(x_pos) else float("nan"),
                "n_x_pos": int(len(x_pos)),
            }
        )
    return pd.DataFrame(rows)


def summarize(events: pd.DataFrame, L: int = PRIMARY_L) -> dict:
    d = events.loc[events["L_ms"] == int(L)].copy()
    out: dict = {
        "L_ms": int(L),
        "n_obs": int(len(d)),
        "n_coins": int(d["base_coin"].nunique()) if len(d) else 0,
    }
    if d.empty:
        return out
    out["effective_latency_ms"] = {
        "median": float(d["effective_latency_ms"].median()),
        "q10": float(d["effective_latency_ms"].quantile(0.10)),
        "q90": float(d["effective_latency_ms"].quantile(0.90)),
        "mean": float(d["effective_latency_ms"].mean()),
    }
    out["arm_A"] = {
        "spearman_spread_delta": spearman_corr(
            d["signal_spread"].to_numpy(), d["delta"].to_numpy(), d["w_time"].to_numpy()
        ),
        "pearson_spread_delta": weighted_corr(
            d["signal_spread"].to_numpy(), d["delta"].to_numpy(), d["w_time"].to_numpy()
        ),
        "spearman_spread_fill_MECHANICAL": spearman_corr(
            d["signal_spread"].to_numpy(), d["fill_spread"].to_numpy(), d["w_time"].to_numpy()
        ),
    }
    out["arm_B"] = {
        "spearman_x_delta": block_bootstrap_corr(d, "x", "delta", method="spearman"),
        "pearson_x_delta": block_bootstrap_corr(d, "x", "delta", method="pearson"),
        "spearman_absx_absdelta": block_bootstrap_corr(d, "abs_x", "abs_delta", method="spearman"),
        "spearman_absx_correction": block_bootstrap_corr(
            d, "abs_x", "correction", method="spearman"
        ),
        "equal_coin_spearman_x_delta": equal_group_corr(d, "x", "delta", "base_coin"),
        "equal_block_spearman_x_delta": equal_group_corr(d, "x", "delta", "block_id"),
    }
    out["arm_C"] = {
        "spearman_z_delta": block_bootstrap_corr(d, "z", "delta", method="spearman"),
        "pearson_z_delta": block_bootstrap_corr(d, "z", "delta", method="pearson"),
        "equal_coin_spearman_z_delta": equal_group_corr(d, "z", "delta", "base_coin"),
        "equal_block_spearman_z_delta": equal_group_corr(d, "z", "delta", "block_id"),
    }
    pos = d.loc[d["x"] > 0]
    out["positive_x"] = {
        "n": int(len(pos)),
        "median_delta": float(pos["delta"].median()) if len(pos) else float("nan"),
        "p_delta_neg": float((pos["delta"] < 0).mean()) if len(pos) else float("nan"),
        "median_correction": float(pos["correction"].median()) if len(pos) else float("nan"),
        "median_R": float(pos["R"].median())
        if len(pos) and pos["R"].notna().any()
        else float("nan"),
        "spearman_x_delta": block_bootstrap_corr(pos, "x", "delta", method="spearman")
        if len(pos) >= 50
        else {},
    }
    out["bins_x"] = conditional_bins(d, "x").to_dict(orient="records")
    out["bins_z"] = conditional_bins(d, "z").to_dict(orient="records")

    slices = {}
    for name, mask in [
        ("trigger_okx", d["trigger"].astype(str).str.lower() == "okx"),
        ("trigger_bybit", d["trigger"].astype(str).str.lower() == "bybit"),
        ("stale_okx", d["stale_okx"] == True),  # noqa: E712
        ("stale_bybit", d["stale_bybit"] == True),  # noqa: E712
        ("fresh_both", (d["stale_okx"] == False) & (d["stale_bybit"] == False)),  # noqa: E712
        ("side_long", d["side"] == "long"),
        ("side_short", d["side"] == "short"),
    ]:
        sub = d.loc[mask]
        if len(sub) < 50:
            slices[name] = {"n": int(len(sub))}
            continue
        slices[name] = {
            "n": int(len(sub)),
            "spearman_x_delta": block_bootstrap_corr(sub, "x", "delta", method="spearman"),
            "p_delta_neg_given_x_pos": float((sub.loc[sub["x"] > 0, "delta"] < 0).mean())
            if (sub["x"] > 0).any()
            else float("nan"),
            "median_delta_given_x_pos": float(sub.loc[sub["x"] > 0, "delta"].median())
            if (sub["x"] > 0).any()
            else float("nan"),
        }
    out["arm_D_slices"] = slices

    if len(pos):
        out["decomp_pos_x"] = {
            "spearman_correction_vs_d_okx_mid": spearman_corr(
                pos["correction"].to_numpy(),
                pos["d_okx_mid"].to_numpy(),
                pos["w_time"].to_numpy(),
            ),
            "spearman_correction_vs_d_bybit_mid": spearman_corr(
                pos["correction"].to_numpy(),
                pos["d_bybit_mid"].to_numpy(),
                pos["w_time"].to_numpy(),
            ),
            "median_abs_d_okx_mid": float(pos["d_okx_mid"].abs().median()),
            "median_abs_d_bybit_mid": float(pos["d_bybit_mid"].abs().median()),
            "median_abs_d_okx_ask": float(pos["d_okx_ask"].abs().median()),
            "median_abs_d_bybit_bid": float(pos["d_bybit_bid"].abs().median()),
        }

    coin_stats = []
    for coin, g in d.groupby("base_coin"):
        if len(g) < 30:
            continue
        coin_stats.append(
            {
                "base_coin": coin,
                "n": int(len(g)),
                "spearman_x_delta": spearman_corr(
                    g["x"].to_numpy(), g["delta"].to_numpy(), g["w_time"].to_numpy()
                ),
            }
        )
    coin_stats.sort(
        key=lambda r: abs(r["spearman_x_delta"]) if np.isfinite(r["spearman_x_delta"]) else 0.0,
        reverse=True,
    )
    out["top_coins_by_abs_corr"] = coin_stats[:15]
    return out


def verdict_from_summary(summary: dict) -> str:
    """One of the four allowed research verdicts (pre-registered criteria)."""
    b = summary.get("arm_B", {})
    sp = b.get("spearman_x_delta", {})
    point = sp.get("point", float("nan"))
    lo = sp.get("ci_low", float("nan"))
    hi = sp.get("ci_high", float("nan"))
    eq_coin = b.get("equal_coin_spearman_x_delta", float("nan"))
    eq_block = b.get("equal_block_spearman_x_delta", float("nan"))
    abs_vol = b.get("spearman_absx_absdelta", {}).get("point", float("nan"))
    pos = summary.get("positive_x", {})
    p_neg = pos.get("p_delta_neg", float("nan"))
    med_delta = pos.get("median_delta", float("nan"))
    bins = summary.get("bins_x") or []

    if not np.isfinite(point) or sp.get("n_blocks", 0) < 10:
        return "данных недостаточно / результат нестабилен"

    slices = summary.get("arm_D_slices", {})
    fresh = slices.get("fresh_both", {}).get("spearman_x_delta", {})
    stale_o = slices.get("stale_okx", {}).get("spearman_x_delta", {})
    stale_b = slices.get("stale_bybit", {}).get("spearman_x_delta", {})
    fresh_pt = fresh.get("point", float("nan"))
    stale_only = (
        np.isfinite(fresh_pt)
        and fresh_pt >= -0.02
        and (
            (np.isfinite(stale_o.get("point", float("nan"))) and stale_o.get("point", 0) < -0.05)
            or (np.isfinite(stale_b.get("point", float("nan"))) and stale_b.get("point", 0) < -0.05)
        )
    )
    if stale_only:
        return "эффект в основном объясняется догонянием ноги"

    if bins and len(bins) >= 3:
        top = bins[-1].get("median_delta", float("nan"))
        mid = bins[len(bins) // 2].get("median_delta", float("nan"))
        bins_ok = np.isfinite(top) and np.isfinite(mid) and top < mid - 1e-12
        top_p = bins[-1].get("p_delta_neg_given_x_pos", float("nan"))
    else:
        bins_ok = False
        top_p = float("nan")

    directed = (
        point < 0
        and np.isfinite(hi)
        and hi < 0
        and np.isfinite(eq_coin)
        and eq_coin < 0
        and np.isfinite(eq_block)
        and eq_block < 0
        and np.isfinite(p_neg)
        and p_neg > 0.5
        and bins_ok
        and np.isfinite(top_p)
        and top_p > 0.5
        and np.isfinite(fresh_pt)
        and fresh_pt < 0
    )
    if directed:
        return "направленное схлопывание статистически поддержано"

    # Pre-registered fail: directed median ~0 while |delta| still rises with |x|
    median_near_zero = np.isfinite(med_delta) and abs(med_delta) < 1e-12
    p_fail = np.isfinite(p_neg) and p_neg <= 0.5
    if median_near_zero and p_fail and np.isfinite(abs_vol) and abs_vol > 0.02:
        return "наблюдается только рост условной волатильности"

    if (
        np.isfinite(abs_vol)
        and abs_vol > 0.05
        and (not np.isfinite(point) or point >= -0.02 or (np.isfinite(lo) and lo <= 0 <= hi))
    ):
        return "наблюдается только рост условной волатильности"

    return "данных недостаточно / результат нестабилен"


def write_summaries(events_path: Path, out_dir: Path, latencies: Sequence[int] = LATENCIES_MS) -> str:
    ev = pd.read_parquet(events_path)
    primary = None
    for L in latencies:
        s = summarize(ev, L=int(L))
        (out_dir / f"summary_L{L}.json").write_text(
            json.dumps(s, indent=2, default=str), encoding="utf-8"
        )
        if int(L) == PRIMARY_L:
            primary = s
    assert primary is not None
    v = verdict_from_summary(primary)
    (out_dir / "verdict.txt").write_text(v + "\n", encoding="utf-8")
    return v


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Build gear22 signal-fill events")
    ap.add_argument("--max-coins", type=int, default=None)
    ap.add_argument("--smoke", action="store_true", help="One 4h segment only")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--summarize-only", action="store_true")
    args = ap.parse_args()
    cfg = RunConfig(workers=args.workers, max_coins=args.max_coins)
    if args.smoke:
        cfg.segments = [("2026-08-18T00:00:00Z", "2026-08-18T04:00:00Z")]
        cfg.out_dir = OUT_DIR / "smoke"
    if args.summarize_only:
        v = write_summaries(cfg.out_dir / "events.parquet", cfg.out_dir, cfg.latencies_ms)
        print("VERDICT:", v)
    else:
        path = run_build_events(cfg)
        ev = pd.read_parquet(path)
        print("events", len(ev), "coins", ev["base_coin"].nunique())
        v = write_summaries(path, cfg.out_dir, cfg.latencies_ms)
        print("VERDICT:", v)
