"""Gear 2.2 observation: three current-spread TW-p50 series + a small HTML chart.

Not a simulator gate and not a live threshold. Floor formula is untouched.
"""

from __future__ import annotations

import argparse
import html
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.offline import plot as plotly_plot
from plotly.subplots import make_subplots

from research.gear22_quiet_regime_viz.candles import (
    BAR_MS,
    SPREAD_LONG_COL,
    SPREAD_SHORT_COL,
    build_5m_bucket_stats,
    downsample_ticks,
)
from research.gear22_quiet_regime_viz.load import load_ticks, parse_since_ms
from research.gear22_quiet_regime_viz.plot import ensure_plotly_js
from research.gear22_quiet_regime_viz.quantiles import (
    ROLL_P50_MIN_MASS_FRAC,
    ROLL_P50_MIN_TICKS,
    WINDOW_1M_MS,
    WINDOW_5M_MS,
    eval_grid_ms,
    rolling_tw_p50,
)

SIDES: tuple[tuple[str, str], ...] = (
    ("long", SPREAD_LONG_COL),
    ("short", SPREAD_SHORT_COL),
)

# Display cadence for the sample HTML (formula is defined at any t).
DEFAULT_EVAL_STEP_MS = 1_000
DEFAULT_LOOKBACK_MS = WINDOW_5M_MS

P50_BAR_COLOR = "#4c78a8"
P50_ROLL_1M_COLOR = "#54a24b"
P50_ROLL_5M_COLOR = "#f58518"
SPREAD_FAINT = "rgba(90, 90, 90, 0.10)"

SPREAD_LONG_LABEL = (
    "spread_long (%) = (bybit_bid − okx_ask) / bybit_bid × 100  → open_long"
)
SPREAD_SHORT_LABEL = (
    "spread_short (%) = (okx_bid − bybit_ask) / okx_bid × 100  → open_short"
)


def p50_bar5m_from_buckets(buckets: pd.DataFrame) -> pd.DataFrame:
    """Closed UTC 5m TW-p50, right-aligned at ``bar_end`` (known only after close)."""
    if buckets is None or buckets.empty:
        return pd.DataFrame(
            columns=["bar_start_ms", "bar_end_ms", "bar_end_dt", "p50_bar5m"]
        )
    out = pd.DataFrame(
        {
            "bar_start_ms": buckets["bar_start_ms"].to_numpy(dtype="int64"),
            "bar_end_ms": buckets["bar_end_ms"].to_numpy(dtype="int64"),
            "p50_bar5m": buckets["tw_p50"].to_numpy(dtype="float64"),
        }
    )
    out["bar_end_dt"] = pd.to_datetime(out["bar_end_ms"], unit="ms", utc=True)
    return out


def rolling_p50_at(
    ticks: pd.DataFrame,
    *,
    value_col: str,
    eval_ts_ms: np.ndarray,
    window_ms: int,
    min_mass_frac: float = ROLL_P50_MIN_MASS_FRAC,
    min_ticks: int = ROLL_P50_MIN_TICKS,
) -> np.ndarray:
    """TW-p50 of ticks in ``[t-W, t]`` at each ``eval_ts_ms`` (causal)."""
    if ticks.empty or value_col not in ticks.columns:
        return np.full(np.asarray(eval_ts_ms).shape, np.nan, dtype="float64")
    return rolling_tw_p50(
        ticks["event_local_ts_ms"].to_numpy(dtype="int64"),
        ticks[value_col].to_numpy(dtype="float64"),
        window_ms=int(window_ms),
        eval_ts_ms=np.asarray(eval_ts_ms, dtype="int64"),
        min_mass_frac=float(min_mass_frac),
        min_ticks=int(min_ticks),
    )


