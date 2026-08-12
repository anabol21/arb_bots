"""Regime metrics v0 for gear 1.5 (causal volume z-score + optional mid amplitude).

See docs/regime-metrics-v0.md. Expert thresholds only — no PnL tuning here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

BAR_MS = 300_000


@dataclass(frozen=True)
class RegimeParams:
    W: int = 48
    W_min: int = 48
    W_s: int = 3
    eps: float = 1e-12
    Z_enter: float = 2.0
    Z_exit: float = 1.0
    K_persist: int = 6
    P_persist: float = 0.5
    require_persistence: bool = False
    Z_amp: float = 2.0
    require_amp: bool = False
    amp_accel_lag: int = 3


def _rolling_z(series: pd.Series, window: int, min_periods: int, eps: float) -> pd.Series:
    mu = series.rolling(window, min_periods=min_periods).mean()
    sigma = series.rolling(window, min_periods=min_periods).std(ddof=0)
    z = (series - mu) / sigma.where(sigma > eps)
    return z


def volume_features(volume: pd.Series, params: RegimeParams) -> pd.DataFrame:
    """Causal volume features aligned to `volume` index."""
    v = volume.astype(float)
    out = pd.DataFrame(index=v.index)
    out["volume"] = v
    out["vol_median_ratio"] = v / v.rolling(params.W, min_periods=params.W_min).median()
    out["vol_pct_rank"] = v.rolling(params.W, min_periods=params.W_min).apply(
        lambda x: pd.Series(x).rank(pct=True).iloc[-1], raw=False
    )
    out["z_vol"] = _rolling_z(v, params.W, params.W_min, params.eps)
    out["z_vol_smooth"] = out["z_vol"].rolling(params.W_s, min_periods=1).mean()
    out["vol_accel"] = out["z_vol"] - out["z_vol"].shift(params.amp_accel_lag)
    raw_hot = out["z_vol"] >= params.Z_enter
    out["vol_persistence"] = raw_hot.rolling(params.K_persist, min_periods=1).mean()
    return out


def apply_hysteresis(
    z_smooth: pd.Series,
    *,
    z_enter: float,
    z_exit: float,
    persistence: Optional[pd.Series] = None,
    p_persist: float = 0.5,
    require_persistence: bool = False,
) -> pd.Series:
    """Stateful on/off from smoothed z with optional persistence gate on enter."""
    on = False
    flags = []
    z_vals = z_smooth.to_numpy(dtype=float)
    p_vals = (
        persistence.to_numpy(dtype=float)
        if persistence is not None
        else np.ones(len(z_vals), dtype=float)
    )
    for z, p in zip(z_vals, p_vals):
        if np.isnan(z):
            flags.append(False)
            on = False
            continue
        if not on:
            ok = z >= z_enter
            if require_persistence:
                ok = ok and (not np.isnan(p)) and (p >= p_persist)
            on = bool(ok)
        else:
            on = bool(z >= z_exit)
        flags.append(on)
    return pd.Series(flags, index=z_smooth.index, dtype=bool, name="regime_on")


def amp_from_ticks(
    ticks: pd.DataFrame,
    bar_starts_ms: pd.Series,
    *,
    bid_col: str = "okx_bid_price",
    ask_col: str = "okx_ask_price",
    ts_col: str = "event_local_ts_ms",
) -> pd.Series:
    """Per-bar amplitude (mid_high-mid_low)/mid_open from L1 ticks (OKX mid)."""
    need = {bid_col, ask_col, ts_col}
    missing = need - set(ticks.columns)
    if missing:
        raise ValueError(f"ticks missing columns: {sorted(missing)}")

    mid = (ticks[bid_col].astype(float) + ticks[ask_col].astype(float)) / 2.0
    ts = ticks[ts_col].astype(np.int64)
    amps = []
    for start in bar_starts_ms.astype(np.int64):
        end = int(start) + BAR_MS
        mask = (ts >= start) & (ts < end)
        m = mid.loc[mask]
        if m.empty or not np.isfinite(m.iloc[0]) or m.iloc[0] == 0:
            amps.append(np.nan)
            continue
        amps.append(float((m.max() - m.min()) / m.iloc[0]))
    return pd.Series(amps, index=bar_starts_ms.index, name="amp_5m", dtype=float)


def build_regime_frame(
    bars: pd.DataFrame,
    *,
    params: RegimeParams = RegimeParams(),
    ticks: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Build regime features + `regime_on` from bar_5m frame (one coin).

    Expected bar columns: `bar_start_ts_ms`, `volume` (and optionally already sorted).
    """
    if "volume" not in bars.columns or "bar_start_ts_ms" not in bars.columns:
        raise ValueError("bars need volume and bar_start_ts_ms")

    df = bars.sort_values("bar_start_ts_ms", kind="mergesort").reset_index(drop=True)
    feats = volume_features(df["volume"], params)
    for col in feats.columns:
        df[col] = feats[col].to_numpy()

    if ticks is not None:
        df["amp_5m"] = amp_from_ticks(ticks, df["bar_start_ts_ms"]).to_numpy()
        df["z_amp"] = _rolling_z(df["amp_5m"], params.W, params.W_min, params.eps)
    else:
        df["amp_5m"] = np.nan
        df["z_amp"] = np.nan

    regime = apply_hysteresis(
        df["z_vol_smooth"],
        z_enter=params.Z_enter,
        z_exit=params.Z_exit,
        persistence=df["vol_persistence"],
        p_persist=params.P_persist,
        require_persistence=params.require_persistence,
    )
    if params.require_amp:
        amp_ok = df["z_amp"] >= params.Z_amp
        regime = regime & amp_ok.fillna(False)
    df["regime_on"] = regime.to_numpy()
    return df


def regime_episodes(regime_on: pd.Series, bar_start_ts_ms: pd.Series) -> pd.DataFrame:
    """Collapse contiguous regime_on runs into episodes."""
    on = regime_on.fillna(False).to_numpy()
    starts = bar_start_ts_ms.to_numpy()
    episodes = []
    i = 0
    n = len(on)
    while i < n:
        if not on[i]:
            i += 1
            continue
        j = i
        while j < n and on[j]:
            j += 1
        episodes.append(
            {
                "start_ts_ms": int(starts[i]),
                "end_ts_ms": int(starts[j - 1]) + BAR_MS,
                "n_bars": int(j - i),
            }
        )
        i = j
    return pd.DataFrame(episodes)


def sanity_summary(df: pd.DataFrame) -> dict:
    """Falsifiable checks for one-coin regime frame."""
    n = len(df)
    on = df["regime_on"].fillna(False)
    share = float(on.mean()) if n else 0.0
    episodes = regime_episodes(on, df["bar_start_ts_ms"])
    single_bar = int((episodes["n_bars"] == 1).sum()) if len(episodes) else 0
    z = df["z_vol_smooth"]
    return {
        "n_bars": n,
        "regime_on_share": share,
        "n_episodes": int(len(episodes)),
        "single_bar_episodes": single_bar,
        "z_vol_smooth_finite_share": float(np.isfinite(z).mean()) if n else 0.0,
        "z_vol_smooth_max": float(np.nanmax(z)) if n and np.isfinite(z).any() else None,
        "warn_high_regime_share": share > 0.25,
        "warn_mostly_single_bar": bool(len(episodes) and single_bar / len(episodes) > 0.7),
    }
