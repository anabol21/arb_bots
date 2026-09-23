"""Gear 2.2 research: latency edge survival on clean anomaly episodes.

Scratch only. Does not retune VARIATION/HYPER/Trade_Lat/fee_rate or touch
model_gear2 / gear2_backtest. L in {50,100,200} are diagnostic horizons.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np
import pandas as pd

from research.gap_fill import DEFAULT_GAP_FILL_SLACK_MS
from research.lean_ticks_io import (
    gear2_lean_columns,
    parse_ts_ms,
    read_and_prepare_lean_ticks,
)

REPO = Path(__file__).resolve().parents[1]
LEAN_TICKS = REPO / "output" / "lean_ticks"
OUT_DIR = REPO / "research" / "output" / "gear22_latency_edge_survival"
MANIFEST_SRC = OUT_DIR / "manifest_user.csv"

LOOKBACK_MS = 300_000
MIN_PAST = 30
SIGMA_MIN = 0.02
QUIET_MAD_SCALE = 1.4826
GAP_SLACK_MS = DEFAULT_GAP_FILL_SLACK_MS
STALE_LEG_MS = 200
INTRA_GAP_BREAK_MS = 360_000
DWELL_MS = 1_000
Z_PRIMARY = 4.0
Z_SECONDARY = 2.0
HORIZON_MS = 60_000  # exact-event search cap after onset (censor beyond)
REPORT_L = (50, 100, 200)
FEE_RATE = 0.00075  # frozen diagnostic copy of gear2 fee_rate; not a retune
C_FEE = 4.0 * 100.0 * FEE_RATE  # 0.3 percentage points
E_SIGNAL_BINS = [(0.0, 0.10), (0.10, 0.25), (0.25, 0.50), (0.50, math.inf)]
E_SIGNAL_BIN_LABELS = ["(0,0.10]", "(0.10,0.25]", "(0.25,0.50]", ">0.50"]
NEAR_ZERO_E = 0.02  # pp; Q_L denom guard
BOOT_N = 400
BOOT_SEED = 22
CANDIDATE_CP_MS = (40, 110)  # annotation only
CHANGEPOINT_MIN_EP = 50
CHANGEPOINT_MIN_PER_FOLD = 10


@dataclass
class EpisodeRow:
    episode_id: str
    shock_cluster_id: str
    base_coin: str
    direction: Optional[str]
    episode_class: Optional[str]
    window_start_ts_utc: str
    onset_search_start_ts_utc: Optional[str]
    window_end_ts_utc: str
    anomaly_start_ts_utc: Optional[str]
    prequiet_verified: bool
    contaminated: bool
    notes: str = ""

    @property
    def search_start_ms(self) -> int:
        if self.onset_search_start_ts_utc and str(self.onset_search_start_ts_utc).strip():
            return parse_ts_ms(self.onset_search_start_ts_utc)
        return parse_ts_ms(self.window_start_ts_utc)

    @property
    def window_start_ms(self) -> int:
        return parse_ts_ms(self.window_start_ts_utc)

    @property
    def window_end_ms(self) -> int:
        return parse_ts_ms(self.window_end_ts_utc)

    @property
    def anomaly_start_ms(self) -> Optional[int]:
        if self.anomaly_start_ts_utc and str(self.anomaly_start_ts_utc).strip():
            return parse_ts_ms(self.anomaly_start_ts_utc)
        return None


def _ms_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_bool(x) -> bool:
    if isinstance(x, bool):
        return x
    s = str(x).strip().lower()
    return s in {"1", "true", "t", "yes", "y"}


def load_manifest(path: Path = MANIFEST_SRC) -> list[EpisodeRow]:
    df = pd.read_csv(path)
    rows = []
    for _, r in df.iterrows():
        ec = r.get("episode_class")
        ec_s = None if pd.isna(ec) or str(ec).strip() == "" else str(ec).strip()
        oss = r.get("onset_search_start_ts_utc")
        oss_s = None if pd.isna(oss) or str(oss).strip() == "" else str(oss).strip()
        d = r.get("direction")
        d_s = None if pd.isna(d) or str(d).strip() == "" else str(d).strip().lower()
        rows.append(
            EpisodeRow(
                episode_id=str(r["episode_id"]),
                shock_cluster_id=str(r["shock_cluster_id"]),
                base_coin=str(r["base_coin"]).upper(),
                direction=d_s,
                episode_class=ec_s,
                window_start_ts_utc=str(r["window_start_ts_utc"]),
                onset_search_start_ts_utc=oss_s,
                window_end_ts_utc=str(r["window_end_ts_utc"]),
                anomaly_start_ts_utc=None
                if pd.isna(r.get("anomaly_start_ts_utc"))
                else str(r.get("anomaly_start_ts_utc")),
                prequiet_verified=_as_bool(r.get("prequiet_verified", False)),
                contaminated=_as_bool(r.get("contaminated", False)),
                notes="" if pd.isna(r.get("notes")) else str(r.get("notes")),
            )
        )
    return rows


def freeze_protocol(episodes: list[EpisodeRow], out_dir: Path) -> dict[str, Any]:
    canon_primary = [
        e
        for e in episodes
        if (e.episode_class == "isolated")
        and e.prequiet_verified
        and (not e.contaminated)
    ]
    payload = {
        "frozen_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "manifest_path": str(MANIFEST_SRC),
        "n_manifest": len(episodes),
        "n_canon_primary_isolated_prequiet": len(canon_primary),
        "canon_primary_note": (
            "Protocol primary requires episode_class=isolated, prequiet_verified=true, "
            "contaminated=false. Manifest has zero such rows."
        ),
        "operational_panel": (
            "contaminated=false + runtime_prequiet (≥1s z≤4 before first positive z>4) "
            "+ derived direction. Explicitly NOT labeled isolated; exploratory relative "
            "to canon primary filters."
        ),
        "direction_rule": "min t with s_d>0 and z_d>4 after ≥1s below; tie → larger x; else ambiguous",
        "shock_cluster_note": "temporal overlap blocks for bootstrap dependence; not causal claim",
        "C_fee_pp": C_FEE,
        "fee_rate_frozen_copy": FEE_RATE,
        "report_L_ms": list(REPORT_L),
        "E_signal_bins_pp": E_SIGNAL_BIN_LABELS,
        "changepoint_deferred_gate": {
            "min_clean_episodes": CHANGEPOINT_MIN_EP,
            "min_per_heldout_fold": CHANGEPOINT_MIN_PER_FOLD,
        },
        "candidate_cp_annotation_ms": list(CANDIDATE_CP_MS),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "protocol_freeze.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def load_coin(coin: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    df, _ = read_and_prepare_lean_ticks(
        LEAN_TICKS,
        start_ms,
        end_ms,
        coins={coin.upper()},
        columns=gear2_lean_columns(check_volume=False),
        need_freshness=True,
        workers=2,
    )
    return df.loc[df["base_coin"] == coin.upper()].sort_values("event_local_ts_ms").reset_index(drop=True)


def _floor_at(s: np.ndarray, ts: np.ndarray, i: int) -> tuple[float, float]:
    left = int(np.searchsorted(ts, ts[i] - LOOKBACK_MS, side="left"))
    if i - left < MIN_PAST:
        return float("nan"), float("nan")
    w = s[left:i]
    med = float(np.median(w))
    mad = float(np.median(np.abs(w - med)))
    return med, max(QUIET_MAD_SCALE * mad, SIGMA_MIN)


def _sparse_z(s: np.ndarray, ts: np.ndarray, cadence_ms: int = 250) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (idxs, z, x) on sparse cadence for onset scan."""
    idxs = []
    last = -10**18
    for i in range(len(ts)):
        if int(ts[i]) - last >= cadence_ms:
            idxs.append(i)
            last = int(ts[i])
    if not idxs or idxs[-1] != len(ts) - 1:
        idxs.append(len(ts) - 1)
    zs = np.full(len(idxs), np.nan)
    xs = np.full(len(idxs), np.nan)
    for k, i in enumerate(idxs):
        F, sig = _floor_at(s, ts, i)
        if not np.isfinite(F):
            continue
        xs[k] = float(s[i] - F)
        zs[k] = xs[k] / sig
    return np.asarray(idxs, dtype=np.int64), zs, xs


