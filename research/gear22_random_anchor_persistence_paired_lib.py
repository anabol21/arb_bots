"""Gear 2.2 scratch: paired state_at_L vs fill_contract_L on frozen random anchors.

Reuses frozen_days/frozen_anchors from gear22_random_anchor_persistence.
No new sampling, no z>4 oversample, no canon changes.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.gear22_random_anchor_persistence_lib import (
    BLOCK_MS,
    BOOT_N,
    BOOT_SEED,
    COINS,
    GAP_SLACK_MS,
    H1,
    INTRA_GAP_MS,
    L_MS,
    LOOKBACK_MS,
    SIDES,
    W_MS,
    day_holes,
    floor_stats,
    hole_mask_intervals,
    tw_q25,
    z_bin_of,
)
from research.lean_ticks_io import gear2_lean_columns, parse_ts_ms, read_and_prepare_lean_ticks

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "research" / "output" / "gear22_random_anchor_persistence"
OUT = REPO / "research" / "output" / "gear22_random_anchor_persistence_paired"
LEAN = REPO / "output" / "lean_ticks"

# Report emphasis
REPORT_L = (50, 100, 200)


def empty_slot():
    return defaultdict(
        lambda: {
            "n": 0,
            "k_c_state": 0,
            "k_e_state": 0,
            "k_c_fill": 0,
            "k_e_fill": 0,
            "k_c_cad": 0,  # cadence u <= -1
            "k_e_cad": 0,
            "sum_u_state": 0.0,
            "sum_u_fill": 0.0,
            "sum_u_cad": 0.0,
            "n_fill_missing": 0,
            "n_fill_gap": 0,
            "n_state_missing": 0,
            "sum_delay": 0.0,
            "n_delay": 0,
        }
    )


def process_coin_day(
    coin: str,
    day: str,
    anchors_ms: np.ndarray,
    holes: list,
    agg: dict,
    counters: dict,
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
                if W == 0:
                    flo_lo, flo_hi = t0 - LOOKBACK_MS, t0
                else:
                    flo_lo, flo_hi = t0 - W - LOOKBACK_MS, t0 - W
                need1 = t0 + int(L_arr[-1]) + int(GAP_SLACK_MS)
                if hole_mask_intervals(coin_holes, flo_lo, need1):
                    counters["reject_hole"] += 1
                    continue

                i_t0 = int(np.searchsorted(ts, t0, side="right")) - 1
                if i_t0 < 0:
                    counters["reject_cover"] += 1
                    continue

                m0, sig0, n_past = floor_stats(ts, s, flo_lo, flo_hi)
                if n_past < 30 or not np.isfinite(sig0):
                    counters["reject_floor"] += 1
                    continue

                if W == 0:
                    s_hold = float(s[i_t0])
                    z_hold = (s_hold - m0) / sig0
                else:
                    s_hold = tw_q25(ts, s, t0 - W, t0)
                    z_hold = tw_q25(ts, (s - m0) / sig0, t0 - W, t0)
                if not np.isfinite(s_hold) or not np.isfinite(z_hold) or not (s_hold > 0):
                    counters["reject_s_hold"] += 1
                    continue

                counters["accepted"] += 1
                zb = z_bin_of(float(z_hold))
                s0 = float(s[i_t0])

                # batch indices
                targets = t0 + L_arr
                j_fill = np.searchsorted(ts, targets, side="left")
                j_state = np.searchsorted(ts, targets, side="right") - 1

                for li, L in enumerate(L_MS):
                    key = (side, int(W), zb, int(L), block, coin, day)
                    slot = agg[key]

                    # state_at_L: last ts <= t0+L, must be >= i_t0 conceptually
                    js = int(j_state[li])
                    u_state = None
                    if js < i_t0 or js < 0 or int(ts[js]) > t0 + L:
                        slot["n_state_missing"] += 1
                    elif hole_mask_intervals(coin_holes, t0, int(ts[js]) + 1):
                        slot["n_state_missing"] += 1
                    else:
                        u_state = (float(s[js]) - s0) / sig0

                    # fill_contract
                    jf = int(j_fill[li])
                    u_fill = None
                    delay = None
                    if jf >= ts.size:
                        slot["n_fill_missing"] += 1
                    else:
                        delay = float(int(ts[jf]) - t0)
                        if delay > L + GAP_SLACK_MS or hole_mask_intervals(coin_holes, t0, int(ts[jf])):
                            slot["n_fill_gap"] += 1
                        else:
                            u_fill = (float(s[jf]) - s0) / sig0
                            slot["sum_delay"] += delay
                            slot["n_delay"] += 1

                    # paired only when both valid
                    if u_state is None or u_fill is None:
                        continue

                    u_cad = u_fill - u_state
                    slot["n"] += 1
                    slot["sum_u_state"] += u_state
                    slot["sum_u_fill"] += u_fill
                    slot["sum_u_cad"] += u_cad
                    slot["k_c_state"] += int(u_state <= -H1)
                    slot["k_e_state"] += int(u_state >= H1)
                    slot["k_c_fill"] += int(u_fill <= -H1)
                    slot["k_e_fill"] += int(u_fill >= H1)
                    # cadence asymmetry: material negative cadence = fill more collapsed than state
                    slot["k_c_cad"] += int(u_cad <= -H1)
                    slot["k_e_cad"] += int(u_cad >= H1)


def collapse(agg: dict) -> pd.DataFrame:
    rows = []
    for (side, W, zb, L, block, coin, day), sl in agg.items():
        if sl["n"] == 0 and sl["n_fill_missing"] == 0 and sl["n_state_missing"] == 0:
            continue
        rows.append(
            {
                "side": side,
                "W_ms": W,
                "z_bin": zb,
                "L_ms": L,
                "block_id": block,
                "base_coin": coin,
                "day": day,
                **{k: sl[k] for k in sl if k != "_cap"},
            }
        )
    return pd.DataFrame(rows)


def summarize(block_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = []
    for (side, W, zb, L), g in block_df.groupby(["side", "W_ms", "z_bin", "L_ms"]):
        n = int(g["n"].sum())
        n_blocks = int(g["block_id"].nunique())
        n_coins = int(g["base_coin"].nunique())
        gate = n_blocks >= 50 and n >= 500 and n_coins >= 5

        def rates(kc, ke):
            if n == 0:
                return np.nan, np.nan, np.nan
            pc, pe = kc / n, ke / n
            return pc, pe, pc - pe

        pcs, pes, As = rates(int(g["k_c_state"].sum()), int(g["k_e_state"].sum()))
        pcf, pef, Af = rates(int(g["k_c_fill"].sum()), int(g["k_e_fill"].sum()))
        pcc, pec, Ac = rates(int(g["k_c_cad"].sum()), int(g["k_e_cad"].sum()))

        rows.append(
            {
                "side": side,
                "W_ms": int(W),
                "z_bin": zb,
                "L_ms": int(L),
                "n_paired": n,
                "n_blocks": n_blocks,
                "n_coins": n_coins,
                "power_gate_pass": gate,
                "mean_u_state": float(g["sum_u_state"].sum() / n) if n else np.nan,
                "mean_u_fill": float(g["sum_u_fill"].sum() / n) if n else np.nan,
                "mean_u_cadence": float(g["sum_u_cad"].sum() / n) if n else np.nan,
                "p_collapse_state": pcs,
                "p_expand_state": pes,
                "A_state": As,
                "p_collapse_fill": pcf,
                "p_expand_fill": pef,
                "A_fill": Af,
                "p_collapse_cadence": pcc,
                "p_expand_cadence": pec,
                "A_cadence": Ac,
                "A_fill_minus_state": (Af - As) if np.isfinite(Af) and np.isfinite(As) else np.nan,
                "fill_missing_rate": float(g["n_fill_missing"].sum() / max(g["n"].sum() + g["n_fill_missing"].sum() + g["n_fill_gap"].sum(), 1)),
                "delay_mean": float(g["sum_delay"].sum() / max(int(g["n_delay"].sum()), 1)),
                "ci_A_state_low": np.nan,
                "ci_A_state_high": np.nan,
                "ci_A_fill_low": np.nan,
                "ci_A_fill_high": np.nan,
                "ci_A_cad_low": np.nan,
                "ci_A_cad_high": np.nan,
                "ci_note": "",
            }
        )
    summary = pd.DataFrame(rows)

    # bootstrap on blocks for A_state, A_fill, A_cadence
    rng = np.random.default_rng(BOOT_SEED + 7)
    boot_rows = []
    for (side, W, zb, L), g in block_df.groupby(["side", "W_ms", "z_bin", "L_ms"]):
        bg = g.groupby("block_id", as_index=False).agg(
            n=("n", "sum"),
            kc_s=("k_c_state", "sum"),
            ke_s=("k_e_state", "sum"),
            kc_f=("k_c_fill", "sum"),
            ke_f=("k_e_fill", "sum"),
            kc_c=("k_c_cad", "sum"),
            ke_c=("k_e_cad", "sum"),
        )
        recs = bg.to_dict("records")
        if not recs:
            continue

        def boot_A(kc_key, ke_key):
            As = []
            for _ in range(BOOT_N):
                draw = rng.choice(len(recs), size=len(recs), replace=True)
                n = kc = ke = 0
                for di in draw:
                    n += recs[di]["n"]
                    kc += recs[di][kc_key]
                    ke += recs[di][ke_key]
                if n:
                    As.append(kc / n - ke / n)
            if not As:
                return np.nan, np.nan, "bootstrap empty"
            lo, hi = float(np.percentile(As, 2.5)), float(np.percentile(As, 97.5))
            note = ""
            # boundary check from point
            return lo, hi, note

        lo_s, hi_s, _ = boot_A("kc_s", "ke_s")
        lo_f, hi_f, _ = boot_A("kc_f", "ke_f")
        lo_c, hi_c, note = boot_A("kc_c", "ke_c")

        mask = (
            (summary.side == side)
            & (summary.W_ms == W)
            & (summary.z_bin == zb)
            & (summary.L_ms == L)
        )
        if not mask.any():
            continue
        pr = summary.loc[mask].iloc[0]
        if pr["n_paired"] == 0:
            note = "no paired obs"
        elif pr["p_collapse_fill"] in (0.0, 1.0) and pr["p_expand_fill"] in (0.0, 1.0):
            note = "boundary estimate; population CI not identified by empirical bootstrap"

        summary.loc[mask, "ci_A_state_low"] = lo_s
        summary.loc[mask, "ci_A_state_high"] = hi_s
        summary.loc[mask, "ci_A_fill_low"] = lo_f
        summary.loc[mask, "ci_A_fill_high"] = hi_f
        summary.loc[mask, "ci_A_cad_low"] = lo_c
        summary.loc[mask, "ci_A_cad_high"] = hi_c
        summary.loc[mask, "ci_note"] = note

        boot_rows.append(
            {
                "side": side,
                "W_ms": int(W),
                "z_bin": zb,
                "L_ms": int(L),
                "A_state": float(pr["A_state"]) if np.isfinite(pr["A_state"]) else np.nan,
                "A_fill": float(pr["A_fill"]) if np.isfinite(pr["A_fill"]) else np.nan,
                "A_cadence": float(pr["A_cadence"]) if np.isfinite(pr["A_cadence"]) else np.nan,
                "ci_A_state": [lo_s, hi_s],
                "ci_A_fill": [lo_f, hi_f],
                "ci_A_cadence": [lo_c, hi_c],
                "n_paired": int(pr["n_paired"]),
                "n_blocks": int(pr["n_blocks"]),
                "note": note,
            }
        )

    return summary, pd.DataFrame(boot_rows)


def loo(block_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    focus = block_df[block_df.L_ms == 100]
    loo_day, loo_coin = [], []

    def A_triple(sub):
        n = int(sub["n"].sum())
        if n == 0:
            return np.nan, np.nan, np.nan
        As = sub["k_c_state"].sum() / n - sub["k_e_state"].sum() / n
        Af = sub["k_c_fill"].sum() / n - sub["k_e_fill"].sum() / n
        Ac = sub["k_c_cad"].sum() / n - sub["k_e_cad"].sum() / n
        return float(As), float(Af), float(Ac)

    for (side, W, zb), g in focus.groupby(["side", "W_ms", "z_bin"]):
        base = A_triple(g)
        for d in sorted(g["day"].unique()):
            As, Af, Ac = A_triple(g[g.day != d])
            loo_day.append(
                {
                    "side": side,
                    "W_ms": int(W),
                    "z_bin": zb,
                    "L_ms": 100,
                    "left_out_day": d,
                    "A_state": As,
                    "A_fill": Af,
                    "A_cadence": Ac,
                    "d_state": As - base[0],
                    "d_fill": Af - base[1],
                    "d_cadence": Ac - base[2],
                    "n": int(g[g.day != d]["n"].sum()),
                }
            )
        for c in sorted(g["base_coin"].unique()):
            As, Af, Ac = A_triple(g[g.base_coin != c])
            loo_coin.append(
                {
                    "side": side,
                    "W_ms": int(W),
                    "z_bin": zb,
                    "L_ms": 100,
                    "left_out_coin": c,
                    "A_state": As,
                    "A_fill": Af,
                    "A_cadence": Ac,
                    "d_state": As - base[0],
                    "d_fill": Af - base[1],
                    "d_cadence": Ac - base[2],
                    "n": int(g[g.base_coin != c]["n"].sum()),
                }
            )
    return pd.DataFrame(loo_day), pd.DataFrame(loo_coin)


def classify(summary: pd.DataFrame) -> dict[str, Any]:
    """Pre-registered paired interpretation at L=100, high-z, W in {50,100}."""
    s = summary[(summary.L_ms == 100) & (summary.z_bin.isin(["z_2_4", "z_gt_4"]))]
    reasons = []

    def pool(metric, W=None):
        sub = s if W is None else s[s.W_ms == W]
        sub = sub[sub.n_paired > 0]
        if sub.empty:
            return np.nan
        w = sub["n_paired"].to_numpy(float)
        return float(np.average(sub[metric], weights=w))

    # Across W=0..100 for high z at L=100
    A_s = pool("A_state")
    A_f = pool("A_fill")
    A_c = pool("A_cadence")
    A_s50 = pool("A_state", 50)
    A_f50 = pool("A_fill", 50)
    A_s100 = pool("A_state", 100)
    A_f100 = pool("A_fill", 100)

    thr = 0.03
    state_pos = np.isfinite(A_s) and A_s > thr
    fill_pos = np.isfinite(A_f) and A_f > thr
    state_persist = (np.isfinite(A_s50) and A_s50 > thr) or (np.isfinite(A_s100) and A_s100 > thr)
    fill_only = fill_pos and (not np.isfinite(A_s) or A_s <= thr)
    both_fill_stronger = state_pos and fill_pos and np.isfinite(A_f) and np.isfinite(A_s) and (A_f > A_s + 0.01)

    # Also check report L curves: mean |A_cadence| vs A_state
    if fill_only:
        verdict = "fill-wait effect (cadence-dominated)"
        reasons.append("A_fill>0 while A_state≈0 at high-z L=100")
    elif both_fill_stronger:
        verdict = "market dynamics + cadence amplification"
        reasons.append(f"A_state={A_s:.3f}>0 and A_fill={A_f:.3f}>A_state; A_cadence={A_c:.3f}")
    elif state_pos and state_persist:
        verdict = "observed L1 dynamics (state_at_L)"
        reasons.append(f"A_state={A_s:.3f} persists at W=50/100; fill does not uniquely create asymmetry")
    elif not state_pos and not fill_pos:
        verdict = "no directional paired effect"
        reasons.append("A_state and A_fill both near 0 on high-z")
    else:
        verdict = "mixed / underpowered paired pattern"
        reasons.append(f"A_state={A_s}, A_fill={A_f}, A_cadence={A_c}")

    # z_gt_4 power
    gt4 = summary[(summary.z_bin == "z_gt_4") & (summary.L_ms == 100)]
    if not gt4.empty and gt4["n_paired"].sum() < 500:
        reasons.append("z_gt_4 still underpowered; no oversample")

    reasons.append(
        "High persistent z linked to more frequent subsequent collapse; "
        "collapse timing under fill-contract still not identified."
    )

    return {
        "verdict": verdict,
        "A_state_highz_L100": A_s,
        "A_fill_highz_L100": A_f,
        "A_cadence_highz_L100": A_c,
        "A_state_W50": A_s50,
        "A_fill_W50": A_f50,
        "A_state_W100": A_s100,
        "A_fill_W100": A_f100,
        "reasons": reasons,
        "frozen_source": str(SRC),
        "disclaimer": (
            "Paired state vs fill on frozen random anchors only; "
            "not competitor latency; not entry-rule handoff."
        ),
    }


def run_all():
    OUT.mkdir(parents=True, exist_ok=True)
    days = pd.read_csv(SRC / "frozen_days.csv")["day"].tolist()
    anchors = pd.read_csv(SRC / "frozen_anchors.csv")
    # copy freeze pointers
    pd.read_csv(SRC / "frozen_days.csv").to_csv(OUT / "frozen_days.csv", index=False)
    anchors.to_csv(OUT / "frozen_anchors.csv", index=False)
    print("reuse frozen days", days, "anchors", len(anchors), flush=True)

    agg = empty_slot()
    qrows = []
    for day in days:
        holes = day_holes(day)
        ams = anchors.loc[anchors.day == day, "anchor_ts_ms"].to_numpy(dtype=np.int64)
        print(f"DAY {day}", flush=True)
        for coin in COINS:
            ctr = defaultdict(int)
            print(f"  {coin}...", flush=True)
            process_coin_day(coin, day, ams, holes, agg, ctr)
            qrows.append({"day": day, "base_coin": coin, **dict(ctr)})
            print(f"    accepted={ctr['accepted']} s_hold_rej={ctr['reject_s_hold']}", flush=True)

    block_df = collapse(agg)
    block_df.to_parquet(OUT / "block_stats_paired.parquet", index=False)
    print("block rows", len(block_df), flush=True)

    summary, boot = summarize(block_df)
    summary.to_csv(OUT / "paired_summary.csv", index=False)
    boot.to_csv(OUT / "paired_bootstrap.csv", index=False)
    loo_day, loo_coin = loo(block_df)
    loo_day.to_csv(OUT / "loo_day.csv", index=False)
    loo_coin.to_csv(OUT / "loo_coin.csv", index=False)
    pd.DataFrame(qrows).to_csv(OUT / "quality_counters.csv", index=False)

    # report slice
    report = summary[summary.L_ms.isin(REPORT_L)].copy()
    report.to_csv(OUT / "paired_report_L.csv", index=False)

    verd = classify(summary)
    (OUT / "verdict.json").write_text(json.dumps(verd, indent=2), encoding="utf-8")
    print("VERDICT", verd, flush=True)
    return verd


if __name__ == "__main__":
    run_all()