def build_state_p50_series(
    ticks: pd.DataFrame,
    *,
    value_col: str,
    plot_start_ms: int,
    plot_end_ms: int,
    eval_step_ms: int = DEFAULT_EVAL_STEP_MS,
) -> dict[str, Any]:
    """Assemble the three locked series for one side.

    Rolling windows need ticks before ``plot_start_ms`` (caller should load a
    5m lookback). Closed-bar p50 uses UTC buckets covering the plot window.
    """
    eval_ts = eval_grid_ms(plot_start_ms, plot_end_ms, eval_step_ms)
    buckets = build_5m_bucket_stats(
        ticks,
        value_col=value_col,
        start_ms=plot_start_ms,
        end_ms=plot_end_ms,
        fill_empty_buckets=True,
    )
    bar = p50_bar5m_from_buckets(buckets)
    # Only bars that have closed inside the plot window.
    if not bar.empty:
        mask = (bar["bar_end_ms"] > plot_start_ms) & (bar["bar_end_ms"] <= plot_end_ms)
        bar = bar.loc[mask].reset_index(drop=True)
    return {
        "eval_ts_ms": eval_ts,
        "eval_dt": pd.to_datetime(eval_ts, unit="ms", utc=True),
        "p50_roll_1m": rolling_p50_at(
            ticks, value_col=value_col, eval_ts_ms=eval_ts, window_ms=WINDOW_1M_MS
        ),
        "p50_roll_5m": rolling_p50_at(
            ticks, value_col=value_col, eval_ts_ms=eval_ts, window_ms=WINDOW_5M_MS
        ),
        "p50_bar5m": bar,
        "buckets": buckets,
    }


def _p50_visible_yrange(*series: Any) -> Optional[list[float]]:
    """Pad around p50 plus the central tick mass so outliers do not hide lines."""
    lows: list[float] = []
    highs: list[float] = []
    for i, raw in enumerate(series):
        if raw is None:
            continue
        arr = np.asarray(raw, dtype="float64")
        finite = arr[np.isfinite(arr)]
        if finite.size == 0:
            continue
        # series[0] is faint ticks (if present); clip to the central mass.
        if i == 0 and finite.size >= 8:
            lo, hi = np.nanpercentile(finite, [5, 95])
        else:
            lo, hi = float(np.nanmin(finite)), float(np.nanmax(finite))
        lows.append(float(lo))
        highs.append(float(hi))
    if not lows:
        return None
    lo, hi = min(lows), max(highs)
    pad = (hi - lo) * 0.12 if hi > lo else 0.01
    return [lo - pad, hi + pad]


def _fig_to_div(fig: go.Figure, *, include_plotlyjs: bool | str) -> str:
    return plotly_plot(
        fig,
        output_type="div",
        include_plotlyjs=include_plotlyjs,
        config={"responsive": True, "displaylogo": False},
    )