def _find_first_positive_crossing(
    s: np.ndarray,
    ts: np.ndarray,
    search0: int,
    search1: int,
    *,
    z_thr: float = Z_PRIMARY,
    dwell_ms: int = DWELL_MS,
) -> Optional[tuple[int, float, float, float]]:
    """Return (index, z, x, F) for first s>0 & z>z_thr after ≥dwell below thr."""
    idxs, zs, xs = _sparse_z(s, ts)
    below_since: Optional[int] = None
    for k, i in enumerate(idxs.tolist()):
        t = int(ts[i])
        if t < search0 or t >= search1:
            if t < search0:
                # allow dwell accrual before search0? No — only inside search window
                continue
            break
        if not np.isfinite(zs[k]):
            below_since = None
            continue
        if zs[k] <= z_thr:
            if below_since is None:
                below_since = t
            continue
        # candidate cross
        if below_since is None:
            continue
        if t - below_since < dwell_ms:
            continue
        if not (xs[k] > 0 and zs[k] > z_thr):
            continue
        # refine exact index: first tick at/after sparse hit with exact floor
        for j in range(max(0, i - 5), min(len(ts), i + 6)):
            if int(ts[j]) < search0 or int(ts[j]) >= search1:
                continue
            F, sig = _floor_at(s, ts, j)
            if not np.isfinite(F):
                continue
            x = float(s[j] - F)
            z = x / sig
            if x > 0 and z > z_thr:
                # re-check dwell using exact path nearby
                return j, z, x, F
        return i, float(zs[k]), float(xs[k]), float(s[i] - xs[k])
    return None


