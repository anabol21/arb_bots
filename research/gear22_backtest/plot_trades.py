"""Plotly observer for gear-2.2 dummy trades. No trading rules.

One figure, three panels (theta / p50 / spread_last), pad around each trade
on that coin only. Fill = 1 Hz ``spread_last``, not Trade_Lat. Not a PnL chart.

Hive I/O is one parquet open per UTC day (not per trade). Plotly slider/dropdown
only switch ``frames`` — they do not copy x/y into every button. Long holds are
stride-downsampled for display; open/close seconds are kept. NaN is not filled.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd

from research.gear22_backtest.replay import OpenPosition

PAD_S = 900  # 15 minutes each side of [ts_open, ts_end]
MAX_POINTS = 2500  # display stride; open/close seconds always kept
DEFAULT_HTML = Path("output/gear22_backtest_trade_plots.html")
PLOT_COLUMNS: tuple[str, ...] = (
    "ts_s",
    "coin",
    "theta_1m_long",
    "theta_1m_short",
    "p50_1m_long",
    "p50_1m_short",
    "spread_last_long",
    "spread_last_short",
)
_LINE_SPECS: tuple[tuple[str, int, str], ...] = (
    ("theta_1m_long", 1, "theta long"),
    ("theta_1m_short", 1, "theta short"),
    ("p50_1m_long", 2, "p50 long"),
    ("p50_1m_short", 2, "p50 short"),
    ("spread_last_long", 3, "spread long"),
    ("spread_last_short", 3, "spread short"),
)
_N_LINE = len(_LINE_SPECS)
_PART = "part-000.parquet"
_ANIMATE = {
    "frame": {"duration": 0, "redraw": True},
    "mode": "immediate",
    "transition": {"duration": 0},
}


@dataclass(frozen=True)
class TradeWindow:
    index: int
    coin: str
    side: str
    ts_open: int
    ts_end: int  # ts_close, or last_ts_s for unclosed
    ts_close: Optional[int]
    t_left: int
    t_right: int
    fill_spread_pp: float
    exit_spread_pp: Optional[float]
    potential_pp: Optional[float]
    status: str
    hold_s: int


def open_spread_column(side: str) -> str:
    """Open-side ``spread_last_*`` (long trade → long book)."""
    if side == "short":
        return "spread_last_short"
    return "spread_last_long"


def close_spread_column(side: str) -> str:
    """Unwind-side ``spread_last_*`` (long position → short book)."""
    if side == "long":
        return "spread_last_short"
    return "spread_last_long"


def utc_date_strings(t_left: int, t_right: int) -> list[str]:
    """Inclusive UTC calendar dates covering ``[t_left, t_right]``."""
    start = datetime.fromtimestamp(int(t_left), tz=timezone.utc).date()
    end = datetime.fromtimestamp(int(t_right), tz=timezone.utc).date()
    out: list[str] = []
    day = start
    while day <= end:
        out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def iter_trade_windows(
    df_trades: pd.DataFrame,
    open_positions: Sequence[OpenPosition] | None = None,
    *,
    pad_s: int = PAD_S,
) -> list[TradeWindow]:
    """One window per trade row, sorted ``(ts_open, coin)``. Unclosed uses last_ts_s."""
    if df_trades is None or df_trades.empty:
        return []
    last_map: dict[tuple[str, int], Optional[int]] = {}
    for pos in open_positions or ():
        last_map[(str(pos.coin), int(pos.ts_open))] = pos.last_ts_s

    work = df_trades.copy()
    work["coin"] = work["coin"].astype(str)
    work = work.sort_values(["ts_open", "coin"], kind="mergesort", ignore_index=True)

    windows: list[TradeWindow] = []
    for i, rec in enumerate(work.itertuples(index=False)):
        coin = str(rec.coin)
        ts_open = int(rec.ts_open)
        status = str(getattr(rec, "status", "closed") or "closed")
        ts_close = _optional_int(getattr(rec, "ts_close", None))
        last_ts = last_map.get((coin, ts_open))
        ts_end = ts_close if ts_close is not None else _optional_int(last_ts)
        if ts_end is None:
            ts_end = ts_open
        t_left = ts_open - int(pad_s)
        t_right = ts_end + int(pad_s)
        fill = float(rec.fill_spread_pp)
        exit_pp = _optional_float(getattr(rec, "exit_spread_pp", None))
        pot = _optional_float(getattr(rec, "potential_pp", None))
        windows.append(
            TradeWindow(
                index=i,
                coin=coin,
                side=str(rec.side),
                ts_open=ts_open,
                ts_end=ts_end,
                ts_close=ts_close,
                t_left=t_left,
                t_right=t_right,
                fill_spread_pp=fill,
                exit_spread_pp=exit_pp,
                potential_pp=pot,
                status=status,
                hold_s=int(ts_end - ts_open),
            )
        )
    return windows


def load_window(
    hive: Path | str,
    coin: str,
    t_left: int,
    t_right: int,
) -> pd.DataFrame:
    """Read one coin's 1 Hz slice. Missing hive days are skipped, not filled."""
    dummy = TradeWindow(
        index=0,
        coin=str(coin),
        side="long",
        ts_open=int(t_left),
        ts_end=int(t_right),
        ts_close=int(t_right),
        t_left=int(t_left),
        t_right=int(t_right),
        fill_spread_pp=0.0,
        exit_spread_pp=None,
        potential_pp=None,
        status="closed",
        hold_s=0,
    )
    return load_trade_slices(hive, [dummy])[0]


