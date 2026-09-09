"""Plotly multi-block HTML writer (one page per coin: long then short)."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.offline import plot as plotly_plot
from plotly.subplots import make_subplots

from research.gear22_quiet_regime_viz.candles import (
    DEFAULT_CANDLE_BINS,
    DEFAULT_CANDLE_TEMPORAL_BINS,
    DEFAULT_LATENCY_BINS,
    DEFAULT_LATENCY_TEMPORAL_BINS,
    DEFAULT_MA_BARS,
    SPREAD_LONG_COL,
    SPREAD_SHORT_COL,
    align_inspect_customdata,
    build_bar_inspect_payloads,
    causal_sma,
    downsample_ticks,
)
from research.gear22_quiet_regime_viz.gaps import gaps_to_vrect_datetimes
from research.gear22_quiet_regime_viz.floors import (
    FLOOR_PANEL_STYLES,
    INNER_BAND_EDGE,
    INNER_BAND_FILL,
    OUTER_BAND_EDGE,
    SMA3_NAME,
    TF_SELECT_25_NAME,
    compute_chosen_floor,
)
from research.gear22_quiet_regime_viz.metrics_ext import (
    MetricTrace,
    extension_help_html,
)
from research.gear22_quiet_regime_viz.quantiles import TW_ROW6_NAMES

# Match app.policy.features / gear2 open_long vs open_short.
SPREAD_LONG_LABEL = (
    "spread_long (%) = (bybit_bid − okx_ask) / bybit_bid × 100  → open_long"
)
SPREAD_SHORT_LABEL = (
    "spread_short (%) = (okx_bid − bybit_ask) / okx_bid × 100  → open_short"
)
MID_OKX_NAME = "OKX mid (bid+ask)/2"
MID_BYBIT_NAME = "Bybit mid (bid+ask)/2"
GAP_FILL = "rgba(220, 40, 40, 0.22)"
TICK_COLOR = "rgba(40, 40, 40, 0.35)"
MA_COLORS = {3: "#1f77b4", 12: "#ff7f0e", 6: "#2ca02c", 24: "#9467bd"}
TW_COLORS = {
    "tw_p25": "#54a24b",
    "tw_p50": "#4c78a8",
    "tw_p95": "#f58518",
    "tw_p99": "#e45756",
}

PANEL_ROW = {
    # Relative row within a spread block figure (1-indexed).
    "candles": 1,
    "tick_count": 2,
    "gap_fraction": 3,
    "mean_std": 4,
    "range_iqr": 5,
    "tw_quantiles": 6,
    "floor": 0,  # standalone figure, not a 6-row extra
    # Back-compat aliases from the first revision.
    "edge": 1,
    "mid": 0,  # mid is a separate figure
}


def coin_html_filename(coin: str) -> str:
    return f"gear22_quiet_regime_{str(coin).upper()}.html"


def ensure_plotly_js(out_dir: Path) -> Path:
    """Always copy plotly.min.js next to HTML (required for reliable file://)."""
    import plotly

    src = Path(plotly.__file__).resolve().parent / "package_data" / "plotly.min.js"
    if not src.is_file():
        raise FileNotFoundError(f"plotly.min.js not found beside plotly package: {src}")
    dest = Path(out_dir) / "plotly.min.js"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists() or dest.stat().st_size != src.stat().st_size:
        dest.write_bytes(src.read_bytes())
    return dest


def _add_gap_vrects(
    fig: go.Figure, gaps: Sequence[tuple[int, int]], rows: Sequence[int]
) -> None:
    for x0, x1 in gaps_to_vrect_datetimes(gaps):
        for row in rows:
            fig.add_vrect(
                x0=x0,
                x1=x1,
                fillcolor=GAP_FILL,
                opacity=1.0,
                line_width=0,
                layer="below",
                row=int(row),
                col=1,
            )


def build_mid_figure(
    *,
    coin: str,
    ticks: pd.DataFrame,
    gaps: Sequence[tuple[int, int]],
    max_tick_points: int = 4_000,
) -> go.Figure:
    """Shared mid-price context (once per page)."""
    fig = make_subplots(rows=1, cols=1, subplot_titles=(f"{coin}: mid context (price)",))
    sparse = downsample_ticks(ticks, max_points=max_tick_points)
    if not sparse.empty:
        fig.add_trace(
            go.Scatter(
                x=sparse["event_dt"],
                y=sparse["okx_mid"],
                mode="lines",
                name=MID_OKX_NAME,
                line=dict(width=1.0, color="#1f77b4"),
                connectgaps=False,
            )
        )
        fig.add_trace(
            go.Scatter(
                x=sparse["event_dt"],
                y=sparse["bybit_mid"],
                mode="lines",
                name=MID_BYBIT_NAME,
                line=dict(width=1.0, color="#2ca02c"),
                connectgaps=False,
            )
        )
    _add_gap_vrects(fig, gaps, rows=(1,))
    fig.update_layout(
        template="plotly_white",
        height=280,
        margin=dict(l=60, r=30, t=40, b=30),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        hovermode="x unified",
    )
    fig.update_yaxes(title_text="price")
    fig.update_xaxes(title_text="UTC")
    return fig


def build_spread_block_figure(
    *,
    coin: str,
    side: str,
    value_col: str,
    side_label: str,
    ticks: pd.DataFrame,
    buckets: pd.DataFrame,
    gaps: Sequence[tuple[int, int]],
    ma_bars: Sequence[int] = DEFAULT_MA_BARS,
    max_tick_points: int = 4_000,
    extension_traces: Optional[Sequence[MetricTrace]] = None,
    candle_bins: int = DEFAULT_CANDLE_BINS,
    candle_temporal_bins: int = DEFAULT_CANDLE_TEMPORAL_BINS,
    latency_bins: int = DEFAULT_LATENCY_BINS,
    latency_temporal_bins: int = DEFAULT_LATENCY_TEMPORAL_BINS,
) -> go.Figure:
    """One long or short stack: candles → stats → TW quantiles (6 rows)."""
    title_prefix = f"{coin} · {side.upper()}"
    titles = (
        f"{title_prefix}: {side_label} — 5m candles + causal MAs + sparse ticks"
        " (click candle → in-bar inspect)",
        f"{title_prefix}: tick count (hover: update_rate_hz)",
        f"{title_prefix}: gap_fraction (extent to bucket edges — not inter-tick holes)",
        f"{title_prefix}: mean ± std (equal-weight ticks / 5m)",
        f"{title_prefix}: min / max / IQR (q25–q75, equal-weight)",
        f"{title_prefix}: time-weighted p25 / p50 / p95 / p99 (hold→next; last→bar end)",
    )
    row_heights = [0.30, 0.12, 0.12, 0.14, 0.14, 0.18]
    n_rows = 6
    fig_height = 1180
    vspace = 0.028
    fig = make_subplots(
        rows=n_rows,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=vspace,
        row_heights=row_heights,
        subplot_titles=titles,
    )

    has_ohlc = buckets["tick_count"].fillna(0).astype(int) > 0
    b_plot = buckets.loc[has_ohlc]
    if not b_plot.empty:
        inspect_map = build_bar_inspect_payloads(
            ticks,
            value_col=value_col,
            n_bins=int(candle_bins),
            n_temporal=int(candle_temporal_bins),
            latency_bins=int(latency_bins),
            latency_temporal_bins=int(latency_temporal_bins),
            side=side,
        )
        customdata = align_inspect_customdata(buckets, inspect_map)
        candle_kwargs: dict[str, Any] = dict(
            x=b_plot["bar_start_dt"],
            open=b_plot["open"],
            high=b_plot["high"],
            low=b_plot["low"],
            close=b_plot["close"],
            name=f"{side} 5m OHLC",
            increasing_line_color="#2ca02c",
            decreasing_line_color="#d62728",
            showlegend=True,
        )
        if customdata and any(cd is not None for cd in customdata):
            candle_kwargs["customdata"] = customdata
            candle_kwargs["hovertemplate"] = (
                "%{x}<br>O=%{open:.5f} H=%{high:.5f} "
                "L=%{low:.5f} C=%{close:.5f}"
                "<br><extra>click for in-bar distribution</extra>"
            )
        fig.add_trace(
            go.Candlestick(**candle_kwargs),
            row=1,
            col=1,
        )

    for w in ma_bars:
        col = f"ma_{int(w)}"
        if col not in buckets.columns:
            continue
        color = MA_COLORS.get(int(w), "#333333")
        fig.add_trace(
            go.Scatter(
                x=buckets["bar_start_dt"],
                y=buckets[col],
                mode="lines",
                name=f"{side} SMA-{int(w)}×5m",
                line=dict(width=1.6, color=color),
                connectgaps=False,
            ),
            row=1,
            col=1,
        )

    sparse = downsample_ticks(ticks, max_points=max_tick_points)
    if not sparse.empty and value_col in sparse.columns:
        fig.add_trace(
            go.Scatter(
                x=sparse["event_dt"],
                y=sparse[value_col],
                mode="markers",
                name=f"{side} sparse ticks",
                marker=dict(size=3, color=TICK_COLOR),
                hovertemplate="%{x}<br>" + side + "=%{y:.5f}%<extra></extra>",
            ),
            row=1,
            col=1,
        )

    fig.add_trace(
        go.Bar(
            x=buckets["bar_start_dt"],
            y=buckets["tick_count"],
            name=f"{side} tick_count",
            marker_color="#4c78a8",
            opacity=0.85,
            customdata=buckets["update_rate_hz"],
            hovertemplate=(
                "%{x}<br>tick_count=%{y}"
                "<br>update_rate_hz=%{customdata:.4f}<extra></extra>"
            ),
        ),
        row=2,
        col=1,
    )
    fig.update_yaxes(title_text="ticks / 5m", row=2, col=1)

    fig.add_trace(
        go.Bar(
            x=buckets["bar_start_dt"],
            y=buckets["gap_fraction"],
            name=f"{side} gap_fraction",
            marker_color="#e45756",
            opacity=0.8,
        ),
        row=3,
        col=1,
    )
    fig.update_yaxes(title_text="fraction", range=[0, 1.05], row=3, col=1)

    fig.add_trace(
        go.Scatter(
            x=buckets["bar_start_dt"],
            y=buckets["mean"],
            mode="lines+markers",
            name=f"{side} mean",
            line=dict(width=1.4, color="#4c78a8"),
            marker=dict(size=5),
        ),
        row=4,
        col=1,
    )
    if not buckets.empty:
        mean = buckets["mean"].to_numpy(dtype="float64")
        std = buckets["std"].to_numpy(dtype="float64")
        fig.add_trace(
            go.Scatter(
                x=list(buckets["bar_start_dt"]) + list(buckets["bar_start_dt"][::-1]),
                y=list(mean + std) + list((mean - std)[::-1]),
                fill="toself",
                fillcolor="rgba(76, 120, 168, 0.18)",
                line=dict(width=0),
                name=f"{side} mean ± std",
                hoverinfo="skip",
            ),
            row=4,
            col=1,
        )

    fig.add_trace(
        go.Scatter(
            x=buckets["bar_start_dt"],
            y=buckets["max"],
            mode="lines",
            name=f"{side} max",
            line=dict(width=1, color="#e45756", dash="dot"),
        ),
        row=5,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=buckets["bar_start_dt"],
            y=buckets["min"],
            mode="lines",
            name=f"{side} min",
            line=dict(width=1, color="#54a24b", dash="dot"),
        ),
        row=5,
        col=1,
    )
    if not buckets.empty:
        q25 = buckets["q25"].to_numpy(dtype="float64")
        q75 = buckets["q75"].to_numpy(dtype="float64")
        fig.add_trace(
            go.Scatter(
                x=list(buckets["bar_start_dt"]) + list(buckets["bar_start_dt"][::-1]),
                y=list(q75) + list(q25[::-1]),
                fill="toself",
                fillcolor="rgba(114, 183, 178, 0.25)",
                line=dict(width=0),
                name=f"{side} IQR",
                hoverinfo="skip",
            ),
            row=5,
            col=1,
        )

    for name in TW_ROW6_NAMES:
        if name not in buckets.columns:
            continue
        fig.add_trace(
            go.Scatter(
                x=buckets["bar_start_dt"],
                y=buckets[name],
                mode="lines+markers",
                name=f"{side} {name}",
                line=dict(width=1.5, color=TW_COLORS.get(name, "#333")),
                marker=dict(size=4),
                connectgaps=False,
            ),
            row=6,
            col=1,
        )

    _add_gap_vrects(fig, gaps, rows=(1,))
    if gaps:
        fig.add_trace(
            go.Scatter(
                x=[None],
                y=[None],
                mode="markers",
                name="data gap (inter-tick > threshold)",
                marker=dict(size=12, color="rgba(220, 40, 40, 0.85)", symbol="square"),
            ),
            row=1,
            col=1,
        )

    side_key = f"{side}_"
    for tr in extension_traces or []:
        panel = tr.panel
        if panel.startswith(side_key):
            panel = panel[len(side_key) :]
        elif panel.startswith("long_") or panel.startswith("short_"):
            continue
        elif panel not in PANEL_ROW:
            continue
        row = PANEL_ROW.get(panel)
        if row is None or row < 1:
            continue
        if row > n_rows:
            continue
        scatter_kw: dict[str, Any] = dict(
            x=list(tr.x),
            y=list(tr.y),
            mode=tr.mode,
            name=tr.name,
            line=dict(**tr.line) if tr.line else None,
            connectgaps=False,
            showlegend=tr.showlegend,
        )
        if tr.legendgroup:
            scatter_kw["legendgroup"] = tr.legendgroup
        fig.add_trace(
            go.Scatter(**scatter_kw),
            row=row,
            col=1,
        )

    fig.update_layout(
        template="plotly_white",
        height=fig_height,
        legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0, font=dict(size=10)),
        margin=dict(l=60, r=30, t=60, b=40),
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
    )
    fig.update_xaxes(rangeslider_visible=False, row=1, col=1)
    pct_rows = (1, 4, 5, 6)
    for r in pct_rows:
        fig.update_yaxes(title_text="%", row=r, col=1)
    fig.update_xaxes(title_text="UTC", row=n_rows, col=1)
    return fig


