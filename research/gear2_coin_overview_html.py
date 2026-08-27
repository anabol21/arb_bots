"""Standalone HTML overview of spread_long/spread_short for every lean-tick coin.

Same calendar as the gear-2 notebook overview cell: 2026-08-05 .. 2026-08-19 UTC
inclusive (END exclusive = 2026-08-20). One batched file pass: full-tick stats
(hist / p-tiles / day counts) plus a thinned line sample.

Each coin: downsampled time series (gap-break ~6 min so missing 5-min files are
honest holes) + histograms and p50/p95/p99 on **all ticks** (running histogram
CDF; see captions), plus Bybit L1 mid, causal Gear 1.5 geom score
(bar ``[t−5m, t)``), Top-10 1.5 bands, and per-UTC-day tick counts.

Universe: every ``base_coin`` present in the window. Index splits crypto vs
акции/non-crypto. Coin pages next/prev stay inside that group.

Output is gitignore-friendly under ``output/``. Not a gear-2 close.

Usage (repo root)::

  ./venv/bin/python research/gear2_coin_overview_html.py
  ./venv/bin/python research/gear2_coin_overview_html.py --limit-coins 8
  ./venv/bin/python research/gear2_coin_overview_html.py --patch-period-ui

All-ticks period chart (not embedded): start the local server and open HTTP, not file://::

  ./venv/bin/python research/gear2_overview_server.py
  # then http://127.0.0.1:8765/0G.html
"""

from __future__ import annotations

import argparse
import html
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import plotly
from plotly.offline import plot as plotly_plot

REPO = Path(__file__).resolve().parents[1]
DEFAULT_TICKS = REPO / "output" / "lean_ticks"
DEFAULT_OUT = REPO / "output" / "gear2_coin_overview"
OVERVIEW_START = "2026-08-05T00:00:00Z"
OVERVIEW_END = "2026-08-20T00:00:00Z"  # 19.08 inclusive
OVERVIEW_MAX_TICKS = 8000
OVERVIEW_GAP_BREAK_MS = 6 * 60 * 1000
MAX_ALL_TICK_POINTS = 300_000  # refuse; never silent-downsample the all-ticks view
DEFAULT_PERIOD_MS = 60 * 60 * 1000
DAY_START = "2026-08-05"
DAY_END_INCL = "2026-08-19"
PERIOD_UI_MARKER = 'id="period-panel"'
PERIOD_JS_NAME = "overview_period.js"
FILE_SERVER_NOTE = (
    "полный ряд тиков нужен локальный сервер: "
    "`./venv/bin/python research/gear2_overview_server.py`"
)
REGIME_TOP_N = 10
OV_COLS = [
    "event_local_ts_ms",
    "base_coin",
    "okx_bid_price",
    "okx_ask_price",
    "bybit_bid_price",
    "bybit_ask_price",
]
KLASS_CRYPTO = "крипто"
KLASS_OTHER = "не крипто"
ALL_TICK_NOTE = (
    "квантили и гистограммы — по всем тикам "
    "(CDF по 20 тыс. корзинам, шаг 0.001 п.п. на [-10, 10]%); "
    "линия — прореженная"
)
PAGE_CSS = """
:root {
  --bg: #f4f1ea;
  --card: #fffdf8;
  --ink: #1c1917;
  --muted: #57534e;
  --line: #e7e0d4;
  --accent: #1d4ed8;
  --accent-ink: #fff;
  --gold: #fbbf24;
  --gold-bg: #fff7db;
  --disabled: #a8a29e;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: "Source Sans 3", "Segoe UI", system-ui, sans-serif;
  background: var(--bg);
  color: var(--ink);
  line-height: 1.45;
}
.wrap { max-width: 96rem; margin: 0 auto; padding: 1.1rem 1.4rem 3rem; }
h1 { font-size: 1.55rem; font-weight: 700; margin: 0.75rem 0 0.45rem; letter-spacing: -0.02em; }
h2 { font-size: 1.2rem; margin: 1.6rem 0 0.4rem; }
p { margin: 0.45rem 0; }
a { color: var(--accent); }
.lede, .note { max-width: 70rem; color: var(--muted); }
.lede { font-size: 1.02rem; }
.meta { color: var(--muted); font-size: 0.92rem; }
nav.coin-nav {
  display: flex;
  flex-wrap: wrap;
  gap: 0.55rem 0.75rem;
  align-items: center;
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 0.65rem 0.9rem;
  position: sticky;
  top: 0.4rem;
  z-index: 20;
}
nav.coin-nav .here { font-weight: 700; }
a.nav-btn, span.nav-disabled {
  display: inline-block;
  padding: 0.38rem 0.7rem;
  border-radius: 8px;
  font-weight: 650;
  text-decoration: none;
  font-size: 0.95rem;
}
a.nav-btn { background: var(--accent); color: var(--accent-ink); }
a.nav-btn:hover { filter: brightness(1.08); }
a.nav-home { background: transparent; border: 1px solid var(--line); color: var(--ink); }
span.nav-disabled {
  background: #eeeae3;
  color: var(--disabled);
  cursor: not-allowed;
}
.kbd { font-size: 0.88rem; color: var(--muted); width: 100%; }
.legend-topn {
  display: flex;
  gap: 0.7rem;
  align-items: flex-start;
  background: var(--gold-bg);
  border: 1px solid var(--gold);
  border-radius: 10px;
  padding: 0.7rem 0.9rem;
  margin: 0.8rem 0;
  max-width: 70rem;
}
.swatch {
  width: 1.15rem;
  height: 1.15rem;
  margin-top: 0.15rem;
  background: rgba(255, 193, 7, 0.55);
  border-radius: 3px;
  flex-shrink: 0;
}
.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(13.5rem, 1fr));
  gap: 0.65rem;
  margin: 0.7rem 0 0.55rem;
}
.stat {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 0.65rem 0.8rem;
}
.stat .k { display: block; font-size: 0.8rem; color: var(--muted); }
.stat .v { display: block; font-size: 1.02rem; font-weight: 650; margin-top: 0.15rem; }
.days { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 0.7rem 0.9rem; overflow: auto; }
.days pre { margin: 0; font-size: 0.82rem; }
.count-pill {
  display: inline-block;
  background: #e8efe8;
  color: #14532d;
  border-radius: 999px;
  padding: 0.12rem 0.55rem;
  font-size: 0.88rem;
  font-weight: 650;
}
table { border-collapse: collapse; width: 100%; background: var(--card); }
th, td { border-bottom: 1px solid var(--line); padding: 0.38rem 0.55rem; font-size: 0.86rem; text-align: left; }
th { background: #efe8dc; position: sticky; top: 0; z-index: 1; }
tbody tr:hover { background: #faf6ee; }
.section-head { display: flex; align-items: baseline; gap: 0.6rem; flex-wrap: wrap; }
.plot-slot {
  margin-top: 1.25rem;
  margin-bottom: 0.5rem;
}
.plot-slot .js-plotly-plot,
.plot-slot .plotly-graph-div {
  width: 100% !important;
}
.period-panel {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 0.75rem 0.95rem;
  margin: 0.85rem 0 0.4rem;
  max-width: 70rem;
}
.period-panel h2 { margin: 0 0 0.35rem; font-size: 1.05rem; }
.period-row {
  display: flex;
  flex-wrap: wrap;
  gap: 0.65rem 1rem;
  align-items: flex-end;
  margin-top: 0.55rem;
}
.period-field label {
  display: block;
  font-size: 0.8rem;
  color: var(--muted);
  margin-bottom: 0.2rem;
}
.period-field input {
  font: inherit;
  padding: 0.35rem 0.5rem;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fff;
}
.period-presets { display: flex; flex-wrap: wrap; gap: 0.35rem; }
.period-panel button {
  font: inherit;
  font-weight: 650;
  padding: 0.38rem 0.7rem;
  border-radius: 8px;
  border: 1px solid var(--line);
  background: #efe8dc;
  cursor: pointer;
}
.period-panel button#period-load {
  background: var(--accent);
  color: var(--accent-ink);
  border-color: var(--accent);
}
.period-panel button:disabled {
  background: #eeeae3;
  color: var(--disabled);
  cursor: not-allowed;
  border-color: var(--line);
}
.period-file-note {
  background: #fff7db;
  border: 1px solid var(--gold);
  border-radius: 8px;
  padding: 0.45rem 0.65rem;
  color: #78350f;
}
.period-err { color: #9f1239; }
.period-plot { min-height: 4rem; width: 100%; }
#period-plot-slot h2 { margin-bottom: 0.35rem; }
"""