def derive_direction_and_onset(
    g: pd.DataFrame,
    search0: int,
    search1: int,
    *,
    z_thr: float = Z_PRIMARY,
) -> dict[str, Any]:
    """Direction rule: earliest positive z>4; tie → larger x; else ambiguous."""
    hits = {}
    for side in ("long", "short"):
        col = "spread_long" if side == "long" else "spread_short"
        s = g[col].to_numpy(dtype=np.float64)
        ts = g["event_local_ts_ms"].to_numpy(dtype=np.int64)
        hit = _find_first_positive_crossing(s, ts, search0, search1, z_thr=z_thr)
        if hit is not None:
            hits[side] = hit  # (i, z, x, F)
    if not hits:
        return {"direction": "ambiguous", "reason": "no_positive_z_cross", "onset_idx": None}
    if len(hits) == 1:
        side = next(iter(hits))
        i, z, x, F = hits[side]
        return {
            "direction": side,
            "reason": "single_side",
            "onset_idx": int(i),
            "z0": float(z),
            "x0": float(x),
            "F0": float(F),
            "t0_ms": int(g["event_local_ts_ms"].iloc[i]),
        }
    # both sides: compare times
    t_long = int(g["event_local_ts_ms"].iloc[hits["long"][0]])
    t_short = int(g["event_local_ts_ms"].iloc[hits["short"][0]])
    if t_long < t_short:
        side = "long"
    elif t_short < t_long:
        side = "short"
    else:
        # simultaneous → larger positive excess over floor
        if hits["long"][2] > hits["short"][2]:
            side = "long"
        elif hits["short"][2] > hits["long"][2]:
            side = "short"
        else:
            return {"direction": "ambiguous", "reason": "tie_indistinguishable", "onset_idx": None}
    i, z, x, F = hits[side]
    return {
        "direction": side,
        "reason": "earliest" if t_long != t_short else "tie_break_larger_x",
        "onset_idx": int(i),
        "z0": float(z),
        "x0": float(x),
        "F0": float(F),
        "t0_ms": int(g["event_local_ts_ms"].iloc[i]),
        "t_long_ms": t_long,
        "t_short_ms": t_short,
    }


def _leg_flags(row: pd.Series) -> dict[str, Any]:
    okx_f = float(row.get("okx_freshness_ms", np.nan))
    by_f = float(row.get("bybit_freshness_ms", np.nan))
    stale_okx = bool(np.isfinite(okx_f) and okx_f > STALE_LEG_MS)
    stale_by = bool(np.isfinite(by_f) and by_f > STALE_LEG_MS)
    if np.isfinite(okx_f) and np.isfinite(by_f):
        trigger = "okx" if okx_f <= by_f else "bybit"
    else:
        trigger = "unknown"
    return {
        "okx_freshness_ms": okx_f,
        "bybit_freshness_ms": by_f,
        "stale_okx": stale_okx,
        "stale_bybit": stale_by,
        "fresh_both": (not stale_okx) and (not stale_by),
        "trigger": trigger,
    }