def _edge_name(label: str, source: str) -> str:
    if source in ("tw_p01", "tw_p05", "tw_p95", "tw_p99"):
        return f"{label} ({source})"
    if source == "inspect_tw":
        return f"{label} (inspect TW)"
    if source == "hist_tw":
        return f"{label} (hist TW reconstructed)"
    if source == "hist":
        return f"{label} (hist reconstructed)"
    if source == "tw_p25":
        return f"{label} (tw p25 fallback)"
    if source == "missing":
        return f"{label} (unavailable)"
    return label


def _inner_band_name(p05_source: str, p95_source: str) -> str:
    if p05_source == "tw_p25":
        return "tw p25–p95 (p5 unavailable on this build)"
    if p05_source == "tw_p05" and p95_source == "tw_p95":
        return "tw p5–p95"
    if p05_source in ("hist", "hist_tw") and p95_source == "tw_p95":
        kind = "hist TW" if p05_source == "hist_tw" else "hist"
        return f"p5 ({kind} reconstructed)–p95 (tw)"
    return "p5–p95"


def _outer_band_name(p01_source: str, p99_source: str) -> str:
    if p01_source == "missing":
        return "p99 (p1 unavailable on this build)"
    if p01_source == "tw_p01" and p99_source == "tw_p99":
        return "tw p1–p99"
    if p01_source == "inspect_tw" and p99_source in ("tw_p99", "inspect_tw"):
        return "p1–p99 (inspect TW p1 + tw p99)"
    if p01_source in ("hist", "hist_tw"):
        kind = "hist TW" if p01_source == "hist_tw" else "hist"
        return f"p1 ({kind} reconstructed)–p99"
    return "p1–p99"


