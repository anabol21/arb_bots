"""5-minute candles + intra-bucket stats from sparse L1 ticks."""

from __future__ import annotations

from typing import Any, Optional, Sequence

import numpy as np
import pandas as pd

from app.schema.lean_event import BAR_INTERVAL_MS
from research.gear22_quiet_regime_viz.quantiles import (
    TW_QUANTILE_NAMES,
    tick_hold_weights_ms,
    time_weighted_quantiles,
)

BAR_MS = int(BAR_INTERVAL_MS)  # 300_000

# Causal SMA windows on **candle closes** (number of completed 5m bars).
DEFAULT_MA_BARS: tuple[int, ...] = (3, 12)  # 15m and 60m lookbacks

# Compact click-to-inspect payloads (build-time; not full ticks in HTML).
DEFAULT_CANDLE_BINS = 32
DEFAULT_CANDLE_TEMPORAL_BINS = 16
DEFAULT_LATENCY_BINS = 24
DEFAULT_LATENCY_TEMPORAL_BINS = 12
LATENCY_OKX_COL = "okx_latency_ms"
LATENCY_BYBIT_COL = "bybit_latency_ms"

# Policy-aligned primary series (see app.policy.features).
SPREAD_LONG_COL = "spread_long"
SPREAD_SHORT_COL = "spread_short"