def _path_metrics(
    ts: np.ndarray,
    s: np.ndarray,
    i0: int,
    *,
    F_exit: float,
    s0: float,
    C_fee: float,
    window_end_ms: int,
) -> dict[str, Any]:
    t0 = int(ts[i0])
    x_den = s0 - F_exit
    E_signal = s0 - F_exit - C_fee

    def R_at(sj: float) -> float:
        if abs(x_den) < 1e-12:
            return float("nan")
        return (sj - F_exit) / x_den

    def E_at(sj: float) -> float:
        return sj - F_exit - C_fee

    T25 = T50 = Tfloor = Tkill = None
    censor_reason = None
    last_j = i0
    for j in range(i0 + 1, len(ts)):
        t = int(ts[j])
        if t > window_end_ms:
            censor_reason = censor_reason or "window_end"
            break
        if t - t0 > HORIZON_MS:
            censor_reason = censor_reason or "horizon"
            break
        # gap / resume
        if t - int(ts[j - 1]) >= INTRA_GAP_BREAK_MS:
            censor_reason = "gap"
            break
        last_j = j
        Rj = R_at(float(s[j]))
        Ej = E_at(float(s[j]))
        u = float(t - t0)
        if T25 is None and np.isfinite(Rj) and Rj <= 0.75:
            T25 = u
        if T50 is None and np.isfinite(Rj) and Rj <= 0.50:
            T50 = u
        if Tfloor is None and np.isfinite(Rj) and Rj <= 0.0:
            Tfloor = u
        if Tkill is None and np.isfinite(Ej) and Ej <= 0.0:
            Tkill = u
        if T25 is not None and T50 is not None and Tfloor is not None and Tkill is not None:
            break
    else:
        if censor_reason is None:
            censor_reason = "data_end"

    # state_at_L / fill_contract_L
    slices = {}
    for L in REPORT_L:
        # state: last ts <= t0+L
        j_state = int(np.searchsorted(ts, t0 + L, side="right")) - 1
        if j_state < i0 or int(ts[j_state]) > t0 + L:
            s_state = float("nan")
            t_state = None
            state_ok = False
            state_censor = "no_state"
        else:
            # reject if gap between i0 and j_state
            gap = False
            for k in range(i0 + 1, j_state + 1):
                if int(ts[k]) - int(ts[k - 1]) >= INTRA_GAP_BREAK_MS:
                    gap = True
                    break
            if gap:
                s_state = float("nan")
                t_state = None
                state_ok = False
                state_censor = "gap"
            else:
                s_state = float(s[j_state])
                t_state = int(ts[j_state])
                state_ok = True
                state_censor = None

        # fill: first ts >= t0+L
        j_fill = int(np.searchsorted(ts, t0 + L, side="left"))
        if j_fill >= len(ts):
            s_fill = float("nan")
            t_fill = None
            Leff = None
            fill_ok = False
            fill_censor = "data_end"
        else:
            delay = int(ts[j_fill]) - t0
            if delay > L + GAP_SLACK_MS:
                s_fill = float("nan")
                t_fill = None
                Leff = float(delay)
                fill_ok = False
                fill_censor = "gap_slack"
            else:
                # also fail if intra gap crosses
                gap = False
                for k in range(i0 + 1, j_fill + 1):
                    if int(ts[k]) - int(ts[k - 1]) >= INTRA_GAP_BREAK_MS:
                        gap = True
                        break
                if gap:
                    s_fill = float("nan")
                    t_fill = None
                    Leff = float(delay)
                    fill_ok = False
                    fill_censor = "gap"
                else:
                    s_fill = float(s[j_fill])
                    t_fill = int(ts[j_fill])
                    Leff = float(delay)
                    fill_ok = True
                    fill_censor = None

        def pack(sj, ok, cens, extra=None):
            if not ok or not np.isfinite(sj):
                return {
                    "ok": False,
                    "censor_reason": cens,
                    "s_L": None,
                    "D_L": None,
                    "R_L": None,
                    "E_fill": None,
                    "Q_L": None,
                    **(extra or {}),
                }
            D = s0 - sj
            R = R_at(sj)
            Ef = E_at(sj)
            if E_signal > NEAR_ZERO_E:
                Q = Ef / E_signal
            else:
                Q = None
            return {
                "ok": True,
                "censor_reason": None,
                "s_L": sj,
                "D_L": float(D),
                "R_L": float(R) if np.isfinite(R) else None,
                "E_fill": float(Ef),
                "Q_L": float(Q) if Q is not None and np.isfinite(Q) else None,
                **(extra or {}),
            }

        slices[str(L)] = {
            "state_at_L": pack(
                s_state,
                state_ok,
                state_censor,
                {"t_state_ms": t_state},
            ),
            "fill_contract_L": pack(
                s_fill,
                fill_ok,
                fill_censor,
                {
                    "t_fill_ms": t_fill,
                    "L_eff": Leff,
                    "L_eff_minus_L": (Leff - L) if Leff is not None else None,
                },
            ),
        }

    return {
        "E_signal": float(E_signal),
        "T25": T25,
        "T50": T50,
        "T_floor": Tfloor,
        "T_edge_kill": Tkill,
        "event_censor_reason": censor_reason,
        "slices": slices,
        "last_obs_ms": int(ts[last_j]) if last_j is not None else t0,
    }


def e_signal_bin(e: float) -> str:
    if not np.isfinite(e) or e <= 0:
        return "nonpositive"
    for (a, b), lab in zip(E_SIGNAL_BINS, E_SIGNAL_BIN_LABELS):
        if a < e <= b or (b == math.inf and e > a):
            # (0,0.10] etc.
            if b == math.inf:
                return lab
            if e > a and e <= b:
                return lab
    return "nonpositive"


