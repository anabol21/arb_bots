"""Gear 2.2 experiment Z1 - stage 2: window statistics, transfer, ICC, verdict.

Consumes the frozen manifest and the per-window cache written by
``gear22_z1_quiet_normalization_lib``. Read-only with respect to the collector,
the canonical simulator and every strategy parameter. No PnL anywhere.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from research.gear22_z1_quiet_normalization_lib import (
    CACHE,
    HALF_MS,
    LIQUIDITY_CLASS,
    OUT,
    OUT_V2,
)

# Manifest-v2 run: the v1 pilot artifacts stay frozen under ``OUT/v1``.
OUTV = OUT_V2

# --- frozen analysis contract ------------------------------------------------
UNKNOWN_GAP_MS = 60_000  # dwell longer than this = unknown L1 state, weight 0
MAD_TO_SIGMA = 1.4826
PRIMARY_P = (0.95, 0.97, 0.99)
BOOT_B = 2000
BOOT_SEED = 20260830
SIGMA_EPS = 1e-15
DIRECTIONS = ("long", "short")


def _load(quiet_id: str) -> pd.DataFrame:
    return pd.read_parquet(CACHE / f"{quiet_id}.parquet")


def dwell_weights(ts: np.ndarray, start: int, end: int) -> np.ndarray:
    """Time-in-state weight per tick, zero where the L1 state is unknown."""
    nxt = np.empty(ts.size, dtype=np.int64)
    nxt[:-1] = ts[1:]
    nxt[-1] = end
    dwell = nxt - ts
    w = dwell.astype(float)
    w[dwell > UNKNOWN_GAP_MS] = 0.0
    w[ts < start] = 0.0
    return w


def clip_to_half(ts: np.ndarray, w: np.ndarray, lo: int, hi: int) -> np.ndarray:
    """Restrict each dwell interval [t, t+w) to [lo, hi)."""
    a = np.maximum(ts, lo)
    b = np.minimum(ts + w, hi)
    return np.maximum(b - a, 0.0)


def wtable(vals: np.ndarray, w: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Compress to unique sorted values with summed positive weights."""
    m = (w > 0) & np.isfinite(vals)
    v, ww = vals[m], w[m]
    if v.size == 0:
        return np.empty(0), np.empty(0)
    order = np.argsort(v, kind="stable")
    v, ww = v[order], ww[order]
    uv, idx = np.unique(v, return_index=True)
    sums = np.add.reduceat(ww, idx)
    return uv, sums


def _cdf(w: np.ndarray) -> np.ndarray:
    tot = w.sum()
    return np.cumsum(w) / tot if tot > 0 else np.zeros_like(w)


def wq(v: np.ndarray, w: np.ndarray, p: float) -> float:
    """Time-weighted quantile: smallest atom with cumulative weight >= p."""
    if v.size == 0:
        return float("nan")
    cw = _cdf(w)
    k = int(np.searchsorted(cw, p, side="left"))
    return float(v[min(k, v.size - 1)])


def wq_cont(v: np.ndarray, w: np.ndarray, p: float) -> float:
    """Resolution-aware quantile: the atom's CDF interval is spread linearly
    over the value gap above it, so discretisation no longer quantises the
    quantile estimate itself."""
    if v.size == 0:
        return float("nan")
    if v.size == 1:
        return float(v[0])
    cw = _cdf(w)
    k = int(np.searchsorted(cw, p, side="left"))
    k = min(k, v.size - 1)
    lo = cw[k - 1] if k > 0 else 0.0
    span = cw[k] - lo
    frac = (p - lo) / span if span > 0 else 0.0
    hi_v = v[k + 1] if k + 1 < v.size else v[k]
    return float(v[k] + frac * (hi_v - v[k]))


def wmedian_abs_dev(v: np.ndarray, w: np.ndarray, center: float) -> float:
    a = np.abs(v - center)
    uv, uw = wtable(a, w)
    return wq(uv, uw, 0.5)


def tail_interval(v: np.ndarray, w: np.ndarray, q: float) -> tuple[float, float]:
    """Tie-aware exceedance interval [P(r > q), P(r >= q)]."""
    if v.size == 0:
        return (float("nan"), float("nan"))
    tot = w.sum()
    gt = w[v > q].sum() / tot
    ge = w[v >= q].sum() / tot
    return (float(gt), float(ge))


def atom_bracket(v: np.ndarray, w: np.ndarray, q: float) -> tuple[float, float]:
    """Exceedance frequencies achievable on the target's own price lattice
    within one atom of ``q``.

    A numeric threshold cannot be placed between two adjacent occupied levels:
    every threshold inside a gap yields the same exceedance. The reachable
    values around ``q`` are therefore P(r >= v_{k+1}) and P(r >= v_k), where
    v_k is the largest occupied level not above ``q``. The lower end equals the
    strict exceedance P(r > q) used by the observed arm, so this bracket is the
    observed value widened by exactly one lattice step.
    """
    if v.size == 0:
        return (float("nan"), float("nan"))
    tot = w.sum()
    if tot <= 0:
        return (float("nan"), float("nan"))
    k = int(np.searchsorted(v, q, side="right")) - 1
    if k < 0:
        return (1.0, 1.0)
    hi = float(w[k:].sum() / tot)
    lo = float(w[k + 1 :].sum() / tot) if k + 1 < v.size else 0.0
    return (lo, hi)


