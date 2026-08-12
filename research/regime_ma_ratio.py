"""MA-ratio volatility composites (causal short blend / MA long).

Design (gear 1.5 canonical screener score; alternative to z-rank composite):
  r_vol = short(volume) / MA_long(volume)
  r_atr = short(ATR) / MA_long(ATR)

Numerator mode (``MaRatioParams.numerator`` / ``numerator=``):
  blend (default, canonical) — short = α·EMA_short + (1−α)·MA_short
                               with ``blend_alpha`` ≈ 0.75 (soft short blend)
  ema                        — short = EMA via pandas ewm(span=SHORT, adjust=False)
  ma                         — short = SMA/MA with the same SHORT window

Denominator is always SMA/MA long.

ATR prefers true-range based series from 5m OHLC:
    TR_t = max(H-L, |H-C_prev|, |L-C_prev|)
    atr_series = SMA(TR, atr_n) with atr_n=1 → raw TR
Fallback when OHLC missing: amp_ohlc / amp_5m as the atr_series.

Raw ratios (not own-history percentiles) feed named composites:
  geom      sqrt(r_vol * r_atr)
  mean      0.5 * (r_vol + r_atr)
  min       min(r_vol, r_atr)
  log_mean  0.5 * (log(max(r_vol, ε)) + log(max(r_atr, ε)))
            (= log of geom; equal log-weight; ε floor on each ratio)
  vol_only  r_vol
  atr_only  r_atr

Unlike delta_vol, elevated plateaus keep r_* > 1 while short stays above long MA.

Related score (own-history, not raw ratio): ``ema_pct_z`` in
``research/regime_composite.py`` — mean(pct(z_vol), pct(z_amp)).

See docs/regime-metrics-v0.md § MA-ratio composites.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd

from research.regime_metrics import RegimeParams, apply_hysteresis, volume_features

EPS_DEFAULT = 1e-12
MA_SHORT_DEFAULT = 6
MA_LONG_DEFAULT = 48
MA_LONG_ALT_DEFAULT = 288  # ~1 day of 5m bars
ATR_N_DEFAULT = 1  # 1 = use TR; >1 = causal SMA of TR before ratio MAs

VARIANT_GEOM = "geom"
VARIANT_MEAN = "mean"
VARIANT_MIN = "min"
VARIANT_LOG_MEAN = "log_mean"
VARIANT_VOL_ONLY = "vol_only"
VARIANT_ATR_ONLY = "atr_only"

VARIANTS: tuple[str, ...] = (
    VARIANT_GEOM,
    VARIANT_MEAN,
    VARIANT_MIN,
    VARIANT_LOG_MEAN,
    VARIANT_VOL_ONLY,
    VARIANT_ATR_ONLY,
)

SCORE_MODE_Z_RANK = "z_rank"
SCORE_MODE_MA_RATIO = "ma_ratio"
SCORE_MODE_EMA_PCT_Z = "ema_pct_z"  # own-history mean(pct(z_vol), pct(z_amp)); see regime_composite

NUMERATOR_BLEND = "blend"
NUMERATOR_EMA = "ema"
NUMERATOR_MA = "ma"
NUMERATORS: tuple[str, ...] = (NUMERATOR_BLEND, NUMERATOR_EMA, NUMERATOR_MA)
BLEND_ALPHA_DEFAULT = 0.75  # soft short blend: weight on EMA in α·EMA+(1−α)·MA


@dataclass(frozen=True)
class MaRatioParams:
    short: int = MA_SHORT_DEFAULT
    long: int = MA_LONG_DEFAULT
    atr_n: int = ATR_N_DEFAULT
    eps: float = EPS_DEFAULT
    # When True and OHLC incomplete, use amp_ohlc/amp_5m as atr_series.
    allow_amp_fallback: bool = True
    # Short-leg smoother: "blend" (canonical) | "ema" | "ma".
    numerator: str = NUMERATOR_BLEND
    # Weight on EMA when numerator="blend". Ignored for pure ema/ma modes.
    blend_alpha: float = BLEND_ALPHA_DEFAULT
    regime: RegimeParams = field(default_factory=RegimeParams)

    def __post_init__(self) -> None:
        if self.short < 1:
            raise ValueError("short must be >= 1")
        if self.long < self.short:
            raise ValueError("long must be >= short")
        if self.atr_n < 1:
            raise ValueError("atr_n must be >= 1")
        if self.eps <= 0:
            raise ValueError("eps must be > 0")
        if self.numerator not in NUMERATORS:
            raise ValueError(f"numerator must be one of {NUMERATORS}, got {self.numerator!r}")
        if not (0.0 <= float(self.blend_alpha) <= 1.0):
            raise ValueError(
                f"blend_alpha must be in [0, 1], got {self.blend_alpha!r}"
            )

    @property
    def label_suffix(self) -> str:
        """e.g. L48 for long=48 — useful when computing several longs."""
        return f"L{self.long}"

    @property
    def numerator_label(self) -> str:
        """Short tag for titles: blendα0.75·s6/MA288, EMA6/MA288, or MA6/MA288."""
        if self.numerator == NUMERATOR_BLEND:
            return f"blendα{self.blend_alpha:g}·s{self.short}/MA{self.long}"
        tag = "EMA" if self.numerator == NUMERATOR_EMA else "MA"
        return f"{tag}{self.short}/MA{self.long}"


def true_range(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
) -> pd.Series:
    """Causal per-bar true range; C_prev via shift(1). First bar = H-L."""
    h = high.astype(float)
    l = low.astype(float)
    c_prev = close.astype(float).shift(1)
    hl = (h - l).abs()
    hc = (h - c_prev).abs()
    lc = (l - c_prev).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1, skipna=False)
    # First bar: no prev close → use H-L only
    if len(tr):
        tr.iloc[0] = hl.iloc[0]
    return tr.rename("true_range")


def atr_series_from_bars(
    bars: pd.DataFrame,
    *,
    atr_n: int = ATR_N_DEFAULT,
    allow_amp_fallback: bool = True,
) -> pd.Series:
    """ATR proxy series used inside EMA/MA ratios.

    Prefer SMA(TR, atr_n) from OHLC. atr_n=1 → raw true range.
    Fallback: amp_ohlc then amp_5m (same units of relative range).
    """
    ohlc = {"high", "low", "close"}
    if ohlc.issubset(bars.columns):
        tr = true_range(bars["high"], bars["low"], bars["close"])
        if atr_n <= 1:
            return tr.rename("atr_series")
        return tr.rolling(atr_n, min_periods=atr_n).mean().rename("atr_series")

    if not allow_amp_fallback:
        raise ValueError("bars missing high/low/close and amp fallback disabled")
    if "amp_ohlc" in bars.columns:
        amp = bars["amp_ohlc"].astype(float)
    elif "amp_5m" in bars.columns:
        amp = bars["amp_5m"].astype(float)
    else:
        raise ValueError("bars missing OHLC and amp_ohlc/amp_5m for ATR fallback")
    if atr_n <= 1:
        return amp.rename("atr_series")
    return amp.rolling(atr_n, min_periods=atr_n).mean().rename("atr_series")


def causal_ma(series: pd.Series, window: int, *, min_periods: Optional[int] = None) -> pd.Series:
    """Causal simple moving average (includes current bar)."""
    mp = window if min_periods is None else min_periods
    return series.astype(float).rolling(window, min_periods=mp).mean()


def causal_ema(series: pd.Series, span: int) -> pd.Series:
    """Causal EMA; span ≈ window (pandas ewm(span=…, adjust=False))."""
    return series.astype(float).ewm(span=span, adjust=False).mean()


def short_numerator(
    series: pd.Series,
    short: int,
    *,
    numerator: str = NUMERATOR_BLEND,
    blend_alpha: float = BLEND_ALPHA_DEFAULT,
) -> pd.Series:
    """Causal short-leg smoother used in the MA-ratio numerator."""
    if numerator not in NUMERATORS:
        raise ValueError(f"numerator must be one of {NUMERATORS}, got {numerator!r}")
    if not (0.0 <= float(blend_alpha) <= 1.0):
        raise ValueError(f"blend_alpha must be in [0, 1], got {blend_alpha!r}")
    if numerator == NUMERATOR_EMA:
        return causal_ema(series, short)
    if numerator == NUMERATOR_MA:
        return causal_ma(series, short)
    # blend: α·EMA + (1−α)·MA
    ema = causal_ema(series, short)
    ma = causal_ma(series, short)
    alpha = float(blend_alpha)
    return (alpha * ema + (1.0 - alpha) * ma).rename(series.name or "short_blend")


def ma_ratio(
    series: pd.Series,
    *,
    short: int,
    long: int,
    eps: float = EPS_DEFAULT,
    numerator: str = NUMERATOR_BLEND,
    blend_alpha: float = BLEND_ALPHA_DEFAULT,
) -> pd.Series:
    """short(series) / MA_long with ε guard on denominator. Causal.

    ``numerator="blend"`` (default): (α·EMA_short + (1−α)·MA_short) / MA_long.
    ``numerator="ema"``: EMA_short / MA_long.
    ``numerator="ma"``: MA_short / MA_long.
    """
    num = short_numerator(
        series, short, numerator=numerator, blend_alpha=blend_alpha
    )
    ma_l = causal_ma(series, long)
    denom = ma_l.where(ma_l.abs() > eps)
    return (num / denom).rename(series.name or "ma_ratio")


def compose_variant(
    r_vol: pd.Series,
    r_atr: pd.Series,
    variant: str,
    *,
    eps: float = EPS_DEFAULT,
) -> pd.Series:
    """Combine raw ratios into a named composite. NaN if either needed input is NaN."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}; expected one of {VARIANTS}")

    rv = r_vol.astype(float)
    ra = r_atr.astype(float)

    if variant == VARIANT_VOL_ONLY:
        return rv.rename("composite")
    if variant == VARIANT_ATR_ONLY:
        return ra.rename("composite")

    both = rv.notna() & ra.notna()
    out = pd.Series(np.nan, index=rv.index, dtype=float)

    if variant == VARIANT_GEOM:
        # sqrt(r_vol * r_atr); require non-negative products for real sqrt
        prod = rv * ra
        ok = both & np.isfinite(prod) & (prod >= 0)
        out.loc[ok] = np.sqrt(prod.loc[ok].to_numpy())
    elif variant == VARIANT_MEAN:
        out.loc[both] = 0.5 * (rv.loc[both] + ra.loc[both])
    elif variant == VARIANT_MIN:
        out.loc[both] = np.minimum(rv.loc[both].to_numpy(), ra.loc[both].to_numpy())
    elif variant == VARIANT_LOG_MEAN:
        # 0.5*(log(r_vol)+log(r_atr)) with floor ε on each ratio.
        # Equivalent to log(geom) when r>0; equal log-weight (not clipped log1p form).
        rv_f = rv.clip(lower=eps)
        ra_f = ra.clip(lower=eps)
        out.loc[both] = 0.5 * (np.log(rv_f.loc[both].to_numpy()) + np.log(ra_f.loc[both].to_numpy()))
    return out.rename("composite")


