"""Build Plotly HTML dashboards from durable lean tick parquet (analyze_screan.ipynb).

Derives spread/latency/freshness at read time from lean L1 + stamps.
Does not modify collector or storage runtime.

Usage (from repo root):
  ./venv/bin/python research/analyze_lean_ticks_html.py
  ./venv/bin/python research/analyze_lean_ticks_html.py --input-dir output/lean_ticks_recent
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional, Union

import duckdb
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.offline import plot

REPO = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = REPO / "output" / "lean_ticks_recent"
SAMPLE_NAME = "_plot_sample.parquet"


def parquet_glob(input_dir: Path) -> str:
    return str(input_dir / "spread_*.parquet")


def build_plot_sample(
    input_dir: Path,
    sample_path: Path,
    sample_seconds: int,
) -> dict:
    """Read lean ticks, derive model fields, keep 1 row per coin per time bucket."""
    glob = parquet_glob(input_dir).replace("\\", "/")
    sample_ms = int(sample_seconds) * 1000
    con = duckdb.connect()
    con.execute(
        f"""
        COPY (
            WITH derived AS (
                SELECT
                    event_local_ts_ms,
                    CAST(base_coin AS VARCHAR) AS base_coin,
                    CAST(trigger AS VARCHAR) AS trigger,
                    (bybit_bid_price - okx_ask_price) / bybit_bid_price * 100
                        AS spread_long,
                    (okx_bid_price - bybit_ask_price) / okx_bid_price * 100
                        AS spread_short,
                    (okx_local_recv_ts_ms - okx_ts_ms) AS okx_latency_ms,
                    (bybit_local_recv_ts_ms - bybit_ts_ms) AS bybit_latency_ms,
                    (calc_local_ts_ms - okx_local_recv_ts_ms) AS okx_freshness_ms,
                    (calc_local_ts_ms - bybit_local_recv_ts_ms) AS bybit_freshness_ms
                FROM read_parquet('{glob}')
                WHERE base_coin IS NOT NULL
                  AND bybit_bid_price IS NOT NULL AND bybit_bid_price > 0
                  AND okx_bid_price IS NOT NULL AND okx_bid_price > 0
                  AND okx_ask_price IS NOT NULL
                  AND bybit_ask_price IS NOT NULL
            )
            SELECT
                epoch_ms(MIN(event_local_ts_ms)) AS event_dt,
                MIN(event_local_ts_ms) AS event_local_ts_ms,
                base_coin,
                arg_min(trigger, event_local_ts_ms) AS trigger,
                arg_min(spread_long, event_local_ts_ms) AS spread_long,
                arg_min(spread_short, event_local_ts_ms) AS spread_short,
                arg_min(okx_latency_ms, event_local_ts_ms) AS okx_latency_ms,
                arg_min(bybit_latency_ms, event_local_ts_ms) AS bybit_latency_ms,
                arg_min(okx_freshness_ms, event_local_ts_ms) AS okx_freshness_ms,
                arg_min(bybit_freshness_ms, event_local_ts_ms) AS bybit_freshness_ms,
                GREATEST(
                    arg_min(okx_latency_ms, event_local_ts_ms),
                    arg_min(bybit_latency_ms, event_local_ts_ms)
                ) AS max_latency_ms,
                GREATEST(
                    arg_min(okx_freshness_ms, event_local_ts_ms),
                    arg_min(bybit_freshness_ms, event_local_ts_ms)
                ) AS max_freshness_ms
            FROM derived
            GROUP BY base_coin, (event_local_ts_ms // {sample_ms})
        ) TO '{sample_path.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    stats = con.execute(
        f"""
        SELECT
            COUNT(*) AS n_rows,
            COUNT(DISTINCT base_coin) AS n_coins,
            MIN(event_dt) AS tmin,
            MAX(event_dt) AS tmax
        FROM read_parquet('{sample_path.as_posix()}')
        """
    ).fetchone()
    return {
        "n_rows": int(stats[0]),
        "n_coins": int(stats[1]),
        "tmin": str(stats[2]),
        "tmax": str(stats[3]),
        "sample_seconds": sample_seconds,
    }