def process_episode(ep: EpisodeRow) -> dict[str, Any]:
    pad0 = ep.search_start_ms - LOOKBACK_MS
    pad1 = ep.window_end_ms + HORIZON_MS + 5_000
    try:
        g = load_coin(ep.base_coin, pad0, pad1)
    except Exception as exc:  # noqa: BLE001
        return {
            "episode_id": ep.episode_id,
            "status": "load_fail",
            "error": str(exc),
            "contaminated": ep.contaminated,
            "manifest_prequiet_verified": ep.prequiet_verified,
        }
    if g.empty:
        return {
            "episode_id": ep.episode_id,
            "status": "no_ticks",
            "contaminated": ep.contaminated,
            "manifest_prequiet_verified": ep.prequiet_verified,
        }

    der = derive_direction_and_onset(g, ep.search_start_ms, ep.window_end_ms, z_thr=Z_PRIMARY)
    if der["direction"] == "ambiguous" or der.get("onset_idx") is None:
        return {
            "episode_id": ep.episode_id,
            "shock_cluster_id": ep.shock_cluster_id,
            "base_coin": ep.base_coin,
            "status": "ambiguous_or_no_onset",
            "direction": "ambiguous",
            "derive": der,
            "contaminated": ep.contaminated,
            "manifest_prequiet_verified": ep.prequiet_verified,
            "runtime_prequiet_ok": False,
        }

    i0 = int(der["onset_idx"])
    side = der["direction"]
    col = "spread_long" if side == "long" else "spread_short"
    ts = g["event_local_ts_ms"].to_numpy(dtype=np.int64)
    s = g[col].to_numpy(dtype=np.float64)
    # exact freeze at onset
    F0, sig0 = _floor_at(s, ts, i0)
    s0 = float(s[i0])
    F_exit = F0  # same past-only floor contract
    x0 = s0 - F_exit
    z0 = x0 / sig0 if np.isfinite(sig0) and sig0 > 0 else float("nan")
    t0 = int(ts[i0])

    # runtime prequiet: dwell satisfied by construction of crossing finder
    runtime_prequiet_ok = True
    # stronger: some below-threshold mass in pre-roll if anomaly_start known
    if ep.anomaly_start_ms is not None and t0 >= ep.anomaly_start_ms:
        # require at least one finite z<=4 tick in [search0, anomaly_start)
        pre_ok = False
        for j in range(len(ts)):
            if int(ts[j]) < ep.search_start_ms:
                continue
            if int(ts[j]) >= ep.anomaly_start_ms:
                break
            F, sig = _floor_at(s, ts, j)
            if not np.isfinite(F):
                continue
            zj = (float(s[j]) - F) / sig
            if zj <= Z_PRIMARY:
                pre_ok = True
                break
        runtime_prequiet_ok = pre_ok

    flags = _leg_flags(g.iloc[i0])
    path = _path_metrics(ts, s, i0, F_exit=F_exit, s0=s0, C_fee=C_FEE, window_end_ms=ep.window_end_ms)
    E_sig = path["E_signal"]

    # secondary z>2 onset time (same side), descriptive only
    sec = _find_first_positive_crossing(s, ts, ep.search_start_ms, ep.window_end_ms, z_thr=Z_SECONDARY)

    return {
        "episode_id": ep.episode_id,
        "shock_cluster_id": ep.shock_cluster_id,
        "base_coin": ep.base_coin,
        "episode_class": ep.episode_class,
        "contaminated": ep.contaminated,
        "manifest_prequiet_verified": ep.prequiet_verified,
        "runtime_prequiet_ok": runtime_prequiet_ok,
        "status": "ok",
        "direction": side,
        "direction_reason": der.get("reason"),
        "t0_ms": t0,
        "t0_utc": _ms_iso(t0),
        "s0": s0,
        "F0": float(F0),
        "F_exit": float(F_exit),
        "x0": float(x0),
        "sigma0": float(sig0),
        "z0": float(z0),
        "C_fee": C_FEE,
        "E_signal": float(E_sig),
        "E_signal_bin": e_signal_bin(float(E_sig)),
        "economic_primary": bool(E_sig > 0),
        "T25": path["T25"],
        "T50": path["T50"],
        "T_floor": path["T_floor"],
        "T_edge_kill": path["T_edge_kill"],
        "event_censor_reason": path["event_censor_reason"],
        "slices": path["slices"],
        "secondary_z2_t_ms": None if sec is None else int(ts[sec[0]]),
        **flags,
    }


def _collect_vals(rows: list[dict], L: int, mode: str, field: str) -> list[float]:
    out = []
    for r in rows:
        sl = r.get("slices", {}).get(str(L), {}).get(mode, {})
        if not sl.get("ok"):
            continue
        v = sl.get(field)
        if v is not None and np.isfinite(v):
            out.append(float(v))
    return out


def summarize_L(rows: list[dict], L: int, mode: str = "state_at_L") -> dict[str, Any]:
    eco = [r for r in rows if r.get("economic_primary")]
    D = _collect_vals(eco, L, mode, "D_L")
    R = _collect_vals(eco, L, mode, "R_L")
    E = _collect_vals(eco, L, mode, "E_fill")
    Q = _collect_vals(eco, L, mode, "Q_L")
    n_eco = len(eco)
    n_ok = sum(1 for r in eco if r.get("slices", {}).get(str(L), {}).get(mode, {}).get("ok"))

    def dist(xs: list[float]) -> dict[str, Any]:
        if not xs:
            return {"n": 0}
        a = np.asarray(xs, dtype=np.float64)
        return {
            "n": int(a.size),
            "mean": float(np.mean(a)),
            "median": float(np.median(a)),
            "q10": float(np.percentile(a, 10)),
            "q25": float(np.percentile(a, 25)),
            "q75": float(np.percentile(a, 75)),
            "q90": float(np.percentile(a, 90)),
            "frac_zero": float(np.mean(np.isclose(a, 0.0))),
        }

    p_surv = float(np.mean(np.asarray(E) > 0)) if E else float("nan")
    return {
        "L": L,
        "mode": mode,
        "n_economic": n_eco,
        "n_ok": n_ok,
        "D": dist(D),
        "R": dist(R),
        "E_fill": dist(E),
        "Q": dist(Q),
        "P_R_ge_0_75": float(np.mean(np.asarray(R) >= 0.75)) if R else float("nan"),
        "P_R_ge_0_50": float(np.mean(np.asarray(R) >= 0.50)) if R else float("nan"),
        "P_R_le_0": float(np.mean(np.asarray(R) <= 0.0)) if R else float("nan"),
        "p_survive": p_surv,
        "P_E_fill_le_0": float(np.mean(np.asarray(E) <= 0.0)) if E else float("nan"),
    }


