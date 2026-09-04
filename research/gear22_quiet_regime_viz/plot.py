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
    DEFAULT_MA_BARS,
    SPREAD_LONG_COL,
    SPREAD_SHORT_COL,
    align_inspect_customdata,
    build_bar_inspect_payloads,
    downsample_ticks,
)
from research.gear22_quiet_regime_viz.gaps import gaps_to_vrect_datetimes
from research.gear22_quiet_regime_viz.metrics_ext import (
    MetricTrace,
    collect_extension_traces,
    extension_help_html,
)
from research.gear22_quiet_regime_viz.quantiles import TW_QUANTILE_NAMES

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
) -> go.Figure:
    """One long or short stack: candles → stats → TW quantiles."""
    title_prefix = f"{coin} · {side.upper()}"
    fig = make_subplots(
        rows=6,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.028,
        row_heights=[0.30, 0.12, 0.12, 0.14, 0.14, 0.18],
        subplot_titles=(
            f"{title_prefix}: {side_label} — 5m candles + causal MAs + sparse ticks"
            " (click candle → in-bar inspect)",
            f"{title_prefix}: tick count (hover: update_rate_hz)",
            f"{title_prefix}: gap_fraction (extent to bucket edges — not inter-tick holes)",
            f"{title_prefix}: mean ± std (equal-weight ticks / 5m)",
            f"{title_prefix}: min / max / IQR (q25–q75, equal-weight)",
            f"{title_prefix}: time-weighted p25 / p50 / p95 / p99 (hold→next; last→bar end)",
        ),
    )

    has_ohlc = buckets["tick_count"].fillna(0).astype(int) > 0
    b_plot = buckets.loc[has_ohlc]
    if not b_plot.empty:
        inspect_map = build_bar_inspect_payloads(
            ticks,
            value_col=value_col,
            n_bins=int(candle_bins),
            n_temporal=int(candle_temporal_bins),
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

    for name in TW_QUANTILE_NAMES:
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
        elif panel not in PANEL_ROW:
            continue
        row = PANEL_ROW.get(panel)
        if row is None or row < 1:
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
        template="plotly_white",
        height=1180,
        legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0, font=dict(size=10)),
        margin=dict(l=60, r=30, t=60, b=40),
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
    )
    fig.update_xaxes(rangeslider_visible=False, row=1, col=1)
    for r in (1, 4, 5, 6):
        fig.update_yaxes(title_text="%", row=r, col=1)
    fig.update_xaxes(title_text="UTC", row=6, col=1)
    return fig


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
        In-bar equal-weight histogram + equal-time means (build-time compact bins; not full ticks).
      </p>
    </div>
    <button type="button" id="candle-inspect-close" aria-label="Close inspect panel">×</button>
  </header>
  <div id="candle-inspect-plot" class="candle-inspect-plot"></div>
