"""Gear 2.2 research: material latency change-point after anomaly onset.

Scratch only — does not retune VARIATION/HYPER/Trade_Lat or touch model_gear2 /
gear2_backtest. Grid L values are analysis_lag_ms diagnostics.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

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
LEAN_TICKS = REPO / "output" / "lean_ticks"
OUT_DIR = REPO / "research" / "output" / "gear22_material_latency_changepoint"
MANIFEST_PATH = OUT_DIR / "manifest.json"

# --- Frozen contracts (aligned with prior gear 2.2 experiments) ---
LOOKBACK_MS = 300_000
MIN_PAST = 30
SIGMA_MIN = 0.02
QUIET_MAD_SCALE = 1.4826
GAP_SLACK_MS = DEFAULT_GAP_FILL_SLACK_MS
STALE_LEG_MS = 200
FILE_INTERVAL_MS = 300_000
INTRA_GAP_BREAK_MS = 360_000  # plot/overview convention
DWELL_MS_PRIMARY = 1_000
DWELL_MS_SENS = 500
Z_PRIMARY = 4.0
Z_SECONDARY = 2.0
HORIZON_MS = 1_000
GRID_PRIMARY = tuple(range(10, 301, 10))
GRID_DIAG = (500, 1000)
GRID_HIGHLIGHT = (50, 100, 200)
GRID_ALL = tuple(sorted(set(GRID_PRIMARY) | set(GRID_DIAG)))
TAU_CANDIDATES = tuple(range(20, 251, 10))
# Fee diagnostic only (not PnL / not fee retune): round-trip 4 legs × 0.00075 → 0.3% spread units
C_FEE_PCT = 4.0 * 0.00075 * 100.0
# h_min method frozen BEFORE response curves (documented):
# min |Δs| consecutive move in [t0-LOOKBACK,t0); if none → 1e-4
H_MIN_FALLBACK = 1e-4
KAPPA_SENS = (0.5, 1.0, 2.0)
R_SENS = (0.10, 0.25, 0.50)
QUIET_PER_ONSET = 5
QUIET_SEED = 22
BOOT_N = 200
BOOT_SEED = 22
KFOLD = 5
PANEL_SEED = 22

# Documented calendar holes overlapping August coverage
KNOWN_HOLES: list[tuple[str, str, str]] = [
    ("2026-08-14T12:25:00Z", "2026-08-14T12:30:00Z", "aug14_5min_slot"),
    ("2026-08-16T17:35:00Z", "2026-08-16T17:50:00Z", "aug16_operator_stop"),
]

# User-supplied anomaly intervals (UTC). episode_class unset → unclassified mixture.
# direction left null; derived at onset as first z>4 side within the interval.
RAW_EPISODES: list[dict[str, Any]] = [
    {"episode_id": "2Z_20260814_0200", "base_coin": "2Z", "start_ts_utc": "2026-08-14T02:00:00Z", "end_ts_utc": "2026-08-15T04:00:00Z", "episode_class": None, "notes": "user list; mid-term anomaly spread"},
    {"episode_id": "ACU_20260813_0539", "base_coin": "ACU", "start_ts_utc": "2026-08-13T05:39:00Z", "end_ts_utc": "2026-08-13T07:00:00Z", "episode_class": None, "notes": ""},
    {"episode_id": "BICO_20260809_0705", "base_coin": "BICO", "start_ts_utc": "2026-08-09T07:05:00Z", "end_ts_utc": "2026-08-09T07:50:00Z", "episode_class": None, "notes": ""},
    {"episode_id": "CAP_20260814_1845", "base_coin": "CAP", "start_ts_utc": "2026-08-14T18:45:00Z", "end_ts_utc": "2026-08-14T19:30:00Z", "episode_class": None, "notes": ""},
    {"episode_id": "ESP_20260807_1100", "base_coin": "ESP", "start_ts_utc": "2026-08-07T11:00:00Z", "end_ts_utc": "2026-08-07T14:20:00Z", "episode_class": None, "notes": ""},
    {"episode_id": "KAITO_20260808_1200", "base_coin": "KAITO", "start_ts_utc": "2026-08-08T12:00:00Z", "end_ts_utc": "2026-08-08T21:00:00Z", "episode_class": None, "notes": ""},
    {"episode_id": "KMNO_20260807_0150", "base_coin": "KMNO", "start_ts_utc": "2026-08-07T01:50:00Z", "end_ts_utc": "2026-08-07T12:00:00Z", "episode_class": None, "notes": ""},
    {"episode_id": "LA_20260807_0100", "base_coin": "LA", "start_ts_utc": "2026-08-07T01:00:00Z", "end_ts_utc": "2026-08-07T08:30:00Z", "episode_class": None, "notes": ""},
    {"episode_id": "LA_20260808_2250", "base_coin": "LA", "start_ts_utc": "2026-08-08T22:50:00Z", "end_ts_utc": "2026-08-09T06:00:00Z", "episode_class": None, "notes": "cross-midnight interpreted as →09 06:00"},
    {"episode_id": "MUBARAK_20260809_1430", "base_coin": "MUBARAK", "start_ts_utc": "2026-08-09T14:30:00Z", "end_ts_utc": "2026-08-09T20:00:00Z", "episode_class": None, "notes": ""},
    {"episode_id": "RVN_20260814_1330", "base_coin": "RVN", "start_ts_utc": "2026-08-14T13:30:00Z", "end_ts_utc": "2026-08-14T17:30:00Z", "episode_class": None, "notes": ""},
    {"episode_id": "TRUST_20260813_0330", "base_coin": "TRUST", "start_ts_utc": "2026-08-13T03:30:00Z", "end_ts_utc": "2026-08-13T04:30:00Z", "episode_class": None, "notes": ""},
    {"episode_id": "WAL_20260815_0900", "base_coin": "WAL", "start_ts_utc": "2026-08-15T09:00:00Z", "end_ts_utc": "2026-08-17T00:00:00Z", "episode_class": None, "notes": "crosses Aug16 sparse + operator stop"},
    {"episode_id": "ZBT_20260806_0000", "base_coin": "ZBT", "start_ts_utc": "2026-08-06T00:00:00Z", "end_ts_utc": "2026-08-07T05:00:00Z", "episode_class": None, "notes": ""},
    {"episode_id": "ZBT_20260808_2000", "base_coin": "ZBT", "start_ts_utc": "2026-08-08T20:00:00Z", "end_ts_utc": "2026-08-09T04:00:00Z", "episode_class": None, "notes": ""},
]


@dataclass
class Episode:
    episode_id: str
    base_coin: str
    direction: Optional[str]
    start_ts_utc: str
    end_ts_utc: str
    episode_class: Optional[str]
    notes: str = ""
    shock_cluster_id: Optional[str] = None

    @property
    def start_ms(self) -> int:
        return parse_ts_ms(self.start_ts_utc)

    @property
    def end_ms(self) -> int:
        return parse_ts_ms(self.end_ts_utc)


def freeze_manifest(path: Path = MANIFEST_PATH) -> list[Episode]:
    path.parent.mkdir(parents=True, exist_ok=True)
    eps = [
        Episode(
            episode_id=r["episode_id"],
            base_coin=str(r["base_coin"]).upper(),
            direction=r.get("direction"),
            start_ts_utc=r["start_ts_utc"],
            end_ts_utc=r["end_ts_utc"],
            episode_class=r.get("episode_class"),
            notes=r.get("notes") or "",
            shock_cluster_id=r.get("shock_cluster_id") or r["episode_id"],
        )
        for r in RAW_EPISODES
    ]
    payload = {
        "frozen_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "unclassified anomaly mixture",
        "reason": "episode_class not supplied; primary≠isolated-only",
        "n_episodes": len(eps),
        "n_coins": len({e.base_coin for e in eps}),
        "coin_sampling": "all (n_coins<=24)",
        "h_min_method": (
            "min consecutive |Δs|>0 in [t0-LOOKBACK,t0); else H_MIN_FALLBACK=1e-4; "
            "frozen before response curves"
        ),
        "dwell_ms": {"primary": DWELL_MS_PRIMARY, "sensitivity": DWELL_MS_SENS},
        "z_levels": {"primary": Z_PRIMARY, "secondary": Z_SECONDARY},
        "analysis_lag_ms_primary": list(GRID_PRIMARY),
        "analysis_lag_ms_diag": list(GRID_DIAG),
        "C_fee_pct_diagnostic": C_FEE_PCT,
        "episodes": [asdict(e) for e in eps],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return eps


def _ms_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def audit_episode_artifacts(ep: Episode, lean: Path = LEAN_TICKS) -> dict[str, Any]:
    """Secondary validity check: file holes, known restarts, intra-coin gaps."""
    files = list_lean_files_overlapping(lean, ep.start_ms, ep.end_ms)
    starts: list[int] = []
    for p in files:
        w = parse_lean_file_window(p)
        if w:
            starts.append(w[0])
    starts = sorted(set(starts))
    start_set = set(starts)
    missing_slots: list[str] = []
    if starts:
        t = starts[0]
        while t < starts[-1]:
            if t not in start_set and ep.start_ms <= t < ep.end_ms:
                missing_slots.append(_ms_iso(t))
            t += FILE_INTERVAL_MS
    known = []
    for a, b, lab in KNOWN_HOLES:
        hs, he = parse_ts_ms(a), parse_ts_ms(b)
        if ep.start_ms < he and hs < ep.end_ms:
            known.append({"label": lab, "start": a, "end": b})

    # per-coin intra gaps after prepare (cheap sample read)
    pad0 = ep.start_ms - LOOKBACK_MS
    pad1 = ep.end_ms + HORIZON_MS + 5_000
    try:
        df, _ = read_and_prepare_lean_ticks(
            lean,
            pad0,
            pad1,
            coins={ep.base_coin},
            columns=gear2_lean_columns(check_volume=False),
            need_freshness=True,
            workers=2,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "episode_id": ep.episode_id,
            "base_coin": ep.base_coin,
            "n_files": len(files),
            "missing_5m_slots": missing_slots,
            "known_holes": known,
            "load_error": str(exc),
            "status": "load_fail",
        }
    g = df.loc[df["base_coin"] == ep.base_coin].sort_values("event_local_ts_ms")
    ts = g["event_local_ts_ms"].to_numpy(dtype=np.int64)
    in_ep = (ts >= ep.start_ms) & (ts < ep.end_ms)
    ts_ep = ts[in_ep]
    intra = []
    if ts_ep.size >= 2:
        d = np.diff(ts_ep.astype(np.float64))
        big = np.where(d >= INTRA_GAP_BREAK_MS)[0]
        for i in big[:50]:
            intra.append(
                {
                    "from": _ms_iso(int(ts_ep[i])),
                    "to": _ms_iso(int(ts_ep[i + 1])),
                    "gap_ms": float(d[i]),
                }
            )
    contaminated = bool(missing_slots or known or intra)
    return {
        "episode_id": ep.episode_id,
        "base_coin": ep.base_coin,
        "n_files": len(files),
        "n_ticks_in_window": int(ts_ep.size),
        "missing_5m_slots": missing_slots,
        "n_missing_5m": len(missing_slots),
        "known_holes": known,
        "intra_coin_gaps_ge_6min": intra,
        "n_intra_gaps_ge_6min": len(intra),
        "contaminated": contaminated,
        "status": "contaminated" if contaminated else "ok",
        "note": (
            "File holes / operator stop / ≥6min coin silence → reject onset if "
            "crossing gap; do not treat hole as quiet market."
        ),
    }


def _floor_at_indices(s: np.ndarray, ts: np.ndarray, idxs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    F = np.full(idxs.size, np.nan, dtype=np.float64)
    sig = np.full(idxs.size, np.nan, dtype=np.float64)
    for k, i in enumerate(idxs.tolist()):
        left = int(np.searchsorted(ts, ts[i] - LOOKBACK_MS, side="left"))
        if i - left < MIN_PAST:
            continue
        w = s[left:i]
        med = float(np.median(w))
        mad = float(np.median(np.abs(w - med)))
        F[k] = med
        sig[k] = max(QUIET_MAD_SCALE * mad, SIGMA_MIN)
    return F, sig


def _rolling_floor(s: np.ndarray, ts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Sparse past-only floor (500ms cadence) forward-filled — for scans only."""
    n = int(s.size)
    F = np.full(n, np.nan, dtype=np.float64)
    sig = np.full(n, np.nan, dtype=np.float64)
    if n == 0:
        return F, sig
    idxs = []
    last_t = -10**18
    for i in range(n):
        if int(ts[i]) - last_t >= 500:
            idxs.append(i)
            last_t = int(ts[i])
    if not idxs or idxs[-1] != n - 1:
        idxs.append(n - 1)
    idx_arr = np.asarray(idxs, dtype=np.int64)
    Ff, sf = _floor_at_indices(s, ts, idx_arr)
    for k, i in enumerate(idxs):
        F[i], sig[i] = Ff[k], sf[k]
    last_f, last_s = np.nan, np.nan
    for i in range(n):
        if np.isfinite(F[i]):
            last_f, last_s = F[i], sig[i]
        else:
            F[i], sig[i] = last_f, last_s
    return F, sig