def load_plot_df(
    parquet_path: Path,
    start_dt: str,
    end_dt: str,
    spread_col: str = "spread_long",
    coin_offset: int = 0,
    top_n_coins: int = 30,
    max_rows_per_coin: int = 500,
) -> pd.DataFrame:
    con = duckdb.connect()
    glob = parquet_path.as_posix()
    query_sql = f"""
        WITH base_data AS (
            SELECT
                event_dt,
                base_coin,
                trigger,
                {spread_col} AS spread_value,
                okx_latency_ms,
                bybit_latency_ms,
                okx_freshness_ms,
                bybit_freshness_ms,
                max_latency_ms,
                max_freshness_ms
            FROM read_parquet('{glob}')
            WHERE event_dt >= TIMESTAMP '{start_dt}'
              AND event_dt < TIMESTAMP '{end_dt}'
              AND base_coin IS NOT NULL
              AND {spread_col} IS NOT NULL
        ),
        coin_counts AS (
            SELECT
                base_coin,
                COUNT(*) AS n_rows
            FROM base_data
            GROUP BY base_coin
        ),
        ranked_coins AS (
            SELECT
                base_coin,
                n_rows,
                ROW_NUMBER() OVER (ORDER BY n_rows DESC, base_coin ASC) AS coin_rank
            FROM coin_counts
        ),
        selected_coins AS (
            SELECT
                base_coin,
                coin_rank
            FROM ranked_coins
            WHERE coin_rank > {coin_offset}
              AND coin_rank <= {coin_offset + top_n_coins}
        )
        SELECT
            s.event_dt,
            s.base_coin,
            sc.coin_rank,
            s.trigger,
            s.spread_value,
            s.okx_latency_ms,
            s.bybit_latency_ms,
            s.okx_freshness_ms,
            s.bybit_freshness_ms,
            s.max_latency_ms,
            s.max_freshness_ms
        FROM base_data AS s
        INNER JOIN selected_coins AS sc
            ON s.base_coin = sc.base_coin
        ORDER BY sc.coin_rank, s.event_dt
    """
    df = con.execute(query_sql).df()
    if df.empty:
        return df

    df["event_dt"] = pd.to_datetime(df["event_dt"])
    df["coin_rank"] = df["coin_rank"].astype(int)
    df["legend_name"] = df["coin_rank"].astype(str) + ". " + df["base_coin"].astype(str)

    if max_rows_per_coin is not None:
        sampled_parts = []
        for _, sub in df.groupby("base_coin", sort=False):
            sub = sub.sort_values("event_dt").copy()
            if len(sub) > max_rows_per_coin:
                idx = np.linspace(0, len(sub) - 1, num=max_rows_per_coin, dtype=int)
                sub = sub.iloc[idx].copy()
            sampled_parts.append(sub)
        df = pd.concat(sampled_parts, ignore_index=True)

    return df.sort_values(["coin_rank", "event_dt"]).reset_index(drop=True)