def _finite_pair(a: np.ndarray, b: np.ndarray) -> bool:
    return bool(np.isfinite(a).any() and np.isfinite(b).any())


def build_floor_only_figure(
    *,
    coin: str,
    side: str,
    x: Sequence[Any],
    sma12: np.ndarray,
    sma3: np.ndarray,
    p01: Optional[np.ndarray] = None,
    p05: Optional[np.ndarray] = None,
    p95: Optional[np.ndarray] = None,
    p99: Optional[np.ndarray] = None,
    p01_source: str = "tw_p01",
    p05_source: str = "tw_p05",
    p95_source: str = "tw_p95",
    p99_source: str = "tw_p99",
) -> go.Figure:
    """Standalone one-row floor + corridor figure (shared x).

    Used by both the generator (after the 6-row stack) and HTML inject.
    Series: dashed p1–p99, filled p5–p95, SMA-3 (display),
    tf-select α25 computed from SMA-12 (not plotted as the midline).
    """
    title_prefix = f"{coin} · {side.upper()}"
    inner_name = _inner_band_name(p05_source, p95_source)
    outer_name = _outer_band_name(p01_source, p99_source)
    fig = go.Figure()
    s12 = np.asarray(sma12, dtype="float64")
    s3 = np.asarray(sma3, dtype="float64")
    chosen = compute_chosen_floor(s12)
    xs = list(x)
    lo01 = np.asarray(p01, dtype="float64") if p01 is not None else np.array([])
    lo05 = np.asarray(p05, dtype="float64") if p05 is not None else np.array([])
    hi95 = np.asarray(p95, dtype="float64") if p95 is not None else np.array([])
    hi99 = np.asarray(p99, dtype="float64") if p99 is not None else np.array([])

    if lo01.size and hi99.size and p01_source != "missing" and _finite_pair(lo01, hi99):
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=list(hi99),
                mode="lines",
                name=_edge_name("p99", p99_source),
                line=dict(width=1.15, color=OUTER_BAND_EDGE, dash="dash"),
                connectgaps=False,
                legendgroup="outer",
                showlegend=False,
            )
        )
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=list(lo01),
                mode="lines",
                name=outer_name,
                line=dict(width=1.15, color=OUTER_BAND_EDGE, dash="dash"),
                connectgaps=False,
                legendgroup="outer",
            )
        )
    elif hi99.size and np.isfinite(hi99).any() and p01_source == "missing":
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=list(hi99),
                mode="lines",
                name=outer_name,
                line=dict(width=1.15, color=OUTER_BAND_EDGE, dash="dash"),
                connectgaps=False,
            )
        )

    if lo05.size and hi95.size and _finite_pair(lo05, hi95):
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=list(hi95),
                mode="lines",
                name=_edge_name("p95", p95_source),
                line=dict(width=0.9, color=INNER_BAND_EDGE),
                connectgaps=False,
                legendgroup="inner",
                showlegend=False,
                hoverinfo="skip",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=list(lo05),
                mode="lines",
                name=inner_name,
                fill="tonexty",
                fillcolor=INNER_BAND_FILL,
                line=dict(width=0.9, color=INNER_BAND_EDGE),
                connectgaps=False,
                legendgroup="inner",
            )
        )

    fig.add_trace(
        go.Scatter(
            x=xs,
            y=list(s3),
            mode="lines",
            name=SMA3_NAME,
            line=dict(FLOOR_PANEL_STYLES[SMA3_NAME]),
            connectgaps=False,
        )
    )
    fig.add_trace(
        go.Scatter(
            x=xs,
            y=list(chosen[TF_SELECT_25_NAME]),
            mode="lines",
            name=TF_SELECT_25_NAME,
            line=dict(FLOOR_PANEL_STYLES[TF_SELECT_25_NAME]),
            connectgaps=False,
        )
    )
    fig.update_yaxes(title_text="%")
    fig.update_xaxes(title_text="UTC")
    fig.update_layout(
        title=dict(
            text=(
                f"{title_prefix}: p1–p99 (dashed) · p5–p95 corridor · "
                "SMA-3 · tf-select α25"
            ),
            font=dict(size=13),
        ),
        template="plotly_white",
        height=420,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(size=10)),
        margin=dict(l=60, r=30, t=70, b=40),
        hovermode="x unified",
    )
    return fig