def load_trade_slices(
    hive: Path | str,
    windows: Sequence[TradeWindow],
) -> list[pd.DataFrame]:
    """One parquet open per UTC day; slice windows in memory."""
    import pyarrow.parquet as pq

    empty = pd.DataFrame(columns=list(PLOT_COLUMNS))
    if not windows:
        return []
    root = Path(hive)
    by_day: dict[str, list[TradeWindow]] = {}
    for window in windows:
        for day in utc_date_strings(window.t_left, window.t_right):
            by_day.setdefault(day, []).append(window)

    day_frames: dict[str, pd.DataFrame] = {}
    for day, group in by_day.items():
        path = root / f"event_date={day}" / _PART
        if not path.is_file():
            continue
        lo = min(w.t_left for w in group)
        hi = max(w.t_right for w in group)
        coins = sorted({w.coin for w in group})
        table = pq.read_table(
            path,
            columns=list(PLOT_COLUMNS),
            filters=[
                ("ts_s", ">=", int(lo)),
                ("ts_s", "<=", int(hi)),
                ("coin", "in", coins),
            ],
        )
        if table.num_rows == 0:
            continue
        pdf = table.to_pandas()
        pdf["coin"] = pdf["coin"].astype(str)
        day_frames[day] = pdf

    slices: list[pd.DataFrame] = []
    for window in windows:
        parts: list[pd.DataFrame] = []
        for day in utc_date_strings(window.t_left, window.t_right):
            pdf = day_frames.get(day)
            if pdf is None or pdf.empty:
                continue
            hit = pdf.loc[
                (pdf["coin"] == window.coin)
                & (pdf["ts_s"] >= window.t_left)
                & (pdf["ts_s"] <= window.t_right)
            ]
            if not hit.empty:
                parts.append(hit)
        if not parts:
            slices.append(empty.copy())
            continue
        out = pd.concat(parts, ignore_index=True)
        out = out.sort_values("ts_s", kind="mergesort", ignore_index=True)
        slices.append(out.drop_duplicates(subset=["ts_s"], keep="last"))
    return slices


def downsample_slice(
    sl: pd.DataFrame,
    window: TradeWindow,
    max_points: int = MAX_POINTS,
) -> pd.DataFrame:
    """Stride long windows for display. Does not interpolate NaN."""
    if sl.empty or len(sl) <= max_points:
        return sl
    n = len(sl)
    step = int(math.ceil(n / max_points))
    keep = np.zeros(n, dtype=bool)
    keep[::step] = True
    keep[0] = True
    keep[-1] = True
    ts = sl["ts_s"].to_numpy()
    keep |= ts == window.ts_open
    if window.ts_close is not None:
        keep |= ts == window.ts_close
    keep |= ts == window.ts_end
    return sl.loc[keep]


