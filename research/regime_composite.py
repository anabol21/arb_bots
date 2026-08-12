"""Composite volatility score for gear-1.5 screening (causal features + cross-sectional ranks).

See docs/regime-metrics-v0.md § Composite volatility score.

This module is the ``score_mode="z_rank"`` path. For MA-ratio composites
(``score_mode="ma_ratio"``: geom/mean/min/log_mean/…), see
``research/regime_ma_ratio.py``.

Feature layer (per coin, own history, no look-ahead):
  1. z_vol — rolling z of volume (same W as regime_metrics)
  2. z_amp — rolling z of amp_ohlc (or short mean / amp_5m) with the same W

Screener layer (at timestamp t, across coins):
  lightly winsorize z_vol / z_amp, then
  composite = mean(r_z_vol, r_z_amp)  # equal-weight cross-sectional percentile ranks

Own-history score ``ema_pct_z`` (comparison / Top‑10 variant; not cross-sectional):
  pct_z_vol = causal own-history percentile of z_vol over N (= pct_lookback)
  pct_z_amp = causal own-history percentile of z_amp over N
  ema_pct_z = mean(pct_z_vol, pct_z_amp)
  Name keeps the notebook trio (ma / ema / ema_pct_z); the score itself does **not**
  use EMA/MA ratios — those are the ma_ratio path. z_* match regime_metrics / this module.

Optional diagnostics (not in default composite):
  delta_vol, own-history pct(z_vol)/pct(amp) — washout / local_score helpers.
  Pass include_delta_vol=True to fold delta_vol into the composite as a third term.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from research.regime_metrics import RegimeParams, apply_hysteresis, volume_features

# Re-export / mirror name used by notebook + regime_ma_ratio
SCORE_MODE_EMA_PCT_Z = "ema_pct_z"
EMA_PCT_Z_COL = "ema_pct_z"


def _rolling_z(series: pd.Series, window: int, min_periods: int, eps: float) -> pd.Series:
    """Causal rolling z (same definition as research.regime_metrics)."""
    mu = series.rolling(window, min_periods=min_periods).mean()
    sigma = series.rolling(window, min_periods=min_periods).std(ddof=0)
    return (series - mu) / sigma.where(sigma > eps)

EPS_DEFAULT = 1e-12
DELTA_VOL_K_DEFAULT = 6
PCT_LOOKBACK_DEFAULT = 288  # ~1 day of 5m bars (optional diagnostics)
AMP_MEAN_BARS_DEFAULT = 1  # 1 = raw amp_ohlc; >1 = short causal mean
WINSOR_LO_DEFAULT = 0.01
WINSOR_HI_DEFAULT = 0.99


@dataclass(frozen=True)
class CompositeParams:
    delta_k: int = DELTA_VOL_K_DEFAULT  # diagnostic / optional third term
    pct_lookback: int = PCT_LOOKBACK_DEFAULT  # optional own-history pct columns
    pct_min_periods: Optional[int] = None  # default: pct_lookback
    amp_mean_bars: int = AMP_MEAN_BARS_DEFAULT
    eps: float = EPS_DEFAULT
    winsor_lo: float = WINSOR_LO_DEFAULT
    winsor_hi: float = WINSOR_HI_DEFAULT
    regime: RegimeParams = RegimeParams()

    @property
    def pct_min(self) -> int:
        return self.pct_min_periods if self.pct_min_periods is not None else self.pct_lookback


def delta_vol(
    volume: pd.Series,
    *,
    k: int = DELTA_VOL_K_DEFAULT,
    eps: float = EPS_DEFAULT,
) -> pd.Series:
    """Relative volume change vs lag-k bar. NaN when baseline is ~0 or missing.

    Kept for washout diagnostics / optional third composite term; not in default score.
    """
    v = volume.astype(float)
    prev = v.shift(k)
    out = (v - prev) / prev.where(prev.abs() > eps)
    return out.rename("delta_vol")


def causal_pct_rank(
    series: pd.Series,
    *,
    window: int,
    min_periods: Optional[int] = None,
) -> pd.Series:
    """Own-history causal percentile of the current value in a trailing window.

    Window includes the current bar. No future bars. Returns NaN until min_periods.
    Defined as mean(window_values <= x_t) over finite window points.
    Optional diagnostic; not used in the default two-metric composite.
    """
    min_p = window if min_periods is None else min_periods
    arr = series.to_numpy(dtype=float, copy=False)
    n = len(arr)
    out = np.full(n, np.nan, dtype=float)
    for i in range(n):
        start = max(0, i - window + 1)
        if i - start + 1 < min_p:
            continue
        last = arr[i]
        if not np.isfinite(last):
            continue
        w = arr[start : i + 1]
        finite = w[np.isfinite(w)]
        if finite.size == 0:
            continue
        out[i] = float(np.mean(finite <= last))
    return pd.Series(out, index=series.index, dtype=float)


def winsorize_series(
    s: pd.Series,
    *,
    lo: float = WINSOR_LO_DEFAULT,
    hi: float = WINSOR_HI_DEFAULT,
) -> pd.Series:
    """Clip to sample quantiles of finite values (cross-section or display)."""
    x = s.astype(float)
    finite = x[np.isfinite(x)]
    if finite.empty:
        return x
    lo_v = float(finite.quantile(lo))
    hi_v = float(finite.quantile(hi))
    return x.clip(lower=lo_v, upper=hi_v)


def build_composite_features(
    bars: pd.DataFrame,
    *,
    params: CompositeParams = CompositeParams(),
) -> pd.DataFrame:
    """Per-coin causal features needed for the composite (one coin, sorted bars).

    Expected columns: bar_start_ts_ms, volume, and amp_ohlc (preferred) or amp_5m.
    Default screener uses z_vol + z_amp (same rolling W). Own-history pct_* and
    delta_vol are filled for diagnostics / washout only.
    """
    need = {"bar_start_ts_ms", "volume"}
    missing = need - set(bars.columns)
    if missing:
        raise ValueError(f"bars missing columns: {sorted(missing)}")

    df = bars.sort_values("bar_start_ts_ms", kind="mergesort").reset_index(drop=True).copy()
    rp = params.regime
    vol_feats = volume_features(df["volume"], rp)
    for col in ("z_vol", "z_vol_smooth", "vol_persistence"):
        df[col] = vol_feats[col].to_numpy()

    amp_col = "amp_ohlc" if "amp_ohlc" in df.columns else ("amp_5m" if "amp_5m" in df.columns else None)
    if amp_col is None and "atr_series" in df.columns:
        # Frames from ma_ratio path may carry atr_series without amp_*; use it for z_amp.
        amp_col = "atr_series"
    if amp_col is None:
        df["amp_used"] = np.nan
    else:
        amp = df[amp_col].astype(float)
        if params.amp_mean_bars > 1:
            amp = amp.rolling(params.amp_mean_bars, min_periods=1).mean()
        df["amp_used"] = amp.to_numpy()

    # Same causal rolling z window as z_vol (regime.W / W_min / eps).
    df["z_amp"] = _rolling_z(
        pd.Series(df["amp_used"], dtype=float),
        rp.W,
        rp.W_min,
        rp.eps,
    ).to_numpy()

    # Optional diagnostics (not in default composite)
    df["delta_vol"] = delta_vol(df["volume"], k=params.delta_k, eps=params.eps).to_numpy()
    df["pct_z_vol"] = causal_pct_rank(
        df["z_vol"], window=params.pct_lookback, min_periods=params.pct_min
    ).to_numpy()
    df["pct_z_amp"] = causal_pct_rank(
        df["z_amp"], window=params.pct_lookback, min_periods=params.pct_min
    ).to_numpy()
    df["pct_amp"] = causal_pct_rank(
        df["amp_used"], window=params.pct_lookback, min_periods=params.pct_min
    ).to_numpy()
    df["pct_delta_vol"] = causal_pct_rank(
        df["delta_vol"], window=params.pct_lookback, min_periods=params.pct_min
    ).to_numpy()
    # Own-history comparison score (not cross-sectional z_rank):
    # ema_pct_z = mean(pct(z_vol), pct(z_amp)) with causal pct over pct_lookback.
    df[EMA_PCT_Z_COL] = df[["pct_z_vol", "pct_z_amp"]].mean(axis=1, skipna=False)
    # Washout helper: own-history mean of the three legacy pct features
    df["local_score"] = df[["pct_delta_vol", "pct_z_vol", "pct_amp"]].mean(axis=1, skipna=False)

    regime = apply_hysteresis(
        df["z_vol_smooth"],
        z_enter=rp.Z_enter,
        z_exit=rp.Z_exit,
        persistence=df["vol_persistence"],
        p_persist=rp.P_persist,
        require_persistence=rp.require_persistence,
    )
    df["regime_on"] = regime.to_numpy()
    return df


def _rank_pct_cross(s: pd.Series) -> pd.Series:
    """Cross-sectional percentile rank in [0, 1]; NaNs stay NaN."""
    return s.rank(method="average", pct=True)


def snapshot_at_ts(
    coin_frames: dict[str, pd.DataFrame],
    ts_ms: int,
) -> pd.DataFrame:
    """One row per coin at bar_start_ts_ms == ts_ms (exact match)."""
    rows = []
    for coin, fr in coin_frames.items():
        hit = fr.loc[fr["bar_start_ts_ms"] == ts_ms]
        if hit.empty:
            continue
        r = hit.iloc[-1]

        def _f(col: str) -> float:
            if col not in r.index:
                return np.nan
            v = r[col]
            return float(v) if np.isfinite(v) else np.nan

        rows.append(
            {
                "base_coin": coin,
                "bar_start_ts_ms": int(ts_ms),
                "z_vol": _f("z_vol"),
                "z_amp": _f("z_amp"),
                "amp_used": _f("amp_used"),
                "volume": _f("volume"),
                "delta_vol": _f("delta_vol"),
                "pct_z_vol": _f("pct_z_vol"),
                "pct_z_amp": _f("pct_z_amp"),
                "pct_amp": _f("pct_amp"),
                "pct_delta_vol": _f("pct_delta_vol"),
                "ema_pct_z": _f(EMA_PCT_Z_COL),
                "local_score": _f("local_score"),
                "regime_on": bool(r["regime_on"]) if "regime_on" in r.index and pd.notna(r["regime_on"]) else False,
            }
        )
    return pd.DataFrame(rows)


def ema_pct_z_formula(*, pct_lookback: int = PCT_LOOKBACK_DEFAULT, W: int = 48) -> str:
    """One-line label for notebook / CLI."""
    return (
        f"ema_pct_z=mean(pct(z_vol),pct(z_amp)) "
        f"own-hist N={pct_lookback} W={W}"
    )


def attach_ema_pct_z_features(
    bars: pd.DataFrame,
    *,
    params: CompositeParams = CompositeParams(),
) -> pd.DataFrame:
    """Build / refresh causal z + pct + ``ema_pct_z`` on a bar/feature frame.

    Equivalent to the own-history columns from ``build_composite_features``; safe to
    call on frames that already have volume (+ amp_ohlc/amp_5m). Does not change
    cross-sectional z_rank composite logic.
    """
    return build_composite_features(bars, params=params)


def cross_sectional_composite(
    snapshot: pd.DataFrame,
    *,
    params: CompositeParams = CompositeParams(),
    winsorize: bool = True,
    include_delta_vol: bool = False,
    winsorize_delta: Optional[bool] = None,  # deprecated alias
) -> pd.DataFrame:
    """Equal-weight mean of cross-sectional percentile ranks of (z_vol, z_amp).

    Default path is two-metric. Set include_delta_vol=True to also rank winsorized
    delta_vol and average three ranks (legacy / experiment).
    """
    if winsorize_delta is not None:
        winsorize = winsorize_delta
    if snapshot.empty:
        return snapshot.copy()

    out = snapshot.copy()
    z_vol = out["z_vol"].astype(float)
    z_amp = out["z_amp"].astype(float)
    if winsorize:
        z_vol = winsorize_series(z_vol, lo=params.winsor_lo, hi=params.winsor_hi)
        z_amp = winsorize_series(z_amp, lo=params.winsor_lo, hi=params.winsor_hi)
    out["z_vol_w"] = z_vol
    out["z_amp_w"] = z_amp
    out["rank_z_vol"] = _rank_pct_cross(out["z_vol_w"])
    out["rank_z_amp"] = _rank_pct_cross(out["z_amp_w"])

    parts = ["rank_z_vol", "rank_z_amp"]
    if include_delta_vol:
        delta = out["delta_vol"].astype(float)
        if winsorize:
            delta = winsorize_series(delta, lo=params.winsor_lo, hi=params.winsor_hi)
        out["delta_vol_w"] = delta
        out["rank_delta"] = _rank_pct_cross(out["delta_vol_w"])
        parts = ["rank_delta", "rank_z_vol", "rank_z_amp"]

    out["composite"] = out[parts].mean(axis=1, skipna=False)
    out = out.sort_values("composite", ascending=False, kind="mergesort").reset_index(drop=True)
    out["rank"] = np.arange(1, len(out) + 1)
    return out


def merge_exchange_composites(
    okx: pd.DataFrame,
    bybit: pd.DataFrame,
    *,
    how: str = "mean",
) -> pd.DataFrame:
    """Combine per-exchange composite scores. how: 'mean' | 'min'."""
    if how not in {"mean", "min"}:
        raise ValueError("how must be 'mean' or 'min'")
    a = okx[["base_coin", "composite"]].rename(columns={"composite": "composite_okx"})
    b = bybit[["base_coin", "composite"]].rename(columns={"composite": "composite_bybit"})
    m = a.merge(b, on="base_coin", how="inner")
    if how == "mean":
        m["composite"] = m[["composite_okx", "composite_bybit"]].mean(axis=1)
    else:
        m["composite"] = m[["composite_okx", "composite_bybit"]].min(axis=1)
    m = m.sort_values("composite", ascending=False, kind="mergesort").reset_index(drop=True)
    m["rank"] = np.arange(1, len(m) + 1)
    return m


def last_common_bar_ts(coin_frames: dict[str, pd.DataFrame]) -> Optional[int]:
    """Latest bar_start_ts_ms present in every non-empty frame."""
    common: Optional[set[int]] = None
    for fr in coin_frames.values():
        if fr is None or fr.empty:
            continue
        ts = set(fr["bar_start_ts_ms"].astype(np.int64).tolist())
        common = ts if common is None else (common & ts)
    if not common:
        return None
    return int(max(common))


def washout_episode_stats(
    frame: pd.DataFrame,
    *,
    min_episode_bars: int = 12,
) -> pd.DataFrame:
    """Per-episode local_score decay vs duration (own-history score, not cross-section).

    Washout hypothesis (legacy): in long regime_on runs, delta_vol → 0 and local_score
    fades even while the coin is still 'hot' by absolute z. Default composite no longer
    uses delta_vol; this helper remains for diagnostics.
    """
    if frame.empty or "regime_on" not in frame.columns:
        return pd.DataFrame()

    on = frame["regime_on"].fillna(False).to_numpy()
    score = frame["local_score"].to_numpy(dtype=float)
    delta = frame["delta_vol"].to_numpy(dtype=float)
    starts = frame["bar_start_ts_ms"].to_numpy(dtype=np.int64)
    rows = []
    i = 0
    n = len(on)
    ep_id = 0
    while i < n:
        if not on[i]:
            i += 1
            continue
        j = i
        while j < n and on[j]:
            j += 1
        length = j - i
        if length >= min_episode_bars:
            s0 = score[i]
            s1 = score[j - 1]
            d0 = delta[i]
            d1 = delta[j - 1]
            mid = i + length // 2
            rows.append(
                {
                    "episode_id": ep_id,
                    "start_ts_ms": int(starts[i]),
                    "end_ts_ms": int(starts[j - 1]),
                    "n_bars": int(length),
                    "local_score_start": float(s0) if np.isfinite(s0) else np.nan,
                    "local_score_mid": float(score[mid]) if np.isfinite(score[mid]) else np.nan,
                    "local_score_end": float(s1) if np.isfinite(s1) else np.nan,
                    "score_decay": float(s0 - s1) if np.isfinite(s0) and np.isfinite(s1) else np.nan,
                    "delta_vol_start": float(d0) if np.isfinite(d0) else np.nan,
                    "delta_vol_end": float(d1) if np.isfinite(d1) else np.nan,
                    "abs_delta_shrink": (
                        float(abs(d0) - abs(d1))
                        if np.isfinite(d0) and np.isfinite(d1)
                        else np.nan
                    ),
                }
            )
            ep_id += 1
        i = j
    return pd.DataFrame(rows)


def build_composite_panel(
    coin_frames: dict[str, pd.DataFrame],
    timestamps: Iterable[int],
    *,
    params: CompositeParams = CompositeParams(),
    winsorize: bool = True,
    include_delta_vol: bool = False,
    stride: int = 1,
    winsorize_delta: Optional[bool] = None,  # deprecated alias
) -> pd.DataFrame:
    """Cross-sectional composite for each t → long panel (t, coin, composite, rank, …).

    Features are taken from pre-built per-coin frames (see build_composite_features).
    At each timestamp only coins with an exact bar match participate in the cross-section.
    ``stride`` > 1 evaluates every M-th bar (heatmap preview / speed).
    """
    if winsorize_delta is not None:
        winsorize = winsorize_delta
    ts_list = [int(t) for t in timestamps]
    if stride < 1:
        raise ValueError("stride must be >= 1")
    if stride > 1:
        ts_list = ts_list[::stride]
    empty_cols = [
        "bar_start_ts_ms",
        "base_coin",
        "composite",
        "rank",
        "rank_z_vol",
        "rank_z_amp",
        "z_vol",
        "z_amp",
        "delta_vol",
        "pct_z_vol",
        "pct_amp",
        "regime_on",
    ]
    if include_delta_vol:
        empty_cols.insert(4, "rank_delta")
    if not ts_list or not coin_frames:
        return pd.DataFrame(columns=empty_cols)

    coins = sorted(coin_frames.keys())
    n_t, n_c = len(ts_list), len(coins)
    ts_index = {t: i for i, t in enumerate(ts_list)}
    ts_set = set(ts_index)

    z_vol = np.full((n_t, n_c), np.nan, dtype=float)
    z_amp = np.full((n_t, n_c), np.nan, dtype=float)
    delta = np.full((n_t, n_c), np.nan, dtype=float)
    pct_z = np.full((n_t, n_c), np.nan, dtype=float)
    pct_a = np.full((n_t, n_c), np.nan, dtype=float)
    regime = np.zeros((n_t, n_c), dtype=bool)

    for j, coin in enumerate(coins):
        fr = coin_frames.get(coin)
        if fr is None or fr.empty:
            continue
        sub = fr.loc[fr["bar_start_ts_ms"].isin(ts_set)]
        if sub.empty:
            continue
        # last row wins on duplicate timestamps
        sub = sub.drop_duplicates(subset=["bar_start_ts_ms"], keep="last")
        idx = sub["bar_start_ts_ms"].map(ts_index).to_numpy(dtype=int)
        z_vol[idx, j] = sub["z_vol"].to_numpy(dtype=float)
        z_amp[idx, j] = sub["z_amp"].to_numpy(dtype=float)
        delta[idx, j] = sub["delta_vol"].to_numpy(dtype=float)
        pct_z[idx, j] = sub["pct_z_vol"].to_numpy(dtype=float)
        pct_a[idx, j] = sub["pct_amp"].to_numpy(dtype=float)
        if "regime_on" in sub.columns:
            regime[idx, j] = sub["regime_on"].fillna(False).to_numpy(dtype=bool)

    def _winsor_rows(mat: np.ndarray) -> np.ndarray:
        out = mat.copy()
        if not winsorize:
            return out
        for i in range(n_t):
            row = out[i]
            finite = row[np.isfinite(row)]
            if finite.size == 0:
                continue
            lo_v = float(np.quantile(finite, params.winsor_lo))
            hi_v = float(np.quantile(finite, params.winsor_hi))
            out[i] = np.clip(row, lo_v, hi_v)
        return out

    z_vol_w = _winsor_rows(z_vol)
    z_amp_w = _winsor_rows(z_amp)
    rank_z = pd.DataFrame(z_vol_w).rank(axis=1, method="average", pct=True).to_numpy()
    rank_a = pd.DataFrame(z_amp_w).rank(axis=1, method="average", pct=True).to_numpy()

    if include_delta_vol:
        delta_w = _winsor_rows(delta)
        rank_d = pd.DataFrame(delta_w).rank(axis=1, method="average", pct=True).to_numpy()
        # skipna=False: any missing component → NaN composite
        composite = (rank_d + rank_z + rank_a) / 3.0
    else:
        rank_d = None
        composite = (rank_z + rank_a) / 2.0

    # ranks within each t (1 = highest composite); NaN composites get NaN rank
    order = np.argsort(np.where(np.isfinite(composite), -composite, np.inf), axis=1, kind="mergesort")
    ranks = np.full((n_t, n_c), np.nan, dtype=float)
    for i in range(n_t):
        row = composite[i]
        finite_count = int(np.isfinite(row).sum())
        if finite_count == 0:
            continue
        pos = 0
        for j in order[i]:
            if not np.isfinite(row[j]):
                break
            ranks[i, j] = float(pos + 1)
            pos += 1

    rows = []
    for i, t in enumerate(ts_list):
        for j, coin in enumerate(coins):
            if not np.isfinite(composite[i, j]):
                if not (
                    np.isfinite(z_vol[i, j]) or np.isfinite(z_amp[i, j]) or np.isfinite(delta[i, j])
                ):
                    continue
            row = {
                "bar_start_ts_ms": int(t),
                "base_coin": coin,
                "composite": float(composite[i, j]) if np.isfinite(composite[i, j]) else np.nan,
                "rank": float(ranks[i, j]) if np.isfinite(ranks[i, j]) else np.nan,
                "rank_z_vol": float(rank_z[i, j]) if np.isfinite(rank_z[i, j]) else np.nan,
                "rank_z_amp": float(rank_a[i, j]) if np.isfinite(rank_a[i, j]) else np.nan,
                "z_vol": float(z_vol[i, j]) if np.isfinite(z_vol[i, j]) else np.nan,
                "z_amp": float(z_amp[i, j]) if np.isfinite(z_amp[i, j]) else np.nan,
                "delta_vol": float(delta[i, j]) if np.isfinite(delta[i, j]) else np.nan,
                "pct_z_vol": float(pct_z[i, j]) if np.isfinite(pct_z[i, j]) else np.nan,
                "pct_amp": float(pct_a[i, j]) if np.isfinite(pct_a[i, j]) else np.nan,
                "regime_on": bool(regime[i, j]),
            }
            if rank_d is not None:
                row["rank_delta"] = float(rank_d[i, j]) if np.isfinite(rank_d[i, j]) else np.nan
            rows.append(row)
    return pd.DataFrame(rows)


def detect_top1_transitions(panel: pd.DataFrame) -> pd.DataFrame:
    """Times when argmax_coin(t) changes vs previous panel timestamp.

    Returns one row per transition: t, new top-1 coin, previous leader, composites.
    First timestamp in the panel is recorded as an initial 'enter' (prev=None).
    """
    if panel is None or panel.empty:
        return pd.DataFrame(
            columns=[
                "bar_start_ts_ms",
                "top1_coin",
                "prev_coin",
                "composite",
                "prev_composite",
                "is_initial",
            ]
        )

    # Prefer precomputed rank==1; fall back to argmax composite per t
    work = panel.dropna(subset=["composite"]).copy()
    if work.empty:
        return detect_top1_transitions(pd.DataFrame())

    if "rank" in work.columns and work["rank"].notna().any():
        leaders = (
            work.loc[work["rank"] == 1, ["bar_start_ts_ms", "base_coin", "composite"]]
            .sort_values("bar_start_ts_ms", kind="mergesort")
            .drop_duplicates(subset=["bar_start_ts_ms"], keep="first")
        )
    else:
        idx = work.groupby("bar_start_ts_ms", sort=True)["composite"].idxmax()
        leaders = work.loc[idx, ["bar_start_ts_ms", "base_coin", "composite"]].sort_values(
            "bar_start_ts_ms", kind="mergesort"
        )

    leaders = leaders.reset_index(drop=True)
    leaders["prev_coin"] = leaders["base_coin"].shift(1)
    leaders["prev_composite"] = leaders["composite"].shift(1)
    changed = leaders["prev_coin"].isna() | (leaders["base_coin"] != leaders["prev_coin"])
    out = leaders.loc[changed].copy()
    out["is_initial"] = out["prev_coin"].isna()
    out = out.rename(columns={"base_coin": "top1_coin"})
    return out[
        [
            "bar_start_ts_ms",
            "top1_coin",
            "prev_coin",
            "composite",
            "prev_composite",
            "is_initial",
        ]
    ].reset_index(drop=True)


def washout_summary(
    coin_frames: dict[str, pd.DataFrame],
    *,
    coins: Optional[Iterable[str]] = None,
    min_episode_bars: int = 12,
) -> dict:
    """Aggregate washout numbers across selected coins (light analysis)."""
    use = list(coins) if coins is not None else list(coin_frames.keys())
    all_eps = []
    for c in use:
        fr = coin_frames.get(c)
        if fr is None or fr.empty:
            continue
        eps = washout_episode_stats(fr, min_episode_bars=min_episode_bars)
        if eps.empty:
            continue
        eps = eps.copy()
        eps["base_coin"] = c
        all_eps.append(eps)
    if not all_eps:
        return {
            "n_coins": len(use),
            "n_long_episodes": 0,
            "corr_duration_vs_decay": None,
            "mean_score_decay": None,
            "mean_abs_delta_shrink": None,
            "note": "no long regime episodes",
        }
    panel = pd.concat(all_eps, ignore_index=True)
    dur = panel["n_bars"].astype(float)
    decay = panel["score_decay"].astype(float)
    mask = np.isfinite(dur) & np.isfinite(decay)
    corr = None
    if int(mask.sum()) >= 3:
        corr = float(np.corrcoef(dur[mask], decay[mask])[0, 1])
    return {
        "n_coins": len(use),
        "n_long_episodes": int(len(panel)),
        "min_episode_bars": min_episode_bars,
        "corr_duration_vs_decay": corr,
        "mean_score_decay": float(decay[mask].mean()) if mask.any() else None,
        "median_score_decay": float(np.median(decay[mask])) if mask.any() else None,
        "mean_abs_delta_shrink": float(panel["abs_delta_shrink"].mean(skipna=True)),
        "mean_episode_bars": float(panel["n_bars"].mean()),
        "top_long_episodes": panel.nlargest(5, "n_bars")[
            ["base_coin", "n_bars", "score_decay", "local_score_start", "local_score_end"]
        ].to_dict(orient="records"),
        "hypothesis": (
            "Long regime_on runs can wash out delta_vol (legacy local_score) "
            "even while absolute z_vol/z_amp stay elevated; default composite "
            "now ranks z_vol+z_amp and is less exposed to that washout path. "
            "Spreads may still fade — flag for later analysis."
        ),
    }