def cluster_bootstrap(
    rows: list[dict],
    *,
    n_boot: int = BOOT_N,
    seed: int = BOOT_SEED,
    mode: str = "state_at_L",
) -> dict[str, Any]:
    """Resample whole shock_cluster_id blocks (equal-episode within draw via cluster resample)."""
    eco = [r for r in rows if r.get("economic_primary") and r.get("status") == "ok"]
    if not eco:
        return {"n": 0}
    clusters = sorted({r["shock_cluster_id"] for r in eco})
    by_c = {c: [r for r in eco if r["shock_cluster_id"] == c] for c in clusters}
    rng = np.random.default_rng(seed)
    out: dict[str, Any] = {"n_episodes": len(eco), "n_clusters": len(clusters), "n_boot": n_boot, "L": {}}
    for L in REPORT_L:
        samples_p = []
        samples_Rmed = []
        samples_Emed = []
        for _ in range(n_boot):
            draw_c = rng.choice(clusters, size=len(clusters), replace=True)
            sample = []
            for c in draw_c:
                sample.extend(by_c[c])
            # equal-episode in sample (clusters may duplicate episodes weight)
            sm = summarize_L(sample, L, mode)
            if sm["n_ok"] == 0:
                continue
            samples_p.append(sm["p_survive"])
            samples_Rmed.append(sm["R"].get("median", np.nan))
            samples_Emed.append(sm["E_fill"].get("median", np.nan))
        def ci(xs):
            xs = [x for x in xs if np.isfinite(x)]
            if not xs:
                return None
            return {
                "mean": float(np.mean(xs)),
                "p025": float(np.percentile(xs, 2.5)),
                "p975": float(np.percentile(xs, 97.5)),
            }
        point = summarize_L(eco, L, mode)
        out["L"][str(L)] = {
            "point": point,
            "p_survive_ci": ci(samples_p),
            "R_median_ci": ci(samples_Rmed),
            "E_fill_median_ci": ci(samples_Emed),
        }
    return out


def equal_coin_summary(rows: list[dict], mode: str = "state_at_L") -> dict[str, Any]:
    eco = [r for r in rows if r.get("economic_primary")]
    coins = sorted({r["base_coin"] for r in eco})
    per = {}
    for c in coins:
        sub = [r for r in eco if r["base_coin"] == c]
        per[c] = {str(L): summarize_L(sub, L, mode) for L in REPORT_L}
    # mean of per-coin p_survive
    agg = {}
    for L in REPORT_L:
        ps = [per[c][str(L)]["p_survive"] for c in coins if per[c][str(L)]["n_ok"] > 0]
        agg[str(L)] = {
            "n_coins": len(ps),
            "p_survive_mean_coin": float(np.nanmean(ps)) if ps else float("nan"),
        }
    return {"per_coin": per, "agg": agg}


def loo_stability(rows: list[dict], key: str, mode: str = "state_at_L") -> dict[str, Any]:
    eco = [r for r in rows if r.get("economic_primary")]
    units = sorted({r[key] for r in eco})
    base = {str(L): summarize_L(eco, L, mode)["p_survive"] for L in REPORT_L}
    deltas = []
    for u in units:
        sub = [r for r in eco if r[key] != u]
        if len(sub) < 2:
            continue
        for L in REPORT_L:
            p = summarize_L(sub, L, mode)["p_survive"]
            deltas.append({"left_out": u, "L": L, "p_survive": p, "delta": p - base[str(L)]})
    return {"base": base, "loo": deltas}


def km_curve(times: list[Optional[float]], censored: list[bool]) -> dict[str, Any]:
    """Simple KM for event times in ms; right-censored flagged."""
    pairs = []
    for t, c in zip(times, censored):
        if t is None:
            pairs.append((HORIZON_MS, True))
        else:
            pairs.append((float(t), bool(c)))
    pairs.sort(key=lambda x: x[0])
    n = len(pairs)
    if n == 0:
        return {"times": [], "survival": []}
    surv = 1.0
    t_out, s_out = [], []
    i = 0
    at_risk = n
    while i < n:
        t = pairs[i][0]
        d = 0
        c = 0
        while i < n and pairs[i][0] == t:
            if pairs[i][1]:
                c += 1
            else:
                d += 1
            i += 1
        if at_risk > 0 and d > 0:
            surv *= 1.0 - d / at_risk
            t_out.append(t)
            s_out.append(surv)
        at_risk -= d + c
    # also evaluate at report L
    at_L = {}
    for L in REPORT_L:
        # P(T > L) ≈ last surv before L
        sL = 1.0
        for t, s in zip(t_out, s_out):
            if t <= L:
                sL = s
            else:
                break
        at_L[str(L)] = {"KM_P_T_gt_L": sL, "KM_P_T_le_L": 1.0 - sL}
    return {"times": t_out, "survival": s_out, "at_report_L": at_L, "n": n}


