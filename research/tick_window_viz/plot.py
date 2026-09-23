"""Plotly figure for one coin × 5-minute lean-tick window.

Raw ticks plus the three locked TW-p50 current-spread overlays on the
spread row. Does not draw SMA / floor (those stay on the gear22 candle
dashboard). SMA in gear 2.2 is one value per 5-minute bar close, not a
tick rolling mean. p50 formulas are unchanged: ``docs/gear22-state-p50.md``.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from research.gear22_quiet_regime_viz.candles import SPREAD_LONG_COL, SPREAD_SHORT_COL
from research.gear22_quiet_regime_viz.load import parse_since_ms
from research.gear22_quiet_regime_viz.state_p50 import (
    DEFAULT_EVAL_STEP_MS,
    P50_BAR_COLOR,
    P50_ROLL_1M_COLOR,
    P50_ROLL_5M_COLOR,
    build_state_p50_series,
)
from research.tick_window_viz.delay import attach_venue_latency, message_delay_ms

DEFAULT_GAP_BREAK_MS = 2000
_L1_PRICE_COLS = (
    "okx_bid_price",
    "okx_ask_price",
    "bybit_bid_price",
    "bybit_ask_price",
)
_SPREAD_HOVER = (
    "time=%{x|%Y-%m-%d %H:%M:%S.%L} UTC<br>"
    "coin=%{customdata[0]}<br>"
    "spread_long=%{customdata[1]:.6f}<br>"
    "spread_short=%{customdata[2]:.6f}"
    "<extra>%{fullData.name}</extra>"
)
_L1_HOVER = (
    "time=%{x|%Y-%m-%d %H:%M:%S.%L} UTC<br>"
    "coin=%{customdata[0]}<br>"
    "venue=%{customdata[1]}<br>"
    "side=%{customdata[2]}<br>"
    "price=%{y}<br>"
    "delay_ms=%{customdata[3]:.1f}<br>"
    "exchange_ts=%{customdata[4]} UTC<br>"
    "local_recv_ts=%{customdata[5]} UTC<br>"
    "trigger=%{customdata[6]}<br>"
    "delay = that venue local_recv − exchange_ts"
    "<extra>%{fullData.name}</extra>"
)
# One L1 series per subplot so bid and ask never share axes.
_L1_TRACES = (
    {
        "col": "okx_bid_price",
        "name": "okx_bid",
        "venue": "okx",
        "side": "bid",
        "row": 2,
        "color": "#1b9e77",
        "title": "OKX bid",
        "yaxis": "OKX bid",
    },
    {
        "col": "okx_ask_price",
        "name": "okx_ask",
        "venue": "okx",
        "side": "ask",
        "row": 3,
        "color": "#66c2a5",
        "title": "OKX ask",
        "yaxis": "OKX ask",
    },
    {
        "col": "bybit_bid_price",
        "name": "bybit_bid",
        "venue": "bybit",
        "side": "bid",
        "row": 4,
        "color": "#d95f02",
        "title": "Bybit bid",
        "yaxis": "Bybit bid",
    },
    {
        "col": "bybit_ask_price",
        "name": "bybit_ask",
        "venue": "bybit",
        "side": "ask",
        "row": 5,
        "color": "#e7298a",
        "title": "Bybit ask",
        "yaxis": "Bybit ask",
    },
)

FIGURE_WIDTH = 1500
FIGURE_HEIGHT_L1 = 1750
FIGURE_HEIGHT_SPREADS_ONLY = 720


def _as_utc_timestamps(series: pd.Series) -> list:
    ts = pd.to_datetime(series, utc=True)
    return [None if pd.isna(v) else v.to_pydatetime() for v in ts]


def _ms_to_utc_str(ms) -> Optional[str]:
    try:
        value = float(ms)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(value):
        return None
    return pd.Timestamp(int(value), unit="ms", tz="UTC").strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )[:-3]


def _xy_with_gap_breaks(
    times: Sequence,
    values: Sequence,
    ts_ms: Sequence,
    custom_rows: Sequence,
    gap_break_ms: int,
) -> tuple[list, list, list]:
    """Insert None breakpoints so Plotly does not interpolate holes."""
    xs: list = []
    ys: list = []
    custom: list = []
    prev_ms: Optional[int] = None
    gap = int(gap_break_ms)
    for t, y, ms, crow in zip(times, values, ts_ms, custom_rows):
        ms_i = int(ms)
        if prev_ms is not None and (ms_i - prev_ms) > gap:
            xs.append(None)
            ys.append(None)
            custom.append(tuple(None for _ in crow))
        xs.append(t)
        if y is None or (isinstance(y, float) and np.isnan(y)):
            ys.append(None)
        else:
            ys.append(float(y))
        custom.append(tuple(crow))
        prev_ms = ms_i
    return xs, ys, custom


def _coin_array(plot: pd.DataFrame, meta: Mapping[str, Any]) -> np.ndarray:
    if "base_coin" in plot.columns:
        return plot["base_coin"].astype(str).str.upper().to_numpy()
    return np.full(len(plot), str(meta.get("coin") or ""), dtype=object)


def _trigger_array(plot: pd.DataFrame) -> np.ndarray:
    if "trigger" in plot.columns:
        return plot["trigger"].astype(str).str.lower().to_numpy()
    return np.full(len(plot), "", dtype=object)


def _venue_delay_array(plot: pd.DataFrame, venue: str) -> np.ndarray:
    lat_col = f"{venue}_latency_ms"
    if lat_col in plot.columns:
        return pd.to_numeric(plot[lat_col], errors="coerce").to_numpy(dtype=float)
    recv_col = f"{venue}_local_recv_ts_ms"
    ts_col = f"{venue}_ts_ms"
    if {recv_col, ts_col} <= set(plot.columns):
        recv = pd.to_numeric(plot[recv_col], errors="coerce")
        exch = pd.to_numeric(plot[ts_col], errors="coerce")
        out = []
        for a, b in zip(recv, exch):
            delay = message_delay_ms(a, b)
            out.append(np.nan if delay is None else float(delay))
        return np.asarray(out, dtype=float)
    return np.full(len(plot), np.nan, dtype=float)


def _venue_ts_strings(plot: pd.DataFrame, venue: str, kind: str) -> np.ndarray:
    col = f"{venue}_local_recv_ts_ms" if kind == "recv" else f"{venue}_ts_ms"
    if col not in plot.columns:
        return np.full(len(plot), None, dtype=object)
    return np.array(
        [_ms_to_utc_str(v) for v in pd.to_numeric(plot[col], errors="coerce")],
        dtype=object,
    )


def _has_l1_prices(df: pd.DataFrame) -> bool:
    return all(c in df.columns for c in _L1_PRICE_COLS)


def _plot_window_ms(plot: pd.DataFrame, meta: Mapping[str, Any]) -> tuple[int, int]:
    start = meta.get("window_start_ms")
    end = meta.get("window_end_ms")
    if start is not None and end is not None:
        return int(start), int(end)
    w0 = meta.get("window_start_utc")
    w1 = meta.get("window_end_utc")
    if w0 and w1:
        return parse_since_ms(str(w0)), parse_since_ms(str(w1))
    ts = plot["event_local_ts_ms"].to_numpy(dtype="int64")
    return int(ts.min()), int(ts.max()) + 1


def _bar_segment_xy(bar: pd.DataFrame) -> tuple[list, list]:
    """Closed-bar p50 as a horizontal segment ``[bar_start, bar_end]``.

    Same value as ``p50_bar5m`` (known at close). Segment is display-only so
    the level is visible inside a 5-minute tick viewport.
    """
    xs: list = []
    ys: list = []
    if bar is None or bar.empty:
        return xs, ys
    for row in bar.itertuples(index=False):
        y = float(row.p50_bar5m)
        if not np.isfinite(y):
            continue
        if xs:
            xs.append(None)
            ys.append(None)
        xs.append(pd.Timestamp(int(row.bar_start_ms), unit="ms", tz="UTC"))
        ys.append(y)
        xs.append(pd.Timestamp(int(row.bar_end_ms), unit="ms", tz="UTC"))
        ys.append(y)
    return xs, ys


def _add_state_p50_traces(
    fig: go.Figure,
    plot: pd.DataFrame,
    meta: Mapping[str, Any],
    *,
    row: Optional[int],
) -> None:
    """Overlay locked TW-p50 series on the spread row. Formula unchanged."""
    start_ms, end_ms = _plot_window_ms(plot, meta)
    row_kw = {"row": row, "col": 1} if row is not None else {}
    sides = (
        ("long", SPREAD_LONG_COL, P50_BAR_COLOR, P50_ROLL_5M_COLOR, P50_ROLL_1M_COLOR),
        ("short", SPREAD_SHORT_COL, "#9ecae1", "#fdbf6f", "#a1d99b"),
    )
    for side, col, bar_c, roll5_c, roll1_c in sides:
        packed = build_state_p50_series(
            plot,
            value_col=col,
            plot_start_ms=start_ms,
            plot_end_ms=end_ms,
            eval_step_ms=DEFAULT_EVAL_STEP_MS,
        )
        bar_x, bar_y = _bar_segment_xy(packed["p50_bar5m"])
        fig.add_trace(
            go.Scatter(
                x=bar_x,
                y=bar_y,
                mode="lines",
                name=f"p50_bar5m {side}",
                line=dict(width=2.4, color=bar_c, shape="hv"),
                connectgaps=False,
                hovertemplate=(
                    "%{x}<br>p50_bar5m=%{y:.5f}%<br>"
                    "window=closed UTC 5m candle<extra></extra>"
                ),
            ),
            **row_kw,
        )
        fig.add_trace(
            go.Scatter(
                x=packed["eval_dt"],
                y=packed["p50_roll_5m"],
                mode="lines",
                name=f"p50_roll_5m {side}",
                line=dict(width=2.0, color=roll5_c),
                connectgaps=False,
                hovertemplate=(
                    "%{x}<br>p50_roll_5m=%{y:.5f}%<br>"
                    "window=[t−300s, t]<extra></extra>"
                ),
            ),
            **row_kw,
        )
        fig.add_trace(
            go.Scatter(
                x=packed["eval_dt"],
                y=packed["p50_roll_1m"],
                mode="lines",
                name=f"p50_roll_1m {side}",
                line=dict(width=1.8, color=roll1_c, dash="dash"),
                connectgaps=False,
                hovertemplate=(
                    "%{x}<br>p50_roll_1m=%{y:.5f}%<br>"
                    "window=[t−60s, t]<extra></extra>"
                ),
            ),
            **row_kw,
        )


def build_tick_window_figure(
    df: pd.DataFrame,
    meta: Optional[Mapping[str, Any]] = None,
    *,
    gap_break_ms: int = DEFAULT_GAP_BREAK_MS,
) -> go.Figure:
    """Spreads plus locked p50 overlays and per-venue bid/ask. No SMA.

    Hover on an L1 point shows that venue's ``local_recv − exchange_ts``
    delay (not a blended pair delay).
    """
    meta = dict(meta or {})
    if df is None or df.empty:
        fig = go.Figure()
        fig.update_layout(title="нет тиков в окне")
        return fig

    need = ["event_dt", "event_local_ts_ms", "spread_long", "spread_short"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise KeyError(f"plot frame missing {missing}")

    plot = attach_venue_latency(df.sort_values("event_local_ts_ms").reset_index(drop=True))
    times = _as_utc_timestamps(plot["event_dt"])
    ts_ms = plot["event_local_ts_ms"].to_numpy()
    longs = plot["spread_long"].to_numpy(dtype=float)
    shorts = plot["spread_short"].to_numpy(dtype=float)
    coins = _coin_array(plot, meta)
    spread_custom = list(zip(coins, longs, shorts))

    x_long, y_long, custom_long = _xy_with_gap_breaks(
        times, longs, ts_ms, spread_custom, gap_break_ms
    )
    x_short, y_short, custom_short = _xy_with_gap_breaks(
        times, shorts, ts_ms, spread_custom, gap_break_ms
    )

    show_l1 = _has_l1_prices(plot)
    if show_l1:
        fig = make_subplots(
            rows=5,
            cols=1,
            shared_xaxes=True,
            vertical_spacing=0.045,
            row_heights=[0.20, 0.20, 0.20, 0.20, 0.20],
            subplot_titles=(
                "spread %",
                *(spec["title"] for spec in _L1_TRACES),
            ),
        )
    else:
        fig = go.Figure()

    fig.add_trace(
        go.Scatter(
            x=x_long,
            y=y_long,
            name="spread_long",
            customdata=custom_long,
            line={"width": 1, "color": "#1f77b4"},
            marker={"size": 5, "color": "#1f77b4"},
            mode="lines+markers",
            connectgaps=False,
            hovertemplate=_SPREAD_HOVER,
        ),
        **({"row": 1, "col": 1} if show_l1 else {}),
    )
    fig.add_trace(
        go.Scatter(
            x=x_short,
            y=y_short,
            name="spread_short",
            customdata=custom_short,
            line={"width": 1, "color": "#d62728"},
            marker={"size": 5, "color": "#d62728"},
            mode="lines+markers",
            connectgaps=False,
            hovertemplate=_SPREAD_HOVER,
        ),
        **({"row": 1, "col": 1} if show_l1 else {}),
    )
    _add_state_p50_traces(fig, plot, meta, row=1 if show_l1 else None)

    if show_l1:
        triggers = _trigger_array(plot)
        for spec in _L1_TRACES:
            prices = pd.to_numeric(plot[spec["col"]], errors="coerce").to_numpy(
                dtype=float
            )
            delays = _venue_delay_array(plot, spec["venue"])
            exch_s = _venue_ts_strings(plot, spec["venue"], "exch")
            recv_s = _venue_ts_strings(plot, spec["venue"], "recv")
            l1_custom = list(
                zip(
                    coins,
                    np.full(len(plot), spec["venue"], dtype=object),
                    np.full(len(plot), spec["side"], dtype=object),
                    delays,
                    exch_s,
                    recv_s,
                    triggers,
                )
            )
            xs, ys, custom = _xy_with_gap_breaks(
                times, prices, ts_ms, l1_custom, gap_break_ms
            )
            fig.add_trace(
                go.Scatter(
                    x=xs,
                    y=ys,
                    name=spec["name"],
                    customdata=custom,
                    line={"width": 1, "color": spec["color"]},
                    marker={"size": 5, "color": spec["color"]},
                    mode="lines+markers",
                    connectgaps=False,
                    hovertemplate=_L1_HOVER,
                ),
                row=spec["row"],
                col=1,
            )

    coin = meta.get("coin") or (str(coins[0]) if len(coins) else "")
    start = meta.get("window_start_utc", "")
    end = meta.get("window_end_utc", "")
    source = meta.get("source", "")
    n_plot = meta.get("n_plot", len(plot))
    fig.update_layout(
        title=f"{coin}  {start} → {end}  ({source}, n={n_plot})",
        hovermode="closest",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "font": {"size": 13}},
        margin={"t": 120, "b": 80, "l": 80, "r": 40},
        template="plotly_white",
        width=FIGURE_WIDTH,
        height=FIGURE_HEIGHT_L1 if show_l1 else FIGURE_HEIGHT_SPREADS_ONLY,
        font={"size": 13},
        hoverlabel={"font_size": 13, "namelength": -1},
    )
    if show_l1:
        fig.update_yaxes(title_text="spread, %", row=1, col=1)
        for spec in _L1_TRACES:
            fig.update_yaxes(title_text=spec["yaxis"], row=spec["row"], col=1)
        fig.update_xaxes(title_text="event_dt UTC", row=5, col=1)
        fig.add_hline(y=0.0, line_width=1, line_color="#888", row=1, col=1)
    else:
        fig.update_layout(xaxis_title="event_dt UTC", yaxis_title="spread, %")
        fig.add_hline(y=0.0, line_width=1, line_color="#888")
    return fig