def _h_min(s: np.ndarray, i0: int, ts: np.ndarray) -> float:
    left = int(np.searchsorted(ts, ts[i0] - LOOKBACK_MS, side="left"))
    if i0 - left < 2:
        return H_MIN_FALLBACK
    d = np.abs(np.diff(s[left:i0]))
    d = d[d > 0]
    if d.size == 0:
        return H_MIN_FALLBACK
    return float(max(np.min(d), H_MIN_FALLBACK))


def _find_onset(
    z: np.ndarray,
    ts: np.ndarray,
    start_ms: int,
    end_ms: int,
    *,
    z_thr: float,
    dwell_ms: int,
    gap_mask: Optional[np.ndarray] = None,
) -> Optional[int]:
    """First index with z>z_thr after ≥dwell_ms of valid time with z≤z_thr."""
    n = ts.size
    i_lo = int(np.searchsorted(ts, start_ms, side="left"))
    i_hi = int(np.searchsorted(ts, end_ms, side="left"))
    below_since: Optional[int] = None
    for i in range(i_lo, i_hi):
        if gap_mask is not None and gap_mask[i]:
            below_since = None
            continue
        if not np.isfinite(z[i]):
            below_since = None
            continue
        if z[i] <= z_thr:
            if below_since is None:
                below_since = int(ts[i])
            continue
        # z > thr
        if below_since is None:
            continue
        if int(ts[i]) - below_since < dwell_ms:
            continue
        return i
    return None


