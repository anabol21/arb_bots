"""Plotly views for the anomaly-onset detector.

All statistical overlays (quantiles, MA, floor band) are the time-weighted series
produced by :mod:`detector`. Functions return ``plotly.graph_objects.Figure`` so
the notebook can ``fig.show()`` or export HTML.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

_EP_FILL = "rgba(214, 39, 40, 0.12)"
_EP_LINE = "rgba(214, 39, 40, 0.6)"


def _shade_episodes(fig: go.Figure, episodes: pd.DataFrame, *, row=None, col=None) -> None:
    if episodes is None or episodes.empty:
        return
    for _, e in episodes.iterrows():
        fig.add_vrect(
            x0=e["onset_dt"], x1=e["end_dt"],
            fillcolor=_EP_FILL, line_width=0, layer="below",
            row=row, col=col,
        )
        fig.add_vline(
            x=e["onset_dt"], line=dict(color=_EP_LINE, width=1, dash="dot"),
            row=row, col=col,
        )


def fig_ticks(frame: pd.DataFrame, episodes: Optional[pd.DataFrame] = None,
              *, coin: str = "", direction: str = "") -> go.Figure:
    """Raw ticks + time-weighted MA + floor corridor (25/50/75) + tail quantiles (90/95/99)."""
    x = frame["event_dt"]
    fig = go.Figure()
    fig.add_trace(go.Scattergl(x=x, y=frame["spread"], mode="markers",
                               marker=dict(size=2, color="rgba(31,119,180,0.35)"),
                               name="spread (all ticks)"))
    if "spread_ma" in frame:
        fig.add_trace(go.Scattergl(x=x, y=frame["spread_ma"], mode="lines",
                                   line=dict(color="black", width=1.3), name="MA (time-weighted)"))
    # corridor band 25/75 with 50 line
    if {"spread_q25", "spread_q75"} <= set(frame.columns):
        fig.add_trace(go.Scattergl(x=x, y=frame["spread_q75"], mode="lines",
                                   line=dict(color="rgba(44,160,44,0.0)"), showlegend=False, hoverinfo="skip"))
        fig.add_trace(go.Scattergl(x=x, y=frame["spread_q25"], mode="lines", fill="tonexty",
                                   fillcolor="rgba(44,160,44,0.12)", line=dict(color="rgba(44,160,44,0.0)"),
                                   name="quiet corridor Q25–Q75"))
    if "spread_q50" in frame:
        fig.add_trace(go.Scattergl(x=x, y=frame["spread_q50"], mode="lines",
                                   line=dict(color="green", width=1), name="corridor median Q50"))
    for lv, dash in (("90", "dot"), ("95", "dashdot"), ("99", "dash")):
        c = f"spread_q{lv}"
        if c in frame:
            fig.add_trace(go.Scattergl(x=x, y=frame[c], mode="lines",
                                       line=dict(color="rgba(255,127,14,0.9)", width=1, dash=dash),
                                       name=f"spread Q{lv}"))
    _shade_episodes(fig, episodes)
    fig.update_layout(title=f"Ticks + corridor — {coin} {direction}", height=420,
                      xaxis_title="UTC", yaxis_title="spread %", legend=dict(orientation="h"))
    return fig


def fig_amplitude(frame: pd.DataFrame, episodes: Optional[pd.DataFrame] = None,
                  *, variable: str = "norm", coin: str = "", direction: str = "") -> go.Figure:
    """Amplitude deviation (z+ or a+) with the quiet threshold and 90/95/99 quantiles."""
    x = frame["event_dt"]
    base = "zplus" if variable == "norm" else "aplus"
    bthr = "z_base" if variable == "norm" else "a_base"
    fig = go.Figure()
    fig.add_trace(go.Scattergl(x=x, y=frame[base], mode="markers",
                               marker=dict(size=2, color="rgba(148,103,189,0.5)"),
                               name=f"{base}"))
    for lv, color in (("90", "rgba(255,187,120,0.9)"), ("95", "rgba(255,127,14,0.9)"), ("99", "rgba(214,39,40,0.9)")):
        c = f"{base}_q{lv}"
        if c in frame:
            fig.add_trace(go.Scattergl(x=x, y=frame[c], mode="lines",
                                       line=dict(color=color, width=1), name=f"{base} Q{lv}"))
    if bthr in frame:
        fig.add_trace(go.Scattergl(x=x, y=frame[bthr], mode="lines",
                                   line=dict(color="black", width=1.4, dash="dash"),
                                   name=f"{bthr} (Q_det threshold)"))
    _shade_episodes(fig, episodes)
    fig.update_layout(title=f"Amplitude {base} — {coin} {direction}", height=340,
                      xaxis_title="UTC", yaxis_title=base, legend=dict(orientation="h"))
    return fig


def fig_metrics(frame: pd.DataFrame, episodes: Optional[pd.DataFrame] = None,
                *, variable: str = "norm", coin: str = "", direction: str = "") -> go.Figure:
    """Occupancy O_W and integral area I_W with quiet thresholds + 90/95/99 quantiles."""
    x = frame["event_dt"]
    occ = f"O_{variable}"
    area = f"I_{variable}"
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08,
                        subplot_titles=(f"occupancy {occ}", f"integral area {area}"))
    fig.add_trace(go.Scattergl(x=x, y=frame[occ], mode="lines",
                               line=dict(color="teal", width=1), name=occ), row=1, col=1)
    if f"O_min_{variable}" in frame:
        fig.add_trace(go.Scattergl(x=x, y=frame[f"O_min_{variable}"], mode="lines",
                                   line=dict(color="black", width=1.2, dash="dash"), name="O_min"), row=1, col=1)
    for lv, color in (("90", "rgba(23,190,207,0.6)"), ("95", "rgba(23,190,207,0.85)"), ("99", "rgba(31,119,180,1)")):
        c = f"{occ}_q{lv}"
        if c in frame:
            fig.add_trace(go.Scattergl(x=x, y=frame[c], mode="lines",
                                       line=dict(color=color, width=1, dash="dot"),
                                       name=f"{occ} Q{lv}", legendgroup="occq"), row=1, col=1)
    fig.add_trace(go.Scattergl(x=x, y=frame[area], mode="lines",
                               line=dict(color="indianred", width=1), name=area), row=2, col=1)
    if f"I_min_{variable}" in frame:
        fig.add_trace(go.Scattergl(x=x, y=frame[f"I_min_{variable}"], mode="lines",
                                   line=dict(color="black", width=1.2, dash="dash"), name="I_min"), row=2, col=1)
    for lv, color in (("90", "rgba(255,152,150,0.7)"), ("95", "rgba(255,127,14,0.9)"), ("99", "rgba(214,39,40,1)")):
        c = f"{area}_q{lv}"
        if c in frame:
            fig.add_trace(go.Scattergl(x=x, y=frame[c], mode="lines",
                                       line=dict(color=color, width=1, dash="dot"),
                                       name=f"{area} Q{lv}", legendgroup="areaq"), row=2, col=1)
    _shade_episodes(fig, episodes, row=1, col=1)
    _shade_episodes(fig, episodes, row=2, col=1)
    fig.update_layout(title=f"Integral metrics ({variable}) — {coin} {direction}",
                      height=560, legend=dict(orientation="h"))
    return fig


def fig_overview(frame: pd.DataFrame, episodes: Optional[pd.DataFrame] = None,
                 *, variable: str = "norm", coin: str = "", direction: str = "") -> go.Figure:
    """Master transition-to-anomaly chart: spread+corridor, amplitude+threshold, area+threshold."""
    x = frame["event_dt"]
    base = "zplus" if variable == "norm" else "aplus"
    bthr = "z_base" if variable == "norm" else "a_base"
    area = f"I_{variable}"
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.05,
                        row_heights=[0.42, 0.29, 0.29],
                        subplot_titles=("spread + quiet corridor", f"amplitude {base} + threshold",
                                        f"integral area {area} + threshold"))
    fig.add_trace(go.Scattergl(x=x, y=frame["spread"], mode="markers",
                               marker=dict(size=2, color="rgba(31,119,180,0.3)"), name="spread"), row=1, col=1)
    if "spread_q75" in frame:
        fig.add_trace(go.Scattergl(x=x, y=frame["spread_q75"], mode="lines",
                                   line=dict(color="rgba(44,160,44,0.9)", width=1), name="corridor Q75"), row=1, col=1)
    if "spread_q50" in frame:
        fig.add_trace(go.Scattergl(x=x, y=frame["spread_q50"], mode="lines",
                                   line=dict(color="rgba(44,160,44,0.5)", width=1, dash="dot"), name="corridor Q50"), row=1, col=1)
    fig.add_trace(go.Scattergl(x=x, y=frame[base], mode="lines",
                               line=dict(color="rgb(148,103,189)", width=1), name=base), row=2, col=1)
    fig.add_trace(go.Scattergl(x=x, y=frame[bthr], mode="lines",
                               line=dict(color="black", width=1.2, dash="dash"), name=bthr), row=2, col=1)
    fig.add_trace(go.Scattergl(x=x, y=frame[area], mode="lines",
                               line=dict(color="indianred", width=1), name=area), row=3, col=1)
    if f"I_min_{variable}" in frame:
        fig.add_trace(go.Scattergl(x=x, y=frame[f"I_min_{variable}"], mode="lines",
                                   line=dict(color="black", width=1.2, dash="dash"), name="I_min"), row=3, col=1)
    for r in (1, 2, 3):
        _shade_episodes(fig, episodes, row=r, col=1)
    fig.update_layout(title=f"Quiet→anomaly overview ({variable}) — {coin} {direction}",
                      height=760, legend=dict(orientation="h"))
    return fig
