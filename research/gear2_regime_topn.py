"""Causal Gear 1.5 Top-N lookup for gear 2 open-only entry (Stage 3 arm B).

Score canon (closed 1.5, not a new metric):
  r_* = (α·EMA_short + (1−α)·MA_short) / MA_long   α≈0.75 blend
  composite = sqrt(r_vol * r_atr)                   geom

Bars: ``output/okx_bar5m_hist_regime/`` (heatmap primary). Optional Bybit root
is accepted but default is OKX.

Causality: the bar covering ``[t−5m, t)`` (``bar_start_ts_ms = t − 5m``) is
used only for ticks with ``event_local_ts_ms >= t``. Equivalently, a tick at
``ts`` uses the last fully closed 5m bar:

    completed_bar_start = floor(ts / 5m) * 5m − 5m

Coins with ticks but no hist bars (QNT, USDC) are never in Top-N (fail closed).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from research.is_crypto import filter_crypto_coins, is_crypto
from research.rank_volatile_coins import (
    BYBIT_ROOT,
    OKX_ROOT,
    WARMUP_BARS_PAD,
    list_coins,
    load_hist_bars_recent,
    warmup_min_ts,
)
from research.regime_ma_ratio import (
    BLEND_ALPHA_DEFAULT,
    NUMERATOR_BLEND,
    SCORE_MODE_MA_RATIO,
    VARIANT_GEOM,
    MaRatioParams,
    build_ma_ratio_features,
    build_ma_ratio_panel,
    select_composite_column,
)

BAR_MS = 300_000
MA_SHORT_CANON = 6
MA_LONG_CANON = 288  # heatmap / closed 1.5 default (~1 day of 5m)


def completed_bar_start_ms(ts_ms: int) -> int:
    """``bar_start`` of ``[t−5m, t)`` used for a tick at ``ts`` (t = floor 5m)."""
    floored = (int(ts_ms) // BAR_MS) * BAR_MS
    return floored - BAR_MS


def canon_ma_params(*, long: int = MA_LONG_CANON) -> MaRatioParams:
    return MaRatioParams(
        short=MA_SHORT_CANON,
        long=int(long),
        numerator=NUMERATOR_BLEND,
        blend_alpha=BLEND_ALPHA_DEFAULT,
    )


def coin_in_topn(coin: str, ts_ms: int, topn_by_bar: dict) -> bool:
    """True iff ``coin`` is in the Top-N set of the last closed bar before ``ts``."""
    b = completed_bar_start_ms(int(ts_ms))
    s = topn_by_bar.get(b)
    if not s:
        return False
    return str(coin).upper() in s


def tick_window_for_completed_bar(bar_start_ms: int) -> tuple[int, int]:
    """Tick ``[lo, hi)`` ms that use completed bar ``bar_start`` for 1.5 / Top-N.

    ``completed_bar_start_ms(ts) == bar_start`` iff ``ts ∈ [bar_start+5m, bar_start+10m)``.
    """
    lo = int(bar_start_ms) + BAR_MS
    return lo, lo + BAR_MS


def topn_intervals_ms(
    coin: str,
    topn_by_bar: dict,
    *,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> list[tuple[int, int]]:
    """Merged ``[lo, hi)`` UTC-ms intervals when ``coin`` is in Top-N.

    Empty if the coin has no hist bars or is never in the Top-N map (fail closed;
    does not invent membership). Consecutive 5m memberships merge.
    """
    if not topn_by_bar:
        return []
    u = str(coin).upper()
    bars = [int(b) for b, s in topn_by_bar.items() if s and u in s]
    if not bars:
        return []
    bars.sort()
    out: list[tuple[int, int]] = []
    for b in bars:
        lo, hi = tick_window_for_completed_bar(b)
        if start_ms is not None and hi <= int(start_ms):
            continue
        if end_ms is not None and lo >= int(end_ms):
            continue
        if start_ms is not None:
            lo = max(lo, int(start_ms))
        if end_ms is not None:
            hi = min(hi, int(end_ms))
        if lo >= hi:
            continue
        if out and lo <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def topn_span_note(
    coin: str,
    intervals: list,
    *,
    has_hist: bool,
) -> Optional[str]:
    """Short caption for TOP-10 overlay (None if bands are present)."""
    if not has_hist:
        return "нет баров гира 1.5 — полос Топ-10 нет (не подставляются)"
    if not intervals:
        return f"{str(coin).upper()} ни разу не входила в Топ-10 в этом окне"
    return None


def _load_one_features(
    root: Path,
    coin: str,
    *,
    min_ts_ms: int,
    max_ts_ms: int,
    params: MaRatioParams,
) -> Optional[pd.DataFrame]:
    bars = load_hist_bars_recent(root, coin, min_ts_ms=min_ts_ms, max_ts_ms=max_ts_ms)
    if bars.empty:
        return None
    try:
        return build_ma_ratio_features(bars, params=params, variants=(VARIANT_GEOM,))
    except Exception:
        return None


def load_crypto_feature_frames(
    *,
    start_ms: int,
    end_ms: int,
    root: Optional[Path] = None,
    params: Optional[MaRatioParams] = None,
    workers: int = 8,
) -> tuple[dict, Path, list]:
    """Load OKX (default) hist bars + canonical blend features for crypto coins."""
    params = params or canon_ma_params()
    root = Path(root) if root is not None else OKX_ROOT
    if not root.is_dir():
        raise FileNotFoundError(f"hist bar root missing: {root}")
    load_min = min(
        warmup_min_ts(start_ms, params, score_mode=SCORE_MODE_MA_RATIO),
        warmup_min_ts(end_ms, params, score_mode=SCORE_MODE_MA_RATIO),
        start_ms - (int(params.long) + int(params.atr_n) + int(params.short) + WARMUP_BARS_PAD)
        * BAR_MS,
    )
    # Last bar start still < end_ms (tick window is [start, end)).
    max_bar = ((int(end_ms) - 1) // BAR_MS) * BAR_MS
    all_coins = list_coins(root)
    coins = filter_crypto_coins(all_coins, crypto=True)
    missing_hist = []
    frames: dict = {}

    def _one(coin: str):
        return coin, _load_one_features(
            root, coin, min_ts_ms=load_min, max_ts_ms=max_bar, params=params
        )

    n_workers = max(1, int(workers))
    if n_workers == 1:
        pairs = [_one(c) for c in coins]
    else:
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            pairs = list(pool.map(_one, coins))
    for coin, fr in pairs:
        if fr is None or fr.empty:
            missing_hist.append(coin)
            continue
        frames[coin] = fr
    return frames, root, missing_hist


def build_topn_by_bar(
    frames: dict,
    *,
    start_ms: int,
    end_ms: int,
    top_n: int = 10,
    params: Optional[MaRatioParams] = None,
) -> dict:
    """Map completed-bar ``bar_start_ts_ms`` → frozenset of Top-N coin codes.

    Ranking universe = coins present in ``frames`` (crypto with hist bars).
    Coins not in the map at a timestamp fail closed.
    """
    n = int(top_n)
    if n < 1:
        raise ValueError("top_n must be >= 1")
    params = params or canon_ma_params()
    first_bar = completed_bar_start_ms(int(start_ms))
    last_bar = completed_bar_start_ms(int(end_ms) - 1 if end_ms > start_ms else int(start_ms))
    if last_bar < first_bar:
        last_bar = first_bar
    timestamps = list(range(int(first_bar), int(last_bar) + BAR_MS, BAR_MS))
    col = select_composite_column(VARIANT_GEOM, long=None)
    panel = build_ma_ratio_panel(
        frames,
        timestamps,
        variant=VARIANT_GEOM,
        long=None,
        primary_long=int(params.long),
    )
    out: dict = {}
    if panel.empty:
        return out
    work = panel.dropna(subset=["composite"])
    if "rank" in work.columns:
        for ts, g in work.groupby("bar_start_ts_ms", sort=False):
            top = g.loc[g["rank"] <= n, "base_coin"].astype(str).str.upper()
            out[int(ts)] = frozenset(top.tolist())
        return out
    for ts, g in work.groupby("bar_start_ts_ms", sort=False):
        g = g.sort_values("composite", ascending=False, kind="mergesort")
        top = g["base_coin"].astype(str).str.upper().head(n)
        out[int(ts)] = frozenset(top.tolist())
    return out


def preview_topn_at(topn_by_bar: dict, ts_ms: int) -> list:
    b = completed_bar_start_ms(int(ts_ms))
    s = topn_by_bar.get(b, frozenset())
    return sorted(s)


def no_hist_tick_coins(tick_coins, hist_coins) -> list:
    """Tick coins that cannot enter Top-N because they have no hist bars."""
    hist = {str(c).upper() for c in hist_coins}
    out = []
    for c in tick_coins:
        u = str(c).upper()
        if u not in hist:
            out.append(u)
    return sorted(set(out))


def describe_canon(params: Optional[MaRatioParams] = None) -> str:
    p = params or canon_ma_params()
    return (
        f"1.5 arm B Top-N: blend α={p.blend_alpha:g} short={p.short} long={p.long} "
        f"variant=geom  (bar [t−5m, t) for ticks ≥ t)"
    )


def load_coin_ma_features(
    coin: str,
    *,
    start_ms: int,
    end_ms: int,
    root: Optional[Path] = None,
    params: Optional[MaRatioParams] = None,
) -> Optional[pd.DataFrame]:
    """Canonical 1.5 blend/geom features for one coin, or None if no hist bars."""
    params = params or canon_ma_params()
    root = Path(root) if root is not None else OKX_ROOT
    if not root.is_dir():
        return None
    load_min = min(
        warmup_min_ts(start_ms, params, score_mode=SCORE_MODE_MA_RATIO),
        warmup_min_ts(end_ms, params, score_mode=SCORE_MODE_MA_RATIO),
        start_ms
        - (int(params.long) + int(params.atr_n) + int(params.short) + WARMUP_BARS_PAD)
        * BAR_MS,
    )
    max_bar = ((int(end_ms) - 1) // BAR_MS) * BAR_MS
    return _load_one_features(
        root,
        str(coin).upper(),
        min_ts_ms=load_min,
        max_ts_ms=max_bar,
        params=params,
    )


def causal_composite_at_ticks(
    ts_ms,
    features: Optional[pd.DataFrame],
    *,
    col: Optional[str] = None,
) -> np.ndarray:
    """Step score of the last fully closed bar ``[t−5m, t)`` at each tick.

    Look-ahead of the bar that contains ``ts`` is not used. Missing hist /
    missing bar → NaN (do not invent a score).
    """
    ts = np.asarray(ts_ms, dtype="int64")
    out = np.full(ts.shape, np.nan, dtype="float64")
    if features is None or len(features) == 0:
        return out
    col = col or select_composite_column(VARIANT_GEOM, long=None)
    if col not in features.columns or "bar_start_ts_ms" not in features.columns:
        return out
    work = pd.DataFrame(
        {
            "bar": pd.to_numeric(features["bar_start_ts_ms"], errors="coerce"),
            "score": pd.to_numeric(features[col], errors="coerce"),
        }
    ).dropna(subset=["bar"])
    mapping = pd.Series(
        work["score"].to_numpy(dtype="float64"),
        index=work["bar"].astype("int64"),
    )
    mapping = mapping[~mapping.index.duplicated(keep="last")]
    bar_ids = (ts // BAR_MS) * BAR_MS - BAR_MS
    return mapping.reindex(bar_ids).to_numpy(dtype="float64")


# Re-export roots for notebook CONFIG.
OKX_BAR_ROOT = OKX_ROOT
BYBIT_BAR_ROOT = BYBIT_ROOT
IS_CRYPTO = is_crypto