def build_floor_figure_from_buckets(
    *,
    coin: str,
    side: str,
    buckets: pd.DataFrame,
) -> Optional[go.Figure]:
    """Generator-path corridor figure from true TW bucket columns.

    Display midline is SMA-3 (``ma_3``). Floor metric stays on SMA-12.
    """
    if buckets is None or buckets.empty or "bar_start_dt" not in buckets.columns:
        return None

    def _sma_col(col: str, window: int) -> np.ndarray | None:
        if col in buckets.columns:
            return buckets[col].to_numpy(dtype="float64")
        if "close" in buckets.columns:
            return causal_sma(buckets["close"].to_numpy(dtype="float64"), window)
        return None

    sma3 = _sma_col("ma_3", 3)
    sma12 = _sma_col("ma_12", 12)
    if sma3 is None or sma12 is None or sma12.size == 0:
        return None

    def _col(name: str) -> np.ndarray | None:
        if name not in buckets.columns:
            return None
        return buckets[name].to_numpy(dtype="float64")

    return build_floor_only_figure(
        coin=coin,
        side=side,
        x=list(buckets["bar_start_dt"]),
        sma12=sma12,
        sma3=sma3,
        p01=_col("tw_p01"),
        p05=_col("tw_p05"),
        p95=_col("tw_p95"),
        p99=_col("tw_p99"),
        p01_source="tw_p01" if _col("tw_p01") is not None else "missing",
        p05_source="tw_p05" if _col("tw_p05") is not None else "missing",
        p95_source="tw_p95",
        p99_source="tw_p99" if _col("tw_p99") is not None else "missing",
    )


def build_window_hist_figure(
    *,
    coin: str,
    side: str,
    side_label: str,
    values: np.ndarray,
    n_bins: int = 60,
) -> go.Figure:
    """Equal-weight histogram of all ticks in the loaded --since/--until window."""
    y = np.asarray(values, dtype="float64")
    y = y[np.isfinite(y)]
    fig = go.Figure()
    if y.size:
        fig.add_trace(
            go.Histogram(
                x=y,
                nbinsx=int(n_bins),
                name=f"{side} ticks",
                marker_color="#4c78a8",
                opacity=0.85,
            )
        )
    fig.update_layout(
        title=(
            f"{coin} · {side.upper()} — all-tick histogram (window, equal weight)<br>"
            f"<sup>{side_label}</sup>"
        ),
        template="plotly_white",
        height=340,
        margin=dict(l=60, r=30, t=70, b=40),
        bargap=0.02,
        showlegend=False,
    )
    fig.update_xaxes(title_text=f"{side} (%)")
    fig.update_yaxes(title_text="count")
    return fig


def _fig_to_div(fig: go.Figure, *, include_plotlyjs: bool | str) -> str:
    return plotly_plot(
        fig,
        output_type="div",
        include_plotlyjs=include_plotlyjs,
        config={"responsive": True, "displaylogo": False},
    )


def _nav_html(coin: str, coins: Sequence[str]) -> str:
    coins_u = [c.upper() for c in coins]
    coin_u = coin.upper()
    try:
        idx = coins_u.index(coin_u)
    except ValueError:
        idx = 0
        coins_u = [coin_u]
    n = len(coins_u)
    prev_c = coins_u[(idx - 1) % n]
    next_c = coins_u[(idx + 1) % n]
    links = " · ".join(
        (
            f'<a class="coin-link{" current" if c == coin_u else ""}" '
            f'href="{html.escape(coin_html_filename(c))}">{html.escape(c)}</a>'
        )
        for c in coins_u
    )
    return f"""
<nav class="coin-nav" aria-label="Coin navigation">
  <a class="nav-btn" href="{html.escape(coin_html_filename(prev_c))}" title="Previous coin (←)">← {html.escape(prev_c)}</a>
  <span class="coin-links">{links}</span>
  <a class="nav-btn" href="{html.escape(coin_html_filename(next_c))}" title="Next coin (→)">{html.escape(next_c)} →</a>
</nav>
<p class="nav-hint">Keyboard ←/→ or swipe left/right cycles coins (wraps). Works with <code>file://</code>.</p>
"""


