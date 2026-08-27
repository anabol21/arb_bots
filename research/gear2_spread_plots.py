"""Plotly spread time series + histograms with p50/p95/p99 (gear 2 overview / trades).

Line charts may be downsampled. Histograms / percentiles use whatever the caller
passes: raw values (``hist_long``) or all-tick binned counts (``hist_*_binned``).
Caption must say which.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

PCT_LEVELS: tuple[int, ...] = (50, 95, 99)
PCT_COLORS = {50: "#444444", 95: "#d67c00", 99: "#c0392b"}
LONG_COLOR = "#1f77b4"
SHORT_COLOR = "#ff7f0e"
PX_COLOR = "#2ca02c"
VOL_COLOR = "#9467bd"
TOPN_FILL = "rgba(255, 193, 7, 0.28)"
TOPN_LEGEND_NAME = "Топ-10 гира 1.5"
BYBIT_PX_NAME = "Bybit L1 mid (bid+ask)/2"
VOL_NAME = "1.5 geom √(r_vol·r_atr)  [t−5m, t)"
NO_HIST_VOL_NOTE = "нет баров гира 1.5 — оценка NaN (не подставляется)"
ALL_TICK_HIST_LO = -10.0
ALL_TICK_HIST_HI = 10.0
ALL_TICK_HIST_BINS = 20_000  # width 0.001 pp; p-tiles interpolated from CDF
ALL_TICK_DISPLAY_BARS = 80
# Display-only axis window (does not change p50/p95/p99).
HIST_VIEW_Q_LO = 0.5
HIST_VIEW_Q_HI = 99.5
HIST_VIEW_PAD_FRAC = 0.12
HIST_VIEW_PAD_MIN = 0.05  # percentage points
HIST_VIEW_MIN_SPAN = 0.40
AXIS_WINDOW_NOTE = (
    "ось гистограмм — тело выборки (квантили 0,5–99,5 по всем тикам); "
    "хвосты обрезаны только на оси, не в p50/p95/p99"
)
_PCT_VLINE_POS = {50: "top left", 95: "top", 99: "top right"}
MS_PER_DAY = 86_400_000


def finite_values(y) -> np.ndarray:
    a = np.asarray(y, dtype="float64")
    if a.size == 0:
        return a
    return a[np.isfinite(a)]


def spread_percentiles(y, levels: Sequence[int] = PCT_LEVELS) -> dict:
    """Percentiles of finite values. Empty dict if no sample."""
    a = finite_values(y)
    if a.size == 0:
        return {}
    return {int(p): float(np.percentile(a, p)) for p in levels}


def format_pcts(pcts: dict) -> str:
    if not pcts:
        return "no finite sample"
    parts = [f"p{p}={pcts[p]:.4f}" for p in PCT_LEVELS if p in pcts]
    return "  ".join(parts)


class RunningSpreadHist:
    """Fixed-bin all-tick histogram. Percentiles interpolated from the CDF.

    Bin width = (hi−lo)/n_bins. Values outside ``[lo, hi)`` go to overflow
    counts (they still contribute to n and to extreme percentiles).
    """

    def __init__(
        self,
        *,
        lo: float = ALL_TICK_HIST_LO,
        hi: float = ALL_TICK_HIST_HI,
        n_bins: int = ALL_TICK_HIST_BINS,
    ) -> None:
        if n_bins < 2 or hi <= lo:
            raise ValueError("invalid hist range")
        self.lo = float(lo)
        self.hi = float(hi)
        self.n_bins = int(n_bins)
        self.width = (self.hi - self.lo) / self.n_bins
        self.counts = np.zeros(self.n_bins, dtype=np.int64)
        self.n_below = 0
        self.n_above = 0
        self.n_finite = 0

    def update(self, values) -> None:
        a = finite_values(values)
        if a.size == 0:
            return
        self.n_finite += int(a.size)
        below = a < self.lo
        above = a >= self.hi
        self.n_below += int(below.sum())
        self.n_above += int(above.sum())
        mid = a[~below & ~above]
        if mid.size == 0:
            return
        idx = np.floor((mid - self.lo) / self.width).astype(np.int64)
        np.clip(idx, 0, self.n_bins - 1, out=idx)
        np.add.at(self.counts, idx, 1)

    def merge_from(self, other: "RunningSpreadHist") -> None:
        if other.n_bins != self.n_bins or other.lo != self.lo or other.hi != self.hi:
            raise ValueError("hist merge requires identical edges")
        self.counts += other.counts
        self.n_below += other.n_below
        self.n_above += other.n_above
        self.n_finite += other.n_finite

    def edges(self) -> np.ndarray:
        return np.linspace(self.lo, self.hi, self.n_bins + 1)

    def percentiles(self, levels: Sequence[int] = PCT_LEVELS) -> dict:
        return percentiles_from_hist(
            self.edges(),
            self.counts,
            n_below=self.n_below,
            n_above=self.n_above,
            levels=levels,
        )

    def view_range(self, pcts: Optional[dict] = None):
        """Axis window from the full-tick CDF. Does not change stored counts."""
        if pcts is None:
            pcts = self.percentiles()
        return hist_view_range(
            self.edges(),
            self.counts,
            n_below=self.n_below,
            n_above=self.n_above,
            pcts=pcts,
        )

    def display_bars(self, n_bars: int = ALL_TICK_DISPLAY_BARS, *, window: bool = True):
        """Coarse bars for Plotly. Default: body window, not the full ±10% domain."""
        if not window:
            return coarsen_hist(
                self.edges(),
                self.counts,
                n_bars=n_bars,
                n_below=self.n_below,
                n_above=self.n_above,
            )
        x0, x1, meta = self.view_range()
        return coarsen_hist_window(
            self.edges(),
            self.counts,
            x0=x0,
            x1=x1,
            n_bars=n_bars,
            n_left=int(meta["n_left"]),
            n_right=int(meta["n_right"]),
        )


def _cdf_quantile(
    edges,
    counts,
    q,
    *,
    n_below: int = 0,
    n_above: int = 0,
):
    """Linear CDF interpolation; ``q`` is 0–100. Same rule as p50/p95/p99."""
    edges = np.asarray(edges, dtype="float64")
    counts = np.asarray(counts, dtype="float64")
    n = float(n_below) + float(counts.sum()) + float(n_above)
    if n <= 0:
        return None
    masses = np.concatenate(([float(n_below)], counts, [float(n_above)]))
    cdf = np.concatenate(([0.0], np.cumsum(masses)))
    x = np.concatenate(([edges[0]], edges, [edges[-1]]))
    target = (float(q) / 100.0) * (n - 1.0) if n > 1 else 0.0
    j = int(np.searchsorted(cdf, target, side="left"))
    if j <= 0:
        return float(edges[0])
    if j >= len(cdf):
        return float(edges[-1])
    c0, c1 = cdf[j - 1], cdf[j]
    x0, x1 = x[j - 1], x[j]
    if c1 <= c0:
        return float(x1)
    t = (target - c0) / (c1 - c0)
    return float(x0 + t * (x1 - x0))


def percentiles_from_hist(
    edges,
    counts,
    *,
    n_below: int = 0,
    n_above: int = 0,
    levels: Sequence[int] = PCT_LEVELS,
) -> dict:
    """Linear CDF interpolation on a histogram plus overflow masses.

    Not bit-identical to ``np.percentile`` on the raw sample; error is bounded
    by the interior bin width except when the quantile sits in overflow.
    """
    out = {}
    for p in levels:
        v = _cdf_quantile(
            edges, counts, p, n_below=n_below, n_above=n_above
        )
        if v is not None:
            out[int(p)] = v
    return out


def _mass_outside(edges, counts, x0: float, x1: float, n_below: int, n_above: int):
    edges = np.asarray(edges, dtype="float64")
    counts = np.asarray(counts, dtype="int64")
    n_left = int(n_below) + int(counts[edges[1:] <= x0].sum()) if counts.size else int(n_below)
    n_right = int(n_above) + int(counts[edges[:-1] >= x1].sum()) if counts.size else int(n_above)
    return n_left, n_right


def hist_view_range(
    edges,
    counts,
    *,
    n_below: int = 0,
    n_above: int = 0,
    pcts: Optional[dict] = None,
    q_lo: float = HIST_VIEW_Q_LO,
    q_hi: float = HIST_VIEW_Q_HI,
):
    """Tight x-window for display. Percentile *numbers* are not recomputed here."""
    edges = np.asarray(edges, dtype="float64")
    counts = np.asarray(counts, dtype="int64")
    domain_lo = float(edges[0]) if edges.size else ALL_TICK_HIST_LO
    domain_hi = float(edges[-1]) if edges.size else ALL_TICK_HIST_HI
    empty = {
        "clipped": False,
        "n_left": 0,
        "n_right": 0,
        "note": "",
    }
    x_lo = _cdf_quantile(edges, counts, q_lo, n_below=n_below, n_above=n_above)
    x_hi = _cdf_quantile(edges, counts, q_hi, n_below=n_below, n_above=n_above)
    if x_lo is None or x_hi is None:
        return domain_lo, domain_hi, empty
    if pcts:
        for p in PCT_LEVELS:
            v = pcts.get(p)
            if v is not None and np.isfinite(v):
                x_lo = min(x_lo, float(v))
                x_hi = max(x_hi, float(v))
    span = float(x_hi) - float(x_lo)
    if not np.isfinite(span) or span <= 0:
        mid = float(x_lo) if np.isfinite(x_lo) else 0.0
        x_lo, x_hi = mid - 0.5 * HIST_VIEW_MIN_SPAN, mid + 0.5 * HIST_VIEW_MIN_SPAN
        span = HIST_VIEW_MIN_SPAN
    pad = max(HIST_VIEW_PAD_MIN, HIST_VIEW_PAD_FRAC * span)
    x0 = float(x_lo) - pad
    x1 = float(x_hi) + pad
    if (x1 - x0) < HIST_VIEW_MIN_SPAN:
        mid = 0.5 * (x0 + x1)
        x0, x1 = mid - 0.5 * HIST_VIEW_MIN_SPAN, mid + 0.5 * HIST_VIEW_MIN_SPAN
    x0 = max(x0, domain_lo)
    x1 = min(x1, domain_hi)
    if x1 <= x0:
        x0, x1 = domain_lo, domain_hi
    n_left, n_right = _mass_outside(edges, counts, x0, x1, n_below, n_above)
    clipped = (n_left + n_right) > 0
    note = ""
    if clipped:
        note = (
            f"хвосты вне окна оси: слева {n_left}, справа {n_right} "
            "(p50/p95/p99 без изменения)"
        )
    return x0, x1, {
        "clipped": clipped,
        "n_left": n_left,
        "n_right": n_right,
        "note": note,
    }


def values_view_range(values, pcts: Optional[dict] = None):
    """Same display window for an unbinned sample. Does not change ``pcts``."""
    a = finite_values(values)
    empty = {"clipped": False, "n_left": 0, "n_right": 0, "note": ""}
    if a.size == 0:
        return -0.5, 0.5, empty
    if a.size >= 20:
        x_lo = float(np.percentile(a, HIST_VIEW_Q_LO))
        x_hi = float(np.percentile(a, HIST_VIEW_Q_HI))
    else:
        x_lo, x_hi = float(np.min(a)), float(np.max(a))
    if pcts:
        for p in PCT_LEVELS:
            v = pcts.get(p)
            if v is not None and np.isfinite(v):
                x_lo = min(x_lo, float(v))
                x_hi = max(x_hi, float(v))
    span = x_hi - x_lo
    if not np.isfinite(span) or span <= 0:
        mid = x_lo if np.isfinite(x_lo) else 0.0
        x_lo, x_hi = mid - 0.5 * HIST_VIEW_MIN_SPAN, mid + 0.5 * HIST_VIEW_MIN_SPAN
        span = HIST_VIEW_MIN_SPAN
    pad = max(HIST_VIEW_PAD_MIN, HIST_VIEW_PAD_FRAC * span)
    x0, x1 = x_lo - pad, x_hi + pad
    if (x1 - x0) < HIST_VIEW_MIN_SPAN:
        mid = 0.5 * (x0 + x1)
        x0, x1 = mid - 0.5 * HIST_VIEW_MIN_SPAN, mid + 0.5 * HIST_VIEW_MIN_SPAN
    n_left = int((a < x0).sum())
    n_right = int((a > x1).sum())
    clipped = (n_left + n_right) > 0
    note = ""
    if clipped:
        note = (
            f"хвосты вне окна оси: слева {n_left}, справа {n_right} "
            "(p50/p95/p99 без изменения)"
        )
    return float(x0), float(x1), {
        "clipped": clipped,
        "n_left": n_left,
        "n_right": n_right,
        "note": note,
    }


def coarsen_hist(
    edges,
    counts,
    *,
    n_bars: int = ALL_TICK_DISPLAY_BARS,
    n_below: int = 0,
    n_above: int = 0,
) -> tuple[np.ndarray, np.ndarray]:
    """Merge fine bins to ``n_bars`` for Plotly. Overflow added to ends."""
    edges = np.asarray(edges, dtype="float64")
    counts = np.asarray(counts, dtype="int64")
    n = int(counts.size)
    if n == 0:
        return edges, counts
    k = max(1, min(int(n_bars), n))
    # group fine bins into k groups
    idx = np.linspace(0, n, k + 1, dtype=int)
    new_counts = np.array(
        [int(counts[idx[i] : idx[i + 1]].sum()) for i in range(k)],
        dtype=np.int64,
    )
    new_edges = np.array([edges[idx[i]] for i in range(k)] + [edges[-1]], dtype="float64")
    new_counts[0] += int(n_below)
    new_counts[-1] += int(n_above)
    return new_edges, new_counts


def coarsen_hist_window(
    edges,
    counts,
    *,
    x0: float,
    x1: float,
    n_bars: int = ALL_TICK_DISPLAY_BARS,
    n_left: int = 0,
    n_right: int = 0,
) -> tuple[np.ndarray, np.ndarray]:
    """Coarsen only fine bins that overlap ``[x0, x1]`` (display window)."""
    edges = np.asarray(edges, dtype="float64")
    counts = np.asarray(counts, dtype="int64")
    if counts.size == 0 or not np.isfinite(x0) or not np.isfinite(x1) or x1 <= x0:
        return np.array([0.0, 1.0], dtype="float64"), np.zeros(1, dtype=np.int64)
    left = edges[:-1]
    right = edges[1:]
    mask = (right > x0) & (left < x1)
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return (
            np.array([float(x0), float(x1)], dtype="float64"),
            np.zeros(1, dtype=np.int64),
        )
    i0, i1 = int(idx[0]), int(idx[-1])
    sub_e = edges[i0 : i1 + 2].copy()
    sub_c = counts[i0 : i1 + 1].copy()
    sub_e[0] = float(x0)
    sub_e[-1] = float(x1)
    return coarsen_hist(
        sub_e, sub_c, n_bars=n_bars, n_below=int(n_left), n_above=int(n_right)
    )


def hist_from_values(
    values,
    *,
    n_bars: int = ALL_TICK_DISPLAY_BARS,
    lo: Optional[float] = None,
    hi: Optional[float] = None,
) -> tuple[np.ndarray, np.ndarray, dict]:
    """Exact ``np.percentile`` plus a display histogram of **all** finite values."""
    a = finite_values(values)
    pcts = spread_percentiles(a)
    if a.size == 0:
        return np.array([0.0, 1.0]), np.zeros(1, dtype=np.int64), pcts
    if lo is None or hi is None:
        if a.size >= 20:
            lo_v = float(np.percentile(a, 0.5))
            hi_v = float(np.percentile(a, 99.5))
        else:
            lo_v, hi_v = float(np.min(a)), float(np.max(a))
        if not np.isfinite(lo_v) or not np.isfinite(hi_v) or lo_v >= hi_v:
            lo_v, hi_v = float(np.min(a)), float(np.max(a))
            if lo_v >= hi_v:
                hi_v = lo_v + 1e-6
        lo = lo_v if lo is None else lo
        hi = hi_v if hi is None else hi
    counts, edges = np.histogram(a, bins=int(n_bars), range=(float(lo), float(hi)))
    n_below = int((a < lo).sum())
    n_above = int((a >= hi).sum())
    counts = counts.astype(np.int64, copy=False)
    counts[0] += n_below
    counts[-1] += n_above
    return edges, counts, pcts


class CoinAllTickStats:
    """Per-coin all-tick day counts + running spread histograms (streamed)."""

    def __init__(
        self,
        *,
        lo: float = ALL_TICK_HIST_LO,
        hi: float = ALL_TICK_HIST_HI,
        n_bins: int = ALL_TICK_HIST_BINS,
    ) -> None:
        self.long = RunningSpreadHist(lo=lo, hi=hi, n_bins=n_bins)
        self.short = RunningSpreadHist(lo=lo, hi=hi, n_bins=n_bins)
        self.n_ticks = 0
        self.day_counts: Counter = Counter()

    def update(self, ts_ms, spread_long, spread_short) -> None:
        ts = np.asarray(ts_ms, dtype="int64")
        sl = np.asarray(spread_long, dtype="float64")
        ss = np.asarray(spread_short, dtype="float64")
        n = int(ts.size)
        self.n_ticks += n
        self.long.update(sl)
        self.short.update(ss)
        if n == 0:
            return
        unix_days = ts // MS_PER_DAY
        uniq, cnt = np.unique(unix_days, return_counts=True)
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        for d, c in zip(uniq.tolist(), cnt.tolist()):
            day = (epoch + timedelta(days=int(d))).strftime("%Y-%m-%d")
            self.day_counts[day] += int(c)

    def merge_from(self, other: "CoinAllTickStats") -> None:
        self.long.merge_from(other.long)
        self.short.merge_from(other.short)
        self.n_ticks += other.n_ticks
        self.day_counts.update(other.day_counts)


def add_hist_with_pcts(
    fig: go.Figure,
    values,
    *,
    row: int,
    col: int,
    name: str,
    color: str,
) -> dict:
    """Histogram of ``values`` plus vertical p50/p95/p99 lines and labels."""
    a = finite_values(values)
    pcts = spread_percentiles(a)
    x0, x1, _meta = values_view_range(a, pcts if a.size else None)
    nbins = max(20, min(ALL_TICK_DISPLAY_BARS, 40))
    size = (x1 - x0) / float(nbins) if x1 > x0 else 0.05
    fig.add_trace(
        go.Histogram(
            x=a.tolist() if a.size else None,
            name=name,
            marker=dict(color=color),
            opacity=0.75,
            showlegend=False,
            xbins=dict(start=x0, end=x1, size=size),
            autobinx=False,
            hovertemplate=name + "=%{x:.4f}%  n=%{y}<extra></extra>",
        ),
        row=row,
        col=col,
    )
    for p in PCT_LEVELS:
        if p not in pcts:
            continue
        v = pcts[p]
        fig.add_vline(
            x=v,
            line_dash="dash",
            line_color=PCT_COLORS[p],
            line_width=1.5,
            annotation_text=f"p{p}={v:.3f}",
            annotation_position=_PCT_VLINE_POS.get(p, "top"),
            annotation_font=dict(size=9, color=PCT_COLORS[p]),
            annotation_yshift=-14,
            row=row,
            col=col,
        )
    fig.update_xaxes(
        title_text=f"{name} %",
        range=[x0, x1],
        autorange=False,
        row=row,
        col=col,
    )
    fig.update_yaxes(title_text="count", row=row, col=col)
    return pcts


def _add_pct_vlines(fig: go.Figure, pcts: dict, *, row: int, col: int) -> None:
    for p in PCT_LEVELS:
        if p not in pcts:
            continue
        v = pcts[p]
        fig.add_vline(
            x=v,
            line_dash="dash",
            line_color=PCT_COLORS[p],
            line_width=1.5,
            annotation_text=f"p{p}={v:.3f}",
            annotation_position=_PCT_VLINE_POS.get(p, "top"),
            annotation_font=dict(size=9, color=PCT_COLORS[p]),
            annotation_yshift=-14,
            row=row,
            col=col,
        )


def add_hist_bars(
    fig: go.Figure,
    edges,
    counts,
    *,
    row: int,
    col: int,
    name: str,
    color: str,
    pcts: Optional[dict] = None,
) -> dict:
    """Bar histogram from pre-binned all-tick counts (not a Plotly sample of values)."""
    edges = np.asarray(edges, dtype="float64")
    counts = np.asarray(counts, dtype="float64")
    if edges.size < 2:
        fig.add_trace(
            go.Bar(x=[0], y=[0], name=name, showlegend=False, marker=dict(color=color)),
            row=row,
            col=col,
        )
        fig.update_xaxes(title_text=f"{name} %", row=row, col=col)
        fig.update_yaxes(title_text="число (все тики)", row=row, col=col)
        return pcts or {}
    centers = 0.5 * (edges[:-1] + edges[1:])
    widths = np.diff(edges)
    fig.add_trace(
        go.Bar(
            x=centers.tolist(),
            y=counts.tolist(),
            width=widths.tolist(),
            name=name,
            marker=dict(color=color),
            opacity=0.75,
            showlegend=False,
            hovertemplate=name + " bin=%{x:.4f}%  n=%{y}<extra></extra>",
        ),
        row=row,
        col=col,
    )
    if pcts is None:
        pcts = percentiles_from_hist(edges, counts)
    _add_pct_vlines(fig, pcts, row=row, col=col)
    x0 = float(edges[0])
    x1 = float(edges[-1])
    if pcts:
        for p in PCT_LEVELS:
            v = pcts.get(p)
            if v is not None and np.isfinite(v):
                x0 = min(x0, float(v))
                x1 = max(x1, float(v))
        pad = max(HIST_VIEW_PAD_MIN, 0.04 * (x1 - x0) if x1 > x0 else HIST_VIEW_PAD_MIN)
        x0 -= pad
        x1 += pad
    fig.update_xaxes(
        title_text=f"{name} % (все тики)",
        range=[x0, x1],
        autorange=False,
        row=row,
        col=col,
    )
    fig.update_yaxes(title_text="число (все тики)", row=row, col=col)
    return pcts


def add_topn_spans(
    fig: go.Figure,
    intervals_ms: Sequence[tuple[int, int]],
    *,
    rows: Sequence[int] = (1, 3, 4),
) -> None:
    """UTC-aligned Top-10 bands on spread / Bybit mid / 1.5 score panels."""
    if not intervals_ms:
        return
    for lo, hi in intervals_ms:
        x0 = pd.to_datetime(int(lo), unit="ms", utc=True)
        x1 = pd.to_datetime(int(hi), unit="ms", utc=True)
        for row in rows:
            fig.add_vrect(
                x0=x0,
                x1=x1,
                fillcolor=TOPN_FILL,
                opacity=1.0,
                line_width=0,
                layer="below",
                row=int(row),
                col=1,
            )
    fig.add_trace(
        go.Scatter(
            x=[None],
            y=[None],
            mode="markers",
            name=TOPN_LEGEND_NAME,
            marker=dict(size=12, color="rgba(255, 193, 7, 0.85)", symbol="square"),
        ),
        row=1,
        col=1,
    )


def bybit_l1_mid(bid, ask) -> np.ndarray:
    """L1 mid from Bybit bid/ask. Non-positive or non-finite → NaN."""
    b = np.asarray(bid, dtype="float64")
    a = np.asarray(ask, dtype="float64")
    mid = (b + a) / 2.0
    bad = ~np.isfinite(b) | ~np.isfinite(a) | (b <= 0) | (a <= 0)
    mid[bad] = np.nan
    return mid


def xy_with_gaps(frame, ycol: str, gap_ms: int, *, ts_col: str = "event_local_ts_ms", x_col: str = "event_dt"):
    """Insert None breaks when consecutive ticks are more than ``gap_ms`` apart."""
    ts = frame[ts_col].to_numpy(dtype="float64", copy=False)
    y = frame[ycol].to_numpy(dtype="float64", copy=False)
    x_dt = frame[x_col].to_list()
    xs = []
    ys = []
    for i in range(len(frame)):
        if i > 0 and (ts[i] - ts[i - 1]) > float(gap_ms):
            xs.append(None)
            ys.append(None)
        xs.append(x_dt[i])
        ys.append(float(y[i]) if y[i] == y[i] else None)
    return xs, ys


def _as_none_y(x) -> list:
    n = 0 if x is None else len(list(x))
    return [None] * n


def make_spread_ts_hist_figure(
    *,
    x_long,
    y_long,
    x_short,
    y_short,
    hist_long=None,
    hist_short=None,
    title: str,
    thresh: Optional[float] = None,
    height: int = 860,
    width: Optional[int] = None,
    use_gl: bool = True,
    connectgaps: bool = False,
    hist_note: Optional[str] = None,
    hover_long: Optional[Sequence[str]] = None,
    hover_short: Optional[Sequence[str]] = None,
    x_px=None,
    y_px=None,
    x_vol=None,
    y_vol=None,
    px_name: str = BYBIT_PX_NAME,
    vol_name: str = VOL_NAME,
    vol_note: Optional[str] = None,
    hist_long_binned=None,
    hist_short_binned=None,
    hist_long_pcts: Optional[dict] = None,
    hist_short_pcts: Optional[dict] = None,
    topn_intervals_ms: Optional[Sequence[tuple[int, int]]] = None,
    topn_note: Optional[str] = None,
) -> go.Figure:
    """Spread (+ hists) on top; Bybit L1 mid; causal 1.5 geom score. UTC-aligned.

    Histograms: pass ``hist_*_binned=(edges, counts)`` for all-tick bars, or
    ``hist_long``/``hist_short`` values (Plotly samples those points).
    ``topn_intervals_ms`` shades [lo, hi) when the coin is in Gear 1.5 Top-10.
    """
    scatter = go.Scattergl if use_gl else go.Scatter
    if x_px is None:
        x_px = x_long
        y_px = _as_none_y(x_long) if y_px is None else y_px
    if y_px is None:
        y_px = _as_none_y(x_px)
    if x_vol is None:
        x_vol = x_long
        y_vol = _as_none_y(x_long) if y_vol is None else y_vol
    if y_vol is None:
        y_vol = _as_none_y(x_vol)
    fig = make_subplots(
        rows=4,
        cols=2,
        column_widths=[0.62, 0.38],
        row_heights=[0.30, 0.22, 0.24, 0.24],
        specs=[
            [{"rowspan": 2}, {}],
            [None, {}],
            [{}, None],
            [{}, None],
        ],
        horizontal_spacing=0.10,
        vertical_spacing=0.10,
    )
    long_kw = dict(
        x=x_long,
        y=y_long,
        mode="lines",
        name="spread_long",
        line=dict(width=1, color=LONG_COLOR),
        connectgaps=connectgaps,
        hovertemplate="%{x}<br>spread_long=%{y:.4f}%<extra></extra>",
    )
    short_kw = dict(
        x=x_short,
        y=y_short,
        mode="lines",
        name="spread_short",
        line=dict(width=1, color=SHORT_COLOR),
        connectgaps=connectgaps,
        hovertemplate="%{x}<br>spread_short=%{y:.4f}%<extra></extra>",
    )
    if hover_long is not None:
        long_kw["customdata"] = list(hover_long)
        long_kw["hovertemplate"] = (
            "%{x}<br>spread_long=%{y:.4f}%<br>%{customdata}<extra></extra>"
        )
    if hover_short is not None:
        short_kw["customdata"] = list(hover_short)
        short_kw["hovertemplate"] = (
            "%{x}<br>spread_short=%{y:.4f}%<br>%{customdata}<extra></extra>"
        )
    fig.add_trace(scatter(**long_kw), row=1, col=1)
    fig.add_trace(scatter(**short_kw), row=1, col=1)
    if thresh is not None:
        fig.add_hline(
            y=float(thresh),
            line_dash="dot",
            line_color="#2ca02c",
            annotation_text=f"+θ={float(thresh):g}",
            annotation_position="top right",
            annotation_font=dict(size=10),
            row=1,
            col=1,
        )
        fig.add_hline(
            y=-float(thresh),
            line_dash="dot",
            line_color="#d62728",
            annotation_text=f"−θ={-float(thresh):g}",
            annotation_position="bottom right",
            annotation_font=dict(size=10),
            row=1,
            col=1,
        )
    if topn_intervals_ms:
        add_topn_spans(fig, topn_intervals_ms, rows=(1, 3, 4))
    if hist_long_binned is not None:
        edges_l, counts_l = hist_long_binned
        pct_l = add_hist_bars(
            fig,
            edges_l,
            counts_l,
            row=1,
            col=2,
            name="spread_long",
            color=LONG_COLOR,
            pcts=hist_long_pcts,
        )
    else:
        pct_l = add_hist_with_pcts(
            fig,
            hist_long if hist_long is not None else [],
            row=1,
            col=2,
            name="spread_long",
            color=LONG_COLOR,
        )
        if hist_long_pcts:
            pct_l = hist_long_pcts
    if hist_short_binned is not None:
        edges_s, counts_s = hist_short_binned
        pct_s = add_hist_bars(
            fig,
            edges_s,
            counts_s,
            row=2,
            col=2,
            name="spread_short",
            color=SHORT_COLOR,
            pcts=hist_short_pcts,
        )
    else:
        pct_s = add_hist_with_pcts(
            fig,
            hist_short if hist_short is not None else [],
            row=2,
            col=2,
            name="spread_short",
            color=SHORT_COLOR,
        )
        if hist_short_pcts:
            pct_s = hist_short_pcts
    fig.add_trace(
        scatter(
            x=x_px,
            y=y_px,
            mode="lines",
            name=px_name,
            line=dict(width=1, color=PX_COLOR),
            connectgaps=connectgaps,
            hovertemplate="%{x}<br>" + px_name + "=%{y:.6g}<extra></extra>",
        ),
        row=3,
        col=1,
    )
    y_vol_arr = np.asarray(y_vol, dtype="float64")
    vol_finite = bool(y_vol_arr.size and np.isfinite(y_vol_arr).any())
    fig.add_trace(
        go.Scatter(
            x=x_vol,
            y=y_vol,
            mode="lines",
            name=vol_name,
            line=dict(width=1.4, color=VOL_COLOR, shape="hv"),
            connectgaps=connectgaps,
            hovertemplate="%{x}<br>" + vol_name + "=%{y:.4f}<extra></extra>",
        ),
        row=4,
        col=1,
    )
    fig.update_xaxes(title_text="", showticklabels=True, row=1, col=1)
    fig.update_xaxes(title_text="", matches="x", row=3, col=1)
    fig.update_xaxes(title_text="UTC", matches="x", row=4, col=1)
    fig.update_yaxes(title_text="spread %", row=1, col=1)
    fig.update_yaxes(title_text="Bybit L1 mid", row=3, col=1)
    y_vol_title = "1.5 geom" if vol_finite else "1.5 geom (empty)"
    fig.update_yaxes(title_text=y_vol_title, row=4, col=1)
    if not vol_finite and not vol_note:
        vol_note = NO_HIST_VOL_NOTE
    note = hist_note or "квантили по выборке гистограммы"
    sub = f"{title}<br><sup>{note}</sup>"
    if vol_note:
        sub = f"{title}<br><sup>{note}  ·  {vol_note}</sup>"
    fig.update_xaxes(automargin=True, tickfont=dict(size=10), title_font=dict(size=11))
    fig.update_yaxes(automargin=True, tickfont=dict(size=10), title_font=dict(size=11))
    fig.update_layout(
        title=dict(
            text=sub,
            x=0.01,
            xanchor="left",
            y=0.995,
            yanchor="top",
            yref="container",
            font=dict(size=15),
            pad=dict(t=4, b=2),
        ),
        height=int(height),
        width=int(width) if width else None,
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=-0.10,
            x=0.0,
            xanchor="left",
            font=dict(size=11),
            bgcolor="rgba(255,253,248,0.92)",
            itemsizing="constant",
        ),
        margin=dict(t=86, b=108, l=72, r=36),
        bargap=0.05,
        showlegend=True,
        paper_bgcolor="#fffdf8",
        plot_bgcolor="#fffdf8",
    )
    return fig


def day_tick_counts(event_dt, day_start: str, day_end_incl: str):
    """Per-UTC-day tick counts and days with zero ticks (inclusive calendar)."""
    days_have = set(event_dt.dt.tz_convert("UTC").dt.strftime("%Y-%m-%d"))
    cal = pd.date_range(day_start, day_end_incl, freq="D", tz="UTC").strftime("%Y-%m-%d")
    missing = [d for d in cal if d not in days_have]
    counts = (
        event_dt.dt.tz_convert("UTC").dt.strftime("%Y-%m-%d").value_counts().sort_index()
    )
    return counts, missing


def day_counts_from_counter(counter, day_start: str, day_end_incl: str):
    """Same as ``day_tick_counts`` but from a ``{YYYY-MM-DD: n}`` map."""
    cal = pd.date_range(day_start, day_end_incl, freq="D", tz="UTC").strftime("%Y-%m-%d")
    data = {d: int(counter.get(d, 0)) for d in cal}
    missing = [d for d in cal if data[d] == 0]
    counts = pd.Series({d: n for d, n in data.items() if n > 0}, dtype="int64")
    if len(counts):
        counts = counts.sort_index()
    return counts, missing


def update_stats_from_table(accs: dict, table) -> None:
    """Stream valid L1 rows from a lean Arrow table into per-coin ``CoinAllTickStats``."""
    import pyarrow as pa
    import pyarrow.compute as pc

    if table is None or table.num_rows == 0:
        return
    need = (
        "event_local_ts_ms",
        "base_coin",
        "okx_bid_price",
        "okx_ask_price",
        "bybit_bid_price",
        "bybit_ask_price",
    )
    if any(c not in table.column_names for c in need):
        return
    ts = pc.cast(pc.floor(pc.cast(table["event_local_ts_ms"], pa.float64())), pa.int64())
    bc = table["base_coin"]
    if pa.types.is_dictionary(bc.type):
        bc = bc.dictionary_decode()
    coins = pc.utf8_upper(bc).to_numpy(zero_copy_only=False)
    ts_np = ts.to_numpy(zero_copy_only=False).astype(np.int64, copy=False)
    ob = np.asarray(table["okx_bid_price"].to_numpy(zero_copy_only=False), dtype="float64")
    oa = np.asarray(table["okx_ask_price"].to_numpy(zero_copy_only=False), dtype="float64")
    bb = np.asarray(table["bybit_bid_price"].to_numpy(zero_copy_only=False), dtype="float64")
    ba = np.asarray(table["bybit_ask_price"].to_numpy(zero_copy_only=False), dtype="float64")
    ok = (
        np.isfinite(ob)
        & np.isfinite(oa)
        & np.isfinite(bb)
        & np.isfinite(ba)
        & (ob > 0)
        & (bb > 0)
    )
    if not bool(ok.any()):
        return
    coins = coins[ok]
    ts_np = ts_np[ok]
    ob, oa, bb, ba = ob[ok], oa[ok], bb[ok], ba[ok]
    sl = (bb - oa) / bb * 100.0
    ss = (ob - ba) / ob * 100.0
    order = np.argsort(coins, kind="stable")
    sorted_c = coins[order]
    change = np.empty(len(sorted_c), dtype=bool)
    change[0] = True
    if len(sorted_c) > 1:
        change[1:] = sorted_c[1:] != sorted_c[:-1]
    starts = np.flatnonzero(change)
    ends = np.append(starts[1:], len(sorted_c))
    for a, b in zip(starts.tolist(), ends.tolist()):
        coin = str(sorted_c[a])
        idx = order[a:b]
        st = accs.get(coin)
        if st is None:
            st = CoinAllTickStats()
            accs[coin] = st
        st.update(ts_np[idx], sl[idx], ss[idx])