def w1(v1: np.ndarray, w1_: np.ndarray, v2: np.ndarray, w2: np.ndarray) -> float:
    """1-Wasserstein distance between two weighted discrete distributions."""
    if v1.size == 0 or v2.size == 0:
        return float("nan")
    grid = np.union1d(v1, v2)
    # step CDFs evaluated on the merged grid
    c1 = _cdf(w1_)
    c2 = _cdf(w2)
    f1 = c1[np.searchsorted(v1, grid, side="right") - 1]
    f1 = np.where(np.searchsorted(v1, grid, side="right") == 0, 0.0, f1)
    f2 = c2[np.searchsorted(v2, grid, side="right") - 1]
    f2 = np.where(np.searchsorted(v2, grid, side="right") == 0, 0.0, f2)
    dv = np.diff(grid)
    return float(np.sum(np.abs(f1[:-1] - f2[:-1]) * dv))


def snap(v: np.ndarray, w: np.ndarray, grid: float) -> tuple[np.ndarray, np.ndarray]:
    """Re-quantise a weighted distribution onto a common coarser grid."""
    if not np.isfinite(grid) or grid <= 0:
        return v, w
    return wtable(np.round(v / grid) * grid, w)


@dataclass
class Window:
    quiet_id: str
    coin: str
    liq: str
    direction: str
    regime: str
    cluster: str
    F0: float
    F0_25: float
    F0_75: float
    sigma0: float
    F1: float
    sigma1: float
    kappa: float
    kappa_eval: float
    delta_s: float
    # eval-half weighted tables in normalized units
    rc_v: np.ndarray
    rc_w: np.ndarray
    rl_v: np.ndarray
    rl_w: np.ndarray
    zp_v: np.ndarray
    zp_w: np.ndarray
    n_levels: int
    a_max: float
    status: str


def build_window(man_row: pd.Series, direction: str) -> Window:
    df = _load(man_row["quiet_id"])
    ts = df["ts"].to_numpy(dtype=np.int64)
    s = df[f"spread_{direction}"].to_numpy(dtype=float)
    res = df[f"res_{direction}"].to_numpy(dtype=float)
    start, end = int(man_row["start_ms"]), int(man_row["end_ms"])
    mid = start + HALF_MS

    w_all = dwell_weights(ts, start, end)
    w_cal = clip_to_half(ts, w_all, start, mid)
    w_ev = clip_to_half(ts, w_all, mid, end)

    cv, cw = wtable(s, w_cal)
    F0 = wq(cv, cw, 0.50)
    F0_25 = wq(cv, cw, 0.25)
    F0_75 = wq(cv, cw, 0.75)
    sigma0 = MAD_TO_SIGMA * wmedian_abs_dev(cv, cw, F0)

    ev, ew = wtable(s, w_ev)
    F1 = wq(ev, ew, 0.50)
    sigma1 = MAD_TO_SIGMA * wmedian_abs_dev(ev, ew, F1)

    denom0 = sigma0 if sigma0 > SIGMA_EPS else float("nan")
    denom1 = sigma1 if sigma1 > SIGMA_EPS else float("nan")

    rc_v, rc_w = (ev - F0) / denom0, ew
    rl_v, rl_w = (ev - F1) / denom1, ew
    zp_v, zp_w = wtable(np.maximum(ev - F0_75, 0.0) / denom0, ew)

    rv, rw = wtable(res, w_ev)
    delta_s = wq(rv, rw, 0.5)
    kappa = delta_s / denom0
    kappa_eval = delta_s / denom1

    # The MAD normalisation is unavailable once the quote lattice step reaches
    # the robust scale itself; wMAD == 0 is the limiting case kappa -> inf.
    status = "ok"
    if not np.isfinite(kappa) or kappa >= 1.0:
        status = "tick_resolution_limited"

    a_max = float(ew.max() / ew.sum()) if ew.size else float("nan")

    return Window(
        quiet_id=man_row["quiet_id"],
        coin=man_row["base_coin"],
        liq=man_row["liquidity_class_pre"],
        direction=direction,
        regime=man_row["quiet_regime_id"],
        cluster=man_row["calendar_cluster_id"],
        F0=F0,
        F0_25=F0_25,
        F0_75=F0_75,
        sigma0=sigma0,
        F1=F1,
        sigma1=sigma1,
        kappa=float(kappa),
        kappa_eval=float(kappa_eval),
        delta_s=float(delta_s),
        rc_v=rc_v,
        rc_w=rc_w,
        rl_v=rl_v,
        rl_w=rl_w,
        zp_v=zp_v,
        zp_w=zp_w,
        n_levels=int(ev.size),
        a_max=a_max,
        status=status,
    )


