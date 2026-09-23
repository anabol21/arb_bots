"""Gear 2.2 follow-up: zero-inflated / hurdle signal→fill response (exploratory).

Does NOT overwrite research/output/gear22_signal_fill or the first experiment note.
Reuses frozen events from the first run (same ticks, floor, L, exclusions).
Spearman(x,Δ) is secondary only — not used as success criterion.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
EVENTS_SRC = REPO / "research" / "output" / "gear22_signal_fill" / "events.parquet"
OUT_DIR = REPO / "research" / "output" / "gear22_signal_fill_hurdle"

PRIMARY_L = 100
LATENCIES = (50, 100, 200)
BOOT_N = 200
BOOT_SEED = 22

# Pre-registered |z| bins — do not reorder after seeing results.
Z_BINS: list[tuple[str, float, float]] = [
    ("(0,1]", 0.0, 1.0),  # 0 < |z| <= 1
    ("(1,2]", 1.0, 2.0),
    ("(2,4]", 2.0, 4.0),
    (">4", 4.0, np.inf),
]

STATUS = "exploratory"  # no unseen post-Aug window available as of 2026-08-27


def load_events(L: int = PRIMARY_L, path: Path = EVENTS_SRC) -> pd.DataFrame:
    import pyarrow.parquet as pq

    t = pq.read_table(path, filters=[("L_ms", "=", int(L))])
    df = t.to_pandas()
    df = df.loc[np.isfinite(df["z"]) & np.isfinite(df["delta"]) & np.isfinite(df["x"])].copy()
    df["abs_z"] = df["z"].abs()
    df["moved"] = df["delta"] != 0.0
    df["hour_id"] = (df["signal_ts_ms"] // 3_600_000).astype(np.int64)
    df["block4h_id"] = (df["signal_ts_ms"] // (4 * 3_600_000)).astype(np.int64)
    df["day_id"] = (df["signal_ts_ms"] // 86_400_000).astype(np.int64)
    df["fresh_both"] = (~df["stale_okx"].astype(bool)) & (~df["stale_bybit"].astype(bool))
    return df


def _bin_mask(abs_z: np.ndarray, lo: float, hi: float) -> np.ndarray:
    if np.isinf(hi):
        return abs_z > lo
    return (abs_z > lo) & (abs_z <= hi)


def hurdle_stats_from_arrays(
    z: np.ndarray,
    delta: np.ndarray,
    *,
    coins: Optional[np.ndarray] = None,
    equal_coin: bool = False,
) -> dict:
    """Compute pre-registered hurdle metrics for fixed |z| bins."""
    abs_z = np.abs(z)
    out_bins = []
    for name, lo, hi in Z_BINS:
        m = _bin_mask(abs_z, lo, hi)
        if equal_coin and coins is not None:
            # mean of per-coin metrics with ≥20 rows in bin
            coin_rows = []
            for c in np.unique(coins[m]):
                cm = m & (coins == c)
                if int(cm.sum()) < 20:
                    continue
                coin_rows.append(
                    _one_bin_stats(z[cm], delta[cm], name=name, n_coins=1)
                )
            if not coin_rows:
                out_bins.append(
                    {
                        "bin": name,
                        "n": int(m.sum()),
                        "n_coins": 0,
                        "empty": True,
                    }
                )
                continue
            agg = {"bin": name, "n": int(m.sum()), "n_coins": len(coin_rows), "equal_coin": True}
            for key in (
                "p_move",
                "p_plus_collapse",
                "I_plus",
                "p_minus_return",
                "I_minus",
                "A",
                "E_delta",
                "E_delta_moved",
                "med_collapse_size",
                "med_continue_size",
            ):
                vals = [r[key] for r in coin_rows if np.isfinite(r.get(key, np.nan))]
                agg[key] = float(np.mean(vals)) if vals else float("nan")
            agg["n_moved"] = int(sum(r.get("n_moved", 0) for r in coin_rows))
            out_bins.append(agg)
            continue
        out_bins.append(_one_bin_stats(z[m], delta[m], name=name, n_coins=None, coins=coins[m] if coins is not None else None))
    return {"bins": out_bins}


def _one_bin_stats(
    z: np.ndarray,
    delta: np.ndarray,
    *,
    name: str,
    n_coins: Optional[int],
    coins: Optional[np.ndarray] = None,
) -> dict:
    n = int(len(z))
    moved = delta != 0.0
    n_moved = int(moved.sum())
    p_move = float(n_moved / n) if n else float("nan")

    pos = z > 0
    neg = z < 0
    pos_m = pos & moved
    neg_m = neg & moved

    # B: P(Δ<0 | moved, z>0)
    if int(pos_m.sum()) > 0:
        p_plus_collapse = float((delta[pos_m] < 0).mean())
    else:
        p_plus_collapse = float("nan")

    # C: I+ on positive z (includes zeros in denom via p_move on z>0)
    pos_n = int(pos.sum())
    if pos_n > 0:
        p_neg_d = float((delta[pos] < 0).mean())
        p_pos_d = float((delta[pos] > 0).mean())
        p_mv_pos = float((moved & pos).sum() / pos_n)
        I_plus = (p_neg_d - p_pos_d) / p_mv_pos if p_mv_pos > 0 else float("nan")
    else:
        I_plus = float("nan")

    # D: negative tail return
    if int(neg_m.sum()) > 0:
        p_minus_return = float((delta[neg_m] > 0).mean())
    else:
        p_minus_return = float("nan")

    neg_n = int(neg.sum())
    if neg_n > 0:
        p_pos_on_neg = float((delta[neg] > 0).mean())
        p_neg_on_neg = float((delta[neg] < 0).mean())
        p_mv_neg = float((moved & neg).sum() / neg_n)
        I_minus = (p_pos_on_neg - p_neg_on_neg) / p_mv_neg if p_mv_neg > 0 else float("nan")
    else:
        I_minus = float("nan")

    A = (
        float(I_plus - I_minus)
        if np.isfinite(I_plus) and np.isfinite(I_minus)
        else float("nan")
    )

    E_delta = float(np.mean(delta)) if n else float("nan")
    E_delta_moved = float(np.mean(delta[moved])) if n_moved else float("nan")

    # sizes on positive z
    collapse = pos & (delta < 0)
    continue_up = pos & (delta > 0)
    med_collapse = float(np.median(-delta[collapse])) if int(collapse.sum()) else float("nan")
    med_continue = float(np.median(delta[continue_up])) if int(continue_up.sum()) else float("nan")

    n_coin = int(n_coins) if n_coins is not None else (int(np.unique(coins).size) if coins is not None else 0)

    return {
        "bin": name,
        "n": n,
        "n_coins": n_coin,
        "n_pos": pos_n,
        "n_neg": neg_n,
        "n_moved": n_moved,
        "n_pos_moved": int(pos_m.sum()),
        "n_neg_moved": int(neg_m.sum()),
        "p_move": p_move,
        "p_plus_collapse": p_plus_collapse,
        "I_plus": float(I_plus) if np.isfinite(I_plus) else float("nan"),
        "p_minus_return": p_minus_return,
        "I_minus": float(I_minus) if np.isfinite(I_minus) else float("nan"),
        "A": A,
        "E_delta": E_delta,
        "E_delta_moved": E_delta_moved,
        "med_collapse_size": med_collapse,
        "med_continue_size": med_continue,
    }


def _bin_counts(z: np.ndarray, delta: np.ndarray) -> dict[str, dict[str, int]]:
    """Sufficient counts per |z| bin for hurdle probabilities."""
    abs_z = np.abs(z)
    out: dict[str, dict[str, int]] = {}
    for name, lo, hi in Z_BINS:
        m = _bin_mask(abs_z, lo, hi)
        zz = z[m]
        dd = delta[m]
        moved = dd != 0.0
        pos = zz > 0
        neg = zz < 0
        out[name] = {
            "n": int(m.sum()),
            "n_moved": int(moved.sum()),
            "n_pos": int(pos.sum()),
            "n_neg": int(neg.sum()),
            "n_pos_moved": int((pos & moved).sum()),
            "n_neg_moved": int((neg & moved).sum()),
            "n_pos_delta_neg": int((pos & (dd < 0)).sum()),
            "n_pos_delta_pos": int((pos & (dd > 0)).sum()),
            "n_neg_delta_pos": int((neg & (dd > 0)).sum()),
            "n_neg_delta_neg": int((neg & (dd < 0)).sum()),
            # for magnitudes: sum and count of collapse/continue sizes on pos
            "sum_delta": float(dd.sum()) if m.any() else 0.0,
            "sum_delta_moved": float(dd[moved].sum()) if moved.any() else 0.0,
            "sum_collapse_size": float((-dd[pos & (dd < 0)]).sum()) if (pos & (dd < 0)).any() else 0.0,
            "n_collapse": int((pos & (dd < 0)).sum()),
            "sum_continue_size": float(dd[pos & (dd > 0)].sum()) if (pos & (dd > 0)).any() else 0.0,
            "n_continue": int((pos & (dd > 0)).sum()),
        }
    return out


def _metrics_from_counts(counts: dict[str, dict]) -> list[dict]:
    rows = []
    for name, _, _ in Z_BINS:
        c = counts[name]
        n = c["n"]
        p_move = c["n_moved"] / n if n else float("nan")
        p_plus = (
            c["n_pos_delta_neg"] / c["n_pos_moved"] if c["n_pos_moved"] else float("nan")
        )
        if c["n_pos"] and c["n_pos_moved"]:
            p_neg_d = c["n_pos_delta_neg"] / c["n_pos"]
            p_pos_d = c["n_pos_delta_pos"] / c["n_pos"]
            p_mv = c["n_pos_moved"] / c["n_pos"]
            I_plus = (p_neg_d - p_pos_d) / p_mv if p_mv > 0 else float("nan")
        else:
            I_plus = float("nan")
        p_minus = (
            c["n_neg_delta_pos"] / c["n_neg_moved"] if c["n_neg_moved"] else float("nan")
        )
        if c["n_neg"] and c["n_neg_moved"]:
            p_pos_on_neg = c["n_neg_delta_pos"] / c["n_neg"]
            p_neg_on_neg = c["n_neg_delta_neg"] / c["n_neg"]
            p_mv_n = c["n_neg_moved"] / c["n_neg"]
            I_minus = (p_pos_on_neg - p_neg_on_neg) / p_mv_n if p_mv_n > 0 else float("nan")
        else:
            I_minus = float("nan")
        A = (
            float(I_plus - I_minus)
            if np.isfinite(I_plus) and np.isfinite(I_minus)
            else float("nan")
        )
        rows.append(
            {
                "bin": name,
                "n": n,
                "n_moved": c["n_moved"],
                "n_pos": c["n_pos"],
                "n_neg": c["n_neg"],
                "n_pos_moved": c["n_pos_moved"],
                "n_neg_moved": c["n_neg_moved"],
                "p_move": float(p_move) if np.isfinite(p_move) else float("nan"),
                "p_plus_collapse": float(p_plus) if np.isfinite(p_plus) else float("nan"),
                "I_plus": float(I_plus) if np.isfinite(I_plus) else float("nan"),
                "p_minus_return": float(p_minus) if np.isfinite(p_minus) else float("nan"),
                "I_minus": float(I_minus) if np.isfinite(I_minus) else float("nan"),
                "A": A,
                "E_delta": c["sum_delta"] / n if n else float("nan"),
                "E_delta_moved": c["sum_delta_moved"] / c["n_moved"] if c["n_moved"] else float("nan"),
                "med_collapse_size": (
                    c["sum_collapse_size"] / c["n_collapse"] if c["n_collapse"] else float("nan")
                ),  # mean proxy in count-boot; median recomputed on point
                "med_continue_size": (
                    c["sum_continue_size"] / c["n_continue"] if c["n_continue"] else float("nan")
                ),
            }
        )
    return rows


def _add_counts(a: dict[str, dict], b: dict[str, dict]) -> dict[str, dict]:
    out = {}
    for name, _, _ in Z_BINS:
        ca, cb = a[name], b[name]
        out[name] = {k: ca.get(k, 0) + cb.get(k, 0) for k in ca}
    return out


def block_bootstrap(
    df: pd.DataFrame,
    block_col: str,
    *,
    n_boot: int = BOOT_N,
    seed: int = BOOT_SEED,
    equal_coin: bool = False,
) -> dict:
    """Resample whole global blocks via pre-aggregated counts (fast, equivalent for rates)."""
    z_all = df["z"].to_numpy(dtype=np.float64)
    d_all = df["delta"].to_numpy(dtype=np.float64)
    coins_all = df["base_coin"].astype(str).to_numpy()
    blocks = df[block_col].to_numpy()
    uniq = np.unique(blocks)
    n_blocks = int(uniq.size)

    # Point estimates with true medians
    point = hurdle_stats_from_arrays(z_all, d_all, coins=coins_all, equal_coin=False)
    # overwrite medians properly
    for row in point["bins"]:
        name = row["bin"]
        lo, hi = next((lo, hi) for n, lo, hi in Z_BINS if n == name)
        m = _bin_mask(np.abs(z_all), lo, hi)
        zz, dd = z_all[m], d_all[m]
        pos = zz > 0
        coll = pos & (dd < 0)
        cont = pos & (dd > 0)
        row["med_collapse_size"] = float(np.median(-dd[coll])) if coll.any() else float("nan")
        row["med_continue_size"] = float(np.median(dd[cont])) if cont.any() else float("nan")
        row["n_blocks"] = n_blocks
        row["n_coins"] = int(np.unique(coins_all[m]).size) if m.any() else 0

    if equal_coin:
        # equal-coin point only (no heavy boot)
        eq_point = hurdle_stats_from_arrays(z_all, d_all, coins=coins_all, equal_coin=True)
        return {
            "point": eq_point,
            "bootstrap": None,
            "n_blocks": n_blocks,
            "block_col": block_col,
            "equal_coin": True,
            "note": "equal-coin: point only (no block bootstrap)",
        }

    # Per-block counts
    block_counts: list[dict[str, dict]] = []
    for b in uniq:
        idx = blocks == b
        block_counts.append(_bin_counts(z_all[idx], d_all[idx]))

    if n_blocks < 8:
        return {"point": point, "bootstrap": None, "n_blocks": n_blocks, "block_col": block_col}

    keys = [
        "p_move",
        "p_plus_collapse",
        "I_plus",
        "p_minus_return",
        "I_minus",
        "A",
        "E_delta",
        "E_delta_moved",
        "med_collapse_size",
        "med_continue_size",
    ]
    samples = {i: {k: [] for k in keys} for i in range(len(Z_BINS))}
    rng = np.random.default_rng(seed)
    for _ in range(n_boot):
        pick = rng.integers(0, n_blocks, size=n_blocks)
        tot = None
        for i in pick:
            tot = block_counts[i] if tot is None else _add_counts(tot, block_counts[i])
        metrics = _metrics_from_counts(tot)
        for i, row in enumerate(metrics):
            for k in keys:
                v = row.get(k, np.nan)
                if np.isfinite(v):
                    samples[i][k].append(float(v))

    boot_bins = []
    for i, (name, _, _) in enumerate(Z_BINS):
        row = {"bin": name}
        for k in keys:
            arr = np.asarray(samples[i][k], dtype=np.float64)
            pt = point["bins"][i].get(k)
            if arr.size == 0:
                row[k] = {"point": pt, "ci_low": float("nan"), "ci_high": float("nan")}
                continue
            lo, hi = np.nanpercentile(arr, [2.5, 97.5])
            row[k] = {
                "point": pt,
                "ci_low": float(lo),
                "ci_high": float(hi),
                "n_boot": int(arr.size),
            }
        boot_bins.append(row)
    return {
        "point": point,
        "bootstrap": boot_bins,
        "n_blocks": n_blocks,
        "block_col": block_col,
        "n_boot": n_boot,
        "equal_coin": False,
    }


def continuous_curves(df: pd.DataFrame, n_bins: int = 12) -> pd.DataFrame:
    """Extra binned curves (not success criterion)."""
    d = df.loc[np.isfinite(df["z"])].copy()
    # signed z quantiles — show both sides
    d["z_bin"] = pd.qcut(d["z"], n_bins, duplicates="drop")
    rows = []
    for b, g in d.groupby("z_bin", observed=True):
        z = g["z"].to_numpy()
        delta = g["delta"].to_numpy()
        moved = delta != 0
        pos = z > 0
        neg = z < 0
        pos_m = pos & moved
        neg_m = neg & moved
        rows.append(
            {
                "z_bin": str(b),
                "median_z": float(g["z"].median()),
                "n": int(len(g)),
                "p_move": float(moved.mean()),
                "p_plus_collapse": float((delta[pos_m] < 0).mean()) if pos_m.any() else np.nan,
                "p_minus_return": float((delta[neg_m] > 0).mean()) if neg_m.any() else np.nan,
                "E_delta": float(delta.mean()),
                "E_delta_moved": float(delta[moved].mean()) if moved.any() else np.nan,
            }
        )
    return pd.DataFrame(rows)


def leg_attribution(df: pd.DataFrame) -> dict:
    """Decompose Δ on positive-z moved rows; slice by freshness/trigger."""
    pos = df.loc[(df["z"] > 0) & (df["delta"] != 0)].copy()
    if pos.empty:
        return {}

    def _slice(g: pd.DataFrame, label: str) -> dict:
        if len(g) < 50:
            return {"label": label, "n": int(len(g))}
        # which leg moved more in mid
        abs_okx = g["d_okx_mid"].abs()
        abs_by = g["d_bybit_mid"].abs()
        return {
            "label": label,
            "n": int(len(g)),
            "p_collapse": float((g["delta"] < 0).mean()),
            "median_delta": float(g["delta"].median()),
            "median_abs_d_okx_mid": float(abs_okx.median()),
            "median_abs_d_bybit_mid": float(abs_by.median()),
            "median_abs_d_okx_ask": float(g["d_okx_ask"].abs().median()),
            "median_abs_d_bybit_bid": float(g["d_bybit_bid"].abs().median()),
            "share_okx_mid_dominates": float((abs_okx > abs_by).mean()),
            "share_bybit_mid_dominates": float((abs_by > abs_okx).mean()),
            "corr_delta_d_okx_mid": float(g["delta"].corr(g["d_okx_mid"])),
            "corr_delta_d_bybit_mid": float(g["delta"].corr(g["d_bybit_mid"])),
        }

    slices = {
        "all_pos_moved": _slice(pos, "all_pos_moved"),
        "fresh_both": _slice(pos.loc[pos["fresh_both"]], "fresh_both"),
        "stale_okx": _slice(pos.loc[pos["stale_okx"] == True], "stale_okx"),  # noqa: E712
        "stale_bybit": _slice(pos.loc[pos["stale_bybit"] == True], "stale_bybit"),  # noqa: E712
        "trigger_okx": _slice(pos.loc[pos["trigger"].astype(str).str.lower() == "okx"], "trigger_okx"),
        "trigger_bybit": _slice(
            pos.loc[pos["trigger"].astype(str).str.lower() == "bybit"], "trigger_bybit"
        ),
        "side_long": _slice(pos.loc[pos["side"] == "long"], "side_long"),
        "side_short": _slice(pos.loc[pos["side"] == "short"], "side_short"),
    }
    # |z|>2 positive moved — upper tail attribution
    upper = pos.loc[pos["abs_z"] > 2]
    slices["pos_moved_absz_gt2"] = _slice(upper, "pos_moved_absz_gt2")
    return slices


def ci_above(ci: dict, threshold: float) -> bool:
    return (
        isinstance(ci, dict)
        and np.isfinite(ci.get("ci_low", np.nan))
        and float(ci["ci_low"]) > threshold
    )


def verdict_from_results(primary_boot: dict, boot_4h: dict, boot_day: dict, legs: dict) -> dict:
    """Apply pre-registered success/fail rules. Returns structured verdict."""
    pb = {b["bin"]: b for b in (primary_boot.get("bootstrap") or [])}
    # Focus upper positive: (2,4] and >4
    upper = [">4", "(2,4]"]
    notes = []

    def get(bin_name: str, key: str):
        return pb.get(bin_name, {}).get(key, {})

    # Check p+_collapse and I+ on upper bins
    collapse_ok = all(ci_above(get(b, "p_plus_collapse"), 0.5) for b in upper if get(b, "p_plus_collapse"))
    I_ok = all(ci_above(get(b, "I_plus"), 0.0) for b in upper if get(b, "I_plus"))
    A_ok = all(ci_above(get(b, "A"), 0.0) for b in upper if get(b, "A"))

    # 4h / day sensitivity: CI still above for I+ on >4
    def boot_map(res):
        return {b["bin"]: b for b in (res.get("bootstrap") or [])}

    b4 = boot_map(boot_4h)
    bd = boot_map(boot_day)
    I_4h = ci_above(b4.get(">4", {}).get("I_plus", {}), 0.0) if b4 else False
    I_day = ci_above(bd.get(">4", {}).get("I_plus", {}), 0.0) if bd else False
    # If day CI crosses 0 → uncertain dependence
    day_I = bd.get(">4", {}).get("I_plus", {})
    uncertain = False
    if day_I and np.isfinite(day_I.get("ci_low", np.nan)) and day_I["ci_low"] <= 0 <= day_I.get("ci_high", 0):
        uncertain = True
        notes.append("day bootstrap CI for I+(>4) crosses 0")
    if b4.get(">4", {}).get("I_plus") and not I_4h:
        notes.append("4h bootstrap I+(>4) CI not entirely > 0")

    # magnitude: med_collapse vs med_continue on >4
    mag = get(">4", "med_collapse_size")
    mag_c = get(">4", "med_continue_size")
    mag_ok = True
    if isinstance(mag, dict) and isinstance(mag_c, dict):
        # fail if continue size systematically larger (point)
        if np.isfinite(mag.get("point", np.nan)) and np.isfinite(mag_c.get("point", np.nan)):
            if mag_c["point"] > mag["point"] * 1.5:
                mag_ok = False
                notes.append("continue-away median size exceeds collapse size by >1.5x on >4")

    # stale-leg: fresh_both should still show collapse
    fresh = legs.get("fresh_both", {})
    stale_only = False
    if fresh.get("n", 0) >= 100 and np.isfinite(fresh.get("p_collapse", np.nan)):
        if fresh["p_collapse"] <= 0.5:
            stale_only = True
            notes.append("fresh_both p_collapse ≤ 0.5")
    upper_leg = legs.get("pos_moved_absz_gt2", {})
    if upper_leg.get("n", 0) >= 50 and np.isfinite(upper_leg.get("p_collapse", np.nan)):
        if upper_leg["p_collapse"] <= 0.5:
            notes.append("upper |z|>2 fresh-agnostic p_collapse ≤ 0.5")

    # Classify
    if uncertain and not (collapse_ok and I_ok and I_4h and I_day):
        verdict = "Результат нестабилен / недостаточно независимых эпизодов"
    elif collapse_ok and I_ok and (I_4h or I_day) and not stale_only and mag_ok:
        if A_ok:
            verdict = (
                "Направленная коррекция положительного хвоста поддержана; "
                "A>0 — сильнее сопоставимого отрицательного хвоста"
            )
        else:
            # I+>0 but A≈0
            A_pt = get(">4", "A")
            if isinstance(A_pt, dict) and np.isfinite(A_pt.get("point", np.nan)) and abs(A_pt["point"]) < 0.05:
                verdict = (
                    "Есть обычная симметричная mean reversion, но нет отдельного evidence "
                    "для ускоренного схлопывания положительных торговых дислокаций"
                )
            elif A_ok is False and isinstance(A_pt, dict) and A_pt.get("ci_high", 1) >= 0 and A_pt.get("ci_low", -1) <= 0:
                verdict = (
                    "Есть обычная симметричная mean reversion, но нет отдельного evidence "
                    "для ускоренного схлопывания положительных торговых дислокаций"
                )
            else:
                verdict = (
                    "Направленная коррекция положительного хвоста поддержана "
                    "(A не подтверждён отдельно)"
                )
    else:
        # check volatility-only: p_move rises with |z| but I+~0
        I_pt = get(">4", "I_plus")
        if isinstance(I_pt, dict) and np.isfinite(I_pt.get("point", np.nan)) and abs(I_pt["point"]) < 0.05:
            verdict = "Наблюдается только рост условной волатильности"
        elif not collapse_ok and not I_ok:
            verdict = "Наблюдается только рост условной волатильности"
        else:
            verdict = "Результат нестабилен / недостаточно независимых эпизодов"

    return {
        "verdict": verdict,
        "status": STATUS,
        "checks": {
            "p_plus_collapse_upper_ci_gt_0.5": collapse_ok,
            "I_plus_upper_ci_gt_0": I_ok,
            "A_upper_ci_gt_0": A_ok,
            "I_plus_gt4_4h": I_4h,
            "I_plus_gt4_day": I_day,
            "uncertain_dependence": uncertain,
            "stale_only": stale_only,
            "magnitude_ok": mag_ok,
        },
        "notes": notes,
    }


def run_all(out_dir: Path = OUT_DIR) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "status": STATUS,
        "reason_no_confirmatory": "lean_ticks end at 2026-08-19T12:00Z; no unseen post-Aug window",
        "events_source": str(EVENTS_SRC),
        "primary_L_ms": PRIMARY_L,
        "z_bins": [b[0] for b in Z_BINS],
        "bootstrap_primary": "hour_id (1h global UTC blocks)",
        "bootstrap_sensitivity": ["block4h_id", "day_id"],
        "first_experiment_not_overwritten": True,
        "spearman_secondary_only": True,
    }
    (out_dir / "run_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print("load L=100...", flush=True)
    df = load_events(PRIMARY_L)
    print(f"rows={len(df)} coins={df.base_coin.nunique()} hours={df.hour_id.nunique()}", flush=True)

    print("primary 1h bootstrap...", flush=True)
    primary = block_bootstrap(df, "hour_id", n_boot=BOOT_N, seed=BOOT_SEED)
    (out_dir / "primary_1h.json").write_text(json.dumps(primary, indent=2, default=str), encoding="utf-8")

    print("equal-coin 1h...", flush=True)
    eq = block_bootstrap(df, "hour_id", n_boot=max(80, BOOT_N // 2), seed=BOOT_SEED + 1, equal_coin=True)
    (out_dir / "equal_coin_1h.json").write_text(json.dumps(eq, indent=2, default=str), encoding="utf-8")

    print("4h bootstrap...", flush=True)
    boot4 = block_bootstrap(df, "block4h_id", n_boot=BOOT_N, seed=BOOT_SEED + 2)
    (out_dir / "sensitivity_4h.json").write_text(json.dumps(boot4, indent=2, default=str), encoding="utf-8")

    print("day bootstrap...", flush=True)
    boot_day = block_bootstrap(df, "day_id", n_boot=BOOT_N, seed=BOOT_SEED + 3)
    (out_dir / "sensitivity_day.json").write_text(json.dumps(boot_day, indent=2, default=str), encoding="utf-8")

    print("curves...", flush=True)
    curves = continuous_curves(df)
    curves.to_parquet(out_dir / "continuous_curves.parquet", index=False)
    curves.to_csv(out_dir / "continuous_curves.csv", index=False)

    print("legs...", flush=True)
    legs = leg_attribution(df)
    (out_dir / "leg_attribution.json").write_text(json.dumps(legs, indent=2, default=str), encoding="utf-8")

    # sensitivity L=50/200 point only (no full bootstrap — cost)
    sens = {}
    for L in (50, 200):
        print(f"point L={L}...", flush=True)
        dL = load_events(L)
        sens[str(L)] = hurdle_stats_from_arrays(
            dL["z"].to_numpy(), dL["delta"].to_numpy(), coins=dL["base_coin"].astype(str).to_numpy()
        )
    (out_dir / "sensitivity_L50_L200_point.json").write_text(
        json.dumps(sens, indent=2, default=str), encoding="utf-8"
    )

    verdict = verdict_from_results(primary, boot4, boot_day, legs)
    (out_dir / "verdict.json").write_text(json.dumps(verdict, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "verdict.txt").write_text(verdict["verdict"] + "\n", encoding="utf-8")
    print("VERDICT:", verdict["verdict"], flush=True)
    return {"primary": primary, "boot4": boot4, "boot_day": boot_day, "legs": legs, "verdict": verdict}


if __name__ == "__main__":
    run_all()