def build_spread_2d_figure(df: pd.DataFrame) -> go.Figure:
    if df.empty:
        raise ValueError("plot_df is empty")

    fig = go.Figure()
    for _, sub in df.groupby("coin_rank", sort=True):
        sub = sub.sort_values("event_dt")
        legend_name = sub["legend_name"].iloc[0]
        fig.add_trace(
            go.Scatter(
                x=sub["event_dt"],
                y=sub["spread_value"],
                mode="lines+markers",
                name=legend_name,
                line=dict(width=1.7),
                marker=dict(size=3),
                customdata=np.stack(
                    [
                        sub["trigger"].astype(str),
                        sub["okx_latency_ms"].fillna(np.nan),
                        sub["bybit_latency_ms"].fillna(np.nan),
                        sub["okx_freshness_ms"].fillna(np.nan),
                        sub["bybit_freshness_ms"].fillna(np.nan),
                    ],
                    axis=-1,
                ),
                hovertemplate=(
                    "coin=%{text}<br>"
                    "time=%{x}<br>"
                    "spread=%{y:.6f}<br>"
                    "trigger=%{customdata[0]}<br>"
                    "okx_latency_ms=%{customdata[1]:.2f}<br>"
                    "bybit_latency_ms=%{customdata[2]:.2f}<br>"
                    "okx_freshness_ms=%{customdata[3]:.2f}<br>"
                    "bybit_freshness_ms=%{customdata[4]:.2f}<extra></extra>"
                ),
                text=[legend_name] * len(sub),
            )
        )

    fig.update_layout(
        title="2D spread",
        width=900,
        height=520,
        xaxis_title="Event time",
        yaxis_title="Spread",
        hovermode="closest",
        legend=dict(font=dict(size=10)),
        margin=dict(l=40, r=20, t=50, b=40),
    )
    return fig


def build_spread_3d_figure(df: pd.DataFrame, x_unit: str = "seconds") -> go.Figure:
    if df.empty:
        raise ValueError("plot_df is empty")

    df = df.copy().sort_values(["coin_rank", "event_dt"])
    t0 = df["event_dt"].min()
    df["seconds_from_start"] = (df["event_dt"] - t0).dt.total_seconds().astype(float)
    df["hours_from_start"] = df["seconds_from_start"] / 3600.0

    if x_unit == "seconds":
        x_col = "seconds_from_start"
        x_title = "Seconds from start"
    elif x_unit == "hours":
        x_col = "hours_from_start"
        x_title = "Hours from start"
    else:
        raise ValueError("x_unit must be 'seconds' or 'hours'")

    rank_min = df["coin_rank"].min()
    df["coin_y"] = df["coin_rank"] - rank_min
    y_tick_df = (
        df[["coin_rank", "coin_y", "legend_name"]]
        .drop_duplicates()
        .sort_values("coin_rank")
    )
    zmin = df["spread_value"].min()
    zmax = df["spread_value"].max()

    fig = go.Figure()
    for _, sub in df.groupby("coin_rank", sort=True):
        sub = sub.sort_values("event_dt")
        legend_name = sub["legend_name"].iloc[0]
        fig.add_trace(
            go.Scatter3d(
                x=sub[x_col].to_numpy(dtype=float),
                y=sub["coin_y"].to_numpy(dtype=float),
                z=sub["spread_value"].to_numpy(dtype=float),
                mode="lines+markers",
                name=legend_name,
                line=dict(width=4),
                marker=dict(size=2.5),
                opacity=0.9,
                customdata=np.stack(
                    [
                        sub["event_dt"].astype(str),
                        sub["trigger"].astype(str),
                        sub["okx_latency_ms"].fillna(np.nan).to_numpy(),
                        sub["bybit_latency_ms"].fillna(np.nan).to_numpy(),
                        sub["okx_freshness_ms"].fillna(np.nan).to_numpy(),
                        sub["bybit_freshness_ms"].fillna(np.nan).to_numpy(),
                    ],
                    axis=-1,
                ),
                hovertemplate=(
                    "coin=%{text}<br>"
                    "time=%{customdata[0]}<br>"
                    f"{x_title}=%{{x:.4f}}<br>"
                    "spread=%{z:.6f}<br>"
                    "trigger=%{customdata[1]}<br>"
                    "okx_latency_ms=%{customdata[2]:.2f}<br>"
                    "bybit_latency_ms=%{customdata[3]:.2f}<br>"
                    "okx_freshness_ms=%{customdata[4]:.2f}<br>"
                    "bybit_freshness_ms=%{customdata[5]:.2f}<extra></extra>"
                ),
                text=[legend_name] * len(sub),
            )
        )

    fig.update_layout(
        title="3D spread map",
        width=900,
        height=520,
        scene=dict(
            xaxis=dict(title=x_title),
            yaxis=dict(
                title="Coin rank",
                tickmode="array",
                tickvals=y_tick_df["coin_y"].tolist(),
                ticktext=y_tick_df["legend_name"].tolist(),
            ),
            zaxis=dict(title="Spread", range=[zmin, zmax]),
            aspectmode="manual",
            aspectratio=dict(x=2.2, y=1.3, z=1.0),
            camera=dict(eye=dict(x=1.7, y=1.55, z=1.05)),
        ),
        legend=dict(font=dict(size=10)),
        margin=dict(l=0, r=0, t=50, b=0),
    )
    return fig