def verdict_science(boot: dict, boot_fill: dict, n_primary: int, n_clusters: int) -> dict[str, Any]:
    reasons = []
    labels = []
    if n_primary < 5 or n_clusters < 3:
        labels.append("underpowered")
        reasons.append(f"n_economic_primary={n_primary}, n_clusters={n_clusters}")
    state_vs_fill = False
    for L in REPORT_L:
        ps = (boot.get("L") or {}).get(str(L), {}).get("p_survive_ci")
        pf = (boot_fill.get("L") or {}).get(str(L), {}).get("p_survive_ci")
        if ps and pf:
            # qualitative disagreement if CIs entirely opposite sides of 0.5
            if (ps["p975"] < 0.5 and pf["p025"] > 0.5) or (ps["p025"] > 0.5 and pf["p975"] < 0.5):
                state_vs_fill = True
        if ps:
            if ps["p025"] > 0.5:
                labels.append(f"edge mostly survives@{L}")
            elif ps["p975"] < 0.5:
                labels.append(f"edge mostly killed@{L}")
            else:
                labels.append(f"partial/heterogeneous@{L}")
                reasons.append(f"L={L} p_survive CI crosses 0.5: [{ps['p025']:.3f},{ps['p975']:.3f}]")
    if state_vs_fill:
        labels.append("sampling-contract sensitive")
        reasons.append("state_at_L vs fill_contract_L disagree across 0.5")
    # unique scientific headline
    if "underpowered" in labels and all("partial" in x or "underpowered" in x or "@" in x for x in labels):
        headline = "underpowered" if n_primary < 5 else "partial / heterogeneous decay"
    elif any(x.startswith("edge mostly killed") for x in labels) and not any(
        x.startswith("edge mostly survives") for x in labels
    ):
        headline = "edge mostly killed"
    elif any(x.startswith("edge mostly survives") for x in labels) and not any(
        x.startswith("edge mostly killed") for x in labels
    ):
        headline = "edge mostly survives"
    else:
        headline = "partial / heterogeneous decay"
    if state_vs_fill:
        headline = "sampling-contract sensitive"
    return {
        "verdict": headline,
        "labels": labels,
        "reasons": reasons,
        "change_point": "change-point deferred: underpowered",
        "disclaimer": (
            "Diagnostic edge survival only; not alpha; not competitor latency; "
            "not an entry-rule handoff."
        ),
    }