def variant_column(variant: str, *, long: Optional[int] = None) -> str:
    """Column name: composite_geom or composite_geom_L288 when long tagged."""
    base = f"composite_{variant}"
    if long is None:
        return base
    return f"{base}_L{int(long)}"


def build_ma_ratio_features(
    bars: pd.DataFrame,
    *,
    params: MaRatioParams = MaRatioParams(),
    variants: Sequence[str] = VARIANTS,
    extra_longs: Sequence[int] = (),
) -> pd.DataFrame:
    """Per-coin causal MA-ratio features (+ optional multi-long presets).

    Always fills r_vol / r_atr / atr_series for ``params.long``, plus
    ``composite_<variant>`` for each requested variant.

    ``extra_longs`` (e.g. (288,)) also fills r_vol_L288, r_atr_L288 and
    composite_<variant>_L288 using the same short window.
    """
    need = {"bar_start_ts_ms", "volume"}
    missing = need - set(bars.columns)
    if missing:
        raise ValueError(f"bars missing columns: {sorted(missing)}")

    for v in variants:
        if v not in VARIANTS:
            raise ValueError(f"unknown variant {v!r}")

    df = bars.sort_values("bar_start_ts_ms", kind="mergesort").reset_index(drop=True).copy()
    vol = df["volume"].astype(float)
    atr = atr_series_from_bars(
        df,
        atr_n=params.atr_n,
        allow_amp_fallback=params.allow_amp_fallback,
    )
    df["atr_series"] = atr.to_numpy()

    num = params.numerator
    alpha = params.blend_alpha

    # Primary long (params.long)
    df["r_vol"] = ma_ratio(
        vol,
        short=params.short,
        long=params.long,
        eps=params.eps,
        numerator=num,
        blend_alpha=alpha,
    ).to_numpy()
    df["r_atr"] = ma_ratio(
        pd.Series(df["atr_series"], dtype=float),
        short=params.short,
        long=params.long,
        eps=params.eps,
        numerator=num,
        blend_alpha=alpha,
    ).to_numpy()
    for v in variants:
        col = variant_column(v)
        df[col] = compose_variant(
            df["r_vol"], df["r_atr"], v, eps=params.eps
        ).to_numpy()

    # Optional additional long windows (same short / numerator / blend_alpha)
    longs_extra = sorted({int(x) for x in extra_longs if int(x) != params.long})
    for L in longs_extra:
        if L < params.short:
            raise ValueError(f"extra long {L} < short {params.short}")
        rv = ma_ratio(
            vol,
            short=params.short,
            long=L,
            eps=params.eps,
            numerator=num,
            blend_alpha=alpha,
        )
        ra = ma_ratio(
            pd.Series(df["atr_series"], dtype=float),
            short=params.short,
            long=L,
            eps=params.eps,
            numerator=num,
            blend_alpha=alpha,
        )
        df[f"r_vol_L{L}"] = rv.to_numpy()
        df[f"r_atr_L{L}"] = ra.to_numpy()
        for v in variants:
            df[variant_column(v, long=L)] = compose_variant(rv, ra, v, eps=params.eps).to_numpy()

    # Keep regime_on for overlay / diagnostics (volume z path; not part of MA score)
    rp = params.regime
    vol_feats = volume_features(vol, rp)
    df["z_vol"] = vol_feats["z_vol"].to_numpy()
    df["z_vol_smooth"] = vol_feats["z_vol_smooth"].to_numpy()
    df["vol_persistence"] = vol_feats["vol_persistence"].to_numpy()
    df["regime_on"] = apply_hysteresis(
        df["z_vol_smooth"],
        z_enter=rp.Z_enter,
        z_exit=rp.Z_exit,
        persistence=df["vol_persistence"],
        p_persist=rp.P_persist,
        require_persistence=rp.require_persistence,
    ).to_numpy()
    return df