def window_stats_row(win: Window) -> dict:
    d: dict = {
        "quiet_id": win.quiet_id,
        "base_coin": win.coin,
        "liquidity_class_pre": win.liq,
        "direction": win.direction,
        "quiet_regime_id": win.regime,
        "calendar_cluster_id": win.cluster,
        "F0": win.F0,
        "F0_75": win.F0_75,
        "sigma0": win.sigma0,
        "F1": win.F1,
        "sigma1": win.sigma1,
        "delta_F": (win.F1 - win.F0) / win.sigma0 if win.sigma0 > SIGMA_EPS else np.nan,
        "L_sigma": np.log(win.sigma1 / win.sigma0)
        if min(win.sigma0, win.sigma1) > SIGMA_EPS
        else np.nan,
        "delta_s": win.delta_s,
        "kappa": win.kappa,
        "kappa_eval": win.kappa_eval,
        "a_max": win.a_max,
        "n_occupied_levels": win.n_levels,
        "normalization_status": win.status,
        "local_status": "tick_resolution_limited"
        if (not np.isfinite(win.kappa_eval) or win.kappa_eval >= 1.0)
        else "ok",
    }
    for name, (v, w) in {
        "rc": (win.rc_v, win.rc_w),
        "rl": (win.rl_v, win.rl_w),
        "zp": (win.zp_v, win.zp_w),
    }.items():
        uv, uw = wtable(v, w) if name != "zp" else (v, w)
        for p in PRIMARY_P:
            d[f"{name}_Q{int(p * 100)}"] = wq(uv, uw, p)
            d[f"{name}_Q{int(p * 100)}_ra"] = wq_cont(uv, uw, p)
        d[f"{name}_B50"] = wq(uv, uw, 0.75) - wq(uv, uw, 0.25)
        d[f"{name}_B90"] = wq(uv, uw, 0.95) - wq(uv, uw, 0.05)
    d["p_zp_pos"] = float(win.zp_w[win.zp_v > 0].sum() / win.zp_w.sum()) if win.zp_w.size else np.nan
    return d


# --- transfer tests ---------------------------------------------------------

def accept_band(p: float) -> tuple[float, float]:
    a = 1.0 - p
    return (a / 2.0, 2.0 * a)


def transfers_within_coin(wins: list[Window]) -> pd.DataFrame:
    rows = []
    by = {}
    for w in wins:
        by.setdefault((w.coin, w.direction), []).append(w)
    for (coin, direction), group in by.items():
        for src in group:
            for dst in group:
                if src.quiet_id == dst.quiet_id:
                    continue
                for rep, (sv, sw, dv, dw) in {
                    "rc": (src.rc_v, src.rc_w, dst.rc_v, dst.rc_w),
                    "rl": (src.rl_v, src.rl_w, dst.rl_v, dst.rl_w),
                    "zp": (src.zp_v, src.zp_w, dst.zp_v, dst.zp_w),
                }.items():
                    suv, suw = wtable(sv, sw)
                    duv, duw = wtable(dv, dw)
                    for p in PRIMARY_P:
                        q = wq(suv, suw, p)
                        gt, _ = tail_interval(duv, duw, q)
                        blo, bhi = atom_bracket(duv, duw, q)
                        lo, hi = accept_band(p)
                        rows.append(
                            {
                                "base_coin": coin,
                                "direction": direction,
                                "representation": rep,
                                "p": p,
                                "src": src.quiet_id,
                                "dst": dst.quiet_id,
                                "q_src": q,
                                "exceed_observed": gt,
                                "bracket_lo": blo,
                                "bracket_hi": bhi,
                                "band_lo": lo,
                                "band_hi": hi,
                                "pass_observed": bool(lo <= gt <= hi),
                                "pass_resolution_aware": bool(bhi >= lo and blo <= hi),
                            }
                        )
    return pd.DataFrame(rows)


def pooled_table(wins: list[Window], rep: str, equal_coin: bool = True):
    """Pool eval-half distributions with equal weight per coin and per window."""
    per_coin: dict[str, list[tuple[np.ndarray, np.ndarray]]] = {}
    for w in wins:
        v, ww = (w.rc_v, w.rc_w) if rep == "rc" else (w.rl_v, w.rl_w)
        uv, uw = wtable(v, ww)
        if uw.sum() <= 0:
            continue
        per_coin.setdefault(w.coin, []).append((uv, uw))
    vs, ws = [], []
    for coin, items in per_coin.items():
        coin_scale = 1.0 / len(per_coin) if equal_coin else 1.0
        for uv, uw in items:
            scale = coin_scale / len(items) if equal_coin else 1.0
            vs.append(uv)
            ws.append(uw / uw.sum() * scale)
    if not vs:
        return np.empty(0), np.empty(0)
    return wtable(np.concatenate(vs), np.concatenate(ws))