def floor_bar_start_ms(ts_ms: int, bar_ms: int = BAR_MS) -> int:
    return (int(ts_ms) // int(bar_ms)) * int(bar_ms)


def causal_sma(values: np.ndarray, window: int) -> np.ndarray:
    """Right-aligned (causal) simple moving average; NaN until window fills.

    Requires all ``window`` samples in the lookback to be finite (empty 5m
    buckets therefore break the MA until a full finite run rebuilds).
    """
    w = int(window)
    x = np.asarray(values, dtype="float64")
    out = np.full(x.shape, np.nan, dtype="float64")
    if w <= 0 or x.size == 0:
        return out
    if w == 1:
        return x.copy()
    for i in range(w - 1, x.size):
        sl = x[i - w + 1 : i + 1]
        if np.all(np.isfinite(sl)):
            out[i] = float(np.mean(sl))
    return out


def _empty_bucket(bar_start: int, bar_end: int) -> dict:
    row = {
        "bar_start_ms": bar_start,
        "bar_end_ms": bar_end,
        "open": np.nan,
        "high": np.nan,
        "low": np.nan,
        "close": np.nan,
        "tick_count": 0,
        "mean": np.nan,
        "std": np.nan,
        "min": np.nan,
        "max": np.nan,
        "q25": np.nan,
        "q75": np.nan,
        "iqr": np.nan,
        "gap_fraction": 1.0,
        "update_rate_hz": 0.0,
        "span_ms": 0,
    }
    for name in TW_QUANTILE_NAMES:
        row[name] = np.nan
    return row


def _bucket_stats(group: pd.DataFrame, value_col: str, bar_start: int, bar_ms: int) -> dict:
    y = group[value_col].to_numpy(dtype="float64")
    ts = group["event_local_ts_ms"].to_numpy(dtype="int64")
    finite = np.isfinite(y)
    yf = y[finite]
    tsf = ts[finite]
    n = int(yf.size)
    bar_end = bar_start + bar_ms
    if n == 0:
        return _empty_bucket(bar_start, bar_end)
    # OHLC in tick order (already sorted by caller).
    order = np.argsort(tsf, kind="mergesort")
    yf = yf[order]
    tsf = tsf[order]
    span_ms = int(tsf[-1] - tsf[0]) if n >= 2 else 0
    # Extent-based hole score: time before first tick + time after last tick.
    # n==1 → first==last → uncovered == bar_ms → gap_fraction 1.0.
    # (Inter-tick holes are the red vrects, not this panel.)
    uncovered = (int(tsf[0]) - bar_start) + (bar_end - int(tsf[-1]))
    gap_fraction = float(np.clip(float(uncovered) / float(bar_ms), 0.0, 1.0))
    q25, q75 = np.percentile(yf, [25, 75])
    weights = tick_hold_weights_ms(tsf, last_end_ms=bar_end)
    tw = time_weighted_quantiles(yf, weights)
    row = {
        "bar_start_ms": bar_start,
        "bar_end_ms": bar_end,
        "open": float(yf[0]),
        "high": float(np.max(yf)),
        "low": float(np.min(yf)),
        "close": float(yf[-1]),
        "tick_count": n,
        "mean": float(np.mean(yf)),
        "std": float(np.std(yf, ddof=0)) if n > 1 else 0.0,
        "min": float(np.min(yf)),
        "max": float(np.max(yf)),
        "q25": float(q25),
        "q75": float(q75),
        "iqr": float(q75 - q25),
        "gap_fraction": gap_fraction,
        "update_rate_hz": float(n) / (float(bar_ms) / 1000.0),
        "span_ms": span_ms,
    }
    row.update(tw)
    return row



def build_5m_bucket_stats(
    ticks: pd.DataFrame,
    *,
    value_col: str = SPREAD_LONG_COL,
    bar_ms: int = BAR_MS,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    fill_empty_buckets: bool = True,
    ma_bars: Sequence[int] = DEFAULT_MA_BARS,
) -> pd.DataFrame:
    """Build UTC-aligned 5m OHLC + intra-bucket stats for one coin.

    ``ticks`` must contain ``event_local_ts_ms`` and ``value_col``.
    When ``fill_empty_buckets`` is True, every 5m slot in
    ``[start_ms, end_ms)`` appears (empty → gap_fraction=1, no OHLC).
    """
    if value_col not in ticks.columns:
        raise KeyError(value_col)
    if ticks.empty and (start_ms is None or end_ms is None):
        return pd.DataFrame()

    ts = ticks["event_local_ts_ms"].to_numpy(dtype="int64")
    if start_ms is None:
        start_ms = floor_bar_start_ms(int(ts.min()), bar_ms)
    else:
        start_ms = floor_bar_start_ms(int(start_ms), bar_ms)
    if end_ms is None:
        end_ms = floor_bar_start_ms(int(ts.max()), bar_ms) + bar_ms
    else:
        end_ms = int(end_ms)

    work = ticks.sort_values("event_local_ts_ms", kind="mergesort").copy()
    work["bar_start_ms"] = (work["event_local_ts_ms"] // bar_ms) * bar_ms

    rows: list[dict] = []
    grouped = {int(k): g for k, g in work.groupby("bar_start_ms", sort=True)}
    if fill_empty_buckets:
        bar_starts = list(range(start_ms, end_ms, bar_ms))
    else:
        bar_starts = sorted(grouped.keys())

    for bs in bar_starts:
        if bs >= end_ms:
            break
        g = grouped.get(bs)
        if g is None or g.empty:
            rows.append(
                _bucket_stats(
                    pd.DataFrame(
                        {
                            "event_local_ts_ms": np.array([], dtype="int64"),
                            value_col: np.array([], dtype="float64"),
                        }
                    ),
                    value_col,
                    bs,
                    bar_ms,
                )
            )
        else:
            rows.append(_bucket_stats(g, value_col, bs, bar_ms))

    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["bar_start_dt"] = pd.to_datetime(out["bar_start_ms"], unit="ms", utc=True)
    out["bar_end_dt"] = pd.to_datetime(out["bar_end_ms"], unit="ms", utc=True)
    closes = out["close"].to_numpy(dtype="float64")
    for w in ma_bars:
        out[f"ma_{int(w)}"] = causal_sma(closes, int(w))
    return out


def downsample_ticks(
    ticks: pd.DataFrame,
    *,
    max_points: int = 4_000,
) -> pd.DataFrame:
    """Evenly keep ≤ ``max_points`` rows so the scatter still looks sparse."""
    n = len(ticks)
    if n <= max_points or max_points <= 0:
        return ticks
    if max_points == 1:
        return ticks.iloc[[0]].copy()
    idx = np.unique(
        np.round(np.linspace(0, n - 1, max_points)).astype(np.int64)
    )
    return ticks.iloc[idx].copy()


def _bar_hist_and_temporal(
    y: np.ndarray,
    ts: np.ndarray,
    *,
    bar_start: int,
    bar_ms: int,
    n_bins: int,
    n_temporal: int,
) -> dict[str, Any]:
    """Compact equal-weight hist + equal-time bin means for one bar."""
    finite = np.isfinite(y)
    yf = y[finite]
    tsf = ts[finite]
    n = int(yf.size)
    payload: dict[str, Any] = {
        "n": n,
        "lo": None,
        "hi": None,
        "c": [0] * max(int(n_bins), 0),
        "tv": [None] * max(int(n_temporal), 0),
    }
    if n == 0 or n_bins <= 0:
        return payload

    y_min = float(np.min(yf))
    y_max = float(np.max(yf))
    if y_min == y_max:
        pad = max(abs(y_min) * 1e-6, 1e-6)
        y_min -= pad
        y_max += pad
    counts, edges = np.histogram(yf, bins=int(n_bins), range=(y_min, y_max))
    payload["lo"] = round(float(edges[0]), 5)
    payload["hi"] = round(float(edges[-1]), 5)
    payload["c"] = [int(v) for v in counts.tolist()]

    n_t = int(n_temporal)
    if n_t > 0:
        # Equal-time bins over [bar_start, bar_end); mean of ticks in each slot.
        rel = (tsf.astype("float64") - float(bar_start)) / float(bar_ms)
        slot = np.clip(np.floor(rel * n_t).astype("int64"), 0, n_t - 1)
        sums = np.zeros(n_t, dtype="float64")
        cnts = np.zeros(n_t, dtype="int64")
        for i in range(n):
            s = int(slot[i])
            sums[s] += float(yf[i])
            cnts[s] += 1
        tv: list[float | None] = []
        for s in range(n_t):
            if cnts[s] > 0:
                tv.append(round(float(sums[s] / float(cnts[s])), 5))
            else:
                tv.append(None)
        payload["tv"] = tv
    return payload


def build_bar_inspect_payloads(
    ticks: pd.DataFrame,
    *,
    value_col: str = SPREAD_LONG_COL,
    bar_ms: int = BAR_MS,
    n_bins: int = DEFAULT_CANDLE_BINS,
    n_temporal: int = DEFAULT_CANDLE_TEMPORAL_BINS,
    latency_bins: int = DEFAULT_LATENCY_BINS,
    latency_temporal_bins: int = DEFAULT_LATENCY_TEMPORAL_BINS,
    side: str = "long",
) -> dict[int, dict[str, Any]]:
    """Build compact per-bar hist + temporal payloads keyed by ``bar_start_ms``.

    Includes optional venue latency hists (``okx_latency_ms`` /
    ``bybit_latency_ms``) when those columns exist. Non-finite latency values
    are skipped per venue; if a venue has zero finite samples in the bar, its
    sub-payload has ``n=0`` and empty counts.

    Intended for click-to-inspect HTML: tens of bins / coarse equal-time means,
    not full tick embedding. Empty bars are omitted.
    """
    if n_bins <= 0:
        return {}
    if value_col not in ticks.columns or ticks.empty:
        return {}
    work = ticks.sort_values("event_local_ts_ms", kind="mergesort")
    bar_starts = (work["event_local_ts_ms"].to_numpy(dtype="int64") // int(bar_ms)) * int(
        bar_ms
    )
    y_all = work[value_col].to_numpy(dtype="float64")
    ts_all = work["event_local_ts_ms"].to_numpy(dtype="int64")
    has_okx = LATENCY_OKX_COL in work.columns
    has_bybit = LATENCY_BYBIT_COL in work.columns
    okx_all = (
        work[LATENCY_OKX_COL].to_numpy(dtype="float64")
        if has_okx
        else None
    )
    bybit_all = (
        work[LATENCY_BYBIT_COL].to_numpy(dtype="float64")
        if has_bybit
        else None
    )
    out: dict[int, dict[str, Any]] = {}
    for bs in np.unique(bar_starts):
        mask = bar_starts == int(bs)
        payload = _bar_hist_and_temporal(
            y_all[mask],
            ts_all[mask],
            bar_start=int(bs),
            bar_ms=int(bar_ms),
            n_bins=int(n_bins),
            n_temporal=int(n_temporal),
        )
        if int(payload["n"]) <= 0:
            continue
        payload["side"] = str(side)
        payload["col"] = str(value_col)
        payload["bs"] = int(bs)
        payload["bar_ms"] = int(bar_ms)
        payload["nb"] = int(n_bins)
        payload["nt"] = int(n_temporal)
        # Latency: compact nested payloads; omit venue key entirely if column absent.
        lat: dict[str, Any] = {}
        ts_m = ts_all[mask]
        if okx_all is not None and int(latency_bins) > 0:
            lat["okx"] = _bar_hist_and_temporal(
                okx_all[mask],
                ts_m,
                bar_start=int(bs),
                bar_ms=int(bar_ms),
                n_bins=int(latency_bins),
                n_temporal=int(latency_temporal_bins),
            )
            lat["okx"]["col"] = LATENCY_OKX_COL
        if bybit_all is not None and int(latency_bins) > 0:
            lat["bybit"] = _bar_hist_and_temporal(
                bybit_all[mask],
                ts_m,
                bar_start=int(bs),
                bar_ms=int(bar_ms),
                n_bins=int(latency_bins),
                n_temporal=int(latency_temporal_bins),
            )
            lat["bybit"]["col"] = LATENCY_BYBIT_COL
        if lat:
            payload["lat"] = lat
            payload["nlb"] = int(latency_bins)
            payload["nlt"] = int(latency_temporal_bins)
        out[int(bs)] = payload
    return out


def align_inspect_customdata(
    buckets: pd.DataFrame,
    payloads: dict[int, dict[str, Any]],
) -> list[Any]:
    """Align inspect payloads to non-empty OHLC rows (candlestick ``customdata``)."""
    if buckets.empty:
        return []
    has_ohlc = buckets["tick_count"].fillna(0).astype(int) > 0
    rows = buckets.loc[has_ohlc]
    out: list[Any] = []
    for bs in rows["bar_start_ms"].to_numpy(dtype="int64"):
        out.append(payloads.get(int(bs)))
    return out