def count_coins(parquet_path: Path, start_dt: str, end_dt: str, spread_col: str) -> int:
    con = duckdb.connect()
    glob = parquet_path.as_posix()
    n = con.execute(
        f"""
        SELECT COUNT(DISTINCT base_coin) AS n_coins
        FROM read_parquet('{glob}')
        WHERE event_dt >= TIMESTAMP '{start_dt}'
          AND event_dt < TIMESTAMP '{end_dt}'
          AND base_coin IS NOT NULL
          AND {spread_col} IS NOT NULL
        """
    ).fetchone()[0]
    return int(n)


def build_dashboard_html(
    parquet_path: Path,
    start_dt: str,
    end_dt: str,
    spread_col: str = "spread_long",
    block_size: int = 15,
    max_rows_per_coin: int = 400,
    x_unit: str = "hours",
    output_file: Union[Path, str] = "output/lean_ticks_recent/spread_dashboard_long.html",
    max_blocks: Optional[int] = None,
    write_block_files: bool = True,
) -> tuple[Path, list[dict]]:
    total_coins = count_coins(parquet_path, start_dt, end_dt, spread_col)
    if total_coins == 0:
        raise ValueError("No coins found in selected window")

    offsets = list(range(0, total_coins, block_size))
    if max_blocks is not None:
        offsets = offsets[:max_blocks]

    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    block_dir = output_path.parent / f"{output_path.stem}_blocks"
    if write_block_files:
        block_dir.mkdir(parents=True, exist_ok=True)

    sections = []
    block_meta: list[dict] = []
    plotly_js_included = False

    for block_idx, offset in enumerate(offsets, start=1):
        df_block = load_plot_df(
            parquet_path=parquet_path,
            start_dt=start_dt,
            end_dt=end_dt,
            spread_col=spread_col,
            coin_offset=offset,
            top_n_coins=block_size,
            max_rows_per_coin=max_rows_per_coin,
        )
        if df_block.empty:
            continue

        tmin = df_block["event_dt"].min()
        tmax = df_block["event_dt"].max()
        dur_sec = (tmax - tmin).total_seconds()
        coin_count = int(df_block["base_coin"].nunique())
        row_count = len(df_block)
        coins = (
            df_block.drop_duplicates("coin_rank")
            .sort_values("coin_rank")["base_coin"]
            .tolist()
        )

        fig3d = build_spread_3d_figure(df_block, x_unit=x_unit)
        fig2d = build_spread_2d_figure(df_block)

        div3d = plot(
            fig3d,
            include_plotlyjs=("cdn" if not plotly_js_included else False),
            output_type="div",
        )
        plotly_js_included = True
        div2d = plot(fig2d, include_plotlyjs=False, output_type="div")

        header = (
            f"Block {block_idx}: coins {offset + 1}–{offset + coin_count}"
        )
        meta_line = (
            f"rows={row_count} | coins={coin_count} | "
            f"time={tmin} → {tmax} | duration_sec={dur_sec:.2f}"
        )
        section_html = f"""
        <section class="block" id="block-{block_idx}">
            <div class="block-header">
                <h2>{header}</h2>
                <div class="meta">{meta_line}</div>
            </div>
            <div class="grid">
                <div class="panel left">
                    {div3d}
                </div>
                <div class="panel right">
                    {div2d}
                </div>
            </div>
        </section>
        """
        sections.append(section_html)

        block_rel = None
        if write_block_files:
            block_div3d = plot(fig3d, include_plotlyjs="cdn", output_type="div")
            block_div2d = plot(fig2d, include_plotlyjs=False, output_type="div")
            block_page = _wrap_html(
                title=f"{spread_col} {header}",
                subtitle=(
                    f"window={start_dt} → {end_dt} | spread_col={spread_col} | "
                    f"block_size={block_size} | max_rows_per_coin={max_rows_per_coin}"
                ),
                body=f"""
        <section class="block">
            <div class="block-header">
                <h2>{header}</h2>
                <div class="meta">{meta_line}</div>
            </div>
            <div class="grid">
                <div class="panel left">{block_div3d}</div>
                <div class="panel right">{block_div2d}</div>
            </div>
        </section>
                """,
            )
            block_name = f"block_{block_idx:02d}.html"
            (block_dir / block_name).write_text(block_page, encoding="utf-8")
            block_rel = f"{block_dir.name}/{block_name}"

        block_meta.append(
            {
                "block_idx": block_idx,
                "offset": offset,
                "coin_count": coin_count,
                "row_count": row_count,
                "coins": coins,
                "html": block_rel,
            }
        )
        print(
            f"built {spread_col} block={block_idx} offset={offset} "
            f"rows={row_count} coins={coin_count}"
        )

    html = _wrap_html(
        title=f"Spread dashboard ({spread_col})",
        subtitle=(
            f"window={start_dt} → {end_dt} | spread_col={spread_col} | "
            f"block_size={block_size} | max_rows_per_coin={max_rows_per_coin} | "
            f"coins={total_coins} | blocks={len(block_meta)}"
        ),
        body="".join(sections),
    )
    output_path.write_text(html, encoding="utf-8")
    print(f"saved html: {output_path.resolve()}")
    return output_path, block_meta


