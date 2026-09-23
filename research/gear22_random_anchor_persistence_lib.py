"""Gear 2.2 scratch: random-anchor latency/persistence baseline (fast aggregate path)."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.gap_fill import DEFAULT_GAP_FILL_SLACK_MS
from research.lean_ticks_io import (
    gear2_lean_columns,
    list_lean_files_overlapping,
    parse_lean_file_window,
    parse_ts_ms,
    read_and_prepare_lean_ticks,
)

REPO = Path(__file__).resolve().parents[1]
LEAN = REPO / "output" / "lean_ticks"
OUT = REPO / "research" / "output" / "gear22_random_anchor_persistence"

COINS = [
    "2Z", "ACU", "BICO", "CAP", "ESP", "KAITO", "KMNO", "LA",
    "MUBARAK", "RVN", "TRUST", "WAL", "ZBT",
]
SIDES = ("long", "short")
W_MS = (0, 20, 50, 100)
L_MS = tuple(range(10, 301, 10))
H1, H2 = 1.0, 2.0
LOOKBACK_MS = 300_000
MIN_PAST = 30
MAD_SCALE = 1.4826
SIGMA_EPS = 1e-12
GAP_SLACK_MS = DEFAULT_GAP_FILL_SLACK_MS
INTRA_GAP_MS = 360_000
FILE_INTERVAL_MS = 300_000
BLOCK_MS = 1_800_000
BOOT_N = 2000
BOOT_SEED = 20260827
KNOWN_HOLES = [
    (parse_ts_ms("2026-08-14T12:25:00Z"), parse_ts_ms("2026-08-14T12:30:00Z")),
    (parse_ts_ms("2026-08-16T17:35:00Z"), parse_ts_ms("2026-08-16T17:50:00Z")),
]
Z_ORDER = ("z_le_1", "z_1_2", "z_2_4", "z_gt_4")


def z_bin_of(z: float) -> str:
    if z <= 1:
        return "z_le_1"
    if z <= 2:
        return "z_1_2"
    if z <= 4:
        return "z_2_4"
    return "z_gt_4"


def tw_q25(ts: np.ndarray, vals: np.ndarray, t_lo: int, t_hi: int) -> float:
    if t_hi <= t_lo or ts.size == 0:
        return np.nan
    i = int(np.searchsorted(ts, t_lo, side="right")) - 1
    if i < 0:
        return np.nan
    durs = []
    vs = []
    t_cur = float(t_lo)
    n = ts.size
    while t_cur < t_hi and i < n:
        t_next = float(ts[i + 1]) if i + 1 < n else float(t_hi)
        t_end = min(float(t_hi), t_next)
        if t_end > t_cur:
            durs.append(t_end - t_cur)
            vs.append(float(vals[i]))
        if t_next >= t_hi:
            break
        i += 1
        t_cur = t_next
    if not durs:
        return np.nan
    order = np.argsort(vs)
    total = float(np.sum(durs))
    target = 0.25 * total
    acc = 0.0
    for k in order:
        acc += durs[k]
        if acc >= target:
            return float(vs[k])
    return float(vs[order[-1]])


def floor_stats(ts: np.ndarray, s: np.ndarray, t_lo: int, t_hi: int) -> tuple[float, float, int]:
    left = int(np.searchsorted(ts, t_lo, side="left"))
    right = int(np.searchsorted(ts, t_hi, side="left"))
    n = right - left
    if n < MIN_PAST:
        return np.nan, np.nan, n
    w = s[left:right]
    med = float(np.median(w))
    mad = float(np.median(np.abs(w - med)))
    if mad <= SIGMA_EPS:
        return med, np.nan, n
    return med, MAD_SCALE * mad, n


def day_holes(day: str) -> list[tuple[int, int]]:
    start = parse_ts_ms(f"{day}T00:00:00Z")
    end = start + 86_400_000
    files = list_lean_files_overlapping(LEAN, start, end)
    starts = []
    for p in files:
        w = parse_lean_file_window(p)
        if w:
            starts.append(w[0])
    starts = sorted(set(starts))
    sset = set(starts)
    holes = []
    if starts:
        t = starts[0]
        while t < starts[-1]:
            if t not in sset:
                holes.append((t, t + FILE_INTERVAL_MS))
            t += FILE_INTERVAL_MS
    for a, b in KNOWN_HOLES:
        if a < end and start < b:
            holes.append((a, b))
    return holes


def hole_mask_intervals(holes: list[tuple[int, int]], t0: int, t1: int) -> bool:
    for a, b in holes:
        if t0 < b and a < t1:
            return True
    return False


# Aggregators: key = (side, W, z_bin, L, block_id, coin, day)
# values: n, sum_delta, sum_u, k_coll1, k_exp1, k_coll2, k_exp2, k_approx, sum_delay, n_delay, n_missing, n_gap


def process_coin_day(
    coin: str,
    day: str,
    anchors_ms: np.ndarray,
    holes: list[tuple[int, int]],
    agg: dict,
    delay_samples: list,
    counters: dict,
    rng: np.random.Generator,
) -> None:
    day0 = parse_ts_ms(f"{day}T00:00:00Z")
    day1 = day0 + 86_400_000
    pad0 = day0 - LOOKBACK_MS - max(W_MS) - 60_000
    pad1 = day1 + max(L_MS) + 5_000
    try:
        df, _ = read_and_prepare_lean_ticks(
            LEAN,
            pad0,
            pad1,
            coins={coin},
            columns=gear2_lean_columns(check_volume=False),
            need_freshness=True,
            workers=2,
        )
    except Exception as exc:  # noqa: BLE001
        counters["load_fail"] += 1
        counters["last_error"] = str(exc)
        return

    g = df.loc[df["base_coin"] == coin].sort_values("event_local_ts_ms").reset_index(drop=True)
    if g.empty:
        counters["no_ticks"] += 1
        return

    ts = g["event_local_ts_ms"].to_numpy(dtype=np.int64)
    coin_holes = list(holes)
    if ts.size >= 2:
        dlt = np.diff(ts)
        for i in np.where(dlt >= INTRA_GAP_MS)[0]:
            coin_holes.append((int(ts[i]), int(ts[i + 1])))

    L_arr = np.asarray(L_MS, dtype=np.int64)

    for side in SIDES:
        s = g["spread_long" if side == "long" else "spread_short"].to_numpy(dtype=np.float64)
        for t0 in anchors_ms:
            t0 = int(t0)
            block = f"{day}_{(t0 - day0) // BLOCK_MS:02d}"
            counters["anchors_side"] += 1

            for W in W_MS:
                flo_lo = t0 - W - LOOKBACK_MS
                flo_hi = t0 - W if W > 0 else t0
                if W == 0:
                    flo_lo, flo_hi = t0 - LOOKBACK_MS, t0
                need1 = t0 + int(L_arr[-1]) + int(GAP_SLACK_MS)
                if hole_mask_intervals(coin_holes, flo_lo, need1):
                    counters["reject_hole"] += 1
                    continue

                i_t0 = int(np.searchsorted(ts, t0, side="right")) - 1
                if i_t0 < 0:
                    counters["reject_cover"] += 1
                    continue

                m0, sig0, n_past = floor_stats(ts, s, flo_lo, flo_hi)
                if n_past < MIN_PAST:
                    counters["reject_lookback"] += 1
                    continue
                if not np.isfinite(sig0):
                    counters["reject_sigma"] += 1
                    continue

                if W == 0:
                    s_hold = float(s[i_t0])
                    z_hold = (s_hold - m0) / sig0
                else:
                    s_hold = tw_q25(ts, s, t0 - W, t0)
                    z_hold = tw_q25(ts, (s - m0) / sig0, t0 - W, t0)
                if not np.isfinite(s_hold) or not np.isfinite(z_hold):
                    counters["reject_cover"] += 1
                    continue
                if not (s_hold > 0):
                    counters["reject_s_hold"] += 1
                    continue

                counters["accepted"] += 1
                zb = z_bin_of(z_hold)
                s0 = float(s[i_t0])

                # vector fills for all L
                targets = t0 + L_arr
                js = np.searchsorted(ts, targets, side="left")
                for li, L in enumerate(L_MS):
                    j = int(js[li])
                    key = (side, int(W), zb, int(L), block, coin, day)
                    slot = agg[key]
                    if j >= ts.size:
                        slot["n_missing"] += 1
                        counters["fill_missing"] += 1
                        continue
                    delay = float(int(ts[j]) - t0)
                    if delay > L + GAP_SLACK_MS or hole_mask_intervals(coin_holes, t0, int(ts[j])):
                        slot["n_gap"] += 1
                        counters["fill_gap"] += 1
                        continue
                    delta = float(s[j]) - s0
                    u = delta / sig0
                    slot["n"] += 1
                    slot["sum_delta"] += delta
                    slot["sum_u"] += u
                    slot["k_c1"] += int(u <= -H1)
                    slot["k_e1"] += int(u >= H1)
                    slot["k_c2"] += int(u <= -H2)
                    slot["k_e2"] += int(u >= H2)
                    slot["k_ap"] += int(abs(u) < 1.0)
                    slot["sum_delay"] += delay
                    slot["n_delay"] += 1
                    _reservoir_add(slot, delta, u, rng)
                    counters["fill_ok"] += 1
                    if len(delay_samples) < 200_000 and (L in (10, 50, 100, 200, 300)):
                        delay_samples.append(
                            {"side": side, "W_ms": W, "z_bin": zb, "L_ms": L, "delay": delay, "day": day, "coin": coin}
                        )


def empty_slot():
    return defaultdict(
        lambda: {
            "n": 0,
            "sum_delta": 0.0,
            "sum_u": 0.0,
            "k_c1": 0,
            "k_e1": 0,
            "k_c2": 0,
            "k_e2": 0,
            "k_ap": 0,
            "sum_delay": 0.0,
            "n_delay": 0,
            "n_missing": 0,
            "n_gap": 0,
            "deltas": [],
            "us": [],
            "_cap": 1500,
        }
    )


def _reservoir_add(slot: dict, delta: float, u: float, rng: np.random.Generator) -> None:
    cap = slot["_cap"]
    n = slot["n"]  # already incremented
    if len(slot["deltas"]) < cap:
        slot["deltas"].append(delta)
        slot["us"].append(u)
    else:
        j = int(rng.integers(0, n))
        if j < cap:
            slot["deltas"][j] = delta
            slot["us"][j] = u


def collapse_agg(agg: dict) -> pd.DataFrame:
    """Collapse block/coin keys into summary + keep block table for bootstrap."""
    # block-level for bootstrap
    block_rows = []
    for (side, W, zb, L, block, coin, day), sl in agg.items():
        if sl["n"] == 0 and sl["n_missing"] == 0 and sl["n_gap"] == 0:
            continue
        block_rows.append(
            {
                "side": side,
                "W_ms": W,
                "z_bin": zb,
                "L_ms": L,
                "block_id": block,
                "base_coin": coin,
                "day": day,
                "n": sl["n"],
                "k_c1": sl["k_c1"],
                "k_e1": sl["k_e1"],
                "k_c2": sl["k_c2"],
                "k_e2": sl["k_e2"],
                "k_ap": sl["k_ap"],
                "sum_delta": sl["sum_delta"],
                "sum_u": sl["sum_u"],
                "sum_delay": sl["sum_delay"],
                "n_delay": sl["n_delay"],
                "n_missing": sl["n_missing"],
                "n_gap": sl["n_gap"],
                "deltas": sl["deltas"],
                "us": sl["us"],
            }
        )
    return pd.DataFrame(block_rows)


def build_outputs(block_df: pd.DataFrame, delay_samples: list, counters_df: pd.DataFrame) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    # explode deltas carefully — may be large; compute cell quantiles while grouping
    summary_rows = []
    delay_rows = []
    count_rows = []

    # For cell counts use L=100 slice of unique obs approx = sum n over blocks at L=100
    for (side, W, zb), g0 in block_df.groupby(["side", "W_ms", "z_bin"]):
        gL = g0[g0.L_ms == 100]
        n_obs = int(gL["n"].sum())
        n_blocks = int(gL["block_id"].nunique())
        n_coins = int(gL["base_coin"].nunique())
        count_rows.append(
            {
                "side": side,
                "W_ms": int(W),
                "z_bin": zb,
                "n_observations_L100": n_obs,
                "n_blocks": n_blocks,
                "n_coins": n_coins,
                "n_days": int(gL["day"].nunique()) if len(gL) else 0,
                "power_gate_pass": bool(n_blocks >= 50 and n_obs >= 500 and n_coins >= 5),
            }
        )

    for (side, W, zb, L), g in block_df.groupby(["side", "W_ms", "z_bin", "L_ms"]):
        n = int(g["n"].sum())
        if n == 0:
            # still record missing rates
            n_miss = int(g["n_missing"].sum())
            n_gap = int(g["n_gap"].sum())
            tot_att = n + n_miss + n_gap
            for h, hname, kc, ke in [
                (H1, "h1", "k_c1", "k_e1"),
                (H2, "h2", "k_c2", "k_e2"),
            ]:
                summary_rows.append(
                    {
                        "side": side,
                        "W_ms": int(W),
                        "z_bin": zb,
                        "L_ms": int(L),
                        "h": h,
                        "h_name": hname,
                        "n_observations": 0,
                        "n_blocks": int(g["block_id"].nunique()),
                        "n_coins": int(g["base_coin"].nunique()),
                        "median_delta_pp": np.nan,
                        "q10_delta_pp": np.nan,
                        "q25_delta_pp": np.nan,
                        "q75_delta_pp": np.nan,
                        "q90_delta_pp": np.nan,
                        "median_u": np.nan,
                        "p_collapse": np.nan,
                        "p_expand": np.nan,
                        "asymmetry": np.nan,
                        "p_approx": np.nan,
                        "fill_missing_rate": (n_miss / tot_att) if tot_att else np.nan,
                        "actual_fill_delay_p50_ms": np.nan,
                        "actual_fill_delay_p95_ms": np.nan,
                        "ci_low": np.nan,
                        "ci_high": np.nan,
                        "ci_note": "no valid fills",
                        "power_gate_pass": False,
                        "k_collapse": 0,
                        "k_expand": 0,
                    }
                )
            continue

        # gather samples (capped per block already as lists)
        deltas = []
        us = []
        for lst in g["deltas"]:
            if isinstance(lst, list):
                deltas.extend(lst)
        for lst in g["us"]:
            if isinstance(lst, list):
                us.extend(lst)
        # if lists were lost in parquet roundtrip, reconstruct from sums only
        darr = np.asarray(deltas, dtype=np.float64) if deltas else np.array([])
        uarr = np.asarray(us, dtype=np.float64) if us else np.array([])
        if uarr.size == 0:
            # fallback from counts only
            kc1, ke1 = int(g["k_c1"].sum()), int(g["k_e1"].sum())
            kc2, ke2 = int(g["k_c2"].sum()), int(g["k_e2"].sum())
            kap = int(g["k_ap"].sum())
            med_d = float(g["sum_delta"].sum() / n)
            med_u = float(g["sum_u"].sum() / n)
            qtiles = [np.nan] * 5
        else:
            kc1 = int(np.sum(uarr <= -H1))
            ke1 = int(np.sum(uarr >= H1))
            kc2 = int(np.sum(uarr <= -H2))
            ke2 = int(np.sum(uarr >= H2))
            kap = int(np.sum(np.abs(uarr) < 1))
            med_d = float(np.median(darr))
            med_u = float(np.median(uarr))
            qtiles = [float(np.percentile(darr, q)) for q in (10, 25, 50, 75, 90)]
            med_d = qtiles[2]

        n_miss = int(g["n_missing"].sum())
        n_gap = int(g["n_gap"].sum())
        tot_att = n + n_miss + n_gap
        n_blocks = int(g["block_id"].nunique())
        n_coins = int(g["base_coin"].nunique())
        gate = n_blocks >= 50 and n >= 500 and n_coins >= 5

        delays = g.loc[g.n_delay > 0]
        # approx delay from weighted mean; p50/p95 from samples if present
        if uarr.size and deltas:
            # delays not stored in lists — use mean
            delay_mean = float(g["sum_delay"].sum() / max(int(g["n_delay"].sum()), 1))
            d50 = delay_mean
            d95 = np.nan
        else:
            delay_mean = float(g["sum_delay"].sum() / max(int(g["n_delay"].sum()), 1))
            d50, d95 = delay_mean, np.nan

        for h, hname, kc, ke in [
            (H1, "h1", kc1, ke1),
            (H2, "h2", kc2, ke2),
        ]:
            pc, pe = kc / n, ke / n
            summary_rows.append(
                {
                    "side": side,
                    "W_ms": int(W),
                    "z_bin": zb,
                    "L_ms": int(L),
                    "h": h,
                    "h_name": hname,
                    "n_observations": n,
                    "n_blocks": n_blocks,
                    "n_coins": n_coins,
                    "median_delta_pp": med_d,
                    "q10_delta_pp": qtiles[0] if uarr.size else np.nan,
                    "q25_delta_pp": qtiles[1] if uarr.size else np.nan,
                    "q75_delta_pp": qtiles[3] if uarr.size else np.nan,
                    "q90_delta_pp": qtiles[4] if uarr.size else np.nan,
                    "median_u": med_u,
                    "p_collapse": pc,
                    "p_expand": pe,
                    "asymmetry": pc - pe,
                    "p_approx": kap / n,
                    "fill_missing_rate": (n_miss / tot_att) if tot_att else np.nan,
                    "actual_fill_delay_p50_ms": d50,
                    "actual_fill_delay_p95_ms": d95,
                    "ci_low": np.nan,
                    "ci_high": np.nan,
                    "ci_note": "",
                    "power_gate_pass": gate,
                    "k_collapse": kc,
                    "k_expand": ke,
                }
            )
        delay_rows.append(
            {
                "side": side,
                "W_ms": int(W),
                "z_bin": zb,
                "L_ms": int(L),
                "n": n,
                "delay_p50": d50,
                "delay_p95": d95,
                "delay_mean": delay_mean,
            }
        )

    # improve delay p50/p95 from samples
    if delay_samples:
        ds = pd.DataFrame(delay_samples)
        for i, r in enumerate(delay_rows):
            sub = ds[
                (ds.side == r["side"])
                & (ds.W_ms == r["W_ms"])
                & (ds.z_bin == r["z_bin"])
                & (ds.L_ms == r["L_ms"])
            ]
            if len(sub):
                r["delay_p50"] = float(sub["delay"].median())
                r["delay_p95"] = float(sub["delay"].quantile(0.95))
                # sync into summary
        for r in summary_rows:
            sub = ds[
                (ds.side == r["side"])
                & (ds.W_ms == r["W_ms"])
                & (ds.z_bin == r["z_bin"])
                & (ds.L_ms == r["L_ms"])
            ]
            if len(sub):
                r["actual_fill_delay_p50_ms"] = float(sub["delay"].median())
                r["actual_fill_delay_p95_ms"] = float(sub["delay"].quantile(0.95))

    summary = pd.DataFrame(summary_rows)
    counts = pd.DataFrame(count_rows)
    pd.DataFrame(delay_rows).to_csv(OUT / "actual_fill_delay.csv", index=False)
    counts.to_csv(OUT / "cell_counts.csv", index=False)

    # bootstrap A h=1 from block aggregates (no need for delta lists)
    rng = np.random.default_rng(BOOT_SEED)
    boot_rows = []
    # drop list cols for groupby speed
    bslim = block_df.drop(columns=["deltas", "us"], errors="ignore")
    for (side, W, zb, L), g in bslim.groupby(["side", "W_ms", "z_bin", "L_ms"]):
        # one row per block (sum over coins in block)
        bg = g.groupby("block_id", as_index=False).agg(
            n=("n", "sum"), k_c1=("k_c1", "sum"), k_e1=("k_e1", "sum")
        )
        blist = bg.to_dict("records")
        if not blist:
            continue
        As = []
        for _ in range(BOOT_N):
            draw = rng.choice(len(blist), size=len(blist), replace=True)
            n = kc = ke = 0
            for di in draw:
                n += blist[di]["n"]
                kc += blist[di]["k_c1"]
                ke += blist[di]["k_e1"]
            if n:
                As.append(kc / n - ke / n)
        mask = (
            (summary.side == side)
            & (summary.W_ms == W)
            & (summary.z_bin == zb)
            & (summary.L_ms == L)
            & (summary.h_name == "h1")
        )
        if not mask.any():
            continue
        pr = summary.loc[mask].iloc[0]
        note = ""
        if pr["k_collapse"] == 0 and pr["k_expand"] == 0:
            lo = hi = np.nan
            note = "boundary estimate; population CI not identified by empirical bootstrap"
        elif not As:
            lo = hi = np.nan
            note = "bootstrap empty"
        else:
            lo, hi = float(np.percentile(As, 2.5)), float(np.percentile(As, 97.5))
            if pr["k_collapse"] == 0 or pr["k_expand"] == 0:
                if lo == hi:
                    note = "boundary estimate; population CI not identified by empirical bootstrap"
        summary.loc[mask, "ci_low"] = lo
        summary.loc[mask, "ci_high"] = hi
        summary.loc[mask, "ci_note"] = note
        boot_rows.append(
            {
                "side": side,
                "W_ms": int(W),
                "z_bin": zb,
                "L_ms": int(L),
                "h": H1,
                "A_point": float(pr["asymmetry"]) if np.isfinite(pr["asymmetry"]) else np.nan,
                "A_ci_low": lo,
                "A_ci_high": hi,
                "n_blocks": int(pr["n_blocks"]),
                "n_obs": int(pr["n_observations"]),
                "k_collapse": int(pr["k_collapse"]),
                "k_expand": int(pr["k_expand"]),
                "note": note,
            }
        )

    summary.to_csv(OUT / "latency_summary.csv", index=False)
    pd.DataFrame(boot_rows).to_csv(OUT / "cluster_bootstrap.csv", index=False)

    # LOO day/coin at L=100 h1
    loo_day, loo_coin = [], []
    focus = bslim[bslim.L_ms == 100]
    for (side, W, zb), g in focus.groupby(["side", "W_ms", "z_bin"]):
        def A_of(sub):
            n = int(sub["n"].sum())
            if n == 0:
                return np.nan
            return float(sub["k_c1"].sum() / n - sub["k_e1"].sum() / n)

        base = A_of(g)
        for d in sorted(g["day"].unique()):
            sub = g[g.day != d]
            loo_day.append(
                {"side": side, "W_ms": int(W), "z_bin": zb, "L_ms": 100, "left_out_day": d, "A": A_of(sub), "A_base": base, "delta": A_of(sub) - base, "n": int(sub["n"].sum())}
            )
        for c in sorted(g["base_coin"].unique()):
            sub = g[g.base_coin != c]
            loo_coin.append(
                {"side": side, "W_ms": int(W), "z_bin": zb, "L_ms": 100, "left_out_coin": c, "A": A_of(sub), "A_base": base, "delta": A_of(sub) - base, "n": int(sub["n"].sum())}
            )
    pd.DataFrame(loo_day).to_csv(OUT / "loo_day.csv", index=False)
    pd.DataFrame(loo_coin).to_csv(OUT / "loo_coin.csv", index=False)
    counters_df.to_csv(OUT / "quality_counters.csv", index=False)

    return {"summary": summary, "counts": counts}


def classify(summary: pd.DataFrame, counts: pd.DataFrame) -> dict:
    s100 = summary[(summary.h_name == "h1") & (summary.L_ms == 100)]
    reasons = []

    def pooled_A(W, zb):
        sub = s100[(s100.W_ms == W) & (s100.z_bin == zb)]
        sub = sub[sub.n_observations > 0]
        if sub.empty:
            return np.nan, 0, False
        w = sub["n_observations"].to_numpy(dtype=float)
        A = float(np.average(sub["asymmetry"], weights=w))
        gate = bool((sub["power_gate_pass"]).any())
        return A, int(w.sum()), gate

    high = ["z_2_4", "z_gt_4"]
    A = {W: [pooled_A(W, zb) for zb in high] for W in W_MS}

    def any_pos(lst, thr=0.05):
        return any(np.isfinite(a) and a > thr for a, _, _ in lst)

    def all_near0(lst, thr=0.05):
        return all((not np.isfinite(a)) or abs(a) <= thr for a, _, _ in lst)

    delays = pd.read_csv(OUT / "actual_fill_delay.csv")
    d10 = delays.loc[delays.L_ms == 10, "delay_p50"]
    cadence = bool(len(d10) and np.nanmedian(d10) > 30)
    if cadence:
        reasons.append(f"fill delay p50 at L=10 ≈ {float(np.nanmedian(d10)):.1f} ms")

    gt4 = counts[counts.z_bin == "z_gt_4"]
    rare_gt4 = bool(gt4.empty or gt4["n_observations_L100"].max() < 50)
    if rare_gt4:
        reasons.append("z_gt_4 rare under random sampling; no post-hoc oversample")

    any_gate = bool(counts["power_gate_pass"].any()) if len(counts) else False

    verdict = "underpowered"
    if any_pos(A[0] + A[20]) and all_near0(A[50] + A[100]):
        verdict = "noise-compatible"
        reasons.append("collapse asymmetry mainly at W=0/20; absent at W=50/100")
    elif any_pos(A[50] + A[100]):
        A_low = pooled_A(100, "z_le_1")[0]
        A_hi = pooled_A(100, "z_gt_4")[0]
        if np.isfinite(A_hi) and np.isfinite(A_low) and A_hi > A_low + 0.02:
            reasons.append("A persists at W=50/100 and increases with z")
        else:
            reasons.append("A persists at W=50/100")
        verdict = "persistent degradation candidate"
        if rare_gt4:
            verdict = "underpowered"
        if cadence:
            verdict = "cadence artifact"
            reasons.append("pattern may follow fill cadence")
    elif all_near0(A[0] + A[20] + A[50] + A[100]):
        verdict = "no directional effect"
        reasons.append("A≈0 across W and z at L=100")

    if not any_gate and verdict == "persistent degradation candidate":
        reasons.append("no cell passes change-point power gate")
        # keep candidate label but note underpowered for CP

    return {
        "verdict": verdict,
        "reasons": reasons,
        "change_point": "underpowered for change-point"
        if not any_gate
        else "power gate pass in some cells; CP not estimated (protocol)",
        "seed": BOOT_SEED,
        "frozen_days": pd.read_csv(OUT / "frozen_days.csv")["day"].tolist(),
        "disclaimer": "Random-market baseline only; not competitor latency; not entry handoff.",
    }


def run_all():
    OUT.mkdir(parents=True, exist_ok=True)
    days = pd.read_csv(OUT / "frozen_days.csv")["day"].tolist()
    anchors = pd.read_csv(OUT / "frozen_anchors.csv")
    print("frozen days", days, "n_anchors", len(anchors), flush=True)

    agg = empty_slot()
    delay_samples: list = []
    qrows = []
    rng = np.random.default_rng(BOOT_SEED)

    for day in days:
        holes = day_holes(day)
        ams = anchors.loc[anchors.day == day, "anchor_ts_ms"].to_numpy(dtype=np.int64)
        print(f"DAY {day} anchors={len(ams)} file_holes={len(holes)}", flush=True)
        for coin in COINS:
            ctr = defaultdict(int)
            print(f"  {coin}...", flush=True)
            process_coin_day(coin, day, ams, holes, agg, delay_samples, ctr, rng)
            qrows.append({"day": day, "base_coin": coin, **dict(ctr)})
            print(f"    accepted={ctr['accepted']} s_hold_rej={ctr['reject_s_hold']} hole={ctr['reject_hole']}", flush=True)

    print("collapse agg...", flush=True)
    block_df = collapse_agg(agg)
    # drop heavy lists to parquet companion? keep in memory for quantiles
    print("block rows", len(block_df), flush=True)
    # save block stats without lists
    block_df.drop(columns=["deltas", "us"], errors="ignore").to_parquet(OUT / "block_stats.parquet", index=False)

    meta = build_outputs(block_df, delay_samples, pd.DataFrame(qrows))
    verd = classify(meta["summary"], meta["counts"])
    (OUT / "verdict.json").write_text(json.dumps(verd, indent=2), encoding="utf-8")
    print("VERDICT", verd, flush=True)
    return verd


if __name__ == "__main__":
    run_all()