def _nav_script(coin: str, coins: Sequence[str]) -> str:
    payload = json.dumps({"coin": coin.upper(), "coins": [c.upper() for c in coins]})
    return f"""
<script>
(function() {{
  const CFG = {payload};
  const fileFor = (c) => "gear22_quiet_regime_" + c + ".html";
  function go(delta) {{
    const list = CFG.coins;
    if (!list || !list.length) return;
    let i = list.indexOf(CFG.coin);
    if (i < 0) i = 0;
    const j = (i + delta + list.length) % list.length; // wrap
    if (list[j] === CFG.coin) return;
    window.location.href = fileFor(list[j]);
  }}
  document.addEventListener("keydown", function(e) {{
    if (e.key === "ArrowLeft") {{ e.preventDefault(); go(-1); }}
    if (e.key === "ArrowRight") {{ e.preventDefault(); go(1); }}
  }});
  let sx = null, sy = null;
  document.addEventListener("touchstart", function(e) {{
    if (!e.changedTouches || !e.changedTouches.length) return;
    sx = e.changedTouches[0].clientX;
    sy = e.changedTouches[0].clientY;
  }}, {{passive: true}});
  document.addEventListener("touchend", function(e) {{
    if (sx === null || !e.changedTouches || !e.changedTouches.length) return;
    const dx = e.changedTouches[0].clientX - sx;
    const dy = e.changedTouches[0].clientY - sy;
    sx = sy = null;
    if (Math.abs(dx) < 60 || Math.abs(dx) < Math.abs(dy) * 1.2) return;
    go(dx < 0 ? 1 : -1); // swipe left → next
  }}, {{passive: true}});
}})();
</script>
"""


def _candle_inspect_panel_html() -> str:
    return """
<aside id="candle-inspect" class="candle-inspect" hidden>
  <header class="candle-inspect-head">
    <div>
      <strong id="candle-inspect-title">Click a 5m candle</strong>
      <p id="candle-inspect-sub" class="candle-inspect-sub">
        Spread: TW-mass hist (hold→next; last→bar end) + TW p50/p95/p99.
        Latency: triggering venue only, equal-weight ticks. Compact bins; not full ticks.
      </p>
    </div>
    <button type="button" id="candle-inspect-close" aria-label="Close inspect panel">×</button>
  </header>
  <div id="candle-inspect-plot" class="candle-inspect-plot"></div>
</aside>
"""