# Injected into already-generated coin HTML (PAGE_CSS already has these for new pages).
PERIOD_EXTRA_CSS = """
.period-panel {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 0.75rem 0.95rem;
  margin: 0.85rem 0 0.4rem;
  max-width: 70rem;
}
.period-panel h2 { margin: 0 0 0.35rem; font-size: 1.05rem; }
.period-row {
  display: flex;
  flex-wrap: wrap;
  gap: 0.65rem 1rem;
  align-items: flex-end;
  margin-top: 0.55rem;
}
.period-field label {
  display: block;
  font-size: 0.8rem;
  color: var(--muted);
  margin-bottom: 0.2rem;
}
.period-field input {
  font: inherit;
  padding: 0.35rem 0.5rem;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fff;
}
.period-presets { display: flex; flex-wrap: wrap; gap: 0.35rem; }
.period-panel button {
  font: inherit;
  font-weight: 650;
  padding: 0.38rem 0.7rem;
  border-radius: 8px;
  border: 1px solid var(--line);
  background: #efe8dc;
  cursor: pointer;
}
.period-panel button#period-load {
  background: var(--accent);
  color: var(--accent-ink);
  border-color: var(--accent);
}
.period-panel button:disabled {
  background: #eeeae3;
  color: var(--disabled);
  cursor: not-allowed;
  border-color: var(--line);
}
.period-file-note {
  background: #fff7db;
  border: 1px solid var(--gold);
  border-radius: 8px;
  padding: 0.45rem 0.65rem;
  color: #78350f;
}
.period-err { color: #9f1239; }
.period-plot { min-height: 4rem; width: 100%; }
#period-plot-slot h2 { margin-bottom: 0.35rem; }
"""