def _gap_invalid_mask(ts: np.ndarray, hole_intervals: list[tuple[int, int]]) -> np.ndarray:
    m = np.zeros(ts.size, dtype=bool)
    for a, b in hole_intervals:
        m |= (ts >= a) & (ts < b)
    # also mark ticks immediately after large Δt as post-resume (exclude as onset)
    if ts.size >= 2:
        d = np.diff(ts.astype(np.int64))
        resume = np.where(d >= INTRA_GAP_BREAK_MS)[0] + 1
        m[resume] = True
    return m


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


def _first_hit_time(
    ts: np.ndarray,
    s: np.ndarray,
    i0: int,
    *,
    pred,
    horizon_ms: int = HORIZON_MS,
) -> Optional[float]:
    t0 = int(ts[i0])
    j_hi = int(np.searchsorted(ts, t0 + horizon_ms, side="right"))
    for j in range(i0 + 1, j_hi):
        if int(ts[j]) - t0 > horizon_ms:
            break
        if pred(j, s[j], s[j - 1] if j > 0 else s[j]):
            return float(ts[j] - t0)
    return None


def _state_at_L(ts: np.ndarray, arr: np.ndarray, t0: int, L: int) -> tuple[Any, Optional[int]]:
    """Last observation with ts <= t0+L."""
    j = int(np.searchsorted(ts, t0 + L, side="right")) - 1
    if j < 0 or int(ts[j]) > t0 + L:
        return np.nan, None
    # require j at/after onset index handled by caller
    return arr[j], int(ts[j])


def _fill_at_L(ts: np.ndarray, arr: np.ndarray, t0: int, L: int, slack: float = GAP_SLACK_MS):
    j = int(np.searchsorted(ts, t0 + L, side="left"))
    if j >= ts.size:
        return np.nan, None, None
    delay = int(ts[j]) - t0
    if delay > L + slack:
        return np.nan, None, None  # reject across hole
    return arr[j], int(ts[j]), float(delay)


def build_onset_record(
    ep: Episode,
    g: pd.DataFrame,
    *,
    side: str,
    z_thr: float,
    dwell_ms: int,
    hole_intervals: list[tuple[int, int]],
    tail: str = "positive",
) -> Optional[dict[str, Any]]:
    spread_col = "spread_long" if side == "long" else "spread_short"
    g = g.sort_values("event_local_ts_ms", kind="mergesort").reset_index(drop=True)
    ts = g["event_local_ts_ms"].to_numpy(dtype=np.int64)
    s = g[spread_col].to_numpy(dtype=np.float64)
    F, sig = _rolling_floor(s, ts)
    z = (s - F) / sig
    if tail == "negative":
        z_use = -z
    else:
        z_use = z
    gap_m = _gap_invalid_mask(ts, hole_intervals)
    i0 = _find_onset(z_use, ts, ep.start_ms, ep.end_ms, z_thr=z_thr, dwell_ms=dwell_ms, gap_mask=gap_m)
    if i0 is None:
        return None
    # Exact past-only floor at onset (refine stride approximation)
    left = int(np.searchsorted(ts, ts[i0] - LOOKBACK_MS, side="left"))
    if i0 - left < MIN_PAST:
        return None
    w = s[left:i0]
    F0 = float(np.median(w))
    sig0 = max(QUIET_MAD_SCALE * float(np.median(np.abs(w - F0))), SIGMA_MIN)
    F[i0], sig[i0] = F0, sig0
    z[i0] = (s[i0] - F0) / sig0
    if tail == "negative":
        if not ((-z[i0]) > z_thr):
            return None
    else:
        if not (z[i0] > z_thr):
            return None
    # refuse onset inside/adjacent to documented holes
    t0 = int(ts[i0])
    for a, b in hole_intervals:
        if a - 1_000 <= t0 < b + 1_000:
            return None
    if gap_m[i0]:
        return None

    s0 = float(s[i0])
    x0 = s0 - F0
    if tail == "positive" and not (x0 > 0):
        return None
    if tail == "negative" and not (x0 < 0):
        return None
    hmin = _h_min(s, i0, ts)
    h0 = max(1.0 * sig0, 0.25 * abs(x0), hmin)
    flags = _leg_flags(g.iloc[i0])

    def pred_any(j, sj, _):
        return sj != s0

    def pred_cum_neg(j, sj, _):
        return (sj - s0) <= -h0

    def pred_cum_pos(j, sj, _):
        return (sj - s0) >= h0

    def pred_jump_neg(j, sj, sp):
        return (sj - sp) <= -h0

    def pred_edge_kill(j, sj, _):
        # virtual edge after frozen floor + fee diagnostic
        return (sj - F0 - C_FEE_PCT) <= 0

    # for negative tail, "collapse toward floor" is positive Δ
    if tail == "negative":
        T_cum_ret = _first_hit_time(ts, s, i0, pred=pred_cum_pos)
        T_cum_away = _first_hit_time(ts, s, i0, pred=pred_cum_neg)
        T_jump = _first_hit_time(ts, s, i0, pred=lambda j, sj, sp: (sj - sp) >= h0)
    else:
        T_cum_ret = _first_hit_time(ts, s, i0, pred=pred_cum_neg)
        T_cum_away = _first_hit_time(ts, s, i0, pred=pred_cum_pos)
        T_jump = _first_hit_time(ts, s, i0, pred=pred_jump_neg)

    T_any = _first_hit_time(ts, s, i0, pred=pred_any)
    T_edge = _first_hit_time(ts, s, i0, pred=pred_edge_kill)

    # curves on grid
    state_rows = []
    fill_rows = []
    for L in GRID_ALL:
        sv, st = _state_at_L(ts, s, t0, L)
        # state index must be >= i0
        if st is not None and st < t0:
            sv, st = np.nan, None
        fv, ft, eff = _fill_at_L(ts, s, t0, L)
        R_state = (float(sv) - F0) / x0 if np.isfinite(sv) and abs(x0) > 1e-12 else np.nan
        R_fill = (float(fv) - F0) / x0 if np.isfinite(fv) and abs(x0) > 1e-12 else np.nan
        state_rows.append(
            {
                "L": L,
                "s": float(sv) if np.isfinite(sv) else None,
                "R": float(R_state) if np.isfinite(R_state) else None,
                "eff_ts": st,
            }
        )
        fill_rows.append(
            {
                "L": L,
                "s": float(fv) if np.isfinite(fv) else None,
                "R": float(R_fill) if np.isfinite(R_fill) else None,
                "eff_latency": eff,
                "eff_minus_L": (eff - L) if eff is not None else None,
            }
        )

    # first changed leg after onset
    first_leg = None
    j_hi = int(np.searchsorted(ts, t0 + HORIZON_MS, side="right"))
    for j in range(i0 + 1, min(j_hi, len(g))):
        if float(s[j]) == s0:
            continue
        r0, r1 = g.iloc[i0], g.iloc[j]
        changes = []
        for col in ("okx_bid_price", "okx_ask_price", "bybit_bid_price", "bybit_ask_price"):
            if float(r1[col]) != float(r0[col]):
                changes.append(col)
        first_leg = {
            "delay_ms": float(ts[j] - t0),
            "changed": changes,
            "delta_s": float(s[j] - s0),
        }
        break

    # sensitivity thresholds τ for material
    sens = {}
    for kappa in KAPPA_SENS:
        for r in R_SENS:
            h = max(kappa * sig0, r * abs(x0), hmin)
            if tail == "negative":
                T = _first_hit_time(ts, s, i0, pred=lambda j, sj, _, hh=h: (sj - s0) >= hh)
            else:
                T = _first_hit_time(ts, s, i0, pred=lambda j, sj, _, hh=h: (sj - s0) <= -hh)
            sens[f"k{kappa}_r{r}"] = T

    return {
        "episode_id": ep.episode_id,
        "shock_cluster_id": ep.shock_cluster_id or ep.episode_id,
        "base_coin": ep.base_coin,
        "direction": side,
        "tail": tail,
        "z_thr": z_thr,
        "dwell_ms": dwell_ms,
        "t0_ms": t0,
        "t0_utc": _ms_iso(t0),
        "s0": s0,
        "F0": F0,
        "x0": x0,
        "sigma0": sig0,
        "z0": float(z[i0]),
        "h_min": hmin,
        "h0": h0,
        "T_any": T_any,
        "T_cum_return": T_cum_ret,  # toward floor
        "T_cum_away": T_cum_away,
        "T_jump_return": T_jump,
        "T_edge_kill": T_edge,
        "sens_T_cum_return": sens,
        "state_at_L": state_rows,
        "fill_contract_L": fill_rows,
        "first_leg_change": first_leg,
        **flags,
        "contam_flags": {
            "near_file_hole": any(a - 5000 <= t0 < b + 5000 for a, b in hole_intervals),
        },
    }