def loco_calibration(
    wins: list[Window], rep: str = "rc", equal_coin: bool = True
) -> pd.DataFrame:
    rows = []
    coins = sorted({w.coin for w in wins})
    for direction in DIRECTIONS:
        pool = [w for w in wins if w.direction == direction]
        for c in coins:
            for scope in ("global", "liquidity"):
                if scope == "global":
                    others = [w for w in pool if w.coin != c]
                else:
                    g = LIQUIDITY_CLASS[c]
                    others = [w for w in pool if w.coin != c and w.liq == g]
                n_other_coins = len({w.coin for w in others})
                if n_other_coins == 0:
                    continue
                pv, pw = pooled_table(others, rep, equal_coin=equal_coin)
                for w in [x for x in pool if x.coin == c]:
                    duv, duw = wtable(*( (w.rc_v, w.rc_w) if rep == "rc" else (w.rl_v, w.rl_w) ))
                    for p in PRIMARY_P:
                        q = wq(pv, pw, p)
                        gt, _ = tail_interval(duv, duw, q)
                        blo, bhi = atom_bracket(duv, duw, q)
                        lo, hi = accept_band(p)
                        rows.append(
                            {
                                "scope": scope,
                                "base_coin": c,
                                "liquidity_class_pre": LIQUIDITY_CLASS[c],
                                "direction": direction,
                                "representation": rep,
                                "quiet_id": w.quiet_id,
                                "kappa": w.kappa,
                                "p": p,
                                "q_pool": q,
                                "exceed_observed": gt,
                                "bracket_lo": blo,
                                "bracket_hi": bhi,
                                "band_lo": lo,
                                "band_hi": hi,
                                "n_other_coins": n_other_coins,
                                "pooling": "equal_coin" if equal_coin else "time_weighted",
                                "descriptive_only": n_other_coins < 2,
                                "pass_observed": bool(lo <= gt <= hi),
                                "pass_resolution_aware": bool(bhi >= lo and blo <= hi),
                                "direction_of_violation": "too_frequent"
                                if gt > hi
                                else ("too_rare" if gt < lo else "in_band"),
                                "direction_of_violation_ra": "too_frequent"
                                if blo > hi
                                else ("too_rare" if bhi < lo else "in_band"),
                            }
                        )
    return pd.DataFrame(rows)


def distance_matrices(wins: list[Window], rep: str = "rc") -> pd.DataFrame:
    rows = []
    for direction in DIRECTIONS:
        sel = [w for w in wins if w.direction == direction]
        for i, a in enumerate(sel):
            for b in sel[i + 1 :]:
                av, aw = wtable(*((a.rc_v, a.rc_w) if rep == "rc" else (a.rl_v, a.rl_w)))
                bv, bw = wtable(*((b.rc_v, b.rc_w) if rep == "rc" else (b.rl_v, b.rl_w)))
                grid = max(a.kappa, b.kappa)
                sav, saw = snap(av, aw, grid)
                sbv, sbw = snap(bv, bw, grid)
                rows.append(
                    {
                        "direction": direction,
                        "representation": rep,
                        "a": a.quiet_id,
                        "b": b.quiet_id,
                        "coin_a": a.coin,
                        "coin_b": b.coin,
                        "liq_a": a.liq,
                        "liq_b": b.liq,
                        "same_coin": a.coin == b.coin,
                        "same_liq": a.liq == b.liq,
                        "grid": grid,
                        "w1_raw": w1(av, aw, bv, bw),
                        "w1_res_matched": w1(sav, saw, sbv, sbw),
                    }
                )
    return pd.DataFrame(rows)


# --- variance decomposition -------------------------------------------------

def icc_point(df: pd.DataFrame, col: str, residualize_liq: bool = False) -> dict:
    d = df[["base_coin", "liquidity_class_pre", col]].dropna()
    if d["base_coin"].nunique() < 2:
        return {"icc": np.nan, "var_coin": np.nan, "var_window": np.nan}
    y = d[col].to_numpy(dtype=float)
    if residualize_liq:
        y = y - d.groupby("liquidity_class_pre")[col].transform("mean").to_numpy()
    d = d.assign(_y=y)
    grp = d.groupby("base_coin")["_y"]
    n_i = grp.size().to_numpy()
    means = grp.mean().to_numpy()
    # within-coin (window) variance, pooled
    ss_w = float(((d["_y"] - grp.transform("mean")) ** 2).sum())
    df_w = int(n_i.sum() - n_i.size)
    var_w = ss_w / df_w if df_w > 0 else np.nan
    # between-coin variance via one-way random-effects moment estimator
    k = n_i.size
    n_bar = n_i.sum() - (n_i**2).sum() / n_i.sum()
    ss_b = float((n_i * (means - np.average(means, weights=n_i)) ** 2).sum())
    ms_b = ss_b / (k - 1) if k > 1 else np.nan
    n0 = n_bar / (k - 1) if k > 1 else np.nan
    var_c = (ms_b - var_w) / n0 if (n0 and n0 > 0) else np.nan
    var_c = max(var_c, 0.0) if np.isfinite(var_c) else np.nan
    tot = var_c + var_w
    return {
        "icc": float(var_c / tot) if (np.isfinite(tot) and tot > 0) else np.nan,
        "var_coin": float(var_c) if np.isfinite(var_c) else np.nan,
        "var_window": float(var_w) if np.isfinite(var_w) else np.nan,
    }