def window_title(window: TradeWindow, n: int) -> str:
    pot = window.potential_pp
    pot_s = "nan" if pot is None else f"{pot:.4f}"
    tag = "unclosed" if window.status == "open" else "closed"
    return (
        f"#{window.index + 1}/{n}  {window.coin}  {window.side}  "
        f"{tag}  potential_pp={pot_s}  hold_s={window.hold_s}"
    )


def trades_figure(
    hive: Path | str,
    df_trades: pd.DataFrame,
    open_positions: Sequence[OpenPosition] | Iterable[OpenPosition] | None = None,
    *,
    pad_s: int = PAD_S,
    max_points: int = MAX_POINTS,
    verbose: bool = False,
):
    """One Plotly figure: dropdown + slider over trades. ``None`` if no rows.

    Slider/dropdown switch ``frames`` (no x/y copy per button).
    """
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    positions = list(open_positions or ())
    windows = iter_trade_windows(df_trades, positions, pad_s=pad_s)
    if not windows:
        return None
    t0 = time.perf_counter()
    slices = load_trade_slices(hive, windows)
    t_read = time.perf_counter() - t0
    n = len(windows)
    drawn = [
        downsample_slice(sl, window, max_points)
        for window, sl in zip(windows, slices)
    ]

    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        subplot_titles=("theta_1m", "p50_1m", "spread_last"),
    )
    first_traces = _window_traces(windows[0], drawn[0])
    for j, (_col, row, _name) in enumerate(_LINE_SPECS):
        fig.add_trace(first_traces[j], row=row, col=1)
    fig.add_trace(first_traces[_N_LINE], row=3, col=1)
    fig.add_trace(first_traces[_N_LINE + 1], row=3, col=1)

    frames = []
    for i, (window, sl) in enumerate(zip(windows, drawn)):
        frames.append(
            go.Frame(
                name=str(i),
                data=_window_traces(window, sl),
                traces=list(range(_N_LINE + 2)),
                layout=go.Layout(
                    title=window_title(window, n),
                    shapes=_vline_shapes(window),
                ),
            )
        )
    fig.frames = frames

    steps = []
    dropdown = []
    for i, window in enumerate(windows):
        args = [[str(i)], _ANIMATE]
        steps.append(
            dict(
                method="animate",
                args=args,
                label=f"{i + 1}/{n} {window.coin}",
            )
        )
        dropdown.append(
            dict(
                method="animate",
                args=args,
                label=f"{i + 1}/{n} {window.coin} {window.side}",
            )
        )

    fig.update_layout(
        template="plotly_white",
        height=780,
        hovermode="x unified",
        legend=dict(orientation="h", y=1.08),
        margin=dict(l=50, r=20, t=80, b=80),
        title=window_title(windows[0], n),
        shapes=_vline_shapes(windows[0]),
        updatemenus=[
            dict(
                type="dropdown",
                direction="down",
                x=0.0,
                y=1.18,
                xanchor="left",
                showactive=True,
                buttons=dropdown,
            )
        ],
        sliders=[
            dict(
                active=0,
                pad=dict(t=30),
                currentvalue=dict(prefix="trade "),
                steps=steps,
            )
        ],
    )
    fig.update_yaxes(title_text="pp", row=1, col=1)
    fig.update_yaxes(title_text="pp", row=2, col=1)
    fig.update_yaxes(title_text="pp", row=3, col=1)
    fig.update_xaxes(title_text="UTC", row=3, col=1)
    if verbose:
        npts = int(sum(len(sl) for sl in drawn))
        print(
            f"plot_trades: n={n} hive_s={t_read:.2f} "
            f"display_points={npts} max_points={max_points}"
        )
    return fig


def write_trades_html(
    fig,
    path: Path | str | None = None,
    *,
    verbose: bool = True,
) -> Path:
    """Standalone HTML (plotly.js inlined). Open in a browser, not ``fig.show()``."""
    if fig is None:
        raise ValueError("no figure to write")
    out = Path(path) if path is not None else DEFAULT_HTML
    if not out.is_absolute():
        out = Path.cwd() / out
    out.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    fig.write_html(
        str(out),
        include_plotlyjs=True,
        full_html=True,
        config={"scrollZoom": True, "displaylogo": False},
    )
    if verbose:
        size_mb = out.stat().st_size / 1e6
        print(
            f"wrote {out} ({size_mb:.1f} MB, {time.perf_counter() - t0:.2f}s) "
            f"file://{out}"
        )
    return out