def episode_hole_intervals(ep: Episode, audit: dict[str, Any]) -> list[tuple[int, int]]:
    holes: list[tuple[int, int]] = []
    for h in audit.get("known_holes") or []:
        holes.append((parse_ts_ms(h["start"]), parse_ts_ms(h["end"])))
    for slot in audit.get("missing_5m_slots") or []:
        a = parse_ts_ms(slot)
        holes.append((a, a + FILE_INTERVAL_MS))
    for g in audit.get("intra_coin_gaps_ge_6min") or []:
        holes.append((parse_ts_ms(g["from"]), parse_ts_ms(g["to"])))
    return holes


def load_coin_window(coin: str, start_ms: int, end_ms: int, lean: Path = LEAN_TICKS) -> pd.DataFrame:
    df, _ = read_and_prepare_lean_ticks(
        lean,
        start_ms,
        end_ms,
        coins={coin.upper()},
        columns=gear2_lean_columns(check_volume=False),
        need_freshness=True,
        workers=2,
    )
    return df.loc[df["base_coin"] == coin.upper()].copy()


def select_primary_onset(
    ep: Episode,
    audit: dict[str, Any],
    lean: Path = LEAN_TICKS,
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]], dict[str, Any]]:
    """Return (primary z>4 onset, secondary z>2, meta). Direction = earliest z>4 side."""
    holes = episode_hole_intervals(ep, audit)
    pad0 = ep.start_ms - LOOKBACK_MS
    pad1 = ep.end_ms + HORIZON_MS + 5_000
    g = load_coin_window(ep.base_coin, pad0, pad1, lean)
    meta = {"n_ticks": int(len(g)), "holes": len(holes)}
    if g.empty:
        return None, None, {**meta, "error": "no_ticks"}

    cands = []
    for side in ("long", "short"):
        rec = build_onset_record(
            ep, g, side=side, z_thr=Z_PRIMARY, dwell_ms=DWELL_MS_PRIMARY, hole_intervals=holes, tail="positive"
        )
        if rec is not None:
            cands.append(rec)
    primary = min(cands, key=lambda r: r["t0_ms"]) if cands else None

    # secondary: z>2 on same side if primary exists, else earliest either side
    secondary = None
    if primary is not None:
        secondary = build_onset_record(
            ep,
            g,
            side=primary["direction"],
            z_thr=Z_SECONDARY,
            dwell_ms=DWELL_MS_PRIMARY,
            hole_intervals=holes,
            tail="positive",
        )
    else:
        c2 = []
        for side in ("long", "short"):
            rec = build_onset_record(
                ep, g, side=side, z_thr=Z_SECONDARY, dwell_ms=DWELL_MS_PRIMARY, hole_intervals=holes, tail="positive"
            )
            if rec:
                c2.append(rec)
        secondary = min(c2, key=lambda r: r["t0_ms"]) if c2 else None

    return primary, secondary, meta


def find_negative_onset(ep: Episode, audit: dict[str, Any], lean: Path = LEAN_TICKS) -> Optional[dict[str, Any]]:
    holes = episode_hole_intervals(ep, audit)
    g = load_coin_window(ep.base_coin, ep.start_ms - LOOKBACK_MS, ep.end_ms + HORIZON_MS + 5_000, lean)
    cands = []
    for side in ("long", "short"):
        rec = build_onset_record(
            ep, g, side=side, z_thr=Z_PRIMARY, dwell_ms=DWELL_MS_PRIMARY, hole_intervals=holes, tail="negative"
        )
        if rec:
            cands.append(rec)
    return min(cands, key=lambda r: r["t0_ms"]) if cands else None