def _wrap_html(title: str, subtitle: str, body: str) -> str:
    return f"""
    <!doctype html>
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <title>{title}</title>
        <style>
            body {{
                margin: 0;
                font-family: Arial, sans-serif;
                background: #f5f5f5;
                color: #111;
            }}
            .page {{
                width: 100%;
                box-sizing: border-box;
                padding: 24px 20px 80px 20px;
            }}
            h1 {{
                margin: 0 0 8px 0;
                font-size: 28px;
            }}
            .sub {{
                margin-bottom: 24px;
                color: #555;
                font-size: 14px;
            }}
            .block {{
                background: white;
                border-radius: 12px;
                padding: 18px 18px 10px 18px;
                margin-bottom: 26px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.06);
            }}
            .block-header {{
                margin-bottom: 12px;
            }}
            .block-header h2 {{
                margin: 0 0 6px 0;
                font-size: 20px;
            }}
            .meta {{
                font-size: 13px;
                color: #666;
            }}
            .grid {{
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 18px;
                align-items: start;
            }}
            .panel {{
                min-width: 0;
                overflow: hidden;
            }}
            a {{ color: #0b57d0; }}
            @media (max-width: 1400px) {{
                .grid {{
                    grid-template-columns: 1fr;
                }}
            }}
        </style>
    </head>
    <body>
        <div class="page">
            <h1>{title}</h1>
            <div class="sub">{subtitle}</div>
            {body}
        </div>
    </body>
    </html>
    """