def _window_traces(window: TradeWindow, sl: pd.DataFrame) -> list:
    import plotly.graph_objects as go

    xs, ys = _trace_xy(window, sl)
    line = dict(width=1)
    traces = []
    for j, (_col, _row, name) in enumerate(_LINE_SPECS):
        traces.append(
            go.Scattergl(
                x=xs[j],
                y=ys[j],
                name=name,
                mode="lines",
                line=line,
                legendgroup=name,
                hovertemplate="%{x|%Y-%m-%d %H:%M:%S} UTC<br>%{y:.4f}<extra>"
                + name
                + "</extra>",
            )
        )
    traces.append(
        go.Scatter(
            x=xs[_N_LINE],
            y=ys[_N_LINE],
            mode="markers",
            marker=dict(symbol="triangle-up", size=12, color="#2ca02c"),
            name="fill (open-side spread_last)",
        )
    )
    traces.append(
        go.Scatter(
            x=xs[_N_LINE + 1],
            y=ys[_N_LINE + 1],
            mode="markers",
            marker=dict(symbol="triangle-down", size=12, color="#d62728"),
            name="exit (unwind-side spread_last)",
        )
    )
    return traces


def _trace_xy(
    window: TradeWindow, sl: pd.DataFrame
) -> tuple[list, list]:
    dt = _dt_index(sl)
    xs: list = []
    ys: list = []
    for col, _row, _name in _LINE_SPECS:
        xs.append(dt)
        if sl.empty:
            ys.append([])
        else:
            ys.append(sl[col].to_numpy(dtype="float64", copy=False))
    fill_y = _marker_y(sl, window.ts_open, open_spread_column(window.side))
    if fill_y is None:
        fill_y = window.fill_spread_pp
    xs.append([_ts_to_dt(window.ts_open)])
    ys.append([fill_y])
    if window.ts_close is not None:
        exit_y = _marker_y(sl, window.ts_close, close_spread_column(window.side))
        if exit_y is None:
            exit_y = window.exit_spread_pp
        xs.append([_ts_to_dt(window.ts_close)])
        ys.append([exit_y])
    else:
        xs.append([_ts_to_dt(window.ts_end)])
        ys.append([window.exit_spread_pp])
    return xs, ys


def _vline_shapes(window: TradeWindow) -> list[dict[str, object]]:
    shapes: list[dict[str, object]] = [
        dict(
            type="line",
            x0=_ts_to_dt(window.ts_open),
            x1=_ts_to_dt(window.ts_open),
            y0=0,
            y1=1,
            xref="x",
            yref="paper",
            line=dict(color="#2ca02c", width=1, dash="dot"),
        )
    ]
    right = window.ts_close if window.ts_close is not None else window.ts_end
    dash = "dot" if window.ts_close is not None else "dash"
    color = "#d62728" if window.ts_close is not None else "#9467bd"
    shapes.append(
        dict(
            type="line",
            x0=_ts_to_dt(right),
            x1=_ts_to_dt(right),
            y0=0,
            y1=1,
            xref="x",
            yref="paper",
            line=dict(color=color, width=1, dash=dash),
        )
    )
    return shapes


def _dt_index(sl: pd.DataFrame):
    if sl.empty:
        return []
    return pd.to_datetime(sl["ts_s"], unit="s", utc=True)


def _ts_to_dt(ts: int):
    return pd.Timestamp(int(ts), unit="s", tz="UTC")


def _marker_y(sl: pd.DataFrame, ts: int, column: str) -> Optional[float]:
    if sl.empty or column not in sl.columns:
        return None
    hit = sl.loc[sl["ts_s"] == int(ts), column]
    if hit.empty:
        return None
    value = hit.iloc[0]
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return float(value)


def _optional_int(value: object) -> Optional[int]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return int(value)


def _optional_float(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return float(value)