</aside>
"""


def _candle_inspect_script() -> str:
    """Post-plot click handler: read candlestick customdata → small dual panel."""
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

  function histCenters(lo, hi, nb, counts) {
    const n = (counts && counts.length) ? counts.length : (nb || 0);
    if (!n || lo == null || hi == null) return {x: [], y: []};
    const width = (hi - lo) / n;
    const x = [];
    const y = [];
    for (let i = 0; i < n; i++) {
      x.push(lo + (i + 0.5) * width);
      y.push(counts[i] || 0);
    }
    return {x: x, y: y};
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

  function renderInspect(cd, xLabel) {
    if (!cd || typeof Plotly === "undefined") return;
    const side = (cd.side || "?").toUpperCase();
    const col = cd.col || "spread";
    const n = cd.n || 0;
    const when = xLabel || (cd.bs != null ? new Date(cd.bs).toISOString() : "");
    showPanel(
      side + " · " + when + " · n=" + n,
      col + " — equal-weight hist (" + (cd.nb || (cd.c || []).length) +
        " bins) + equal-time means (" + (cd.nt || (cd.tv || []).length) + " slots)"
    );
    const hist = histCenters(cd.lo, cd.hi, cd.nb, cd.c || []);
    const temp = temporalSeries(cd.bs, cd.bar_ms || 300000, cd.tv || []);
    const traces = [
      {
        type: "bar",
        x: hist.x,
        y: hist.y,
        name: "in-bar hist",
        marker: {color: "#4c78a8"},
        xaxis: "x",
        yaxis: "y"
      },
      {
        type: "scatter",
        mode: "lines+markers",
        x: temp.x,
        y: temp.y,
        name: "equal-time mean",
        line: {width: 1.6, color: "#f58518"},
        marker: {size: 5},
        connectgaps: false,
        xaxis: "x2",
        yaxis: "y2"
      }
    ];
    const layout = {
      grid: {rows: 1, columns: 2, pattern: "independent"},
      margin: {l: 48, r: 20, t: 28, b: 40},
      height: 260,
      paper_bgcolor: "#fff",
      plot_bgcolor: "#fff",
      showlegend: false,
      title: {text: "In-bar distribution (compact)", font: {size: 12}},
      xaxis: {title: col + " (%)", domain: [0, 0.46]},
      yaxis: {title: "count"},
      xaxis2: {title: "UTC (within 5m)", domain: [0.54, 1]},
      yaxis2: {title: col + " (%)", anchor: "x2"}
    };
    Plotly.react("candle-inspect-plot", traces, layout, {
      responsive: true,
      displaylogo: false,
      staticPlot: false
    });
  }

  function bindGraph(gd) {
    if (!gd || gd._gear22InspectBound) return;
    const data = gd.data || [];
    const hasCandle = data.some(function(t) { return t && t.type === "candlestick"; });
    if (!hasCandle) return;
    gd._gear22InspectBound = true;
    gd.on("plotly_click", function(ev) {
      if (!ev || !ev.points || !ev.points.length) return;
      const pt = ev.points[0];
      const tr = (gd.data || [])[pt.curveNumber];
      if (!tr || tr.type !== "candlestick") return;
      const cd = pt.customdata;
      if (!cd) return;
      renderInspect(cd, pt.x);
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
  // Late Plotly hydration (some browsers defer inline newPlot).
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

    ext = collect_extension_traces(
        ticks,
        buckets_long,
        buckets_short=buckets_short,
        coin=coin,
    )
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
        extension_traces=ext,
        candle_bins=candle_bins,
        candle_temporal_bins=candle_temporal_bins,
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
        extension_traces=ext,
        candle_bins=candle_bins,
        candle_temporal_bins=candle_temporal_bins,
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
{script_tag}<style>
  body {{ font-family: "IBM Plex Sans", "Segoe UI", sans-serif; margin: 1.25rem; color: #1b1b1b; background: #f7f5f2; padding-bottom: 18rem; }}
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
    padding: 0.55rem 1rem 0.75rem; max-height: 46vh; overflow: auto;
  }}
  .candle-inspect[hidden] {{ display: none !important; }}
  .candle-inspect-head {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem; }}
  .candle-inspect-sub {{ margin: 0.15rem 0 0; color: #555; font-size: 0.85rem; }}
  .candle-inspect-plot {{ min-height: 260px; }}
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
<strong>Click a 5m candle</strong> to open the in-bar distribution panel (compact bins +
equal-time means; not full ticks). Not a live trading terminal; no threshold candidates
are invented on this page.
</p>
<table class="meta"><tbody>{meta_rows}</tbody></table>

{div_mid}

<h2 class="block">LONG spread — open_long</h2>
{div_long}
{div_hist_long}

<h2 class="block short">SHORT spread — open_short</h2>
{div_short}
{div_hist_short}

{extension_help_html()}
{_candle_inspect_panel_html()}
{_nav_script(coin, coins)}
{_candle_inspect_script()}
</body>
</html>
"""
    out_path.write_text(page, encoding="utf-8")
    return out_path