def icc_bootstrap(df: pd.DataFrame, col: str, residualize_liq: bool, rng) -> np.ndarray:
    """Cluster bootstrap over coins: a coin enters with all of its windows."""
    coins = sorted(df["base_coin"].unique())
    out = np.empty(BOOT_B)
    parts = {c: df[df["base_coin"] == c] for c in coins}
    for b in range(BOOT_B):
        pick = rng.choice(len(coins), size=len(coins), replace=True)
        frames = []
        for j, idx in enumerate(pick):
            f = parts[coins[idx]].copy()
            f["base_coin"] = f"{coins[idx]}#{j}"
            frames.append(f)
        out[b] = icc_point(pd.concat(frames, ignore_index=True), col, residualize_liq)["icc"]
    return out


def icc_bootstrap_units(
    df: pd.DataFrame, col: str, residualize_liq: bool, rng, unit_col: str
) -> np.ndarray:
    """Cluster bootstrap over an arbitrary dependence unit.

    A unit enters with all of its rows. A coin that spans several units keeps
    one label inside a replicate, so between-coin structure is not destroyed;
    only a repeated draw of the same unit is relabelled into a separate copy.
    """
    units = sorted(df[unit_col].unique())
    parts = {u: df[df[unit_col] == u] for u in units}
    out = np.empty(BOOT_B)
    for b in range(BOOT_B):
        pick = rng.choice(len(units), size=len(units), replace=True)
        seen: dict = {}
        frames = []
        for idx in pick:
            u = units[idx]
            k = seen.get(u, 0)
            seen[u] = k + 1
            f = parts[u].copy()
            if k:
                f["base_coin"] = f["base_coin"].astype(str) + f"#d{k}"
            frames.append(f)
        out[b] = icc_point(pd.concat(frames, ignore_index=True), col, residualize_liq)["icc"]
    return out


def run_icc_cluster_sensitivity(stats: pd.DataFrame) -> pd.DataFrame:
    """Section 14 sensitivity: resample calendar clusters instead of coins."""
    rng = np.random.default_rng(BOOT_SEED)
    rows = []
    metrics = []
    for p in PRIMARY_P:
        metrics.append(f"rc_Q{int(p * 100)}")
        metrics.append(f"rc_Q{int(p * 100)}_ra")
    for direction in DIRECTIONS:
        sub = stats[stats["direction"] == direction]
        for col in metrics:
            for resid in (False, True):
                pt = icc_point(sub, col, resid)
                bs = icc_bootstrap_units(
                    sub, col, resid, rng, "calendar_cluster_id"
                )
                bs = bs[np.isfinite(bs)]
                rows.append(
                    {
                        "direction": direction,
                        "metric": col,
                        "arm": "resolution_aware" if col.endswith("_ra") else "observed",
                        "conditioning": "coin|liq" if resid else "coin",
                        "bootstrap_unit": "calendar_cluster_id",
                        "icc": pt["icc"],
                        "var_coin": pt["var_coin"],
                        "var_window": pt["var_window"],
                        "lcb95": float(np.quantile(bs, 0.05)) if bs.size else np.nan,
                        "ucb95": float(np.quantile(bs, 0.95)) if bs.size else np.nan,
                        "n_boot_ok": int(bs.size),
                    }
                )
    return pd.DataFrame(rows)