def find_quiet_anchors(
    onset: dict[str, Any],
    anomaly_intervals: list[tuple[str, int, int]],
    lean: Path = LEAN_TICKS,
    n: int = QUIET_PER_ONSET,
    seed: int = QUIET_SEED,
) -> list[dict[str, Any]]:
    """Match quiet |z|<=1 same coin/side; exclude all anomaly intervals for that coin."""
    coin = onset["base_coin"]
    side = onset["direction"]
    t0 = int(onset["t0_ms"])
    # search ±12h excluding anomalies
    win0 = t0 - 12 * 3600_000
    win1 = t0 + 12 * 3600_000
    g = load_coin_window(coin, win0, win1, lean)
    if g.empty:
        return []
    spread_col = "spread_long" if side == "long" else "spread_short"
    g = g.sort_values("event_local_ts_ms").reset_index(drop=True)
    ts = g["event_local_ts_ms"].to_numpy(dtype=np.int64)
    s = g[spread_col].to_numpy(dtype=np.float64)
    # sparse floor only at 60s candidates
    cand_stride_ms = 60_000
    probe = []
    last_keep = -10**18
    for i in range(len(ts)):
        if int(ts[i]) - last_keep < cand_stride_ms:
            continue
        probe.append(i)
        last_keep = int(ts[i])
    if not probe:
        return []
    Ff, sf = _floor_at_indices(s, ts, np.asarray(probe, dtype=np.int64))
    blocked = [(a, b) for c, a, b in anomaly_intervals if c == coin]

    def in_blocked(t):
        return any(a <= t < b for a, b in blocked)

    tod0 = t0 % 86_400_000
    cands = []
    for k, i in enumerate(probe):
        if not np.isfinite(Ff[k]) or not np.isfinite(sf[k]):
            continue
        z_i = (s[i] - Ff[k]) / sf[k]
        if abs(z_i) > 1.0:
            continue
        t = int(ts[i])
        if in_blocked(t):
            continue
        if abs((t % 86_400_000) - tod0) > 3 * 3600_000:
            continue
        cands.append(i)
    if not cands:
        return []
    rng = np.random.default_rng(seed + int(hashlib.md5(onset["episode_id"].encode()).hexdigest()[:8], 16) % 10_000)
    pick = rng.choice(cands, size=min(n, len(cands)), replace=False)
    out = []
    for i in pick:
        # exact floor at quiet anchor
        left = int(np.searchsorted(ts, ts[i] - LOOKBACK_MS, side="left"))
        if i - left < MIN_PAST:
            continue
        w = s[left:i]
        F0 = float(np.median(w))
        sig0 = max(QUIET_MAD_SCALE * float(np.median(np.abs(w - F0))), SIGMA_MIN)
        s0 = float(s[i])
        x0 = s0 - F0
        z0 = x0 / sig0
        if abs(z0) > 1.0:
            continue
        hmin = _h_min(s, i, ts)
        h0 = max(1.0 * sig0, 0.25 * max(abs(x0), SIGMA_MIN), hmin)
        flags = _leg_flags(g.iloc[i])
        T_any = _first_hit_time(ts, s, i, pred=lambda j, sj, _: sj != s0)
        T_cum = _first_hit_time(ts, s, i, pred=lambda j, sj, _: (sj - s0) <= -h0)
        T_away = _first_hit_time(ts, s, i, pred=lambda j, sj, _: (sj - s0) >= h0)
        T_jump = _first_hit_time(ts, s, i, pred=lambda j, sj, sp: (sj - sp) <= -h0)
        out.append(
            {
                "episode_id": f"quiet_{onset['episode_id']}_{int(ts[i])}",
                "parent_episode_id": onset["episode_id"],
                "shock_cluster_id": f"quiet_{onset['episode_id']}_{int(ts[i])}",
                "base_coin": coin,
                "direction": side,
                "tail": "quiet",
                "t0_ms": int(ts[i]),
                "t0_utc": _ms_iso(int(ts[i])),
                "s0": s0,
                "F0": F0,
                "x0": x0,
                "sigma0": sig0,
                "z0": float(z0),
                "h0": h0,
                "T_any": T_any,
                "T_cum_return": T_cum,
                "T_cum_away": T_away,
                "T_jump_return": T_jump,
                **flags,
            }
        )
    return out


def survival_cdf(times: Sequence[Optional[float]], grid: Sequence[int] = GRID_ALL) -> dict[str, float]:
    arr = np.array([np.nan if t is None else float(t) for t in times], dtype=np.float64)
    n = arr.size
    out = {}
    for L in grid:
        if n == 0:
            out[str(L)] = np.nan
        else:
            out[str(L)] = float(np.nanmean(arr <= L))
    return out


def competing_hazards(
    T_ret: Sequence[Optional[float]],
    T_away: Sequence[Optional[float]],
    grid: Sequence[int] = GRID_PRIMARY,
) -> dict[str, dict[str, float]]:
    """Discrete 10ms competing-risk hazards."""
    tr = np.array([np.inf if t is None else float(t) for t in T_ret], dtype=np.float64)
    ta = np.array([np.inf if t is None else float(t) for t in T_away], dtype=np.float64)
    h_m, h_p = {}, {}
    for u in grid:
        at_risk = (tr >= u) & (ta >= u)
        n_risk = int(at_risk.sum())
        if n_risk == 0:
            h_m[str(u)] = np.nan
            h_p[str(u)] = np.nan
            continue
        h_m[str(u)] = float(((tr >= u) & (tr < u + 10) & at_risk).sum() / n_risk)
        h_p[str(u)] = float(((ta >= u) & (ta < u + 10) & at_risk).sum() / n_risk)
    return {"h_material_collapse": h_m, "h_material_continuation": h_p}


def estimate_tau_star(h_collapse: dict[str, float], candidates: Sequence[int] = TAU_CANDIDATES) -> Optional[int]:
    """τ* = argmax discrete hazard jump h(τ)-h(τ-10) on candidates."""
    best_t, best_j = None, -np.inf
    for tau in candidates:
        cur = h_collapse.get(str(tau), np.nan)
        prev = h_collapse.get(str(tau - 10), np.nan)
        if not (np.isfinite(cur) and np.isfinite(prev)):
            continue
        jump = cur - prev
        if jump > best_j:
            best_j, best_t = jump, tau
    return best_t


def fit_compare_models(
    events: list[dict[str, Any]],
    tau: int,
) -> dict[str, float]:
    """Held-out style scores on discrete material hazard indicators (in-sample proxy when called on fold)."""
    # Build per-episode Bernoulli outcomes y_u = 1{T in [u,u+10)} | at risk
    us = list(GRID_PRIMARY)
    ys = []
    Xs_smooth = []
    Xs_cp = []
    for ev in events:
        tr = ev.get("T_cum_return")
        ta = ev.get("T_cum_away")
        trv = np.inf if tr is None else float(tr)
        tav = np.inf if ta is None else float(ta)
        for u in us:
            if not (trv >= u and tav >= u):
                continue
            y = 1.0 if (u <= trv < u + 10) else 0.0
            ys.append(y)
            Xs_smooth.append([1.0, float(u)])
            Xs_cp.append([1.0, float(u), max(0.0, float(u - tau))])
    if len(ys) < 20:
        return {"n": float(len(ys)), "ll_smooth": np.nan, "ll_cp": np.nan, "brier_smooth": np.nan, "brier_cp": np.nan}
    y = np.asarray(ys)
    Xs = np.asarray(Xs_smooth)
    Xc = np.asarray(Xs_cp)

    def _logit_fit(X, y, l2=1e-2):
        # IRLS-lite with ridge
        beta = np.zeros(X.shape[1])
        for _ in range(25):
            eta = X @ beta
            p = 1 / (1 + np.exp(-np.clip(eta, -20, 20)))
            w = p * (1 - p) + 1e-6
            z = eta + (y - p) / w
            W = np.diag(w)
            A = X.T @ W @ X + l2 * np.eye(X.shape[1])
            b = X.T @ W @ z
            beta = np.linalg.solve(A, b)
        p = 1 / (1 + np.exp(-np.clip(X @ beta, -20, 20)))
        return p

    ps = _logit_fit(Xs, y)
    pc = _logit_fit(Xc, y)
    eps = 1e-6
    ll_s = float(np.sum(y * np.log(ps + eps) + (1 - y) * np.log(1 - ps + eps)))
    ll_c = float(np.sum(y * np.log(pc + eps) + (1 - y) * np.log(1 - pc + eps)))
    return {
        "n": float(len(y)),
        "ll_smooth": ll_s,
        "ll_cp": ll_c,
        "delta_ll": ll_c - ll_s,
        "brier_smooth": float(np.mean((ps - y) ** 2)),
        "brier_cp": float(np.mean((pc - y) ** 2)),
    }