def run_all(out_dir: Path = OUT_DIR) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    episodes = load_manifest()
    proto = freeze_protocol(episodes, out_dir)
    print(json.dumps(proto, indent=2), flush=True)

    results = []
    for ep in episodes:
        print(f"process {ep.episode_id} contam={ep.contaminated}...", flush=True)
        results.append(process_episode(ep))

    (out_dir / "episode_results.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    # panels
    canon = [
        r
        for r in results
        if r.get("status") == "ok"
        and r.get("episode_class") == "isolated"
        and r.get("manifest_prequiet_verified")
        and not r.get("contaminated")
    ]
    operational = [
        r
        for r in results
        if r.get("status") == "ok"
        and not r.get("contaminated")
        and r.get("runtime_prequiet_ok")
        and r.get("direction") not in (None, "ambiguous")
    ]
    contam_panel = [r for r in results if r.get("status") == "ok" and r.get("contaminated")]
    no_econ = [r for r in operational if not r.get("economic_primary")]
    primary = [r for r in operational if r.get("economic_primary")]

    power = {
        "n_manifest": len(episodes),
        "n_canon_isolated_prequiet": len(canon),
        "n_operational_clean": len(operational),
        "n_economic_primary": len(primary),
        "n_nonpositive_E_signal": len(no_econ),
        "n_contaminated_ok_onset": len(contam_panel),
        "n_clusters_operational": len({r["shock_cluster_id"] for r in operational}),
        "n_clusters_economic": len({r["shock_cluster_id"] for r in primary}),
        "excluded": [
            {
                "episode_id": r.get("episode_id"),
                "status": r.get("status"),
                "contaminated": r.get("contaminated"),
                "runtime_prequiet_ok": r.get("runtime_prequiet_ok"),
                "direction": r.get("direction"),
                "E_signal": r.get("E_signal"),
            }
            for r in results
            if r.get("episode_id") not in {x["episode_id"] for x in primary}
        ],
        "note": (
            "Canon primary empty (no isolated+prequiet_verified). "
            "Analysis primary = operational clean ∩ E_signal>0."
        ),
    }
    (out_dir / "power_gate.json").write_text(json.dumps(power, indent=2), encoding="utf-8")
    print("power", power, flush=True)

    # summaries
    sum_state = {str(L): summarize_L(primary, L, "state_at_L") for L in REPORT_L}
    sum_fill = {str(L): summarize_L(primary, L, "fill_contract_L") for L in REPORT_L}
    (out_dir / "summary_state_at_L.json").write_text(json.dumps(sum_state, indent=2), encoding="utf-8")
    (out_dir / "summary_fill_contract_L.json").write_text(json.dumps(sum_fill, indent=2), encoding="utf-8")

    boot_state = cluster_bootstrap(primary, mode="state_at_L")
    boot_fill = cluster_bootstrap(primary, mode="fill_contract_L", seed=BOOT_SEED + 1)
    (out_dir / "bootstrap_state.json").write_text(json.dumps(boot_state, indent=2), encoding="utf-8")
    (out_dir / "bootstrap_fill.json").write_text(json.dumps(boot_fill, indent=2), encoding="utf-8")

    eq_coin = equal_coin_summary(primary, "state_at_L")
    (out_dir / "equal_coin_state.json").write_text(json.dumps(eq_coin, indent=2), encoding="utf-8")

    per_ep = []
    for r in primary:
        row = {
            "episode_id": r["episode_id"],
            "coin": r["base_coin"],
            "cluster": r["shock_cluster_id"],
            "direction": r["direction"],
            "E_signal": r["E_signal"],
            "E_signal_bin": r["E_signal_bin"],
            "fresh_both": r.get("fresh_both"),
            "T25": r.get("T25"),
            "T50": r.get("T50"),
            "T_edge_kill": r.get("T_edge_kill"),
        }
        for L in REPORT_L:
            st = r["slices"][str(L)]["state_at_L"]
            row[f"R_{L}_state"] = st.get("R_L")
            row[f"E_{L}_state"] = st.get("E_fill")
            row[f"surv_{L}_state"] = None if st.get("E_fill") is None else bool(st["E_fill"] > 0)
        per_ep.append(row)
    pd.DataFrame(per_ep).to_csv(out_dir / "per_episode_primary.csv", index=False)

    # E_signal bins
    bins_out = {}
    for lab in E_SIGNAL_BIN_LABELS:
        sub = [r for r in primary if r.get("E_signal_bin") == lab]
        bins_out[lab] = {
            "n": len(sub),
            "n_clusters": len({r["shock_cluster_id"] for r in sub}),
            "by_L": {str(L): summarize_L(sub, L, "state_at_L") for L in REPORT_L},
            "T25": [r.get("T25") for r in sub],
            "T50": [r.get("T50") for r in sub],
            "T_edge_kill": [r.get("T_edge_kill") for r in sub],
        }
    (out_dir / "by_E_signal_bin.json").write_text(json.dumps(bins_out, indent=2), encoding="utf-8")

    # freshness panels
    fresh_panels = {}
    for name, pred in [
        ("fresh_both", lambda r: r.get("fresh_both") is True),
        ("stale_okx", lambda r: r.get("stale_okx") is True),
        ("stale_bybit", lambda r: r.get("stale_bybit") is True),
    ]:
        sub = [r for r in primary if pred(r)]
        fresh_panels[name] = {
            "n": len(sub),
            "by_L": {str(L): summarize_L(sub, L, "state_at_L") for L in REPORT_L},
        }
    (out_dir / "freshness_panels.json").write_text(json.dumps(fresh_panels, indent=2), encoding="utf-8")

    # KM exact times
    def km_for(field):
        times = []
        cens = []
        for r in primary:
            t = r.get(field)
            if t is None:
                times.append(None)
                cens.append(True)
            else:
                times.append(float(t))
                cens.append(False)
        return km_curve(times, cens)

    km = {
        "T25": km_for("T25"),
        "T50": km_for("T50"),
        "T_floor": km_for("T_floor"),
        "T_edge_kill": km_for("T_edge_kill"),
        "annotation_candidate_cp_ms": list(CANDIDATE_CP_MS),
    }
    (out_dir / "km_exact_events.json").write_text(json.dumps(km, indent=2), encoding="utf-8")

    loo_ep = loo_stability(primary, "episode_id")
    loo_cl = loo_stability(primary, "shock_cluster_id")
    loo_coin = loo_stability(primary, "base_coin")
    (out_dir / "loo.json").write_text(
        json.dumps({"episode": loo_ep, "cluster": loo_cl, "coin": loo_coin}, indent=2),
        encoding="utf-8",
    )

    # contaminated / non-econ audits
    (out_dir / "audit_nonpositive_E.json").write_text(json.dumps(no_econ, indent=2, default=str), encoding="utf-8")
    (out_dir / "audit_contaminated.json").write_text(json.dumps(contam_panel, indent=2, default=str), encoding="utf-8")

    cp_gate = len(primary) >= CHANGEPOINT_MIN_EP and (len(primary) / 5) >= CHANGEPOINT_MIN_PER_FOLD
    verd = verdict_science(
        boot_state,
        boot_fill,
        n_primary=len(primary),
        n_clusters=len({r["shock_cluster_id"] for r in primary}),
    )
    verd["canon_primary_n"] = len(canon)
    verd["operational_panel"] = True
    verd["change_point"] = (
        "change-point deferred: underpowered"
        if not cp_gate
        else "gate passed but not run in this scratch (out of scope for survival primary)"
    )
    (out_dir / "verdict.json").write_text(json.dumps(verd, indent=2), encoding="utf-8")
    (out_dir / "verdict.txt").write_text(
        verd["verdict"]
        + "\n"
        + "\n".join(verd["reasons"])
        + "\n"
        + verd["change_point"]
        + "\n"
        + verd["disclaimer"]
        + "\n",
        encoding="utf-8",
    )
    print("VERDICT", verd["verdict"], flush=True)
    return {"power": power, "verdict": verd, "sum_state": sum_state, "boot_state": boot_state}


if __name__ == "__main__":
    run_all()