def variance_decomposition(stats: pd.DataFrame) -> pd.DataFrame:
    """Nested class / coin / window split of each primary tail metric."""
    rows = []
    metrics = [f"rc_Q{int(p * 100)}" for p in PRIMARY_P]
    metrics += [f"rc_Q{int(p * 100)}_ra" for p in PRIMARY_P]
    metrics += [f"rl_Q{int(p * 100)}" for p in PRIMARY_P]
    for direction in DIRECTIONS:
        sub = stats[stats["direction"] == direction]
        for col in metrics:
            d = sub[["liquidity_class_pre", "base_coin", col]].dropna()
            if d.empty:
                continue
            y = d[col].to_numpy(dtype=float)
            grand = y.mean()
            class_mean = d.groupby("liquidity_class_pre")[col].transform("mean").to_numpy()
            coin_mean = d.groupby("base_coin")[col].transform("mean").to_numpy()
            ss_class = float(((class_mean - grand) ** 2).sum())
            ss_coin = float(((coin_mean - class_mean) ** 2).sum())
            ss_window = float(((y - coin_mean) ** 2).sum())
            tot = ss_class + ss_coin + ss_window
            rows.append(
                {
                    "direction": direction,
                    "metric": col,
                    "arm": "resolution_aware" if col.endswith("_ra") else "observed",
                    "n": int(len(d)),
                    "ss_class": ss_class,
                    "ss_coin_within_class": ss_coin,
                    "ss_window_within_coin": ss_window,
                    "frac_class": ss_class / tot if tot > 0 else np.nan,
                    "frac_coin": ss_coin / tot if tot > 0 else np.nan,
                    "frac_window": ss_window / tot if tot > 0 else np.nan,
                }
            )
    return pd.DataFrame(rows)


def tail_quantile_table(stats: pd.DataFrame) -> pd.DataFrame:
    """Long-form P95/P97/P99 for every representation and both arms."""
    rows = []
    for _, r in stats.iterrows():
        for rep, label in (
            ("rc", "r_causal"),
            ("rl", "r_local"),
            ("zp", "z_plus_causal"),
        ):
            for p in PRIMARY_P:
                k = int(p * 100)
                rows.append(
                    {
                        "quiet_id": r["quiet_id"],
                        "base_coin": r["base_coin"],
                        "liquidity_class_pre": r["liquidity_class_pre"],
                        "direction": r["direction"],
                        "representation": label,
                        "p": p,
                        "q_observed": r[f"{rep}_Q{k}"],
                        "q_resolution_aware": r[f"{rep}_Q{k}_ra"],
                        "normalization_status": r["normalization_status"],
                        "kappa": r["kappa"],
                    }
                )
    return pd.DataFrame(rows)


def discretisation_table(stats: pd.DataFrame, man: pd.DataFrame) -> pd.DataFrame:
    keep = [
        "quiet_id",
        "base_coin",
        "liquidity_class_pre",
        "direction",
        "sigma0",
        "sigma1",
        "delta_s",
        "kappa",
        "kappa_eval",
        "a_max",
        "n_occupied_levels",
        "normalization_status",
        "local_status",
    ]
    d = stats[keep].copy()
    d["mad_status"] = np.where(
        d["sigma0"] <= SIGMA_EPS, "wMAD_zero", "wMAD_positive"
    )
    return d.merge(
        man[
            [
                "quiet_id",
                "tick_okx",
                "tick_bybit",
                "eff_res_long_pct",
                "eff_res_short_pct",
            ]
        ],
        on="quiet_id",
        how="left",
    )


def run_icc(stats: pd.DataFrame) -> pd.DataFrame:
    rng = np.random.default_rng(BOOT_SEED)
    rows = []
    metrics = []
    for rep in ("rc", "rl", "zp"):
        for p in PRIMARY_P:
            metrics.append(f"{rep}_Q{int(p * 100)}")
            metrics.append(f"{rep}_Q{int(p * 100)}_ra")
    for direction in DIRECTIONS:
        sub = stats[stats["direction"] == direction]
        for col in metrics:
            for resid in (False, True):
                pt = icc_point(sub, col, resid)
                bs = icc_bootstrap(sub, col, resid, rng)
                bs = bs[np.isfinite(bs)]
                rows.append(
                    {
                        "direction": direction,
                        "metric": col,
                        "arm": "resolution_aware" if col.endswith("_ra") else "observed",
                        "conditioning": "coin|liq" if resid else "coin",
                        "bootstrap_unit": "base_coin",
                        "icc": pt["icc"],
                        "var_coin": pt["var_coin"],
                        "var_window": pt["var_window"],
                        "lcb95": float(np.quantile(bs, 0.05)) if bs.size else np.nan,
                        "ucb95": float(np.quantile(bs, 0.95)) if bs.size else np.nan,
                        "n_boot_ok": int(bs.size),
                    }
                )
    return pd.DataFrame(rows)


# --- verdict ----------------------------------------------------------------

def _systematic_offenders(loco: pd.DataFrame, arm: str) -> dict:
    """Section 14.1 condition 2, evaluated on one arm.

    A coin offends when the violation holds on every one of its windows, points
    the same way, and appears on at least two of P95/P97/P99.
    """
    pass_col = "pass_observed" if arm == "observed" else "pass_resolution_aware"
    dir_col = "direction_of_violation" if arm == "observed" else "direction_of_violation_ra"
    out = {}
    for direction in DIRECTIONS:
        offenders = []
        sub = loco[loco["direction"] == direction]
        for coin, cg in sub.groupby("base_coin"):
            levels = []
            for p in PRIMARY_P:
                pg = cg[cg["p"] == p]
                if pg.empty:
                    continue
                viol = ~pg[pass_col]
                dirs = set(pg.loc[viol, dir_col])
                if viol.all() and len(dirs) == 1:
                    levels.append((p, dirs.pop()))
            if len(levels) >= 2 and len({d for _, d in levels}) == 1:
                offenders.append(
                    {
                        "coin": coin,
                        "liq": LIQUIDITY_CLASS[coin],
                        "violation": levels[0][1],
                        "levels": [p for p, _ in levels],
                        "n_windows": int(cg["quiet_id"].nunique()),
                    }
                )
        classes = {o["liq"] for o in offenders}
        out[direction] = {
            "offenders": offenders,
            "n_classes": len(classes),
            "met": len(offenders) >= 2 and len(classes) >= 2,
        }
    return out