def blocked_time_folds(onsets: list[dict[str, Any]], k: int = KFOLD) -> list[list[int]]:
    """Assign shock clusters to folds by sorted first onset time."""
    clusters: dict[str, int] = {}
    for ev in onsets:
        cid = ev["shock_cluster_id"]
        clusters[cid] = min(clusters.get(cid, ev["t0_ms"]), ev["t0_ms"])
    ordered = sorted(clusters.items(), key=lambda kv: kv[1])
    folds = [[] for _ in range(k)]
    for i, (cid, _) in enumerate(ordered):
        folds[i % k].append(cid)
    # map to indices
    out = [[] for _ in range(k)]
    for i, ev in enumerate(onsets):
        for f, cids in enumerate(folds):
            if ev["shock_cluster_id"] in cids:
                out[f].append(i)
    return out


def resolution_audit(onsets: list[dict[str, Any]]) -> dict[str, Any]:
    # use fill_contract effective latencies and state uniqueness
    deltas = []
    same_fill_neighbor = 0
    neigh_tot = 0
    eff_minus = []
    unique_states = []
    for ev in onsets:
        fills = {r["L"]: r for r in ev.get("fill_contract_L") or []}
        states = {r["L"]: r for r in ev.get("state_at_L") or []}
        # approx inter-update from T_any distribution separately
        Ls = sorted(fills)
        prev_s = None
        uniq = set()
        for L in Ls:
            fr = fills[L]
            if fr.get("eff_latency") is not None:
                eff_minus.append(fr["eff_minus_L"])
            if fr.get("s") is not None:
                uniq.add(round(fr["s"], 10))
            if prev_s is not None and L - 10 in fills:
                neigh_tot += 1
                if fr.get("s") == fills[L - 10].get("s"):
                    same_fill_neighbor += 1
            prev_s = fr.get("s")
        unique_states.append(len(uniq))
        if ev.get("T_any") is not None:
            deltas.append(ev["T_any"])
    return {
        "n_onsets": len(onsets),
        "T_any_p50": float(np.nanmedian(deltas)) if deltas else None,
        "T_any_p90": float(np.nanpercentile(deltas, 90)) if deltas else None,
        "frac_neighbor_L_same_fill": (same_fill_neighbor / neigh_tot) if neigh_tot else None,
        "eff_minus_L_p50": float(np.nanmedian(eff_minus)) if eff_minus else None,
        "eff_minus_L_p90": float(np.nanpercentile(eff_minus, 90)) if eff_minus else None,
        "unique_fill_states_mean": float(np.mean(unique_states)) if unique_states else None,
        "grid_10ms_note": (
            "If frac_neighbor_L_same_fill high and T_any >> 10ms, limit τ* precision to real cadence."
        ),
    }


def episode_bootstrap_ci(
    onsets: list[dict[str, Any]],
    *,
    n_boot: int = BOOT_N,
    seed: int = BOOT_SEED,
) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    n = len(onsets)
    if n == 0:
        return {"n": 0}
    haz_list = []
    taus = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, size=n)
        sample = [onsets[i] for i in idx]
        haz = competing_hazards(
            [e.get("T_cum_return") for e in sample],
            [e.get("T_cum_away") for e in sample],
        )
        haz_list.append(haz["h_material_collapse"])
        taus.append(estimate_tau_star(haz["h_material_collapse"]))
    # CI for hazards at highlight L
    out = {"n_units": n, "n_boot": n_boot}
    for L in GRID_HIGHLIGHT:
        vals = [h.get(str(L), np.nan) for h in haz_list]
        vals = [v for v in vals if np.isfinite(v)]
        if vals:
            out[f"h_collapse_{L}_p025"] = float(np.percentile(vals, 2.5))
            out[f"h_collapse_{L}_p975"] = float(np.percentile(vals, 97.5))
            out[f"h_collapse_{L}_mean"] = float(np.mean(vals))
    tau_v = [t for t in taus if t is not None]
    out["tau_star_mode"] = int(pd.Series(tau_v).mode().iloc[0]) if tau_v else None
    out["tau_star_p025"] = float(np.percentile(tau_v, 2.5)) if tau_v else None
    out["tau_star_p975"] = float(np.percentile(tau_v, 97.5)) if tau_v else None
    out["tau_star_unique"] = sorted(set(tau_v))
    return out


def leave_one_out_tau(onsets: list[dict[str, Any]], key: str = "episode_id") -> dict[str, Any]:
    base_h = competing_hazards(
        [e.get("T_cum_return") for e in onsets],
        [e.get("T_cum_away") for e in onsets],
    )
    base_tau = estimate_tau_star(base_h["h_material_collapse"])
    units = sorted({e[key] for e in onsets})
    taus = []
    for u in units:
        sub = [e for e in onsets if e[key] != u]
        if len(sub) < 3:
            continue
        h = competing_hazards([e.get("T_cum_return") for e in sub], [e.get("T_cum_away") for e in sub])
        taus.append({"left_out": u, "tau": estimate_tau_star(h["h_material_collapse"]), "n": len(sub)})
    return {"base_tau": base_tau, "loo": taus}


def run_kfold(onsets: list[dict[str, Any]]) -> dict[str, Any]:
    folds = blocked_time_folds(onsets, KFOLD)
    results = []
    wins = 0
    sign_agree = 0
    taus = []
    for hold in range(KFOLD):
        test_idx = set(folds[hold])
        train = [onsets[i] for i in range(len(onsets)) if i not in test_idx]
        test = [onsets[i] for i in folds[hold]]
        if len(train) < 3 or len(test) < 1:
            results.append({"fold": hold, "status": "underpowered", "n_train": len(train), "n_test": len(test)})
            continue
        h_tr = competing_hazards([e.get("T_cum_return") for e in train], [e.get("T_cum_away") for e in train])
        tau = estimate_tau_star(h_tr["h_material_collapse"])
        # pick best tau by train ll among candidates
        best_tau, best_dll = tau, -np.inf
        for t_cand in TAU_CANDIDATES:
            sc = fit_compare_models(train, t_cand)
            dll = sc.get("delta_ll", np.nan)
            if np.isfinite(dll) and dll > best_dll:
                best_dll, best_tau = dll, t_cand
        tau = best_tau if best_tau is not None else tau
        sc_te = fit_compare_models(test, tau or 100)
        h_te = competing_hazards([e.get("T_cum_return") for e in test], [e.get("T_cum_away") for e in test])
        jump = np.nan
        if tau is not None:
            jump = h_te["h_material_collapse"].get(str(tau), np.nan) - h_te["h_material_collapse"].get(
                str(tau - 10), np.nan
            )
        cp_better = bool(np.isfinite(sc_te.get("delta_ll", np.nan)) and sc_te["delta_ll"] > 0)
        if cp_better:
            wins += 1
        if np.isfinite(jump) and jump > 0:
            sign_agree += 1
        taus.append(tau)
        results.append(
            {
                "fold": hold,
                "n_train": len(train),
                "n_test": len(test),
                "tau_star": tau,
                "heldout": sc_te,
                "jump_at_tau": float(jump) if np.isfinite(jump) else None,
                "cp_better_heldout": cp_better,
            }
        )
    return {
        "folds": results,
        "n_folds_cp_better": wins,
        "n_folds_positive_jump": sign_agree,
        "tau_stars": taus,
    }


