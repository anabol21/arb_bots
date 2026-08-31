"""Anomaly-onset detector core (time-weighted, tick-exact, O(n log U)).

Pipeline per (coin, direction) on the directional spread ``s(t)``:

1. Floor band ``F25/F50/F75`` from time-weighted quantiles over the trailing
   ``H_floor`` window, refreshed on a bounded cadence (step function, causal).
   Robust scale ``sigma = (F75 - F25) / 1.349`` (normal-consistent IQR scale).
   Every tick in the window enters the statistic; the cadence only bounds how
   often the slow floor is recomputed (this is NOT downsampling/smoothing).
2. Amplitude deviation above the corridor top ``F75``:
       z+(t) = max(0, s - F75) / sigma      (normalized)
       a+(t) = max(0, s - F75)              (absolute, percent)
3. Time-weighted sliding-window (``W``) metrics, tick-exact (dwell-weighted):
       O_W(t) = (1/W) ∫ 1[z+ >= z_base] du       (occupancy)
       I_W(t) = (1/W) ∫ [z+ - z_base]_+   du       (integral excursion area)
   for both the normalized and absolute variable.
4. Thresholds = high time-weighted quantiles (default 0.99) of each metric over
   the trailing ``H_floor`` quiet reference: ``z_base``/``a_base`` (amplitude),
   ``O_min``/``I_min`` (occupancy / area). Quiet is the DEFAULT regime.
5. Onset fires when the three metrics of the selected variable space exceed their
   thresholds simultaneously.

Performance: trailing time-weighted quantiles use a Fenwick tree over value
ranks and advance monotone window pointers across refresh times, so each tick is
inserted/removed once — O((n + refreshes) log U) per series, no per-refresh sort.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from research.anomaly_onset.twstats import weighted_quantile

__all__ = [
    "FloorParams",
    "MetricParams",
    "ThresholdParams",
    "DetectParams",
    "DEFAULT_SPREAD_LEVELS",
    "DEFAULT_METRIC_LEVELS",
    "rolling_tw_quantiles",
    "rolling_tw_sum",
    "analyze",
    "episodes_from_fire",
]

DEFAULT_SPREAD_LEVELS: tuple[float, ...] = (0.25, 0.5, 0.75, 0.90, 0.95, 0.99)
DEFAULT_METRIC_LEVELS: tuple[float, ...] = (0.90, 0.95, 0.99)
_IQR_TO_SIGMA = 1.349  # normal: IQR = 1.349 * sigma


@dataclass(frozen=True)
class FloorParams:
    H_floor_ms: int = 12 * 3600_000        # trailing quiet history for the floor
    refresh_ms: int = 60_000               # floor/threshold recompute cadence (not smoothing)
    q_lo: float = 0.25
    q_mid: float = 0.50
    q_hi: float = 0.75
    min_cover_ms: int = 30 * 60_000        # min time-weighted coverage to be "warm"
    sigma_eps: float = 1e-9


@dataclass(frozen=True)
class MetricParams:
    W_ms: int = 30 * 60_000                # integral / occupancy window


@dataclass(frozen=True)
class ThresholdParams:
    q_det: float = 0.99                    # universal quantile level; per-coin numeric threshold
    q_amp: Optional[float] = None          # override for amplitude z_base/a_base
    q_occ: Optional[float] = None          # override for O_min
    q_area: Optional[float] = None         # override for I_min

    def amp(self) -> float:
        return self.q_amp if self.q_amp is not None else self.q_det

    def occ(self) -> float:
        return self.q_occ if self.q_occ is not None else self.q_det

    def area(self) -> float:
        return self.q_area if self.q_area is not None else self.q_det


@dataclass(frozen=True)
class DetectParams:
    combine: str = "or"                    # norm_only | abs_only | and | or
    cooldown_ms: int = 5 * 60_000          # fire must stay off this long to end an episode
    merge_gap_ms: int = 10 * 60_000        # merge episodes separated by less than this
    with_viz_quantiles: bool = True        # also compute 90/95/99 metric traces for plots
    metric_levels: tuple[float, ...] = DEFAULT_METRIC_LEVELS
    spread_levels: tuple[float, ...] = DEFAULT_SPREAD_LEVELS


# --------------------------------------------------------------------------- #
# Fenwick (Binary Indexed Tree) for weighted order statistics over value ranks
# --------------------------------------------------------------------------- #
class _Fenwick:
    __slots__ = ("n", "tree", "tot")

    def __init__(self, n: int):
        self.n = n
        self.tree = np.zeros(n + 1, dtype=np.float64)
        self.tot = 0.0

    def add(self, i: int, w: float) -> None:  # i is 0-based rank
        self.tot += w
        i += 1
        t = self.tree
        while i <= self.n:
            t[i] += w
            i += i & (-i)

    def total(self) -> float:
        return self.tot

    def find(self, target: float) -> int:
        """Smallest 0-based rank whose prefix weight sum >= target."""
        pos = 0
        rem = target
        t = self.tree
        log = self.n.bit_length()
        for k in range(log, -1, -1):
            nxt = pos + (1 << k)
            if nxt <= self.n and t[nxt] < rem:
                pos = nxt
                rem -= t[nxt]
        return min(pos, self.n - 1)


def rolling_tw_quantiles(
    ts: np.ndarray,
    values: np.ndarray,
    weights: np.ndarray,
    *,
    H_ms: int,
    refresh_ms: int,
    levels,
    min_cover_ms: int,
) -> np.ndarray:
    """Causal trailing time-weighted quantiles on a bounded refresh cadence.

    Returns ``(len(ts), len(levels))``: each tick gets the quantiles from the most
    recent refresh whose window ``[tau-H, tau)`` lies strictly before it. Rows
    without enough trailing time coverage are ``nan``. NaN values are ignored.
    """
    ts = np.asarray(ts, dtype=np.int64)
    values = np.asarray(values, dtype=np.float64)
    weights = np.asarray(weights, dtype=np.float64)
    levels = np.atleast_1d(np.asarray(levels, dtype=np.float64))
    n = len(ts)
    L = len(levels)
    if n == 0:
        return np.empty((0, L), dtype=np.float64)

    finite = np.isfinite(values)
    sv = np.unique(values[finite]) if finite.any() else np.array([0.0])
    ranks = np.full(n, -1, dtype=np.int64)
    if finite.any():
        ranks[finite] = np.searchsorted(sv, values[finite])

    taus = np.arange(int(ts[0]), int(ts[-1]) + refresh_ms, refresh_ms, dtype=np.int64)
    q_at_tau = np.full((len(taus), L), np.nan, dtype=np.float64)

    fen = _Fenwick(len(sv))
    right = 0   # next tick to add (include ts < tau)
    left = 0    # next tick to remove (drop ts < tau-H)
    for k, tau in enumerate(taus):
        lo = tau - H_ms
        while right < n and ts[right] < tau:
            if ranks[right] >= 0:
                fen.add(int(ranks[right]), float(weights[right]))
            right += 1
        while left < right and ts[left] < lo:
            if ranks[left] >= 0:
                fen.add(int(ranks[left]), -float(weights[left]))
            left += 1
        tot = fen.total()
        if tot < min_cover_ms or tot <= 0:
            continue
        for j in range(L):
            r = fen.find(levels[j] * tot)
            q_at_tau[k, j] = sv[r]

    idx = np.clip(np.searchsorted(taus, ts, side="right") - 1, 0, len(taus) - 1)
    return q_at_tau[idx]


def rolling_tw_sum(ts: np.ndarray, contrib: np.ndarray, W_ms: int) -> np.ndarray:
    """Time-weighted sliding-window sum over ``[ts_i - W, ts_i]`` divided by ``W``.

    ``contrib`` already embeds dwell weights (e.g. ``dwell * indicator``). O(n)
    two-pointer; windows never span more than ``W`` of wall-clock time.
    """
    ts = np.asarray(ts, dtype=np.int64)
    c = np.nan_to_num(np.asarray(contrib, dtype=np.float64), nan=0.0)
    n = len(ts)
    out = np.zeros(n, dtype=np.float64)
    left = 0
    run = 0.0
    for i in range(n):
        run += c[i]
        lo = ts[i] - W_ms
        while ts[left] < lo:
            run -= c[left]
            left += 1
        out[i] = run
    return out / float(W_ms)


def analyze(
    df_coin: pd.DataFrame,
    direction: str,
    *,
    floor: FloorParams = FloorParams(),
    metric: MetricParams = MetricParams(),
    thr: ThresholdParams = ThresholdParams(),
    detect: DetectParams = DetectParams(),
) -> pd.DataFrame:
    """Per-tick detector frame for one coin and one direction."""
    scol = "spread_long" if direction == "long" else "spread_short"
    g = df_coin.sort_values("event_local_ts_ms", kind="mergesort")
    ts = g["event_local_ts_ms"].to_numpy(dtype=np.int64)
    s = g[scol].to_numpy(dtype=np.float64)
    w = g["dwell_ms"].to_numpy(dtype=np.float64)
    n = len(ts)

    out = pd.DataFrame(
        {
            "event_local_ts_ms": ts,
            "event_dt": pd.to_datetime(ts, unit="ms", utc=True),
            "spread": s,
            "dwell_ms": w,
        }
    )
    if n == 0:
        return out

    # 1) floor band (+ IQR robust scale) ----------------------------------------
    spread_levels = tuple(detect.spread_levels)
    need_levels = sorted(set(spread_levels) | {floor.q_lo, floor.q_mid, floor.q_hi})
    fq = rolling_tw_quantiles(
        ts, s, w,
        H_ms=floor.H_floor_ms, refresh_ms=floor.refresh_ms,
        levels=need_levels, min_cover_ms=floor.min_cover_ms,
    )
    lvl_idx = {lv: i for i, lv in enumerate(need_levels)}
    F25 = fq[:, lvl_idx[floor.q_lo]]
    F50 = fq[:, lvl_idx[floor.q_mid]]
    F75 = fq[:, lvl_idx[floor.q_hi]]
    for lv in spread_levels:
        out[f"spread_q{int(round(lv*100))}"] = fq[:, lvl_idx[lv]]

    sigma = (F75 - F25) / _IQR_TO_SIGMA
    warm = np.isfinite(F75) & np.isfinite(sigma) & (sigma > floor.sigma_eps)
    out["F25"], out["F50"], out["F75"], out["sigma"], out["warm"] = F25, F50, F75, sigma, warm

    # 2) amplitude deviation above corridor top ---------------------------------
    excess = np.where(warm, np.maximum(0.0, s - F75), np.nan)
    zplus = np.where(warm, excess / np.where(sigma > 0, sigma, np.nan), np.nan)
    aplus = excess
    out["zplus"], out["aplus"] = zplus, aplus

    # 3) amplitude thresholds z_base / a_base (quiet quantile) -------------------
    z_base = rolling_tw_quantiles(
        ts, np.where(warm, np.nan_to_num(zplus, nan=0.0), np.nan), w,
        H_ms=floor.H_floor_ms, refresh_ms=floor.refresh_ms,
        levels=[thr.amp()], min_cover_ms=floor.min_cover_ms,
    )[:, 0]
    a_base = rolling_tw_quantiles(
        ts, np.where(warm, np.nan_to_num(aplus, nan=0.0), np.nan), w,
        H_ms=floor.H_floor_ms, refresh_ms=floor.refresh_ms,
        levels=[thr.amp()], min_cover_ms=floor.min_cover_ms,
    )[:, 0]
    out["z_base"], out["a_base"] = z_base, a_base

    # 4) occupancy + integral area (tick-exact, dwell-weighted) ------------------
    zp0 = np.nan_to_num(zplus, nan=0.0)
    ap0 = np.nan_to_num(aplus, nan=0.0)
    zb0 = np.nan_to_num(z_base, nan=np.inf)
    ab0 = np.nan_to_num(a_base, nan=np.inf)

    occ_norm = rolling_tw_sum(ts, w * (zp0 >= zb0), metric.W_ms)
    area_norm = rolling_tw_sum(ts, w * np.maximum(0.0, zp0 - zb0), metric.W_ms)
    occ_abs = rolling_tw_sum(ts, w * (ap0 >= ab0), metric.W_ms)
    area_abs = rolling_tw_sum(ts, w * np.maximum(0.0, ap0 - ab0), metric.W_ms)
    out["O_norm"], out["I_norm"] = occ_norm, area_norm
    out["O_abs"], out["I_abs"] = occ_abs, area_abs

    # time-weighted moving average of the spread over W (for the MA plot)
    _wsum = rolling_tw_sum(ts, w, metric.W_ms)
    _ws = rolling_tw_sum(ts, w * s, metric.W_ms)
    out["spread_ma"] = np.where(_wsum > 0, _ws / np.where(_wsum > 0, _wsum, np.nan), np.nan)

    # 5) occupancy / area thresholds (quiet quantile) ----------------------------
    O_min_norm = _tw_thr(ts, occ_norm, w, thr.occ(), floor)
    I_min_norm = _tw_thr(ts, area_norm, w, thr.area(), floor)
    O_min_abs = _tw_thr(ts, occ_abs, w, thr.occ(), floor)
    I_min_abs = _tw_thr(ts, area_abs, w, thr.area(), floor)
    out["O_min_norm"], out["I_min_norm"] = O_min_norm, I_min_norm
    out["O_min_abs"], out["I_min_abs"] = O_min_abs, I_min_abs

    if detect.with_viz_quantiles:
        for name, series in (("zplus", zp0), ("O_norm", occ_norm), ("I_norm", area_norm),
                             ("aplus", ap0), ("O_abs", occ_abs), ("I_abs", area_abs)):
            tq = rolling_tw_quantiles(
                ts, series, w,
                H_ms=floor.H_floor_ms, refresh_ms=floor.refresh_ms,
                levels=detect.metric_levels, min_cover_ms=floor.min_cover_ms,
            )
            for j, lv in enumerate(detect.metric_levels):
                out[f"{name}_q{int(round(lv*100))}"] = tq[:, j]

    # 6) onset condition ---------------------------------------------------------
    cond_norm = warm & (zp0 >= zb0) & (occ_norm >= np.nan_to_num(O_min_norm, nan=np.inf)) & (
        area_norm >= np.nan_to_num(I_min_norm, nan=np.inf)
    )
    cond_abs = warm & (ap0 >= ab0) & (occ_abs >= np.nan_to_num(O_min_abs, nan=np.inf)) & (
        area_abs >= np.nan_to_num(I_min_abs, nan=np.inf)
    )
    out["cond_norm"], out["cond_abs"] = cond_norm, cond_abs
    out["fire"] = _combine(cond_norm, cond_abs, detect.combine)
    return out.reset_index(drop=True)


def _combine(cond_norm: np.ndarray, cond_abs: np.ndarray, mode: str) -> np.ndarray:
    if mode == "norm_only":
        return cond_norm
    if mode == "abs_only":
        return cond_abs
    if mode == "and":
        return cond_norm & cond_abs
    return cond_norm | cond_abs  # "or"


def _tw_thr(ts, series, w, q, floor: FloorParams) -> np.ndarray:
    return rolling_tw_quantiles(
        ts, np.asarray(series, dtype=np.float64), w,
        H_ms=floor.H_floor_ms, refresh_ms=floor.refresh_ms,
        levels=[q], min_cover_ms=floor.min_cover_ms,
    )[:, 0]


def episodes_from_fire(frame: pd.DataFrame, detect: DetectParams = DetectParams()) -> pd.DataFrame:
    """Collapse the ``fire`` flag into anomaly episodes with cooldown + merge."""
    cols = ["episode_id", "onset_ts", "confirmed_ts", "end_ts", "onset_dt", "end_dt",
            "duration_ms", "zplus_max", "I_norm_max"]
    if frame.empty or not frame["fire"].any():
        return pd.DataFrame(columns=cols)
    ts = frame["event_local_ts_ms"].to_numpy(dtype=np.int64)
    fire = frame["fire"].to_numpy(dtype=bool)

    raw: list[tuple[int, int]] = []
    i, n = 0, len(ts)
    while i < n:
        if not fire[i]:
            i += 1
            continue
        j = i
        last_true = i
        while j < n:
            if fire[j]:
                last_true = j
            elif ts[j] - ts[last_true] > detect.cooldown_ms:
                break
            j += 1
        raw.append((i, last_true))
        i = j

    merged: list[tuple[int, int]] = []
    for a, b in raw:
        if merged and ts[a] - ts[merged[-1][1]] <= detect.merge_gap_ms:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))

    zpl = frame["zplus"].to_numpy(dtype=np.float64)
    inorm = frame["I_norm"].to_numpy(dtype=np.float64)
    rows = []
    for eid, (a, b) in enumerate(merged):
        rows.append(
            {
                "episode_id": eid,
                "onset_ts": int(ts[a]),
                "confirmed_ts": int(ts[a]),
                "end_ts": int(ts[b]),
                "onset_dt": pd.to_datetime(ts[a], unit="ms", utc=True),
                "end_dt": pd.to_datetime(ts[b], unit="ms", utc=True),
                "duration_ms": int(ts[b] - ts[a]),
                "zplus_max": float(np.nanmax(zpl[a : b + 1])) if b >= a else np.nan,
                "I_norm_max": float(np.nanmax(inorm[a : b + 1])) if b >= a else np.nan,
            }
        )
    return pd.DataFrame(rows)