def build_state_p50_figure(
    *,
    coin: str,
    ticks: pd.DataFrame,
    series_by_side: Mapping[str, Mapping[str, Any]],
    max_tick_points: int = 4_000,
) -> go.Figure:
    """One chart, two rows (long / short): faint spread + three p50 series."""
    titles = (
        f"{coin} · LONG — {SPREAD_LONG_LABEL}",
        f"{coin} · SHORT — {SPREAD_SHORT_LABEL}",
    )
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        subplot_titles=titles,
    )
    sparse = downsample_ticks(ticks, max_points=max_tick_points)
    for row, (side, value_col) in enumerate(SIDES, start=1):
        packed = series_by_side[side]
        tick_y = None
        if not sparse.empty and value_col in sparse.columns:
            tick_y = sparse[value_col]
            fig.add_trace(
                go.Scatter(
                    x=sparse["event_dt"],
                    y=tick_y,
                    mode="markers",
                    name=f"{side} ticks (faint)",
                    marker=dict(size=2, color=SPREAD_FAINT),
                    hovertemplate="%{x}<br>" + side + "=%{y:.5f}%<extra></extra>",
                    legendgroup=f"{side}-ticks",
                    legendrank=20,
                    showlegend=(row == 1),
                ),
                row=row,
                col=1,
            )
        bar = packed["p50_bar5m"]
        if bar is not None and not bar.empty:
            bar_x = bar["bar_end_dt"]
            bar_y = bar["p50_bar5m"]
        else:
            bar_x = []
            bar_y = []
        fig.add_trace(
            go.Scatter(
                x=bar_x,
                y=bar_y,
                mode="lines",
                name="p50_bar5m",
                line=dict(width=2.8, color=P50_BAR_COLOR, shape="hv"),
                connectgaps=False,
                legendgroup="p50_bar5m",
                legendrank=1,
                showlegend=(row == 1),
                hovertemplate=(
                    "%{x}<br>p50_bar5m=%{y:.5f}%<br>"
                    "window=closed UTC 5m candle<extra></extra>"
                ),
            ),
            row=row,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=packed["eval_dt"],
                y=packed["p50_roll_5m"],
                mode="lines",
                name="p50_roll_5m",
                line=dict(width=2.4, color=P50_ROLL_5M_COLOR),
                connectgaps=False,
                legendgroup="p50_roll_5m",
                legendrank=2,
                showlegend=(row == 1),
                hovertemplate=(
                    "%{x}<br>p50_roll_5m=%{y:.5f}%<br>"
                    "window=[t−300s, t]<extra></extra>"
                ),
            ),
            row=row,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=packed["eval_dt"],
                y=packed["p50_roll_1m"],
                mode="lines",
                name="p50_roll_1m",
                line=dict(width=2.2, color=P50_ROLL_1M_COLOR, dash="dash"),
                connectgaps=False,
                legendgroup="p50_roll_1m",
                legendrank=3,
                showlegend=(row == 1),
                hovertemplate=(
                    "%{x}<br>p50_roll_1m=%{y:.5f}%<br>"
                    "window=[t−60s, t]<extra></extra>"
                ),
            ),
            row=row,
            col=1,
        )
        y_range = _p50_visible_yrange(tick_y, bar_y, packed["p50_roll_5m"], packed["p50_roll_1m"])
        fig.update_yaxes(title_text="%", range=y_range, row=row, col=1)
    fig.update_xaxes(title_text="UTC", row=2, col=1)
    fig.update_layout(
        template="plotly_white",
        height=820,
        legend=dict(orientation="h", yanchor="bottom", y=1.04, x=0, font=dict(size=11)),
        margin=dict(l=60, r=30, t=70, b=40),
        hovermode="closest",
        title=dict(
            text=(
                f"{coin}: current-spread TW-p50 — closed 5m step vs rolling 1m / 5m"
            ),
            font=dict(size=14),
        ),
    )
    return fig


