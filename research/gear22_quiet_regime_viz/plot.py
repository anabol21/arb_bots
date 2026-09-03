"""Plotly multi-subplot HTML writer (one page per coin)."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import pandas as pd
import plotly.graph_objects as go
from plotly.offline import plot as plotly_plot
from plotly.subplots import make_subplots

from research.gear22_quiet_regime_viz.candles import DEFAULT_MA_BARS, downsample_ticks
from research.gear22_quiet_regime_viz.gaps import gaps_to_vrect_datetimes
from research.gear22_quiet_regime_viz.metrics_ext import (
    MetricTrace,
    collect_extension_traces,
    extension_help_html,
)

EDGE_NAME = "OKX−Bybit mid edge (%)  = (okx_mid − bybit_mid) / bybit_mid × 100"
MID_OKX_NAME = "OKX mid (bid+ask)/2"
MID_BYBIT_NAME = "Bybit mid (bid+ask)/2"
GAP_FILL = "rgba(220, 40, 40, 0.22)"
TICK_COLOR = "rgba(40, 40, 40, 0.35)"
MA_COLORS = {3: "#1f77b4", 12: "#ff7f0e", 6: "#2ca02c", 24: "#9467bd"}

PANEL_ROW = {
    "edge": 1,
    "mid": 2,
    "tick_count": 3,
    "gap_fraction": 4,
    "mean_std": 5,
    "range_iqr": 6,
}


def _copy_plotly_js(out_dir: Path) -> str:
    try:
        import plotly

        src = Path(plotly.__file__).resolve().parent / "package_data" / "plotly.min.js"
        dest = out_dir / "plotly.min.js"
        if src.is_file():
            if not dest.exists() or dest.stat().st_size != src.stat().st_size:
                dest.write_bytes(src.read_bytes())
            return "plotly.min.js"
    except Exception:
        pass
    return "https://cdn.plot.ly/plotly-2.35.2.min.js"


def build_figure(
    *,
    coin: str,
    ticks: pd.DataFrame,
    buckets: pd.DataFrame,
    gaps: Sequence[tuple[int, int]],
    ma_bars: Sequence[int] = DEFAULT_MA_BARS,
    max_tick_points: int = 4_000,
    extension_traces: Optional[Sequence[MetricTrace]] = None,
) -> go.Figure:
    """Multi-row figure: edge candles + mid context + intra-bucket stats."""
    fig = make_subplots(
        rows=6,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.34, 0.16, 0.12, 0.12, 0.13, 0.13],
        subplot_titles=(
            f"{coin}: {EDGE_NAME} — 5m candles + causal MAs + sparse ticks",
            f"{coin}: mid context (price)",
            "Intra-bucket tick count / update rate",
            "Intra-bucket gap fraction (extent uncovered / 5m)",
            "Edge mean ± std (per 5m)",
            "Edge min / max / IQR (q25–q75)",
        ),
    )

    has_ohlc = buckets["tick_count"].fillna(0).astype(int) > 0
    b_plot = buckets.loc[has_ohlc]
    if not b_plot.empty:
        fig.add_trace(
            go.Candlestick(
                x=b_plot["bar_start_dt"],
                open=b_plot["open"],
                high=b_plot["high"],
                low=b_plot["low"],
                close=b_plot["close"],
                name="edge 5m OHLC",
                increasing_line_color="#2ca02c",
                decreasing_line_color="#d62728",
                showlegend=True,
            ),
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
                name=f"SMA-{int(w)}×5m (causal close)",
                line=dict(width=1.6, color=color),
                connectgaps=False,
            ),
            row=1,
            col=1,
        )

    sparse = downsample_ticks(ticks, max_points=max_tick_points)
    if not sparse.empty:
        fig.add_trace(
            go.Scatter(
                x=sparse["event_dt"],
                y=sparse["edge_pct"],
                mode="markers",
                name=f"sparse ticks (n≤{max_tick_points})",
                marker=dict(size=3, color=TICK_COLOR),
                hovertemplate="%{x}<br>edge=%{y:.5f}%<extra></extra>",
            ),
            row=1,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=sparse["event_dt"],
                y=sparse["okx_mid"],
                mode="lines",
                name=MID_OKX_NAME,
                line=dict(width=1.0, color="#1f77b4"),
                connectgaps=False,
            ),
            row=2,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=sparse["event_dt"],
                y=sparse["bybit_mid"],
                mode="lines",
                name=MID_BYBIT_NAME,
                line=dict(width=1.0, color="#2ca02c"),
                connectgaps=False,
            ),
            row=2,
            col=1,
        )

    # Intra-bucket panels (include empty buckets so holes read as zeros / 1.0).
    fig.add_trace(
        go.Bar(
            x=buckets["bar_start_dt"],
            y=buckets["tick_count"],
            name="tick_count",
            marker_color="#4c78a8",
            opacity=0.85,
            customdata=buckets["update_rate_hz"],
            hovertemplate=(
                "%{x}<br>tick_count=%{y}"
                "<br>update_rate_hz=%{customdata:.4f}<extra></extra>"
            ),
        ),
        row=3,
        col=1,
    )
    fig.update_yaxes(title_text="ticks / 5m", row=3, col=1)

    fig.add_trace(
        go.Bar(
            x=buckets["bar_start_dt"],
            y=buckets["gap_fraction"],
            name="gap_fraction",
            marker_color="#e45756",
            opacity=0.8,
        ),
        row=4,
        col=1,
    )
    fig.update_yaxes(title_text="fraction", range=[0, 1.05], row=4, col=1)

    fig.add_trace(
        go.Scatter(
            x=buckets["bar_start_dt"],
            y=buckets["mean"],
            mode="lines+markers",
            name="edge mean",
            line=dict(width=1.4, color="#4c78a8"),
            marker=dict(size=5),
        ),
        row=5,
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
                name="mean ± std",
                hoverinfo="skip",
            ),
            row=5,
            col=1,
        )

    fig.add_trace(
        go.Scatter(
            x=buckets["bar_start_dt"],
            y=buckets["max"],
            mode="lines",
            name="max",
            line=dict(width=1, color="#e45756", dash="dot"),
        ),
        row=6,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=buckets["bar_start_dt"],
            y=buckets["min"],
            mode="lines",
            name="min",
            line=dict(width=1, color="#54a24b", dash="dot"),
        ),
        row=6,
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
                name="IQR (q25–q75)",
                hoverinfo="skip",
            ),
            row=6,
            col=1,
        )

    # Red gap regions on edge + mid panels.
    for x0, x1 in gaps_to_vrect_datetimes(gaps):
        for row in (1, 2):
            fig.add_vrect(
                x0=x0,
                x1=x1,
                fillcolor=GAP_FILL,
                opacity=1.0,
                line_width=0,
                layer="below",
                row=row,
                col=1,
            )
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

    for tr in extension_traces or []:
        row = PANEL_ROW.get(tr.panel)
        if row is None:
            continue
        fig.add_trace(
            go.Scatter(
                x=list(tr.x),
                y=list(tr.y),
                mode=tr.mode,
                name=tr.name,
                line=dict(**tr.line) if tr.line else None,
            ),
            row=row,
            col=1,
        )

    fig.update_layout(
        title=dict(
            text=(
                f"Gear 2.2 quiet-regime observer — {coin}<br>"
                f"<sup>Primary: mid edge (%). Red bands: inter-tick gaps. "
                f"MAs: causal SMA on 5m closes.</sup>"
            ),
            x=0.01,
            xanchor="left",
        ),
        template="plotly_white",
        height=1280,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        margin=dict(l=60, r=30, t=100, b=40),
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
    )
    # Disable rangeslider on candle subplot explicitly.
    fig.update_xaxes(rangeslider_visible=False, row=1, col=1)
    fig.update_yaxes(title_text="edge %", row=1, col=1)
    fig.update_yaxes(title_text="price", row=2, col=1)
    fig.update_yaxes(title_text="edge %", row=5, col=1)
    fig.update_yaxes(title_text="edge %", row=6, col=1)
    fig.update_xaxes(title_text="UTC", row=6, col=1)
    return fig


def write_coin_html(
    out_path: Path,
    *,
    coin: str,
    ticks: pd.DataFrame,
    buckets: pd.DataFrame,
    gaps: Sequence[tuple[int, int]],
    meta: Mapping[str, Any],
    ma_bars: Sequence[int] = DEFAULT_MA_BARS,
    max_tick_points: int = 4_000,
    plotly_js: Optional[str] = None,
) -> Path:
    """Write a self-contained (or CDN) HTML page for one coin."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if plotly_js is None:
        plotly_js = _copy_plotly_js(out_path.parent)

    ext = collect_extension_traces(ticks, buckets, coin=coin)
    fig = build_figure(
        coin=coin,
        ticks=ticks,
        buckets=buckets,
        gaps=gaps,
        ma_bars=ma_bars,
        max_tick_points=max_tick_points,
        extension_traces=ext,
    )
    inner = plotly_plot(
        fig,
        output_type="div",
        include_plotlyjs=False,
        config={"responsive": True, "displaylogo": False},
    )
    meta_rows = "".join(
        f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>"
        for k, v in meta.items()
    )
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Gear 2.2 quiet-regime — {html.escape(coin)}</title>
<script src="{html.escape(plotly_js)}"></script>
<style>
  body {{ font-family: "IBM Plex Sans", "Segoe UI", sans-serif; margin: 1.25rem; color: #1b1b1b; background: #f7f5f2; }}
  h1 {{ font-size: 1.35rem; margin: 0 0 0.35rem; }}
  .sub {{ color: #444; margin-bottom: 1rem; max-width: 70rem; }}
  table.meta {{ border-collapse: collapse; margin-bottom: 1rem; font-size: 0.9rem; }}
  table.meta th, table.meta td {{ border: 1px solid #ccc; padding: 0.25rem 0.55rem; text-align: left; }}
  table.meta th {{ background: #ece8e1; }}
  .ext-help {{ margin-top: 1.25rem; padding: 0.75rem 1rem; background: #fff; border-left: 4px solid #4c78a8; max-width: 70rem; }}
  .ext-help h2 {{ font-size: 1.05rem; margin: 0 0 0.4rem; }}
</style>
</head>
<body>
<h1>Gear 2.2 — quiet-regime L1 observer ({html.escape(coin)})</h1>
<p class="sub">
Research visualizer for noisy/gappy cross-exchange L1. Primary series is the
OKX−Bybit <em>mid edge</em> (percent), with mid prices as context. Red regions mark
inter-tick gaps above the configured threshold. Intra-candle panels summarize
each UTC-aligned 5-minute bucket. Not a live trading terminal; no threshold
candidates are invented on this page.
</p>
<table class="meta"><tbody>{meta_rows}</tbody></table>
{inner}
{extension_help_html()}
</body>
</html>
"""
    out_path.write_text(page, encoding="utf-8")
    return out_path
