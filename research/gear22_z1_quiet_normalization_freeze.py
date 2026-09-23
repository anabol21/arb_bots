"""Gear 2.2 experiment Z1 - manifest freeze under the unknown-time union rule.

Stage 1b. Consumes ``manifest_audit.csv`` and the per-window tick cache written
by ``gear22_z1_quiet_normalization_lib``; emits the frozen manifest that stage 2
reads. Read-only with respect to ``output/lean_ticks``. No PnL anywhere.

The binding contamination rule is the union of the intervals in which the L1
state is unknown, not a per-gap threshold: a long inter-tick gap with all
5-minute files present is either a per-coin feed outage or simply an unchanged
L1, and the latter is normal inside a quiet regime.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from research.gear22_z1_quiet_normalization_lib import (
    CACHE,
    FILE_INTERVAL_MS,
    HALF_MS,
    LEAN,
    LIQUIDITY_CLASS,
    OUT_V2,
)
from research.lean_ticks_io import list_lean_files_overlapping, parse_lean_file_window

# --- frozen contract (identical to the v1 pilot) ----------------------------
UNKNOWN_GAP_MS = 60_000  # dwell longer than this = unknown L1 state
CONTAM_WINDOW = 0.05  # unknown-time union over the whole window
CONTAM_HALF = 0.10  # unknown-time union inside either half
MIN_TICKS_PER_HALF = 2_000

MANIFEST_VERSION = "v2"

FROZEN_COLS = [
    "quiet_id",
    "quiet_regime_id",
    "calendar_cluster_id",
    "base_coin",
    "liquidity_class_pre",
    "window_start_ts_utc",
    "window_end_ts_utc",
    "start_ms",
    "end_ms",
    "quiet_visual_verified",
    "data_valid",
    "contaminated",
    "primary",
    "n_ticks",
    "n_ticks_cal",
    "n_ticks_eval",
    "n_miss",
    "unk_frac",
    "unk_cal",
    "unk_ev",
    "max_unk_min",
    "tick_okx",
    "tick_bybit",
    "eff_res_long_pct",
    "eff_res_short_pct",
    "selection_notes",
    "rejection_reason",
]


def missing_file_intervals(start: int, end: int) -> list[tuple[int, int]]:
    """5-minute slots with no lean file: the L1 state there is unrecorded."""
    have = set()
    for p in list_lean_files_overlapping(LEAN, start, end):
        w = parse_lean_file_window(p)
        if w:
            have.add(w[0])
    return [
        (t, t + FILE_INTERVAL_MS)
        for t in range(start, end, FILE_INTERVAL_MS)
        if t not in have
    ]


def dwell_intervals(ts: np.ndarray, start: int, end: int) -> list[tuple[int, int]]:
    """Inter-tick stretches longer than the unknown-state horizon.

    The leading stretch ``[start, t_first)`` and the trailing ``[t_last, end)``
    are treated the same way: no tick carries the L1 state there either.
    """
    out: list[tuple[int, int]] = []
    if ts.size == 0:
        return [(start, end)]
    if ts[0] - start > UNKNOWN_GAP_MS:
        out.append((start, int(ts[0])))
    d = np.diff(ts)
    for i in np.flatnonzero(d > UNKNOWN_GAP_MS):
        out.append((int(ts[i]), int(ts[i + 1])))
    if end - ts[-1] > UNKNOWN_GAP_MS:
        out.append((int(ts[-1]), end))
    return out


def merge_intervals(
    iv: list[tuple[int, int]], start: int, end: int
) -> list[tuple[int, int]]:
    clipped = sorted((max(a, start), min(b, end)) for a, b in iv)
    out: list[list[int]] = []
    for a, b in clipped:
        if b <= a:
            continue
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def unknown_time_row(row: pd.Series) -> dict:
    start, end = int(row["start_ms"]), int(row["end_ms"])
    mid = start + HALF_MS
    path = CACHE / f"{row['quiet_id']}.parquet"
    if path.is_file():
        ts = np.sort(pd.read_parquet(path, columns=["ts"])["ts"].to_numpy(np.int64))
    else:
        ts = np.empty(0, dtype=np.int64)
    miss = missing_file_intervals(start, end)
    iv = merge_intervals(dwell_intervals(ts, start, end) + miss, start, end)
    tot = sum(b - a for a, b in iv)
    cal = sum(max(0, min(b, mid) - max(a, start)) for a, b in iv)
    ev = sum(max(0, min(b, end) - max(a, mid)) for a, b in iv)
    return {
        "quiet_id": row["quiet_id"],
        "coin": row["base_coin"],
        "liq": row["liquidity_class_pre"],
        "n_miss": len(miss),
        "n_unk_iv": len(iv),
        "unk_frac": tot / (end - start),
        "unk_cal": cal / HALF_MS,
        "unk_ev": ev / HALF_MS,
        "max_unk_min": max((b - a for a, b in iv), default=0) / 60_000.0,
        "n_cal": int(row["n_ticks_cal"]),
        "n_ev": int(row["n_ticks_eval"]),
    }


def freeze(out_dir: Path = OUT_V2) -> pd.DataFrame:
    audit = pd.read_csv(out_dir / "manifest_audit.csv")
    unk = pd.DataFrame([unknown_time_row(r) for _, r in audit.iterrows()])

    reasons, contam = [], []
    for _, u in unk.iterrows():
        rs = []
        half = max(u["unk_cal"], u["unk_ev"])
        if u["unk_frac"] > CONTAM_WINDOW:
            rs.append(f"unknown_frac={u['unk_frac']:.3f}")
        if half > CONTAM_HALF:
            rs.append(f"unknown_half={half:.3f}")
        if min(u["n_cal"], u["n_ev"]) < MIN_TICKS_PER_HALF:
            rs.append(f"thin_half cal={u['n_cal']} eval={u['n_ev']}")
        reasons.append(";".join(rs))
        contam.append(bool(rs))
    unk["contam_v2"] = contam
    unk.to_csv(out_dir / "unknown_time_audit.csv", index=False)

    m = audit.merge(
        unk[["quiet_id", "n_miss", "unk_frac", "unk_cal", "unk_ev", "max_unk_min"]],
        on="quiet_id",
    )
    m["contaminated"] = contam
    m["rejection_reason"] = reasons
    m["data_valid"] = (
        m[["n_ticks_cal", "n_ticks_eval"]].min(axis=1) >= MIN_TICKS_PER_HALF
    )
    m["primary"] = m["data_valid"] & ~m["contaminated"]
    frozen = m[FROZEN_COLS].sort_values("quiet_id").reset_index(drop=True)
    frozen.to_csv(out_dir / "manifest_frozen.csv", index=False)

    rejected = {
        r["quiet_id"]: r["rejection_reason"]
        for _, r in frozen[~frozen["primary"]].iterrows()
    }
    prim = frozen[frozen["primary"]]
    clusters = (
        prim.groupby("calendar_cluster_id")["quiet_id"].count().sort_index().to_dict()
    )
    meta = {
        "manifest_version": MANIFEST_VERSION,
        "contract": {
            "UNKNOWN_GAP_MS": UNKNOWN_GAP_MS,
            "CONTAM_WINDOW": CONTAM_WINDOW,
            "CONTAM_HALF": CONTAM_HALF,
            "MIN_TICKS_PER_HALF": MIN_TICKS_PER_HALF,
            "unknown_time_definition": (
                "union of inter-tick dwells > 60 s (including the leading and "
                "trailing stretch of the window) with the 5-minute slots that "
                "have no lean file"
            ),
        },
        "liquidity_class_pre": LIQUIDITY_CLASS,
        "n_declared": int(len(frozen)),
        "n_primary": int(prim.shape[0]),
        "rejected": rejected,
        "windows_per_coin": prim.groupby("base_coin")["quiet_id"]
        .count()
        .sort_index()
        .to_dict(),
        "windows_per_coin_declared": frozen.groupby("base_coin")["quiet_id"]
        .count()
        .sort_index()
        .to_dict(),
        "n_calendar_clusters_primary": int(prim["calendar_cluster_id"].nunique()),
        "calendar_cluster_sizes_primary": clusters,
        "n_quiet_regimes_primary": int(prim["quiet_regime_id"].nunique()),
    }
    (out_dir / "manifest_frozen_meta.json").write_text(json.dumps(meta, indent=2))
    return frozen


if __name__ == "__main__":
    f = freeze()
    cols = [
        "quiet_id",
        "liquidity_class_pre",
        "n_ticks_cal",
        "n_ticks_eval",
        "n_miss",
        "unk_frac",
        "unk_cal",
        "unk_ev",
        "primary",
        "rejection_reason",
    ]
    with pd.option_context("display.width", 220, "display.max_columns", 50):
        print(f[cols].to_string(index=False))
    print(json.dumps(json.loads((OUT_V2 / "manifest_frozen_meta.json").read_text()), indent=2))