def _candle_inspect_script() -> str:
    """Post-plot click handler: read candlestick customdata → spread + latency panel."""
    return """
<script>
(function() {
  function el(id) { return document.getElementById(id); }
  function showPanel(title, sub) {
    const box = el("candle-inspect");
    if (!box) return;
    box.hidden = false;
    el("candle-inspect-title").textContent = title;
    el("candle-inspect-sub").textContent = sub;
  }
  function hidePanel() {
    const box = el("candle-inspect");
    if (box) box.hidden = true;
  }
  const closeBtn = el("candle-inspect-close");
  if (closeBtn) closeBtn.addEventListener("click", hidePanel);

  function fmtNum(v, digits) {
    if (v == null || !isFinite(v)) return "—";
    const d = (digits == null) ? 4 : digits;
    return Number(v).toFixed(d);
  }

  function histCenters(lo, hi, nb, counts) {
    const n = (counts && counts.length) ? counts.length : (nb || 0);
    if (!n || lo == null || hi == null || !isFinite(lo) || !isFinite(hi)) {
      return {x: [], y: [], width: null};
    }
    const span = hi - lo;
    const width = span / n;
    const x = [];
    const y = [];
    for (let i = 0; i < n; i++) {
      x.push(lo + (i + 0.5) * width);
      y.push(counts[i] || 0);
    }
    return {x: x, y: y, width: width * 0.92};
  }

  function temporalSeries(bs, barMs, tv) {
    const n = (tv && tv.length) ? tv.length : 0;
    const x = [];
    const y = [];
    if (!n || bs == null || !barMs) return {x: x, y: y};
    const step = barMs / n;
    for (let i = 0; i < n; i++) {
      const v = tv[i];
      if (v == null || !isFinite(v)) continue;
      x.push(new Date(bs + (i + 0.5) * step));
      y.push(v);
    }
    return {x: x, y: y};
  }

  function twVlines(tw, xref) {
    if (!tw) return [];
    const yref = (xref === "x3") ? "y3 domain" : "y domain";
    const spec = [
      {k: "p50", color: "#4c78a8", dash: "solid", w: 2},
      {k: "p95", color: "#f58518", dash: "dash", w: 1.5},
      {k: "p99", color: "#e45756", dash: "dot", w: 1.5}
    ];
    const out = [];
    spec.forEach(function(s) {
      const v = tw[s.k];
      if (v == null || !isFinite(v)) return;
      out.push({
        type: "line",
        xref: xref,
        yref: yref,
        x0: v, x1: v, y0: 0, y1: 1,
        line: {color: s.color, width: s.w, dash: s.dash}
      });
    });
    return out;
  }

  function twAnnotations(tw, xref, yref) {
    if (!tw) return [];
    const spec = [
      {k: "p50", color: "#4c78a8"},
      {k: "p95", color: "#f58518"},
      {k: "p99", color: "#e45756"}
    ];
    const out = [];
    let yi = 0.98;
    spec.forEach(function(s) {
      const v = tw[s.k];
      if (v == null || !isFinite(v)) return;
      out.push({
        xref: xref,
        yref: yref + " domain",
        x: v,
        y: yi,
        text: s.k + "=" + fmtNum(v, 4),
        showarrow: false,
        xanchor: "left",
        font: {size: 10, color: s.color},
        bgcolor: "rgba(255,255,255,0.7)"
      });
      yi -= 0.12;
    });
    return out;
  }

  function renderInspect(cd, xLabel) {
    if (!cd || typeof Plotly === "undefined") return;
    const side = (cd.side || "?").toUpperCase();
    const col = cd.col || "spread";
    const n = cd.n || 0;
    const when = xLabel || (cd.bs != null ? new Date(cd.bs).toISOString() : "");
    const tw = cd.tw || {};
    const lat = cd.lat || {};
    const okx = lat.okx || null;
    const bybit = lat.bybit || null;
    const okxN = okx ? (okx.n || 0) : 0;
    const bybitN = bybit ? (bybit.n || 0) : 0;
    let latNote = "latency omitted (need trigger + venue latency cols)";
    if (okx || bybit) {
      latNote = "latency (trigger venue only, c_w=count) n okx=" + okxN +
        " bybit=" + bybitN + " (NaN/missing skipped)";
    }
    const sub =
      col + " TW hist (c_w=tw_ms, range≈p01–p99) · " +
      "TW p50=" + fmtNum(tw.p50) +
      " p95=" + fmtNum(tw.p95) +
      " p99=" + fmtNum(tw.p99) +
      " mean=" + fmtNum(tw.mean) +
      " · temporal=equal-time · " + latNote;
    showPanel(side + " · " + when + " · n=" + n, sub);

    const hist = histCenters(cd.lo, cd.hi, cd.nb, cd.c || []);
    const temp = temporalSeries(cd.bs, cd.bar_ms || 300000, cd.tv || []);
    const barW = hist.width;
    const traces = [
      {
        type: "bar",
        x: hist.x,
        y: hist.y,
        width: barW,
        name: "spread TW mass",
        marker: {color: "rgba(76,120,168,0.75)"},
        xaxis: "x",
        yaxis: "y",
        hovertemplate: "%{x:.5f}<br>TW mass=%{y:.1f} ms<extra>spread</extra>"
      },
      {
        type: "scatter",
        mode: "lines+markers",
        x: temp.x,
        y: temp.y,
        name: "spread equal-time mean",
        line: {width: 1.6, color: "#f58518"},
        marker: {size: 5},
        connectgaps: false,
        xaxis: "x2",
        yaxis: "y2"
      }
    ];
    // Invisible legend proxies for percentile vlines.
    [["p50", "#4c78a8"], ["p95", "#f58518"], ["p99", "#e45756"]].forEach(function(pair) {
      const k = pair[0], color = pair[1];
      if (tw[k] == null || !isFinite(tw[k])) return;
      traces.push({
        type: "scatter",
        mode: "lines",
        x: [tw[k], tw[k]],
        y: [0, Math.max.apply(null, hist.y.concat([1]))],
        name: "TW " + k,
        line: {color: color, width: k === "p50" ? 2 : 1.4, dash: k === "p50" ? "solid" : (k === "p95" ? "dash" : "dot")},
        xaxis: "x",
        yaxis: "y",
        hoverinfo: "skip",
        showlegend: true
      });
    });
    if (okx && okxN > 0) {
      const h = histCenters(okx.lo, okx.hi, okx.nb || cd.nlb, okx.c || []);
      traces.push({
        type: "bar",
        x: h.x,
        y: h.y,
        width: h.width,
        name: "okx_latency_ms (trigger, count)",
        marker: {color: "rgba(31,119,180,0.55)"},
        xaxis: "x3",
        yaxis: "y3",
        hovertemplate: "%{x:.2f} ms<br>count=%{y}<extra>okx</extra>"
      });
      const t = temporalSeries(cd.bs, cd.bar_ms || 300000, okx.tv || []);
      traces.push({
        type: "scatter",
        mode: "lines+markers",
        x: t.x,
        y: t.y,
        name: "okx lat equal-time mean",
        line: {width: 1.4, color: "#1f77b4"},
        marker: {size: 4},
        connectgaps: false,
        xaxis: "x4",
        yaxis: "y4"
      });
    }
    if (bybit && bybitN > 0) {
      const h = histCenters(bybit.lo, bybit.hi, bybit.nb || cd.nlb, bybit.c || []);
      traces.push({
        type: "bar",
        x: h.x,
        y: h.y,
        width: h.width,
        name: "bybit_latency_ms (trigger, count)",
        marker: {color: "rgba(44,160,44,0.55)"},
        xaxis: "x3",
        yaxis: "y3",
        hovertemplate: "%{x:.2f} ms<br>count=%{y}<extra>bybit</extra>"
      });
      const t = temporalSeries(cd.bs, cd.bar_ms || 300000, bybit.tv || []);
      traces.push({
        type: "scatter",
        mode: "lines+markers",
        x: t.x,
        y: t.y,
        name: "bybit lat equal-time mean",
        line: {width: 1.4, color: "#2ca02c"},
        marker: {size: 4},
        connectgaps: false,
        xaxis: "x4",
        yaxis: "y4"
      });
    }
    const hasLat = (okx && okxN > 0) || (bybit && bybitN > 0);
    const shapes = twVlines(tw, "x");
    // Optional latency p50/p95/p99 on shared latency hist when readable.
    if (hasLat && okx && okx.tw) {
      shapes.push.apply(shapes, twVlines(okx.tw, "x3"));
    } else if (hasLat && bybit && bybit.tw) {
      shapes.push.apply(shapes, twVlines(bybit.tw, "x3"));
    }
    const annotations = twAnnotations(tw, "x", "y");
    const xRange = (cd.lo != null && cd.hi != null && isFinite(cd.lo) && isFinite(cd.hi))
      ? [cd.lo, cd.hi] : undefined;
    const layout = {
      grid: {rows: hasLat ? 2 : 1, columns: 2, pattern: "independent"},
      margin: {l: 56, r: 20, t: 40, b: 46},
      height: hasLat ? 500 : 280,
      paper_bgcolor: "#fff",
      plot_bgcolor: "#fff",
      barmode: "overlay",
      bargap: 0.05,
      showlegend: true,
      legend: {orientation: "h", y: 1.14, x: 0, font: {size: 10}},
      title: {
        text: "Spread TW-mass + latency equal-weight (trigger venue) · compact",
        font: {size: 12}
      },
      shapes: shapes,
      annotations: annotations,
      xaxis: {
        title: col + " (%)  [TW p01–p99]",
        domain: [0, 0.46],
        range: xRange,
        zeroline: false
      },
      yaxis: {
        title: "TW mass (ms)",
        domain: hasLat ? [0.55, 1] : [0, 1]
      },
      xaxis2: {title: "UTC (within 5m)", domain: [0.54, 1], anchor: "y2"},
      yaxis2: {
        title: col + " (%)",
        anchor: "x2",
        domain: hasLat ? [0.55, 1] : [0, 1]
      }
    };
    if (hasLat) {
      layout.xaxis3 = {
        title: "latency (ms) [equal-weight p01–p99, trigger venue]",
        domain: [0, 0.46],
        anchor: "y3"
      };
      layout.yaxis3 = {title: "tick count", domain: [0, 0.42]};
      layout.xaxis4 = {title: "UTC (within 5m)", domain: [0.54, 1], anchor: "y4"};
      layout.yaxis4 = {title: "latency (ms)", anchor: "x4", domain: [0, 0.42]};
    }
    Plotly.react("candle-inspect-plot", traces, layout, {
      responsive: true,
      displaylogo: false,
      staticPlot: false
    });
  }

  function toMs(v) {
    if (v == null) return NaN;
    if (typeof v === "number" && isFinite(v)) return v;
    const ms = new Date(v).getTime();
    return isFinite(ms) ? ms : NaN;
  }

  function axisId(tr, key, fallback) {
    if (!tr || tr[key] == null || tr[key] === "") return fallback;
    return tr[key];
  }

  /**
   * Resolve inspect payload by click time against a candlestick's x + customdata.
   * Needed because MA / sparse-tick overlays sit above the candle and steal plotly_click.
   * Prefer bar containment [bar_start, bar_start + bar_ms); else nearest within half-bar.
   */
  function lookupCandleByTime(candleTraces, xVal) {
    const target = toMs(xVal);
    if (!isFinite(target)) return null;
    let best = null;
    let bestDist = Infinity;
    for (let ti = 0; ti < candleTraces.length; ti++) {
      const tr = candleTraces[ti];
      const xs = tr.x || [];
      const cds = tr.customdata || [];
      for (let i = 0; i < xs.length; i++) {
        const xm = toMs(xs[i]);
        if (!isFinite(xm)) continue;
        const cd = cds[i];
        const barMs = (cd && cd.bar_ms) ? cd.bar_ms : 300000;
        if (target >= xm && target < xm + barMs) {
          return {cd: cd, x: xs[i]};
        }
        const dist = Math.abs(xm - target);
        if (dist < bestDist) {
          bestDist = dist;
          best = {cd: cd, x: xs[i], dist: dist};
        }
      }
    }
    if (best && best.cd && best.dist <= 150000) return best;
    return null;
  }

  function bindGraph(gd) {
    if (!gd || gd._gear22InspectBound) return;
    const data = gd.data || [];
    const candleTraces = data.filter(function(t) {
      return t && t.type === "candlestick";
    });
    if (!candleTraces.length) return;
    gd._gear22InspectBound = true;
    gd.on("plotly_click", function(ev) {
      if (!ev || !ev.points || !ev.points.length) return;
      const pt = ev.points[0];
      const tr = (gd.data || [])[pt.curveNumber];
      let cd = null;
      let xLabel = pt.x;
      if (tr && tr.type === "candlestick") {
        cd = pt.customdata;
      } else {
        // Overlay (MA / sparse ticks) or other row: only resolve when click
        // shares the candle subplot y-axis so lower panels stay unchanged.
        const clickY = axisId(tr, "yaxis", "y");
        const onCandleRow = candleTraces.filter(function(ct) {
          return axisId(ct, "yaxis", "y") === clickY;
        });
        if (!onCandleRow.length) return;
        const hit = lookupCandleByTime(onCandleRow, pt.x);
        if (!hit || !hit.cd) return;
        cd = hit.cd;
        xLabel = hit.x;
      }
      if (!cd) return;
      renderInspect(cd, xLabel);
    });
  }

  function bindAll() {
    document.querySelectorAll(".js-plotly-plot").forEach(bindGraph);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bindAll);
  } else {
    bindAll();
  }
  setTimeout(bindAll, 0);
  setTimeout(bindAll, 250);
})();
</script>
"""