def decide_verdict(
    stats: pd.DataFrame,
    icc: pd.DataFrame,
    loco: pd.DataFrame,
    stats_all: pd.DataFrame,
) -> dict:
    notes: list[str] = []

    # condition 1: ICC_coin LCB > 0.5 for >= 2 of P95/P97/P99, resolution-aware arm
    cond1 = {}
    for direction in DIRECTIONS:
        hits = []
        for p in PRIMARY_P:
            row = icc[
                (icc["direction"] == direction)
                & (icc["metric"] == f"rc_Q{int(p * 100)}_ra")
                & (icc["conditioning"] == "coin")
            ]
            if not row.empty and np.isfinite(row.iloc[0]["lcb95"]) and row.iloc[0]["lcb95"] > 0.5:
                hits.append(p)
        cond1[direction] = hits
    cond1_met = {d: len(v) >= 2 for d, v in cond1.items()}

    g = loco[(loco["scope"] == "global") & (~loco["descriptive_only"])]
    obs = _systematic_offenders(g, "observed")
    ra = _systematic_offenders(g, "resolution_aware")

    rates = {}
    for direction in DIRECTIONS:
        sub = g[g["direction"] == direction]
        rates[direction] = {
            "observed": float(sub["pass_observed"].mean()) if not sub.empty else np.nan,
            "resolution_aware": float(sub["pass_resolution_aware"].mean())
            if not sub.empty
            else np.nan,
        }

    n_limited = int((stats_all["normalization_status"] != "ok").sum())
    frac_limited = n_limited / max(len(stats_all), 1)
    n_regimes = int(stats["quiet_regime_id"].nunique())
    n_coins = int(stats["base_coin"].nunique())
    coins_ge2 = int((stats.groupby(["base_coin", "direction"])["quiet_id"].nunique() >= 2).groupby("base_coin").any().sum())

    # is the ICC arm able to separate within-coin from between-coin variation?
    prim = icc[
        icc["metric"].isin([f"rc_Q{int(p * 100)}_ra" for p in PRIMARY_P])
        & (icc["conditioning"] == "coin")
    ]
    icc_uninformative = bool(
        len(prim) > 0
        and (prim["lcb95"].fillna(0) < 0.05).all()
        and (prim["ucb95"].fillna(1) > 0.3).all()
    )
    if icc_uninformative:
        notes.append(
            "ICC cluster-bootstrap CI span the low and mid range for every primary "
            "tail metric: within-coin and between-coin variation are not separated"
        )

    reject_obs = any(obs[d]["met"] for d in DIRECTIONS)
    reject_ra = any(ra[d]["met"] for d in DIRECTIONS)

    # is the observed/resolution-aware discrepancy localised in tick resolution?
    loc = {}
    for direction in DIRECTIONS:
        sub = g[(g["direction"] == direction) & g["kappa"].notna()]
        if len(sub) > 5:
            fail_obs = ~sub["pass_observed"]
            loc[direction] = {
                "kappa_median_fail": float(sub.loc[fail_obs, "kappa"].median())
                if fail_obs.any()
                else np.nan,
                "kappa_median_pass": float(sub.loc[~fail_obs, "kappa"].median())
                if (~fail_obs).any()
                else np.nan,
            }

    if reject_ra and any(cond1_met[d] for d in DIRECTIONS):
        verdict = "REJECT_GLOBAL"
    elif reject_obs and not reject_ra:
        verdict = "REJECT_GLOBAL_NUMERIC_Z_ONLY"
    elif n_regimes < 10 or coins_ge2 < 5 or frac_limited > 0.20 or icc_uninformative:
        verdict = "INCONCLUSIVE"
        if n_regimes < 10:
            notes.append(f"only {n_regimes} independent quiet regimes")
        if coins_ge2 < 5:
            notes.append(f"only {coins_ge2} coins retain >=2 primary windows")
        if frac_limited > 0.20:
            notes.append(f"{frac_limited:.0%} of windows are tick_resolution_limited")
    elif reject_ra:
        # shape rejected on the resolution-aware arm but the ICC arm cannot
        # confirm that coin identity dominates window turnover
        verdict = "INCONCLUSIVE"
        notes.append(
            "resolution-aware arm shows systematic cross-coin violation while "
            "condition 1 (ICC LCB > 0.5) is not met"
        )
    else:
        verdict = "ADVANCE_GLOBAL"

    return {
        "verdict": verdict,
        "cond1_icc_hits": {d: [float(p) for p in v] for d, v in cond1.items()},
        "cond1_met": cond1_met,
        "observed_arm": obs,
        "resolution_aware_arm": ra,
        "reject_observed": reject_obs,
        "reject_resolution_aware": reject_ra,
        "loco_global_pass_rates": rates,
        "kappa_localisation": loc,
        "n_windows_tick_resolution_limited": n_limited,
        "frac_windows_tick_resolution_limited": frac_limited,
        "n_quiet_regimes": n_regimes,
        "n_coins": n_coins,
        "n_coins_with_ge2_windows": coins_ge2,
        "icc_uninformative": icc_uninformative,
        "notes": notes,
    }