def _downsample_keep_ends(frame: pd.DataFrame, max_points: int) -> pd.DataFrame:
    n = len(frame)
    if max_points is None or max_points <= 0 or n <= max_points:
        return frame
    step = max(1, n // int(max_points))
    idx = list(range(0, n, step))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return frame.iloc[idx]


def prepare_overview_spreads(raw: pd.DataFrame) -> pd.DataFrame:
    """Match the notebook overview cell: derive spreads, no stale-cross drop."""
    ov = raw.copy()
    ov["base_coin"] = ov["base_coin"].astype(str).str.upper()
    ov["event_local_ts_ms"] = pd.to_numeric(ov["event_local_ts_ms"], errors="coerce")
    for c in ("okx_bid_price", "okx_ask_price", "bybit_bid_price", "bybit_ask_price"):
        ov[c] = pd.to_numeric(ov[c], errors="coerce")
    ov = ov.dropna(
        subset=[
            "event_local_ts_ms",
            "okx_bid_price",
            "okx_ask_price",
            "bybit_bid_price",
            "bybit_ask_price",
        ]
    )
    ov = ov.loc[(ov["okx_bid_price"] > 0) & (ov["bybit_bid_price"] > 0)].copy()
    ov["spread_long"] = (
        (ov["bybit_bid_price"] - ov["okx_ask_price"]) / ov["bybit_bid_price"] * 100.0
    )
    ov["spread_short"] = (
        (ov["okx_bid_price"] - ov["bybit_ask_price"]) / ov["okx_bid_price"] * 100.0
    )
    ov["event_dt"] = pd.to_datetime(ov["event_local_ts_ms"], unit="ms", utc=True)
    return ov.sort_values(["base_coin", "event_local_ts_ms"], kind="mergesort").reset_index(
        drop=True
    )


def _copy_plotly_js(out_dir: Path) -> str:
    src = Path(plotly.__file__).resolve().parent / "package_data" / "plotly.min.js"
    dest = out_dir / "plotly.min.js"
    if src.is_file():
        dest.write_bytes(src.read_bytes())
        return "plotly.min.js"
    return "https://cdn.plot.ly/plotly-2.35.2.min.js"


def group_sorted_coins(coins, is_crypto_fn) -> tuple[list[str], list[str]]:
    crypto = sorted(c for c in coins if is_crypto_fn(c))
    other = sorted(c for c in coins if not is_crypto_fn(c))
    return crypto, other


def nav_neighbors(group: list[str], coin: str) -> tuple[str | None, str | None]:
    """Prev/next inside ``group``; None at the ends (no wrap)."""
    if coin not in group:
        return None, None
    i = group.index(coin)
    prev_c = group[i - 1] if i > 0 else None
    next_c = group[i + 1] if i + 1 < len(group) else None
    return prev_c, next_c


class WindowTooWideError(ValueError):
    """All-ticks window exceeds the Plotly safety cap; caller must not downsample."""

    def __init__(self, n: int, max_points: int) -> None:
        self.n = int(n)
        self.max_points = int(max_points)
        super().__init__(
            f"окно слишком широкое: {self.n} тиков (лимит {self.max_points}). "
            "Сузьте период — полный ряд без прореживания, иначе Plotly зависнет."
        )


def ms_to_iso_z(ms: int) -> str:
    return pd.Timestamp(int(ms), unit="ms", tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_to_datetime_local(iso: str) -> str:
    s = str(iso).replace("Z", "").replace("+00:00", "")
    if len(s) >= 19:
        return s[:19]
    if len(s) >= 16:
        return s[:16]
    return s


def default_period_bounds(start_ms: int, end_ms: int, used_files=None):
    """Last 1 hour ending at the last overlapping parquet file, else last 1h of calendar."""
    from research.lean_ticks_io import parse_lean_file_window

    pe = int(end_ms)
    if used_files:
        last = parse_lean_file_window(used_files[-1])
        if last is not None:
            pe = int(last[1])
    ps = pe - int(DEFAULT_PERIOD_MS)
    if ps < int(start_ms):
        ps = int(start_ms)
    if ps >= pe:
        pe = min(int(end_ms), ps + 5 * 60 * 1000)
    return int(ps), int(pe)


def infer_default_period(ticks: Path, start_ms: int, end_ms: int):
    from research.lean_ticks_io import list_lean_files_overlapping

    used = list_lean_files_overlapping(ticks, start_ms, end_ms)
    return default_period_bounds(start_ms, end_ms, used)


def load_window_overview(
    ticks: Path,
    coin: str,
    start_ms: int,
    end_ms: int,
    *,
    workers: int,
    max_points: int,
    columns=None,
):
    """Every matching row for one coin. No even-take. Raises WindowTooWideError."""
    import pyarrow as pa

    from research.lean_ticks_io import iter_lean_tables

    if end_ms <= start_ms:
        raise ValueError("END must be after START")
    u = str(coin).upper()
    cols = list(columns) if columns is not None else list(OV_COLS)
    tables = []
    used = []
    n_raw = 0
    try:
        stream = iter_lean_tables(
            ticks,
            int(start_ms),
            int(end_ms),
            coins={u},
            workers=int(workers),
            columns=cols,
            chunk=16,
        )
        for path, table in stream:
            n_raw += int(table.num_rows)
            if n_raw > int(max_points):
                raise WindowTooWideError(n_raw, max_points)
            tables.append(table)
            used.append(path)
    except FileNotFoundError:
        return None, [], 0
    if not tables:
        return None, used, 0
    raw = pa.concat_tables(tables, promote_options="permissive").to_pandas()
    del tables
    ov = prepare_overview_spreads(raw)
    del raw
    if len(ov) > int(max_points):
        raise WindowTooWideError(len(ov), max_points)
    return ov, used, n_raw


def coin_figure(
    ov: pd.DataFrame,
    coin: str,
    *,
    max_ticks: int,
    thresh: float,
    gap_ms: int,
    features=None,
    stats=None,
    topn_intervals=None,
    topn_note=None,
    title=None,
    line_is_all_ticks: bool = False,
):
    from research.gear2_regime_topn import causal_composite_at_ticks
    from research.gear2_spread_plots import (
        AXIS_WINDOW_NOTE,
        NO_HIST_VOL_NOTE,
        bybit_l1_mid,
        make_spread_ts_hist_figure,
        spread_percentiles,
        xy_with_gaps,
    )

    ov_plot = _downsample_keep_ends(ov, max_ticks).copy()
    ov_plot["bybit_l1_mid"] = bybit_l1_mid(
        ov_plot["bybit_bid_price"], ov_plot["bybit_ask_price"]
    )
    ov_plot["score_15"] = causal_composite_at_ticks(
        ov_plot["event_local_ts_ms"], features
    )
    x_long, y_long = xy_with_gaps(ov_plot, "spread_long", gap_ms)
    x_short, y_short = xy_with_gaps(ov_plot, "spread_short", gap_ms)
    x_px, y_px = xy_with_gaps(ov_plot, "bybit_l1_mid", gap_ms)
    x_vol, y_vol = xy_with_gaps(ov_plot, "score_15", gap_ms)
    has_feat = features is not None and len(features) > 0

    hist_long_binned = hist_short_binned = None
    pct_l = pct_s = None
    n_all = None
    if stats is not None:
        n_all = int(stats.n_ticks)
        pct_l = stats.long.percentiles()
        pct_s = stats.short.percentiles()
        hist_long_binned = stats.long.display_bars()
        hist_short_binned = stats.short.display_bars()
        if line_is_all_ticks:
            hist_note = (
                f"квантили, гистограммы и линия — все тики окна, n={n_all}; "
                f"{AXIS_WINDOW_NOTE}"
            )
        else:
            hist_note = (
                f"квантили и гистограммы — по всем тикам, n_все={n_all}; "
                f"{AXIS_WINDOW_NOTE}"
            )
    else:
        pct_l = spread_percentiles(ov["spread_long"])
        pct_s = spread_percentiles(ov["spread_short"])
        if line_is_all_ticks:
            hist_note = (
                f"квантили, гистограммы и линия — все тики окна, n={len(ov)}; "
                f"{AXIS_WINDOW_NOTE}"
            )
        else:
            hist_note = (
                f"квантили по загруженной выборке обзора, n={len(ov)}; "
                f"на линии={len(ov_plot)} — не все тики"
            )

    fig = make_spread_ts_hist_figure(
        x_long=x_long,
        y_long=y_long,
        x_short=x_short,
        y_short=y_short,
        hist_long=None if hist_long_binned is not None else ov["spread_long"].to_numpy(
            dtype="float64", copy=False
        ),
        hist_short=None if hist_short_binned is not None else ov["spread_short"].to_numpy(
            dtype="float64", copy=False
        ),
        title=title or f"{coin}  тики lean  {DAY_START} … {DAY_END_INCL}",
        thresh=thresh,
        height=1280,
        width=1400,
        use_gl=True,
        connectgaps=False,
        hist_note=hist_note,
        x_px=x_px,
        y_px=y_px,
        x_vol=x_vol,
        y_vol=y_vol,
        vol_note=None if has_feat else NO_HIST_VOL_NOTE,
        hist_long_binned=hist_long_binned,
        hist_short_binned=hist_short_binned,
        hist_long_pcts=pct_l,
        hist_short_pcts=pct_s,
        topn_intervals_ms=topn_intervals or (),
        topn_note=topn_note,
    )
    return fig, ov_plot, pct_l, pct_s


def build_window_figure(
    ov: pd.DataFrame,
    coin: str,
    *,
    start_ms: int,
    end_ms: int,
    thresh: float,
    gap_ms: int,
    features=None,
    topn_intervals=None,
    topn_note=None,
):
    """All ticks in ``ov`` (already L1-filtered). Hist/percentiles use that same set."""
    from research.gear2_spread_plots import CoinAllTickStats

    st = CoinAllTickStats()
    if len(ov):
        st.update(ov["event_local_ts_ms"], ov["spread_long"], ov["spread_short"])
    title = f"{coin}  все тики  {ms_to_iso_z(start_ms)} … {ms_to_iso_z(end_ms)} UTC"
    return coin_figure(
        ov,
        coin,
        max_ticks=None,
        thresh=float(thresh),
        gap_ms=int(gap_ms),
        features=features,
        stats=st if len(ov) else None,
        topn_intervals=topn_intervals,
        topn_note=topn_note,
        title=title,
        line_is_all_ticks=True,
    )


def _stats_block(
    ov: pd.DataFrame,
    ov_plot: pd.DataFrame,
    n_files: int,
    *,
    stats=None,
    topn_note: str | None = None,
    n_topn_intervals: int = 0,
) -> str:
    from research.gear2_spread_plots import (
        day_counts_from_counter,
        day_tick_counts,
        format_pcts,
        spread_percentiles,
    )

    if stats is not None:
        counts, missing = day_counts_from_counter(stats.day_counts, DAY_START, DAY_END_INCL)
        pct_l = stats.long.percentiles()
        pct_s = stats.short.percentiles()
        n_all = int(stats.n_ticks)
        sample_line = (
            f"n_все_тики={n_all}  на_линии={len(ov_plot)}  "
            f"строк_выборки_графика={len(ov)}  файлов_в_проходе={n_files}\n"
            f"{ALL_TICK_NOTE}\n"
        )
    else:
        counts, missing = day_tick_counts(ov["event_dt"], DAY_START, DAY_END_INCL)
        pct_l = spread_percentiles(ov["spread_long"])
        pct_s = spread_percentiles(ov["spread_short"])
        sample_line = (
            f"загружено тиков={len(ov)}  на_линии={len(ov_plot)}  файлов_в_проходе={n_files}\n"
            "квантили по загруженной выборке обзора (не все тики)\n"
        )
    miss_txt = ", ".join(missing) if missing else "нет (дыры внутри суток всё равно возможны)"
    counts_txt = counts.to_string() if len(counts) else "(пусто)"
    topn_line = (
        f"Полосы Топ-10 гира 1.5: {n_topn_intervals} интервалов"
        if n_topn_intervals
        else (topn_note or "Полос Топ-10 нет")
    )
    if topn_line and topn_line[0].islower():
        topn_line = topn_line[0].upper() + topn_line[1:]
    n_show = int(stats.n_ticks) if stats is not None else len(ov)
    axis_extra = ""
    if stats is not None:
        bits = []
        for hist, lab in ((stats.long, "spread_long"), (stats.short, "spread_short")):
            _x0, _x1, meta = hist.view_range()
            if meta.get("clipped"):
                bits.append(
                    f"<code>{lab}</code>: вне окна слева {meta['n_left']}, "
                    f"справа {meta['n_right']}"
                )
        if bits:
            axis_extra = " " + "; ".join(bits) + "."
    return (
        f'<div class="legend-topn"><span class="swatch" aria-hidden="true"></span>'
        f"<div><b>Золотые полосы</b> — интервалы, когда эта монета входила в "
        f"<b>Топ-{REGIME_TOP_N}</b> гира 1.5 "
        f"(геом. √(<code>r_vol</code>·<code>r_atr</code>), смесь α≈0,75, бар "
        f"<code>[t−5м, t)</code>, история OKX; тот же канон, что правило входа). "
        f"Пустые бары гира 1.5 (акции, <code>QNT</code>/<code>USDC</code>) — полос нет, "
        f"оценка не подставляется. {html.escape(topn_line)}</div></div>"
        f'<div class="stats-grid">'
        f'<div class="stat"><span class="k">Все тики (после фильтра стакана)</span>'
        f'<span class="v">{n_show}</span></div>'
        f'<div class="stat"><span class="k">Точек на линии</span>'
        f'<span class="v">{len(ov_plot)}</span></div>'
        f'<div class="stat"><span class="k"><code>spread_long</code> p50 / p95 / p99</span>'
        f'<span class="v">{html.escape(format_pcts(pct_l))}</span></div>'
        f'<div class="stat"><span class="k"><code>spread_short</code> p50 / p95 / p99</span>'
        f'<span class="v">{html.escape(format_pcts(pct_s))}</span></div>'
        f"</div>"
        f'<p class="meta">Квантили и гистограммы — по <b>всем</b> тикам окна '
        f"(не по прореженной линии; максимум {OVERVIEW_MAX_TICKS} точек на графике, "
        f"разрыв ~6 мин). Панели: спред + гистограммы · середина стакана Bybit "
        f"<code>(bid+ask)/2</code> · оценка 1.5 того же бара.</p>"
        f'<p class="note">Ось гистограмм показывает тело распределения '
        f"(квантили 0,5–99,5 по всем тикам). Хвосты обрезаны только на оси; "
        f"числа <code>p50</code>/<code>p95</code>/<code>p99</code> считаются по полной выборке."
        f"{axis_extra}</p>"
        f'<div class="days"><pre>{html.escape(sample_line)}'
        f"тики по суткам UTC (все тики):\n{html.escape(counts_txt)}\n"
        f"сутки без тиков этой монеты: {html.escape(miss_txt)}</pre></div>"
    )


def _nav_link(coin: str | None, *, direction: str, group_label: str) -> str:
    if coin is None:
        label = "← предыдущая" if direction == "prev" else "следующая →"
        end = "начало списка" if direction == "prev" else "конец списка"
        return (
            f'<span class="nav-disabled" title="{html.escape(end)} '
            f'({html.escape(group_label)})">{label} ({end})</span>'
        )
    href = f"{html.escape(coin)}.html"
    if direction == "prev":
        return (
            f'<a id="nav-prev" class="nav-btn prev" href="{href}">'
            f"← предыдущая {html.escape(coin)}</a>"
        )
    return (
        f'<a id="nav-next" class="nav-btn next" href="{href}">'
        f"следующая {html.escape(coin)} →</a>"
    )


def period_config_json(
    coin: str,
    *,
    calendar_start: str,
    calendar_end: str,
    period_start: str,
    period_end: str,
    max_points: int,
) -> str:
    return json.dumps(
        {
            "coin": str(coin).upper(),
            "calendarStart": calendar_start,
            "calendarEnd": calendar_end,
            "defaultStart": period_start,
            "defaultEnd": period_end,
            "maxPoints": int(max_points),
            "gapMs": OVERVIEW_GAP_BREAK_MS,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def period_panel_html(
    coin: str,
    *,
    calendar_start: str,
    calendar_end: str,
    period_start: str,
    period_end: str,
    max_points: int,
) -> str:
    cfg = period_config_json(
        coin,
        calendar_start=calendar_start,
        calendar_end=calendar_end,
        period_start=period_start,
        period_end=period_end,
        max_points=max_points,
    )
    start_val = html.escape(iso_to_datetime_local(period_start))
    end_val = html.escape(iso_to_datetime_local(period_end))
    cal_min = html.escape(iso_to_datetime_local(calendar_start))
    cal_max = html.escape(iso_to_datetime_local(calendar_end))
    return f"""<section id="period-panel" class="period-panel" data-period-ui="1">
<h2>Период — все тики</h2>
<p class="note">Верхний график — обзор календаря {html.escape(DAY_START)}…{html.escape(DAY_END_INCL)} UTC
(прореженная линия, разрыв ~6 мин). Ниже можно загрузить <b>каждый</b> тик выбранного
интервала. Поля времени — <b>UTC</b>. Если точек больше {int(max_points)}, сервер откажет
(без тихого прореживания).</p>
<p id="period-file-note" class="period-file-note">{html.escape(FILE_SERVER_NOTE)}</p>
<div class="period-row">
<div class="period-field">
<label for="period-start">Начало (UTC)</label>
<input id="period-start" type="datetime-local" step="1" value="{start_val}" min="{cal_min}" max="{cal_max}"/>
</div>
<div class="period-field">
<label for="period-end">Конец (UTC, исключая)</label>
<input id="period-end" type="datetime-local" step="1" value="{end_val}" min="{cal_min}" max="{cal_max}"/>
</div>
<div class="period-presets" role="group" aria-label="Готовые окна">
<button type="button" class="period-preset" data-ms="300000">5 мин</button>
<button type="button" class="period-preset" data-ms="900000">15 мин</button>
<button type="button" class="period-preset" data-ms="3600000">1 час</button>
</div>
<button type="button" id="period-load" disabled>Показать все тики</button>
</div>
<p id="period-status" class="meta"></p>
</section>
<script type="application/json" id="gear2-overview-config">{cfg}</script>"""


def period_plot_slot_html() -> str:
    return f"""<div class="plot-slot" id="period-plot-slot">
<h2>Все тики выбранного периода</h2>
<div id="period-plot" class="period-plot"></div>
</div>
<script src="{html.escape(PERIOD_JS_NAME)}"></script>
"""


def copy_period_js(out_dir: Path) -> Path:
    src = Path(__file__).resolve().parent / "gear2_overview_period.js"
    dest = Path(out_dir) / PERIOD_JS_NAME
    dest.write_bytes(src.read_bytes())
    return dest


def inject_period_into_page(
    text: str,
    coin: str,
    *,
    calendar_start: str,
    calendar_end: str,
    period_start: str,
    period_end: str,
    max_points: int,
) -> str:
    """Idempotent: skip if the period panel is already present."""
    if PERIOD_UI_MARKER in text:
        return text
    extra = (
        f'<style id="period-ui-css">{PERIOD_EXTRA_CSS}</style>\n'
    )
    if "</style>" in text:
        idx = text.find("</style>") + len("</style>")
        text = text[:idx] + "\n" + extra + text[idx:]
    else:
        text = extra + text
    h1 = f"<h1>{coin}</h1>"
    panel = period_panel_html(
        coin,
        calendar_start=calendar_start,
        calendar_end=calendar_end,
        period_start=period_start,
        period_end=period_end,
        max_points=max_points,
    )
    if h1 in text:
        text = text.replace(h1, h1 + "\n" + panel, 1)
    slot = period_plot_slot_html()
    needle = '</div>\n<script>\ndocument.addEventListener("keydown"'
    if needle in text:
        text = text.replace(needle, slot + needle, 1)
    else:
        alt = '<script>\ndocument.addEventListener("keydown"'
        if alt in text:
            text = text.replace(alt, slot + alt, 1)
        else:
            text = text.replace("</body>", slot + "</body>", 1)
    return text


def patch_overview_pages(
    out_dir: Path,
    *,
    calendar_start: str = OVERVIEW_START,
    calendar_end: str = OVERVIEW_END,
    period_start: str = "",
    period_end: str = "",
    max_points: int = MAX_ALL_TICK_POINTS,
    ticks: Path | None = None,
) -> int:
    """Add period UI to existing coin HTML without rescanning ticks."""
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        raise FileNotFoundError(out_dir)
    copy_period_js(out_dir)
    if not period_start or not period_end:
        from research.lean_ticks_io import parse_ts_ms

        ps, pe = infer_default_period(
            Path(ticks) if ticks is not None else DEFAULT_TICKS,
            parse_ts_ms(calendar_start),
            parse_ts_ms(calendar_end),
        )
        period_start = ms_to_iso_z(ps)
        period_end = ms_to_iso_z(pe)
    n = 0
    for path in sorted(out_dir.glob("*.html")):
        if path.name.lower() == "index.html":
            idx = path.read_text(encoding="utf-8")
            patched = _patch_index_server_note(idx)
            if patched != idx:
                path.write_text(patched, encoding="utf-8")
                n += 1
            continue
        coin = path.stem
        old = path.read_text(encoding="utf-8")
        new = inject_period_into_page(
            old,
            coin,
            calendar_start=calendar_start,
            calendar_end=calendar_end,
            period_start=period_start,
            period_end=period_end,
            max_points=int(max_points),
        )
        if new != old:
            path.write_text(new, encoding="utf-8")
            n += 1
    return n


INDEX_SERVER_NOTE = (
    f'<p class="note">Полный ряд тиков на странице монеты — только через локальный сервер '
    f"(не <code>file://</code>): <code>./venv/bin/python research/gear2_overview_server.py</code>, "
    f"затем <code>http://127.0.0.1:8765/0G.html</code>. "
    f"Окно шире ~{MAX_ALL_TICK_POINTS} точек сервер отклонит, без тихого прореживания.</p>"
)
INDEX_SERVER_MARKER = "gear2_overview_server.py"


def _patch_index_server_note(text: str) -> str:
    if INDEX_SERVER_MARKER in text:
        return text
    needle = '<div class="section-head" id="crypto">'
    if needle in text:
        return text.replace(needle, INDEX_SERVER_NOTE + "\n" + needle, 1)
    return text


def write_coin_html(
    path: Path,
    coin: str,
    klass: str,
    fig,
    stats_html: str,
    plotly_js: str,
    *,
    prev_coin: str | None,
    next_coin: str | None,
    group_label: str,
    period_start: str | None = None,
    period_end: str | None = None,
    calendar_start: str = OVERVIEW_START,
    calendar_end: str = OVERVIEW_END,
    max_points: int = MAX_ALL_TICK_POINTS,
) -> None:
    inner = plotly_plot(
        fig,
        output_type="div",
        include_plotlyjs=False,
        config={"responsive": True},
    )
    if not period_start or not period_end:
        from research.lean_ticks_io import parse_ts_ms

        ps, pe = default_period_bounds(
            parse_ts_ms(calendar_start), parse_ts_ms(calendar_end)
        )
        period_start = period_start or ms_to_iso_z(ps)
        period_end = period_end or ms_to_iso_z(pe)
    panel = period_panel_html(
        coin,
        calendar_start=calendar_start,
        calendar_end=calendar_end,
        period_start=period_start,
        period_end=period_end,
        max_points=int(max_points),
    )
    index_href = "index.html#crypto" if klass == KLASS_CRYPTO else "index.html#non-crypto"
    page = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<title>{html.escape(coin)} — обзор спреда</title>
<script src="{html.escape(plotly_js)}"></script>
<style>{PAGE_CSS}</style>
</head>
<body>
<div class="wrap">
<nav class="coin-nav" aria-label="Соседние монеты">
<a class="nav-btn nav-home" href="{index_href}">к списку</a>
{_nav_link(prev_coin, direction="prev", group_label=group_label)}
<span class="here">{html.escape(coin)}</span>
<span class="meta">{html.escape(klass)}</span>
{_nav_link(next_coin, direction="next", group_label=group_label)}
<span class="kbd">Клавиши ← и → — соседняя монета в том же списке. На краях переход выключен, без зацикливания.</span>
</nav>
<h1>{html.escape(coin)}</h1>
{panel}
{stats_html}
<div class="plot-slot">
{inner}
</div>
{period_plot_slot_html()}
</div>
<script>
document.addEventListener("keydown", function (e) {{
  if (e.target && (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT")) return;
  if (e.key === "ArrowLeft") {{
    var a = document.getElementById("nav-prev");
    if (a && a.href) location.href = a.href;
  }}
  if (e.key === "ArrowRight") {{
    var a = document.getElementById("nav-next");
    if (a && a.href) location.href = a.href;
  }}
}});
</script>
</body>
</html>
"""
    path.write_text(page, encoding="utf-8")


def _index_table(rows: list) -> str:
    body_rows = []
    for r in rows:
        body_rows.append(
            "<tr>"
            f"<td><a href=\"{html.escape(r['href'])}\">{html.escape(r['coin'])}</a></td>"
            f"<td>{html.escape(r['klass'])}</td>"
            f"<td>{r['n_all']}</td>"
            f"<td>{r['n_plot']}</td>"
            f"<td>{r['n_days']}</td>"
            f"<td>{html.escape(r['missing'])}</td>"
            f"<td>{html.escape(r['p_long'])}</td>"
            f"<td>{html.escape(r['p_short'])}</td>"
            f"<td>{r['n_topn']}</td>"
            "</tr>"
        )
    return f"""<table>
<thead>
<tr><th>монета</th><th>класс</th><th>все тики</th><th>на линии</th><th>суток&gt;0</th><th>сутки без тиков</th><th>long p50/p95/p99 (все тики)</th><th>short p50/p95/p99 (все тики)</th><th>интервалов Топ-10</th></tr>
</thead>
<tbody>
{''.join(body_rows)}
</tbody>
</table>"""


def write_index(path: Path, rows: list, *, n_files: int, elapsed_s: float, note: str) -> None:
    crypto_rows = [r for r in rows if r["klass"] == KLASS_CRYPTO]
    other_rows = [r for r in rows if r["klass"] != KLASS_CRYPTO]
    page = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<title>Обзор спредов по монетам — гир 2</title>
<style>{PAGE_CSS}</style>
</head>
<body>
<div class="wrap">
<h1>Обзор спредов по монетам</h1>
<p class="lede">Календарь <b>{DAY_START} … {DAY_END_INCL} UTC</b> (включительно).
Это покрытие тиков <code>lean</code> для обзора. Статус модели:
<b>гир 2 = закрыт (контур; 2.2 вне scope)</b>; следующий этап лестницы —
<b>гир 2.2</b> (stats/C/D), не закрытие этого HTML.</p>
<p class="note">Квантили <code>p50</code>/<code>p95</code>/<code>p99</code> и гистограммы —
по <b>всем тикам</b> после тех же фильтров стакана, что и график
(CDF по 20 тыс. корзинам, шаг 0,001 п.п. на [-10, 10]%; не побитово совпадает с
<code>np.percentile</code> по сырому вектору).
Ось гистограмм на странице монеты сужена к телу выборки (квантили 0,5–99,5);
это окно оси, не пересчёт квантилей.
Линия прорежена: сначала потолок на файл и монету, затем не больше
{OVERVIEW_MAX_TICKS} точек; разрыв дольше ~6 мин не склеивается — это дыра файла,
не «тишина рынка».
Честные дыры: утро 2026-08-05 (тики примерно с 11:50) и после 12:00 2026-08-19;
пропуски пятиминутных файлов видны как разрывы линии.</p>
<p class="note">На странице монеты три панели с общей осью времени: спред
(<code>spread_long</code> / <code>spread_short</code>) и гистограммы; середина стакана
Bybit <code>(bid+ask)/2</code>; каузальная оценка гира 1.5
√(<code>r_vol</code>·<code>r_atr</code>) закрытого бара <code>[t−5м, t)</code>
(история OKX). Золотые полосы — вхождение в <b>Топ-{REGIME_TOP_N}</b> по той же
закрытой метрике 1.5 (канон <code>USE_REGIME_TOPN</code> / <code>REGIME_TOP_N=10</code>).
Монеты без баров истории (акции, <code>QNT</code>, <code>USDC</code>) полос не получают.
Списки разделены через <code>research.is_crypto</code>; «назад / вперёд» на странице
монеты не перескакивает в другой список.</p>
<p class="meta">файлов={n_files} · монет={len(rows)}
(крипто={len(crypto_rows)}, не крипто={len(other_rows)}) · {elapsed_s:.1f} с</p>
<pre class="note">{html.escape(note)}</pre>
{INDEX_SERVER_NOTE}
<div class="section-head" id="crypto"><h2>Крипто</h2>
<span class="count-pill">{len(crypto_rows)}</span></div>
<p class="meta">Соседние страницы листают только этот список.</p>
{_index_table(crypto_rows)}
<div class="section-head" id="non-crypto"><h2>Не крипто</h2>
<span class="count-pill">{len(other_rows)}</span></div>
<p class="meta">Акции, фонды, металлы и прочее из того же окна. Соседние страницы
остаются в этом списке. Без баров гира 1.5 полос Топ-10 нет.</p>
{_index_table(other_rows)}
</div>
</body>
</html>
"""
    path.write_text(page, encoding="utf-8")


def scan_plot_and_stats(
    ticks: Path,
    start_ms: int,
    end_ms: int,
    *,
    workers: int,
    max_ticks: int,
    limit_coins: int,
):
    from research.gear2_spread_plots import update_stats_from_table
    from research.lean_ticks_io import (
        _even_take_per_coin,
        iter_lean_tables,
        list_lean_files_overlapping,
    )

    listed = list_lean_files_overlapping(ticks, start_ms, end_ms)
    per_coin_cap = max(2, int(max_ticks) // max(1, len(listed)))
    coins_filter = None
    print(
        f"overview HTML: {len(listed)} files, per_file_per_coin_cap={per_coin_cap} "
        f"(line only), full-tick stats streamed, workers={workers}",
        flush=True,
    )
    plot_tables = []
    used = []
    accs: dict = {}
    n_seen = 0
    for path, table in iter_lean_tables(
        ticks,
        start_ms,
        end_ms,
        coins=coins_filter,
        workers=int(workers),
        columns=OV_COLS,
        chunk=16,
    ):
        used.append(path)
        update_stats_from_table(accs, table)
        plot_tables.append(_even_take_per_coin(table, per_coin_cap))
        n_seen += 1
        del table
    if not plot_tables:
        raise ValueError("no rows in window after time filter")
    import pyarrow as pa

    raw = pa.concat_tables(plot_tables, promote_options="permissive").to_pandas()
    del plot_tables
    ov_all = prepare_overview_spreads(raw)
    del raw
    coins = sorted(ov_all["base_coin"].unique().tolist())
    if limit_coins and limit_coins > 0:
        coins = coins[: int(limit_coins)]
        keep = set(coins)
        ov_all = ov_all.loc[ov_all["base_coin"].isin(keep)].copy()
        accs = {k: v for k, v in accs.items() if k in keep}
        print(f"limit-coins={limit_coins} → {len(coins)}", flush=True)
    return ov_all, used, accs, coins, per_coin_cap


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticks", type=Path, default=DEFAULT_TICKS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--start", default=OVERVIEW_START)
    parser.add_argument("--end", default=OVERVIEW_END)
    parser.add_argument("--max-ticks", type=int, default=OVERVIEW_MAX_TICKS)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit-coins", type=int, default=0, help="0 = all coins")
    parser.add_argument("--thresh", type=float, default=0.5)
    parser.add_argument(
        "--patch-period-ui",
        action="store_true",
        help="inject period controls into existing HTML without rescanning ticks",
    )
    parser.add_argument(
        "--max-all-ticks",
        type=int,
        default=MAX_ALL_TICK_POINTS,
        help="safety cap for the all-ticks period view (refuse, do not downsample)",
    )
    args = parser.parse_args(argv)

    import sys

    sys.path.insert(0, str(REPO))
    from research.lean_ticks_io import parse_ts_ms

    if args.patch_period_ui:
        n = patch_overview_pages(
            args.out,
            calendar_start=args.start,
            calendar_end=args.end,
            max_points=int(args.max_all_ticks),
            ticks=args.ticks,
        )
        print(f"patched period UI in {n} html files → {args.out}", flush=True)
        return 0

    from research.gear2_spread_plots import day_counts_from_counter, format_pcts
    from research.gear2_regime_topn import (
        OKX_BAR_ROOT,
        build_topn_by_bar,
        canon_ma_params,
        load_coin_ma_features,
        load_crypto_feature_frames,
        topn_intervals_ms,
        topn_span_note,
    )
    from research.is_crypto import is_crypto

    start_ms = parse_ts_ms(args.start)
    end_ms = parse_ts_ms(args.end)
    t0 = time.perf_counter()
    ov_all, used, accs, coins, per_coin_cap = scan_plot_and_stats(
        args.ticks,
        start_ms,
        end_ms,
        workers=int(args.workers),
        max_ticks=int(args.max_ticks),
        limit_coins=int(args.limit_coins),
    )
    print(
        f"scan done: plot_rows={len(ov_all)} coins={len(coins)} "
        f"stat_coins={len(accs)} files={len(used)} in {time.perf_counter()-t0:.1f}s "
        f"per_file_per_coin_cap={per_coin_cap}",
        flush=True,
    )

    t_feat = time.perf_counter()
    if args.limit_coins and args.limit_coins > 0:
        frames = {}
        for c in coins:
            fr = load_coin_ma_features(
                c, start_ms=start_ms, end_ms=end_ms, root=OKX_BAR_ROOT
            )
            if fr is not None and not fr.empty:
                frames[str(c).upper()] = fr
        print(
            f"1.5 features (limit) coins={len(frames)} in {time.perf_counter()-t_feat:.1f}s",
            flush=True,
        )
    else:
        frames, feat_root, missing_hist = load_crypto_feature_frames(
            start_ms=start_ms,
            end_ms=end_ms,
            root=OKX_BAR_ROOT,
            params=canon_ma_params(),
            workers=int(args.workers),
        )
        frames = {str(k).upper(): v for k, v in frames.items()}
        print(
            f"1.5 features frames={len(frames)} missing_hist={len(missing_hist)} "
            f"root={feat_root} in {time.perf_counter()-t_feat:.1f}s",
            flush=True,
        )

    t_top = time.perf_counter()
    topn_by_bar = {}
    if frames:
        topn_by_bar = build_topn_by_bar(
            frames,
            start_ms=start_ms,
            end_ms=end_ms,
            top_n=REGIME_TOP_N,
            params=canon_ma_params(),
        )
    print(
        f"Top-{REGIME_TOP_N} map bars={len(topn_by_bar)} in {time.perf_counter()-t_top:.1f}s",
        flush=True,
    )

    crypto_list, other_list = group_sorted_coins(coins, is_crypto)
    group_of = {c: crypto_list for c in crypto_list}
    group_of.update({c: other_list for c in other_list})

    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    plotly_js = _copy_plotly_js(out_dir)
    copy_period_js(out_dir)
    ps, pe = default_period_bounds(start_ms, end_ms, used)
    period_start = ms_to_iso_z(ps)
    period_end = ms_to_iso_z(pe)

    rows = []
    n_files = len(used)
    for i, coin in enumerate(coins):
        ov = ov_all.loc[ov_all["base_coin"] == coin].reset_index(drop=True)
        if ov.empty:
            continue
        klass = KLASS_CRYPTO if is_crypto(coin) else KLASS_OTHER
        feats = frames.get(str(coin).upper())
        has_hist = feats is not None and len(feats) > 0
        spans = (
            topn_intervals_ms(
                coin, topn_by_bar, start_ms=start_ms, end_ms=end_ms
            )
            if has_hist
            else []
        )
        tnote = topn_span_note(coin, spans, has_hist=has_hist)
        fig, ov_plot, pct_l, pct_s = coin_figure(
            ov,
            coin,
            max_ticks=int(args.max_ticks),
            thresh=float(args.thresh),
            gap_ms=OVERVIEW_GAP_BREAK_MS,
            features=feats,
            stats=accs.get(str(coin).upper()),
            topn_intervals=spans,
            topn_note=tnote,
        )
        st = accs.get(str(coin).upper())
        stats_html = _stats_block(
            ov,
            ov_plot,
            n_files,
            stats=st,
            topn_note=tnote,
            n_topn_intervals=len(spans),
        )
        href = f"{coin}.html"
        group = group_of.get(coin, [coin])
        prev_c, next_c = nav_neighbors(group, coin)
        write_coin_html(
            out_dir / href,
            coin,
            klass,
            fig,
            stats_html,
            plotly_js,
            prev_coin=prev_c,
            next_coin=next_c,
            group_label=klass,
            period_start=period_start,
            period_end=period_end,
            calendar_start=args.start,
            calendar_end=args.end,
            max_points=int(args.max_all_ticks),
        )
        if st is not None:
            counts, missing = day_counts_from_counter(st.day_counts, DAY_START, DAY_END_INCL)
            n_all = int(st.n_ticks)
        else:
            from research.gear2_spread_plots import day_tick_counts

            counts, missing = day_tick_counts(ov["event_dt"], DAY_START, DAY_END_INCL)
            n_all = len(ov)
        rows.append(
            {
                "coin": coin,
                "klass": klass,
                "href": href,
                "n_all": n_all,
                "n_plot": len(ov_plot),
                "n_days": int(len(counts)),
                "missing": ", ".join(missing) if missing else "—",
                "p_long": format_pcts(pct_l),
                "p_short": format_pcts(pct_s),
                "n_topn": len(spans),
            }
        )
        if (i + 1) % 25 == 0 or i + 1 == len(coins):
            print(f"  wrote {i+1}/{len(coins)} html", flush=True)

    elapsed = time.perf_counter() - t0
    note = (
        f"START={args.start} END={args.end} (END исключён). "
        f"Честные дыры: утро 2026-08-05 (тики примерно с 11:50), "
        f"день 2026-08-19 (после 12:00 тиков нет). "
        f"QNT/USDC могут быть в тиках без баров гира 1.5 "
        f"(пустая панель оценки, без полос Топ-10). "
        f"Цена = середина стакана Bybit (bid+ask)/2. "
        f"Оценка = каузальный геом 1.5 бара [t−5м, t). "
        f"Топ-10 = закрытый геом 1.5 √(r_vol·r_atr), смесь α≈0,75, бар [t−5м, t), "
        f"история OKX, N={REGIME_TOP_N}."
    )
    write_index(out_dir / "index.html", rows, n_files=n_files, elapsed_s=elapsed, note=note)
    print(f"wrote {len(rows)} coins → {out_dir / 'index.html'} in {elapsed:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