def select_composite_column(
    variant: str,
    *,
    long: Optional[int] = None,
    primary_long: Optional[int] = None,
) -> str:
    """Resolve which composite_* column to use as the active score."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}")
    if long is None or (primary_long is not None and int(long) == int(primary_long)):
        return variant_column(variant)
    return variant_column(variant, long=int(long))


def snapshot_ma_ratio_at_ts(
    coin_frames: dict[str, pd.DataFrame],
    ts_ms: int,
    *,
    variant: str = VARIANT_GEOM,
    long: Optional[int] = None,
    primary_long: Optional[int] = None,
) -> pd.DataFrame:
    """One row per coin at bar_start_ts_ms == ts_ms with raw composite + ranks later."""
    col = select_composite_column(variant, long=long, primary_long=primary_long)
    rows = []
    for coin, fr in coin_frames.items():
        hit = fr.loc[fr["bar_start_ts_ms"] == ts_ms]
        if hit.empty:
            continue
        r = hit.iloc[-1]
        if col not in fr.columns:
            raise KeyError(f"{coin}: missing column {col}")
        comp = float(r[col]) if np.isfinite(r[col]) else np.nan
        rows.append(
            {
                "base_coin": coin,
                "bar_start_ts_ms": int(ts_ms),
                "composite": comp,
                "r_vol": float(r["r_vol"]) if "r_vol" in r and np.isfinite(r["r_vol"]) else np.nan,
                "r_atr": float(r["r_atr"]) if "r_atr" in r and np.isfinite(r["r_atr"]) else np.nan,
                "atr_series": (
                    float(r["atr_series"])
                    if "atr_series" in r and np.isfinite(r["atr_series"])
                    else np.nan
                ),
                "volume": float(r["volume"]) if np.isfinite(r["volume"]) else np.nan,
                "regime_on": bool(r["regime_on"]) if "regime_on" in r and pd.notna(r["regime_on"]) else False,
                "variant": variant,
                "composite_col": col,
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    # Cross-sectional percentile of raw composite (ranking aid; heatmap uses raw)
    out["rank_xs"] = out["composite"].rank(method="average", pct=True)
    out = out.sort_values(
        "composite", ascending=False, kind="mergesort", na_position="last"
    ).reset_index(drop=True)
    ranks = np.full(len(out), np.nan, dtype=float)
    finite_n = int(out["composite"].notna().sum())
    if finite_n:
        ranks[:finite_n] = np.arange(1, finite_n + 1, dtype=float)
    out["rank"] = ranks
    return out


def build_ma_ratio_panel(
    coin_frames: dict[str, pd.DataFrame],
    timestamps: Iterable[int],
    *,
    variant: str = VARIANT_GEOM,
    long: Optional[int] = None,
    primary_long: Optional[int] = None,
    stride: int = 1,
    store_all_variants: bool = False,
    variants: Sequence[str] = VARIANTS,
) -> pd.DataFrame:
    """Panel of raw MA-ratio composites (default heatmap Z).

    ``composite`` = selected variant (raw ratio-based score, not cross-rank).
    ``rank`` = argmax order of that composite within each t (for top-1).
    ``rank_xs`` = cross-sectional percentile of composite within t (optional).

    When ``store_all_variants`` is True, also attach composite_<v> columns
    for the primary long (comparison / corr cell).
    """
    col = select_composite_column(variant, long=long, primary_long=primary_long)
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
        "rank_xs",
        "r_vol",
        "r_atr",
        "regime_on",
        "variant",
    ]
    if not ts_list or not coin_frames:
        return pd.DataFrame(columns=empty_cols)

    coins = sorted(coin_frames.keys())
    n_t, n_c = len(ts_list), len(coins)
    ts_index = {t: i for i, t in enumerate(ts_list)}
    ts_set = set(ts_index)

    # Detect which r_vol / r_atr columns match the selected long
    use_tagged = long is not None and primary_long is not None and int(long) != int(primary_long)
    r_vol_col = f"r_vol_L{int(long)}" if use_tagged else "r_vol"
    r_atr_col = f"r_atr_L{int(long)}" if use_tagged else "r_atr"

    comp = np.full((n_t, n_c), np.nan, dtype=float)
    r_vol = np.full((n_t, n_c), np.nan, dtype=float)
    r_atr = np.full((n_t, n_c), np.nan, dtype=float)
    regime = np.zeros((n_t, n_c), dtype=bool)
    extra_mats: dict[str, np.ndarray] = {}
    if store_all_variants:
        for v in variants:
            # primary-long untagged columns for side-by-side compare
            extra_mats[variant_column(v)] = np.full((n_t, n_c), np.nan, dtype=float)

    for j, coin in enumerate(coins):
        fr = coin_frames.get(coin)
        if fr is None or fr.empty:
            continue
        if col not in fr.columns:
            raise KeyError(f"{coin}: missing column {col}; build features with matching long/variant")
        sub = fr.loc[fr["bar_start_ts_ms"].isin(ts_set)]
        if sub.empty:
            continue
        sub = sub.drop_duplicates(subset=["bar_start_ts_ms"], keep="last")
        idx = sub["bar_start_ts_ms"].map(ts_index).to_numpy(dtype=int)
        comp[idx, j] = sub[col].to_numpy(dtype=float)
        if r_vol_col in sub.columns:
            r_vol[idx, j] = sub[r_vol_col].to_numpy(dtype=float)
        if r_atr_col in sub.columns:
            r_atr[idx, j] = sub[r_atr_col].to_numpy(dtype=float)
        if "regime_on" in sub.columns:
            regime[idx, j] = sub["regime_on"].fillna(False).to_numpy(dtype=bool)
        if store_all_variants:
            for vcol, mat in extra_mats.items():
                if vcol in sub.columns:
                    mat[idx, j] = sub[vcol].to_numpy(dtype=float)

    # Cross-sectional percentile of raw composite (ranking aid)
    rank_xs = pd.DataFrame(comp).rank(axis=1, method="average", pct=True).to_numpy()

    # Top-1 style ranks (1 = highest composite)
    order = np.argsort(np.where(np.isfinite(comp), -comp, np.inf), axis=1, kind="mergesort")
    ranks = np.full((n_t, n_c), np.nan, dtype=float)
    for i in range(n_t):
        row = comp[i]
        if not np.isfinite(row).any():
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
            if not np.isfinite(comp[i, j]):
                if not (np.isfinite(r_vol[i, j]) or np.isfinite(r_atr[i, j])):
                    continue
            row = {
                "bar_start_ts_ms": int(t),
                "base_coin": coin,
                "composite": float(comp[i, j]) if np.isfinite(comp[i, j]) else np.nan,
                "rank": float(ranks[i, j]) if np.isfinite(ranks[i, j]) else np.nan,
                "rank_xs": float(rank_xs[i, j]) if np.isfinite(rank_xs[i, j]) else np.nan,
                "r_vol": float(r_vol[i, j]) if np.isfinite(r_vol[i, j]) else np.nan,
                "r_atr": float(r_atr[i, j]) if np.isfinite(r_atr[i, j]) else np.nan,
                "regime_on": bool(regime[i, j]),
                "variant": variant,
            }
            if store_all_variants:
                for vcol, mat in extra_mats.items():
                    row[vcol] = float(mat[i, j]) if np.isfinite(mat[i, j]) else np.nan
            rows.append(row)
    return pd.DataFrame(rows)


def variant_corr_table(panel: pd.DataFrame, variants: Sequence[str] = VARIANTS) -> pd.DataFrame:
    """Pairwise Pearson corr of composite_<v> columns (needs store_all_variants panel)."""
    cols = [variant_column(v) for v in variants if variant_column(v) in panel.columns]
    if len(cols) < 2:
        return pd.DataFrame()
    return panel[cols].corr()


def top1_overlap(
    panel_a: pd.DataFrame,
    panel_b: pd.DataFrame,
) -> dict:
    """Share of timestamps where top-1 coin agrees between two panels."""
    def _leaders(p: pd.DataFrame) -> pd.Series:
        work = p.dropna(subset=["composite"])
        if work.empty:
            return pd.Series(dtype=object)
        if "rank" in work.columns and work["rank"].notna().any():
            lead = (
                work.loc[work["rank"] == 1, ["bar_start_ts_ms", "base_coin"]]
                .drop_duplicates("bar_start_ts_ms", keep="first")
                .set_index("bar_start_ts_ms")["base_coin"]
            )
            return lead
        idx = work.groupby("bar_start_ts_ms")["composite"].idxmax()
        return work.loc[idx].set_index("bar_start_ts_ms")["base_coin"]

    a = _leaders(panel_a)
    b = _leaders(panel_b)
    common = a.index.intersection(b.index)
    if len(common) == 0:
        return {"n_common_t": 0, "agree": None, "n_agree": 0}
    agree = (a.loc[common].to_numpy() == b.loc[common].to_numpy()).sum()
    return {
        "n_common_t": int(len(common)),
        "n_agree": int(agree),
        "agree": float(agree / len(common)),
    }


def formula_doc(variant: str) -> str:
    """One-line formula for CLI / notebook labels."""
    docs = {
        VARIANT_GEOM: "sqrt(r_vol * r_atr)",
        VARIANT_MEAN: "0.5*(r_vol + r_atr)",
        VARIANT_MIN: "min(r_vol, r_atr)",
        VARIANT_LOG_MEAN: "0.5*(log(max(r_vol,ε))+log(max(r_atr,ε)))",
        VARIANT_VOL_ONLY: "r_vol",
        VARIANT_ATR_ONLY: "r_atr",
    }
    if variant not in docs:
        raise ValueError(f"unknown variant {variant!r}")
    return docs[variant]


def numerator_formula(
    numerator: str = NUMERATOR_BLEND,
    *,
    blend_alpha: float = BLEND_ALPHA_DEFAULT,
) -> str:
    """r_* definition for labels."""
    if numerator == NUMERATOR_BLEND:
        return (
            f"r = (α·EMA_short + (1−α)·MA_short) / MA_long  "
            f"(α={float(blend_alpha):g})"
        )
    if numerator == NUMERATOR_EMA:
        return "r = EMA_short / MA_long"
    if numerator == NUMERATOR_MA:
        return "r = MA_short / MA_long"
    raise ValueError(f"numerator must be one of {NUMERATORS}, got {numerator!r}")


def snapshot_score_at_ts(
    coin_frames: dict[str, pd.DataFrame],
    ts_ms: int,
    *,
    score_col: str,
    component_cols: Sequence[str] = (),
) -> pd.DataFrame:
    """One row per coin at bar_start_ts_ms == ts_ms ranked by ``score_col`` (desc).

    Generic ranking helper for metric-variant Top‑N cells (MA / EMA / ema_pct_z).
    """
    rows = []
    for coin, fr in coin_frames.items():
        if fr is None or fr.empty:
            continue
        if score_col not in fr.columns:
            raise KeyError(f"{coin}: missing score column {score_col!r}")
        hit = fr.loc[fr["bar_start_ts_ms"] == ts_ms]
        if hit.empty:
            continue
        r = hit.iloc[-1]
        score = float(r[score_col]) if np.isfinite(r[score_col]) else np.nan
        row = {
            "base_coin": coin,
            "bar_start_ts_ms": int(ts_ms),
            "composite": score,
            "score_col": score_col,
        }
        for c in component_cols:
            if c in r.index and pd.notna(r[c]) and np.isfinite(r[c]):
                row[c] = float(r[c])
            else:
                row[c] = np.nan
        rows.append(row)
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out = out.sort_values(
        "composite", ascending=False, kind="mergesort", na_position="last"
    ).reset_index(drop=True)
    ranks = np.full(len(out), np.nan, dtype=float)
    finite_n = int(out["composite"].notna().sum())
    if finite_n:
        ranks[:finite_n] = np.arange(1, finite_n + 1, dtype=float)
    out["rank"] = ranks
    return out


def metrics_series_from_features(
    fr: pd.DataFrame,
    t0: int,
    t1: int,
    *,
    score_col: str,
    component_cols: Sequence[str] = (),
) -> pd.DataFrame:
    """Slice feature frame into a detail-plot metrics table (composite + components)."""
    if score_col not in fr.columns:
        raise KeyError(f"missing score column {score_col!r}")
    m = (fr["bar_start_ts_ms"] >= t0) & (fr["bar_start_ts_ms"] <= t1)
    sub = fr.loc[m].sort_values("bar_start_ts_ms").copy()
    keep = ["bar_start_ts_ms"]
    sub["composite"] = sub[score_col]
    keep.append("composite")
    for c in component_cols:
        if c in sub.columns:
            keep.append(c)
    return sub[keep].reset_index(drop=True)