def _pass_rates(loco: pd.DataFrame) -> dict:
    g = loco[(loco["scope"] == "global") & (~loco["descriptive_only"])]
    out = {}
    for direction in DIRECTIONS:
        sub = g[g["direction"] == direction]
        out[direction] = {
            "observed": float(sub["pass_observed"].mean()) if not sub.empty else np.nan,
            "resolution_aware": float(sub["pass_resolution_aware"].mean())
            if not sub.empty
            else np.nan,
            "n": int(len(sub)),
        }
    return out


def run_stage2(out_dir: Path = OUTV) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    man = pd.read_csv(out_dir / "manifest_frozen.csv")
    primary = man[man["primary"]].reset_index(drop=True)
    all_wins: list[Window] = []
    for _, r in primary.iterrows():
        for d in DIRECTIONS:
            all_wins.append(build_window(r, d))

    stats_all = pd.DataFrame([window_stats_row(w) for w in all_wins])
    stats_all.to_csv(out_dir / "window_stats.csv", index=False)
    tail_quantile_table(stats_all).to_csv(out_dir / "tail_quantiles.csv", index=False)
    discretisation_table(stats_all, man).to_csv(
        out_dir / "discretisation.csv", index=False
    )

    # Section 14.3 forbids a verdict driven by tick_resolution_limited windows,
    # so they are held out of the primary comparison and reported separately.
    wins = [w for w in all_wins if w.status == "ok"]
    stats = stats_all[stats_all["normalization_status"] == "ok"].reset_index(drop=True)

    tr = transfers_within_coin(wins)
    tr.to_csv(out_dir / "within_coin_transfer.csv", index=False)

    loco = pd.concat(
        [loco_calibration(wins, "rc"), loco_calibration(wins, "rl")], ignore_index=True
    )
    loco.to_csv(out_dir / "loco_calibration.csv", index=False)

    # sensitivity: raw time-weighted pooling instead of equal weight per coin
    loco_tw = pd.concat(
        [
            loco_calibration(wins, "rc", equal_coin=False),
            loco_calibration(wins, "rl", equal_coin=False),
        ],
        ignore_index=True,
    )
    loco_tw.to_csv(out_dir / "loco_calibration_time_weighted.csv", index=False)

    dist = pd.concat(
        [distance_matrices(wins, "rc"), distance_matrices(wins, "rl")], ignore_index=True
    )
    dist.to_csv(out_dir / "distances.csv", index=False)

    icc = run_icc(stats)
    icc.to_csv(out_dir / "icc.csv", index=False)

    vdec = variance_decomposition(stats)
    vdec.to_csv(out_dir / "variance_decomposition.csv", index=False)

    icc_cl = run_icc_cluster_sensitivity(stats)
    icc_cl.to_csv(out_dir / "icc_calendar_cluster.csv", index=False)

    verdict = decide_verdict(stats, icc, loco[loco["representation"] == "rc"], stats_all)

    # sensitivity: same verdict machinery with the degenerate windows included
    loco_s = loco_calibration(all_wins, "rc")
    icc_s = run_icc(stats_all)
    verdict["sensitivity_including_tick_limited"] = decide_verdict(
        stats_all, icc_s, loco_s, stats_all
    )["verdict"]
    verdict["sensitivity_time_weighted_pooling_pass_rates"] = _pass_rates(
        loco_tw[loco_tw["representation"] == "rc"]
    )
    verdict["calendar_cluster_bootstrap"] = icc_cl[
        icc_cl["conditioning"] == "coin"
    ].to_dict(orient="records")
    verdict["n_primary_windows"] = int(len(primary))
    verdict["n_calendar_clusters"] = int(primary["calendar_cluster_id"].nunique())
    verdict["manifest_version"] = "v2"

    (out_dir / "verdict.json").write_text(json.dumps(verdict, indent=2, default=str))
    return verdict


if __name__ == "__main__":
    v = run_stage2()
    print(json.dumps(v, indent=2, default=str))