def write_state_p50_html(
    out_path: Path,
    *,
    coin: str,
    ticks: pd.DataFrame,
    series_by_side: Mapping[str, Mapping[str, Any]],
    meta: Mapping[str, Any],
    inline_plotly: bool = False,
    max_tick_points: int = 4_000,
) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if inline_plotly:
        include_js: bool | str = True
        script_tag = ""
    else:
        ensure_plotly_js(out_path.parent)
        include_js = False
        script_tag = '<script src="plotly.min.js"></script>\n'
    fig = build_state_p50_figure(
        coin=coin,
        ticks=ticks,
        series_by_side=series_by_side,
        max_tick_points=max_tick_points,
    )
    div = _fig_to_div(fig, include_plotlyjs=include_js)
    meta_rows = "".join(
        f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>"
        for k, v in meta.items()
    )
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Gear 2.2 state p50 — {html.escape(coin)}</title>
{script_tag}<style>
  body {{ font-family: "IBM Plex Sans", "Segoe UI", sans-serif; margin: 1.25rem; color: #1b1b1b; background: #f7f5f2; }}
  h1 {{ font-size: 1.35rem; margin: 0 0 0.35rem; }}
  .sub {{ color: #444; margin-bottom: 0.75rem; max-width: 72rem; }}
  table.meta {{ border-collapse: collapse; margin-bottom: 1rem; font-size: 0.9rem; }}
  table.meta th, table.meta td {{ border: 1px solid #ccc; padding: 0.25rem 0.55rem; text-align: left; }}
  table.meta th {{ background: #ece8e1; }}
  code {{ background: #ece8e1; padding: 0.05rem 0.25rem; }}
</style>
</head>
<body>
<h1>Gear 2.2 — current-spread p50 ({html.escape(coin)})</h1>
<p class="sub">
Observation only: three time-weighted p50 trackers of <code>spread_long</code> /
<code>spread_short</code>. Closed UTC 5m p50 is a <strong>step</strong> (hv) at
bar close. Rolling 1m / 5m are trailing windows, not the UTC candle.
Same hold→next TW as <code>tw_p50</code>. Holes are not interpolated.
NaN if window mass &lt; 20% of W or fewer than 2 positive-hold ticks.
Not a live threshold, not a simulator gate, not a profitability claim.
Floor formula is unchanged and not drawn here.
</p>
<table class="meta"><tbody>{meta_rows}</tbody></table>
{div}
</body>
</html>
"""
    out_path.write_text(page, encoding="utf-8")
    return out_path


def state_p50_html_filename(coin: str) -> str:
    return f"gear22_state_p50_{str(coin).upper()}.html"


def _parse_coins(raw: str) -> list[str]:
    parts = [p.strip().upper() for p in str(raw).split(",")]
    coins = [p for p in parts if p]
    if not coins:
        raise argparse.ArgumentTypeError("coins list is empty")
    return coins


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m research.gear22_quiet_regime_viz.state_p50",
        description=(
            "Build a file:// HTML chart of p50_bar5m / p50_roll_1m / p50_roll_5m."
        ),
    )
    p.add_argument("--data-root", type=Path, required=True)
    p.add_argument("--coins", type=_parse_coins, default=["SOL"])
    p.add_argument("--since", required=True, help="Plot window start UTC")
    p.add_argument("--until", required=True, help="Plot window end UTC")
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--eval-step-ms", type=int, default=DEFAULT_EVAL_STEP_MS)
    p.add_argument("--lookback-ms", type=int, default=DEFAULT_LOOKBACK_MS)
    p.add_argument("--inline-plotly", action="store_true")
    p.add_argument("--max-tick-points", type=int, default=4_000)
    return p


def run(
    *,
    data_root: Path,
    coins: Sequence[str],
    since: str,
    until: str,
    out_dir: Path,
    eval_step_ms: int = DEFAULT_EVAL_STEP_MS,
    lookback_ms: int = DEFAULT_LOOKBACK_MS,
    inline_plotly: bool = False,
    max_tick_points: int = 4_000,
) -> list[Path]:
    plot_start = parse_since_ms(since)
    plot_end = parse_since_ms(until)
    load_start = plot_start - int(lookback_ms)
    written: list[Path] = []
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    all_ticks = load_ticks(
        Path(data_root),
        coins=list(coins),
        since_ms=load_start,
        until_ms=plot_end,
    )
    for coin in coins:
        sub = all_ticks.loc[all_ticks["base_coin"] == str(coin).upper()].copy()
        if sub.empty:
            raise ValueError(f"no ticks for {coin} in plot window")
        series_by_side: dict[str, dict[str, Any]] = {}
        for side, col in SIDES:
            series_by_side[side] = build_state_p50_series(
                sub,
                value_col=col,
                plot_start_ms=plot_start,
                plot_end_ms=plot_end,
                eval_step_ms=int(eval_step_ms),
            )
        n_finite_1m = int(np.isfinite(series_by_side["long"]["p50_roll_1m"]).sum())
        n_eval = int(np.asarray(series_by_side["long"]["eval_ts_ms"]).size)
        meta = {
            "coin": str(coin).upper(),
            "since": since,
            "until": until,
            "ticks": int(len(sub)),
            "eval_step_ms": int(eval_step_ms),
            "eval_points": n_eval,
            "long_p50_roll_1m_finite": n_finite_1m,
            "lookback_ms": int(lookback_ms),
            "NaN_rule": (
                f"mass < {ROLL_P50_MIN_MASS_FRAC:.0%} of W or "
                f"<{ROLL_P50_MIN_TICKS} positive-hold ticks"
            ),
            "p50_bar5m_x": "bar_end (hv step)",
            "data_root": str(Path(data_root).resolve()),
        }
        path = out_dir / state_p50_html_filename(coin)
        write_state_p50_html(
            path,
            coin=str(coin).upper(),
            ticks=sub,
            series_by_side=series_by_side,
            meta=meta,
            inline_plotly=inline_plotly,
            max_tick_points=max_tick_points,
        )
        written.append(path)
    return written


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    paths = run(
        data_root=args.data_root,
        coins=args.coins,
        since=args.since,
        until=args.until,
        out_dir=args.out_dir,
        eval_step_ms=args.eval_step_ms,
        lookback_ms=args.lookback_ms,
        inline_plotly=args.inline_plotly,
        max_tick_points=args.max_tick_points,
    )
    for p in paths:
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