def write_index(
    out_dir: Path,
    start_dt: str,
    end_dt: str,
    n_files: int,
    n_coins: int,
    long_blocks: list[dict],
    short_blocks: list[dict],
    sample_stats: dict,
) -> Path:
    def block_list(blocks: list[dict], combined: str) -> str:
        items = []
        for b in blocks:
            href = b["html"] or f"{combined}#block-{b['block_idx']}"
            coins = ", ".join(b["coins"][:8])
            extra = "…" if len(b["coins"]) > 8 else ""
            items.append(
                f'<li><a href="{href}">Block {b["block_idx"]}</a> '
                f'({b["coin_count"]} coins, {b["row_count"]} plot rows): '
                f"{coins}{extra}</li>"
            )
        return "\n".join(items)

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Lean ticks spread dashboards</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 32px; color: #111; }}
    code {{ background: #f3f3f3; padding: 1px 4px; }}
  </style>
</head>
<body>
  <h1>Lean ticks spread dashboards</h1>
  <p>Window: <code>{start_dt}</code> → <code>{end_dt}</code></p>
  <p>Source files: {n_files} compacted lean parquets | coins: {n_coins} |
     sample: 1 row / {sample_stats.get("sample_seconds")}s / coin
     ({sample_stats.get("n_rows")} sampled rows)</p>
  <h2>Combined pages</h2>
  <ul>
    <li><a href="spread_dashboard_long.html">spread_dashboard_long.html</a> ({len(long_blocks)} blocks)</li>
    <li><a href="spread_dashboard_short.html">spread_dashboard_short.html</a> ({len(short_blocks)} blocks)</li>
  </ul>
  <p>Combined pages can be heavy. Prefer a single block page if the browser stalls.</p>
  <h2>Long blocks</h2>
  <ul>
    {block_list(long_blocks, "spread_dashboard_long.html")}
  </ul>
  <h2>Short blocks</h2>
  <ul>
    {block_list(short_blocks, "spread_dashboard_short.html")}
  </ul>
</body>
</html>
"""
    path = out_dir / "index.html"
    path.write_text(html, encoding="utf-8")
    return path


def write_readme(
    out_dir: Path,
    start_dt: str,
    end_dt: str,
    n_files: int,
    bytes_total: int,
    n_coins: int,
    n_blocks: int,
    sample_stats: dict,
    file_start: str = "",
    file_end: str = "",
) -> Path:
    gib = bytes_total / (1024**3)
    file_line = ""
    if file_start and file_end:
        file_line = (
            f"Filenames: `{file_start}` → `{file_end}`.\n"
        )
    text = f"""# Lean ticks (recent production backup)

{file_line}Event times: `{start_dt}` → `{end_dt}` UTC (`event_local_ts_ms`).

- Compacted files: {n_files} (`spread_YYYYMMDDTHHMMSSZ_YYYYMMDDTHHMMSSZ.parquet`)
- Downloaded size: {gib:.3f} GiB
- Coins in sample: {n_coins}
- Legend pages: {n_blocks} blocks × 15 coins (`block_size=15`)
- Plot sample: 1 point per {sample_stats.get("sample_seconds")}s per coin, then linspace to 400 rows/coin

## Open

```bash
open {out_dir / "index.html"}
```

Or open `spread_dashboard_long.html` / `spread_dashboard_short.html` in a browser.
If a combined page is slow, use `spread_dashboard_long_blocks/block_01.html` (and short equivalent).

Lean spreads/latency/freshness are computed at read time, not stored in these files.
"""
    path = out_dir / "README.md"
    path.write_text(text, encoding="utf-8")
    return path


def infer_window_from_files(files: list[Path]) -> tuple[str, str]:
    names = sorted(p.name for p in files)
    first = names[0]
    last = names[-1]
    # spread_YYYYMMDDTHHMMSSZ_YYYYMMDDTHHMMSSZ.parquet
    start_token = first.split("_")[1]
    end_token = last.split("_")[2].removesuffix(".parquet")

    def tok_to_sql(tok: str) -> str:
        # 20260811T000000Z -> 2026-08-11 00:00:00
        return (
            f"{tok[0:4]}-{tok[4:6]}-{tok[6:8]} "
            f"{tok[9:11]}:{tok[11:13]}:{tok[13:15]}"
        )

    return tok_to_sql(start_token), tok_to_sql(end_token)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--block-size", type=int, default=15)
    parser.add_argument("--max-rows-per-coin", type=int, default=400)
    parser.add_argument("--sample-seconds", type=int, default=60)
    parser.add_argument("--x-unit", choices=("seconds", "hours"), default="hours")
    parser.add_argument("--rebuild-sample", action="store_true")
    args = parser.parse_args()

    input_dir = args.input_dir.resolve()
    files = sorted(input_dir.glob("spread_*.parquet"))
    if not files:
        raise SystemExit(f"No spread_*.parquet files in {input_dir}")

    bytes_total = sum(p.stat().st_size for p in files)
    start_dt, end_dt = infer_window_from_files(files)
    # Inclusive end of last compacted window: filename end is exclusive in
    # collector naming (start_end), so keep SQL end as last file's end token.
    print(
        f"input={input_dir} files={len(files)} "
        f"bytes={bytes_total} ({bytes_total / 1024**3:.3f} GiB) "
        f"window={start_dt} → {end_dt}"
    )

    sample_path = input_dir / SAMPLE_NAME
    if args.rebuild_sample or not sample_path.exists():
        print(f"building plot sample ({args.sample_seconds}s buckets) → {sample_path}")
        sample_stats = build_plot_sample(input_dir, sample_path, args.sample_seconds)
    else:
        con = duckdb.connect()
        row = con.execute(
            f"""
            SELECT COUNT(*), COUNT(DISTINCT base_coin), MIN(event_dt), MAX(event_dt)
            FROM read_parquet('{sample_path.as_posix()}')
            """
        ).fetchone()
        sample_stats = {
            "n_rows": int(row[0]),
            "n_coins": int(row[1]),
            "tmin": str(row[2]),
            "tmax": str(row[3]),
            "sample_seconds": args.sample_seconds,
        }
    print("sample:", json.dumps(sample_stats, default=str))

    # Use observed sample timestamps so the last partial day is included.
    plot_start = sample_stats["tmin"]
    # DuckDB filter is end-exclusive; pad 1s past last tick.
    plot_end = str(pd.Timestamp(sample_stats["tmax"]) + pd.Timedelta(seconds=1))

    long_path, long_blocks = build_dashboard_html(
        parquet_path=sample_path,
        start_dt=plot_start,
        end_dt=plot_end,
        spread_col="spread_long",
        block_size=args.block_size,
        max_rows_per_coin=args.max_rows_per_coin,
        x_unit=args.x_unit,
        output_file=input_dir / "spread_dashboard_long.html",
    )
    short_path, short_blocks = build_dashboard_html(
        parquet_path=sample_path,
        start_dt=plot_start,
        end_dt=plot_end,
        spread_col="spread_short",
        block_size=args.block_size,
        max_rows_per_coin=args.max_rows_per_coin,
        x_unit=args.x_unit,
        output_file=input_dir / "spread_dashboard_short.html",
    )

    n_coins = sample_stats["n_coins"]
    n_blocks = len(long_blocks)
    write_index(
        input_dir,
        plot_start,
        plot_end,
        n_files=len(files),
        n_coins=n_coins,
        long_blocks=long_blocks,
        short_blocks=short_blocks,
        sample_stats=sample_stats,
    )
    write_readme(
        input_dir,
        plot_start,
        plot_end,
        n_files=len(files),
        bytes_total=bytes_total,
        n_coins=n_coins,
        n_blocks=n_blocks,
        sample_stats=sample_stats,
        file_start=start_dt,
        file_end=end_dt,
    )
    print(f"long={long_path}")
    print(f"short={short_path}")
    print(f"index={input_dir / 'index.html'}")
    print(f"coins={n_coins} blocks={n_blocks} block_size={args.block_size}")


if __name__ == "__main__":
    main()