def write_coins_json(out_dir: Path, coins: Sequence[str]) -> Path:
    path = Path(out_dir) / "coins.json"
    path.write_text(
        json.dumps({"coins": [c.upper() for c in coins]}, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def write_coin_html(
    out_path: Path,
    *,
    coin: str,
    coins: Sequence[str],
    ticks: pd.DataFrame,
    buckets_long: pd.DataFrame,
    buckets_short: pd.DataFrame,
    gaps: Sequence[tuple[int, int]],
    meta: Mapping[str, Any],
    ma_bars: Sequence[int] = DEFAULT_MA_BARS,
    max_tick_points: int = 4_000,
    inline_plotly: bool = False,
    candle_bins: int = DEFAULT_CANDLE_BINS,
    candle_temporal_bins: int = DEFAULT_CANDLE_TEMPORAL_BINS,
    latency_bins: int = DEFAULT_LATENCY_BINS,
    latency_temporal_bins: int = DEFAULT_LATENCY_TEMPORAL_BINS,
    floors: bool = True,
) -> Path:
    """Write one coin page: mid context + long stack + hist + short stack + hist."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if inline_plotly:
        include_js: bool | str = True
        script_tag = ""
    else:
        ensure_plotly_js(out_path.parent)
        include_js = False
        script_tag = '<script src="plotly.min.js"></script>\n'

    mid_fig = build_mid_figure(
        coin=coin, ticks=ticks, gaps=gaps, max_tick_points=max_tick_points
    )
    long_fig = build_spread_block_figure(
        coin=coin,
        side="long",
        value_col=SPREAD_LONG_COL,
        side_label=SPREAD_LONG_LABEL,
        ticks=ticks,
        buckets=buckets_long,
        gaps=gaps,
        ma_bars=ma_bars,
        max_tick_points=max_tick_points,
        extension_traces=None,
        candle_bins=candle_bins,
        candle_temporal_bins=candle_temporal_bins,
        latency_bins=latency_bins,
        latency_temporal_bins=latency_temporal_bins,
    )
    short_fig = build_spread_block_figure(
        coin=coin,
        side="short",
        value_col=SPREAD_SHORT_COL,
        side_label=SPREAD_SHORT_LABEL,
        ticks=ticks,
        buckets=buckets_short,
        gaps=gaps,
        ma_bars=ma_bars,
        max_tick_points=max_tick_points,
        extension_traces=None,
        candle_bins=candle_bins,
        candle_temporal_bins=candle_temporal_bins,
        latency_bins=latency_bins,
        latency_temporal_bins=latency_temporal_bins,
    )
    hist_long = build_window_hist_figure(
        coin=coin,
        side="long",
        side_label=SPREAD_LONG_LABEL,
        values=ticks[SPREAD_LONG_COL].to_numpy(dtype="float64"),
    )
    hist_short = build_window_hist_figure(
        coin=coin,
        side="short",
        side_label=SPREAD_SHORT_LABEL,
        values=ticks[SPREAD_SHORT_COL].to_numpy(dtype="float64"),
    )

    div_mid = _fig_to_div(mid_fig, include_plotlyjs=include_js)
    div_long = _fig_to_div(long_fig, include_plotlyjs=False)
    div_hist_long = _fig_to_div(hist_long, include_plotlyjs=False)
    div_short = _fig_to_div(short_fig, include_plotlyjs=False)
    div_hist_short = _fig_to_div(hist_short, include_plotlyjs=False)
    if floors:
        long_floor = build_floor_figure_from_buckets(
            coin=coin, side="long", buckets=buckets_long
        )
        short_floor = build_floor_figure_from_buckets(
            coin=coin, side="short", buckets=buckets_short
        )
        div_floor_long = (
            _fig_to_div(long_floor, include_plotlyjs=False) if long_floor else ""
        )
        div_floor_short = (
            _fig_to_div(short_floor, include_plotlyjs=False) if short_floor else ""
        )
    else:
        div_floor_long = ""
        div_floor_short = ""

    meta_rows = "".join(
        f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>"
        for k, v in meta.items()
    )
    floor_sub = (
        "After each 6-row stack: one graph — TW p1–p99 (dashed) and "
        "p5–p95 corridor, SMA-3 inside the inner band, tf-select α25 "
        "floor (from SMA-12). Observation only — not a live threshold."
        if floors
        else ""
    )
    floor_help = extension_help_html() if floors else ""
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Gear 2.2 quiet-regime — {html.escape(coin)}</title>
{script_tag}<style>
  body {{ font-family: "IBM Plex Sans", "Segoe UI", sans-serif; margin: 1.25rem; color: #1b1b1b; background: #f7f5f2; padding-bottom: 28rem; }}
  h1 {{ font-size: 1.35rem; margin: 0 0 0.35rem; }}
  h2.block {{ font-size: 1.15rem; margin: 1.4rem 0 0.4rem; padding: 0.35rem 0.55rem; background: #ece8e1; border-left: 4px solid #4c78a8; }}
  h2.block.short {{ border-left-color: #f58518; }}
  .sub {{ color: #444; margin-bottom: 0.75rem; max-width: 72rem; }}
  table.meta {{ border-collapse: collapse; margin-bottom: 1rem; font-size: 0.9rem; }}
  table.meta th, table.meta td {{ border: 1px solid #ccc; padding: 0.25rem 0.55rem; text-align: left; }}
  table.meta th {{ background: #ece8e1; }}
  .coin-nav {{ display: flex; flex-wrap: wrap; align-items: center; gap: 0.75rem 1rem; margin: 0.5rem 0 0.25rem; }}
  .coin-nav .nav-btn {{ text-decoration: none; color: #1b1b1b; background: #fff; border: 1px solid #bbb; padding: 0.35rem 0.7rem; }}
  .coin-nav .coin-links a {{ margin: 0 0.25rem; text-decoration: none; color: #334; }}
  .coin-nav .coin-links a.current {{ font-weight: 700; border-bottom: 2px solid #4c78a8; }}
  .nav-hint {{ color: #666; font-size: 0.85rem; margin: 0 0 1rem; }}
  .ext-help {{ margin-top: 1.25rem; padding: 0.75rem 1rem; background: #fff; border-left: 4px solid #4c78a8; max-width: 72rem; }}
  .ext-help h2 {{ font-size: 1.05rem; margin: 0 0 0.4rem; }}
  .candle-inspect {{
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 40;
    background: #fff; border-top: 3px solid #4c78a8;
    box-shadow: 0 -6px 18px rgba(0,0,0,0.12);
    padding: 0.55rem 1rem 0.75rem; max-height: 62vh; overflow: auto;
  }}
  .candle-inspect[hidden] {{ display: none !important; }}
  .candle-inspect-head {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem; }}
  .candle-inspect-sub {{ margin: 0.15rem 0 0; color: #555; font-size: 0.85rem; }}
  .candle-inspect-plot {{ min-height: 280px; }}
  #candle-inspect-close {{
    border: 1px solid #bbb; background: #f7f5f2; font-size: 1.25rem;
    line-height: 1; width: 2rem; height: 2rem; cursor: pointer;
  }}
</style>
</head>
<body>
<h1>Gear 2.2 — quiet-regime L1 observer ({html.escape(coin)})</h1>
{_nav_html(coin, coins)}
<p class="sub">
Research visualizer for noisy/gappy cross-exchange L1. Stacked blocks match live policy
spreads from <code>app.policy.features</code>: <strong>long</strong> then <strong>short</strong>.
Red bands mark inter-tick gaps. Time-weighted quantile panels use hold-until-next-tick
weights (last tick → bar end). Window histograms are equal-weight over all loaded ticks.
<strong>Click a 5m candle</strong> to open the in-bar panel: spread
<strong>TW-mass</strong> hist (robust p01–p99 axis) with TW p50/p95/p99 and
equal-time temporal means; plus <strong>trigger-venue latency</strong>
(equal-weight ticks, <code>c_w=count</code>) — only
<code>okx_latency_ms</code> on <code>trigger=okx</code> rows and only
<code>bybit_latency_ms</code> on <code>trigger=bybit</code> rows
(local_recv − exchange_ts; NaN/missing skipped). Compact bins only — not
full ticks. Not a live trading terminal; no threshold candidates are
invented on this page.
{floor_sub}
</p>
<table class="meta"><tbody>{meta_rows}</tbody></table>

{div_mid}

<h2 class="block">LONG spread — open_long</h2>
{div_long}
{div_floor_long}
{div_hist_long}

<h2 class="block short">SHORT spread — open_short</h2>
{div_short}
{div_floor_short}
{div_hist_short}

{floor_help}
{_candle_inspect_panel_html()}
{_nav_script(coin, coins)}
{_candle_inspect_script()}
</body>
</html>
"""
    out_path.write_text(page, encoding="utf-8")
    return out_path