def verdict_from_results(payload: dict[str, Any]) -> dict[str, Any]:
    kfold = payload.get("kfold") or {}
    res = payload.get("resolution") or {}
    loo = payload.get("loo_episode") or {}
    legs = payload.get("leg_panels") or {}
    quiet = payload.get("quiet_hazards") or {}
    neg = payload.get("negative_hazards") or {}
    primary_h = payload.get("hazards") or {}

    reasons = []
    label = "underpowered"

    n = payload.get("power_gate", {}).get("n_primary_onsets_z4", 0) or payload.get("power_gate", {}).get("n_primary_onsets", 0)
    if n < 8:
        reasons.append(f"only {n} primary onsets; K=5 held-out weak")
        label = "underpowered"

    wins = kfold.get("n_folds_cp_better", 0)
    jumps = kfold.get("n_folds_positive_jump", 0)
    taus = [t for t in (kfold.get("tau_stars") or []) if t is not None]
    tau_span = (max(taus) - min(taus)) if len(taus) >= 2 else None

    frac_same = res.get("frac_neighbor_L_same_fill")
    if frac_same is not None and frac_same > 0.7:
        reasons.append("neighbor L often share same fill state → cadence-limited")
        label = "quote-update cadence artifact"

    # fresh_both persistence
    fb = legs.get("fresh_both", {})
    if fb.get("n", 0) >= 3:
        if fb.get("tau_star") is None and primary_h.get("tau_star") is not None:
            reasons.append("τ* disappears on fresh_both")
            label = "stale-leg catch-up"

    # quiet coincidence
    if quiet.get("tau_star") is not None and primary_h.get("tau_star") == quiet.get("tau_star"):
        reasons.append("τ* matches quiet-control τ*")
        label = "quote-update cadence artifact"

    # negative symmetry
    if neg.get("tau_star") is not None and primary_h.get("tau_star") is not None:
        if abs(int(neg["tau_star"]) - int(primary_h["tau_star"])) <= 20:
            reasons.append("negative-tail τ* close to positive → symmetric MR")
            if label not in ("underpowered", "quote-update cadence artifact"):
                label = "symmetric mean reversion"

    loo_taus = [x["tau"] for x in loo.get("loo", []) if x.get("tau") is not None]
    if loo_taus and loo.get("base_tau") is not None:
        if len(set(loo_taus)) >= max(3, len(loo_taus) // 2):
            reasons.append("leave-one-episode τ* unstable")
            label = "heterogeneous latency across episodes/coins"

    success_flags = {
        "kfold_cp_ge4": wins >= 4,
        "kfold_jump_sign": jumps >= 4,
        "tau_concentrated": tau_span is not None and tau_span <= 60,
        "state_not_only_fill": payload.get("state_vs_fill", {}).get("state_has_tau", False),
        "fresh_both_ok": legs.get("fresh_both", {}).get("tau_star") is not None,
        "loo_stable": bool(loo_taus) and len(set(loo_taus)) <= 3,
        "not_quiet_artifact": primary_h.get("tau_star") != quiet.get("tau_star"),
        "sens_h_stable": payload.get("sens_tau_stable", False),
        "continuation_no_jump": payload.get("continuation_no_symmetric_jump", False),
    }
    if all(success_flags.values()):
        label = "clear material latency change-point"
        reasons.append("all success criteria met")
    elif wins < 4 and n >= 8:
        if label == "underpowered":
            label = "smooth decay, no single latency"
        reasons.append(f"change-point held-out wins={wins}/5")

    return {
        "verdict": label,
        "status": "unclassified anomaly mixture",
        "success_flags": success_flags,
        "reasons": reasons,
        "disclaimer": (
            "Observational L1 response after local anomaly detection only; "
            "does not identify competitor algorithm latency."
        ),
    }


def run_all(out_dir: Path = OUT_DIR, lean: Path = LEAN_TICKS) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    episodes = freeze_manifest(out_dir / "manifest.json")
    print(f"manifest n={len(episodes)} coins={len({e.base_coin for e in episodes})}", flush=True)

    audits = []
    for ep in episodes:
        print(f"audit {ep.episode_id}...", flush=True)
        audits.append(audit_episode_artifacts(ep, lean))
    (out_dir / "artifact_audit.json").write_text(json.dumps(audits, indent=2), encoding="utf-8")
    audit_by_id = {a["episode_id"]: a for a in audits}

    anomaly_intervals = [(e.base_coin, e.start_ms, e.end_ms) for e in episodes]

    primaries: list[dict[str, Any]] = []
    secondaries: list[dict[str, Any]] = []
    negatives: list[dict[str, Any]] = []
    quiets: list[dict[str, Any]] = []
    skipped = []

    for ep in episodes:
        aud = audit_by_id[ep.episode_id]
        print(f"onset {ep.episode_id} status={aud.get('status')}...", flush=True)
        # still attempt onset but holes mask invalid times
        p, s, meta = select_primary_onset(ep, aud, lean)
        if p is None:
            skipped.append({"episode_id": ep.episode_id, "reason": "no_z4_onset", "meta": meta, "audit": aud.get("status")})
        else:
            p["audit_status"] = aud.get("status")
            primaries.append(p)
            print(f"  primary {p['direction']} t0={p['t0_utc']} z0={p['z0']:.2f} T_cum={p['T_cum_return']}", flush=True)
            qs = find_quiet_anchors(p, anomaly_intervals, lean)
            quiets.extend(qs)
        if s is not None:
            secondaries.append(s)
        neg = find_negative_onset(ep, aud, lean)
        if neg is not None:
            negatives.append(neg)

    pd.DataFrame(
        [{k: v for k, v in r.items() if k not in ("state_at_L", "fill_contract_L", "sens_T_cum_return", "first_leg_change")} for r in primaries]
    ).to_parquet(out_dir / "onsets_primary.parquet", index=False)
    (out_dir / "onsets_primary.json").write_text(json.dumps(primaries, indent=2, default=str), encoding="utf-8")
    (out_dir / "onsets_secondary.json").write_text(json.dumps(secondaries, indent=2, default=str), encoding="utf-8")
    (out_dir / "onsets_negative.json").write_text(json.dumps(negatives, indent=2, default=str), encoding="utf-8")
    (out_dir / "quiet_controls.json").write_text(json.dumps(quiets, indent=2, default=str), encoding="utf-8")
    (out_dir / "skipped.json").write_text(json.dumps(skipped, indent=2, default=str), encoding="utf-8")

    power = {
        "n_manifest_episodes": len(episodes),
        "n_coins": len({e.base_coin for e in episodes}),
        "episode_class": "unclassified anomaly mixture",
        "n_isolated": None,
        "n_common_shock": None,
        "n_shock_clusters": len({p["shock_cluster_id"] for p in primaries}),
        "n_primary_onsets_z4": len(primaries),
        "n_secondary_onsets_z2": len(secondaries),
        "n_negative_onsets_z4": len(negatives),
        "n_quiet_controls": len(quiets),
        "n_skipped": len(skipped),
        "contaminated_episodes": [a["episode_id"] for a in audits if a.get("contaminated")],
        "note": "No isolated/common_shock labels; do not inflate N from same shock.",
    }
    (out_dir / "power_gate.json").write_text(json.dumps(power, indent=2), encoding="utf-8")
    print("power_gate", power, flush=True)

    res = resolution_audit(primaries)
    (out_dir / "resolution_audit.json").write_text(json.dumps(res, indent=2), encoding="utf-8")

    def pack_haz(events, name):
        if not events:
            return {"name": name, "n": 0}
        cdf_any = survival_cdf([e.get("T_any") for e in events])
        cdf_cum = survival_cdf([e.get("T_cum_return") for e in events])
        cdf_jump = survival_cdf([e.get("T_jump_return") for e in events])
        cdf_away = survival_cdf([e.get("T_cum_away") for e in events])
        cdf_edge = survival_cdf([e.get("T_edge_kill") for e in events])
        haz = competing_hazards([e.get("T_cum_return") for e in events], [e.get("T_cum_away") for e in events])
        tau = estimate_tau_star(haz["h_material_collapse"])
        return {
            "name": name,
            "n": len(events),
            "cdf_any": cdf_any,
            "cdf_cum_return": cdf_cum,
            "cdf_jump_return": cdf_jump,
            "cdf_cum_away": cdf_away,
            "cdf_edge_kill": cdf_edge,
            "hazards": haz,
            "tau_star": tau,
        }

    haz_primary = pack_haz(primaries, "primary_positive_z4")
    haz_quiet = pack_haz(quiets, "quiet_controls")
    haz_neg = pack_haz(negatives, "negative_z4")
    (out_dir / "hazards_primary.json").write_text(json.dumps(haz_primary, indent=2), encoding="utf-8")
    (out_dir / "hazards_quiet.json").write_text(json.dumps(haz_quiet, indent=2), encoding="utf-8")
    (out_dir / "hazards_negative.json").write_text(json.dumps(haz_neg, indent=2), encoding="utf-8")

    # state vs fill τ*
    def tau_from_state_curve(events, key="state_at_L"):
        # proxy: material collapse if R drops by h0/x0 using state s
        # Use T from events (state-based times already from path) — compare fill vs state via effective
        # Here: recompute τ* using only events where fill eff_minus_L==0-ish vs state
        return estimate_tau_star(
            competing_hazards(
                [e.get("T_cum_return") for e in events],
                [e.get("T_cum_away") for e in events],
            )["h_material_collapse"]
        )

    state_vs_fill = {
        "state_has_tau": haz_primary.get("tau_star") is not None,
        "note": "T_* computed on state path (tick stream); fill_contract used for resolution/eff latency",
        "tau_state_path": haz_primary.get("tau_star"),
    }

    # leg panels
    leg_panels = {}
    for panel, pred in [
        ("fresh_both", lambda e: e.get("fresh_both") is True),
        ("stale_okx", lambda e: e.get("stale_okx") is True),
        ("stale_bybit", lambda e: e.get("stale_bybit") is True),
        ("trigger_okx", lambda e: e.get("trigger") == "okx"),
        ("trigger_bybit", lambda e: e.get("trigger") == "bybit"),
    ]:
        sub = [e for e in primaries if pred(e)]
        leg_panels[panel] = pack_haz(sub, panel)
    (out_dir / "leg_panels.json").write_text(json.dumps(leg_panels, indent=2), encoding="utf-8")

    # sensitivity of τ* across h(κ,r)
    sens_taus = {}
    for kappa in KAPPA_SENS:
        for r in R_SENS:
            key = f"k{kappa}_r{r}"
            times = [e.get("sens_T_cum_return", {}).get(key) for e in primaries]
            away = [e.get("T_cum_away") for e in primaries]
            h = competing_hazards(times, away)
            sens_taus[key] = estimate_tau_star(h["h_material_collapse"])
    sens_vals = [v for v in sens_taus.values() if v is not None]
    sens_stable = bool(sens_vals) and (max(sens_vals) - min(sens_vals) <= 60)
    (out_dir / "sens_tau.json").write_text(json.dumps({"taus": sens_taus, "stable_60ms": sens_stable}, indent=2), encoding="utf-8")

    # continuation jump at primary τ*
    cont_ok = False
    if haz_primary.get("tau_star") is not None:
        tau = haz_primary["tau_star"]
        hc = haz_primary["hazards"]["h_material_collapse"]
        hp = haz_primary["hazards"]["h_material_continuation"]
        jc = hc.get(str(tau), np.nan) - hc.get(str(tau - 10), np.nan)
        jp = hp.get(str(tau), np.nan) - hp.get(str(tau - 10), np.nan)
        cont_ok = bool(np.isfinite(jc) and np.isfinite(jp) and jc > 0 and not (jp >= jc * 0.8))

    kfold = run_kfold(primaries) if len(primaries) >= 3 else {"folds": [], "n_folds_cp_better": 0, "n_folds_positive_jump": 0, "tau_stars": []}
    (out_dir / "kfold.json").write_text(json.dumps(kfold, indent=2), encoding="utf-8")

    boot = episode_bootstrap_ci(primaries)
    (out_dir / "bootstrap_episode.json").write_text(json.dumps(boot, indent=2), encoding="utf-8")
    loo_ep = leave_one_out_tau(primaries, "episode_id")
    loo_coin = leave_one_out_tau(primaries, "base_coin")
    loo_sh = leave_one_out_tau(primaries, "shock_cluster_id")
    (out_dir / "loo.json").write_text(
        json.dumps({"episode": loo_ep, "coin": loo_coin, "shock_cluster": loo_sh}, indent=2),
        encoding="utf-8",
    )

    payload = {
        "power_gate": power,
        "resolution": res,
        "hazards": haz_primary,
        "quiet_hazards": haz_quiet,
        "negative_hazards": haz_neg,
        "leg_panels": leg_panels,
        "kfold": kfold,
        "bootstrap": boot,
        "loo_episode": loo_ep,
        "state_vs_fill": state_vs_fill,
        "sens_tau_stable": sens_stable,
        "sens_taus": sens_taus,
        "continuation_no_symmetric_jump": cont_ok,
    }
    verd = verdict_from_results(payload)
    (out_dir / "verdict.json").write_text(json.dumps(verd, indent=2), encoding="utf-8")
    (out_dir / "verdict.txt").write_text(
        verd["verdict"] + "\n" + "\n".join(verd["reasons"]) + "\n" + verd["disclaimer"] + "\n",
        encoding="utf-8",
    )
    print("VERDICT:", verd["verdict"], flush=True)
    return {"power": power, "hazards": haz_primary, "kfold": kfold, "verdict": verd, "resolution": res}


if __name__ == "__main__":
    run_all()
