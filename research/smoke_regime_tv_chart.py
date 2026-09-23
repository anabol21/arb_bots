"""Smoke: load one coin month + build JupyterChart path (no notebook UI required).

Usage:
  ./venv/bin/python research/smoke_regime_tv_chart.py
  ./venv/bin/python research/smoke_regime_tv_chart.py --coin ETH
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
from lightweight_charts import JupyterChart

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from research.regime_metrics import (  # noqa: E402
    RegimeParams,
    build_regime_frame,
    regime_episodes,
    sanity_summary,
)

OKX_ROOT = REPO / "output" / "okx_bar5m_hist_regime"
BYBIT_ROOT = REPO / "output" / "bybit_bar5m_hist_regime"

REGIME_COLORS = {
    "okx": "rgba(255, 165, 0, 0.32)",
    "bybit": "rgba(70, 130, 180, 0.32)",
}


def load_hist_bars(root: Path, base_coin: str, start: str, end: str) -> pd.DataFrame:
    coin_dir = root / f"base_coin={base_coin}"
    days = sorted(
        p.name.split("=", 1)[1] for p in coin_dir.glob("event_date=*") if p.is_dir()
    )
    days = [d for d in days if start <= d < end]
    if not days:
        raise FileNotFoundError(f"no days for {base_coin} in [{start},{end}) under {root}")
    parts = [pd.read_parquet(coin_dir / f"event_date={d}" / "part.parquet") for d in days]
    df = pd.concat(parts, ignore_index=True)
    return (
        df.sort_values("bar_start_ts_ms", kind="mergesort")
        .drop_duplicates(subset=["bar_start_ts_ms"], keep="last")
        .reset_index(drop=True)
    )


def overlay_regime_on(chart: JupyterChart, frames: dict[str, pd.DataFrame]) -> dict[str, dict[str, int]]:
    """Shade regime_on bars via full-height histogram (vertical_span is broken in LWC 2.x)."""
    stats: dict[str, dict[str, int]] = {}
    for ex, fr in frames.items():
        name = f"regime:{ex}"
        color = REGIME_COLORS.get(ex, "rgba(252, 219, 3, 0.28)")
        hist = chart.create_histogram(
            name=name,
            color=color,
            price_line=False,
            price_label=False,
            scale_margin_top=0.0,
            scale_margin_bottom=0.0,
        )
        on = fr["regime_on"].fillna(False)
        n_eps = int(len(regime_episodes(fr["regime_on"], fr["bar_start_ts_ms"])))
        if not on.any():
            hist.set(
                pd.DataFrame(
                    {
                        "time": pd.Series(dtype="datetime64[ns]"),
                        name: pd.Series(dtype=float),
                    }
                )
            )
            stats[ex] = {"on_bars": 0, "episodes": 0}
            continue
        hist.set(
            pd.DataFrame(
                {
                    "time": fr.loc[on, "bar_dt"].dt.tz_convert("UTC").dt.tz_localize(None),
                    name: 1.0,
                }
            )
        )
        stats[ex] = {"on_bars": int(on.sum()), "episodes": n_eps}
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", default="BTC")
    ap.add_argument("--start", default="2026-07-08")
    ap.add_argument("--end", default="2026-08-08")
    args = ap.parse_args()

    params = RegimeParams()
    okx = load_hist_bars(OKX_ROOT, args.coin, args.start, args.end)
    bybit = load_hist_bars(BYBIT_ROOT, args.coin, args.start, args.end)
    okx_r = build_regime_frame(okx, params=params, ticks=None)
    bybit_r = build_regime_frame(bybit, params=params, ticks=None)
    okx_r["bar_dt"] = pd.to_datetime(okx_r["bar_start_ts_ms"], unit="ms", utc=True)
    bybit_r["bar_dt"] = pd.to_datetime(bybit_r["bar_start_ts_ms"], unit="ms", utc=True)

    print(
        f"{args.coin} OKX bars={len(okx_r)} days="
        f"{okx_r['bar_dt'].dt.strftime('%Y-%m-%d').nunique()} "
        f"sanity={sanity_summary(okx_r)}"
    )
    print(
        f"{args.coin} Bybit bars={len(bybit_r)} days="
        f"{bybit_r['bar_dt'].dt.strftime('%Y-%m-%d').nunique()} "
        f"sanity={sanity_summary(bybit_r)}"
    )

    ohlcv = pd.DataFrame(
        {
            "time": okx_r["bar_dt"].dt.tz_convert("UTC").dt.tz_localize(None),
            "open": okx_r["open"].astype(float),
            "high": okx_r["high"].astype(float),
            "low": okx_r["low"].astype(float),
            "close": okx_r["close"].astype(float),
            "volume": okx_r["volume"].astype(float),
        }
    )
    chart = JupyterChart(width=900, height=500)
    chart.volume_config(scale_margin_top=0.8)
    chart.set(ohlcv)

    frames = {"okx": okx_r, "bybit": bybit_r}
    overlay_stats = overlay_regime_on(chart, frames)
    n_spans = sum(v["episodes"] for v in overlay_stats.values())
    n_on_bars = sum(v["on_bars"] for v in overlay_stats.values())

    z_pane = chart.create_subchart(position="bottom", width=1.0, height=0.28, sync=True)
    overlay_regime_on(z_pane, frames)
    z_line = z_pane.create_line(name="z_smooth:okx", color="#ffcc66", width=2)
    z_line.set(
        pd.DataFrame(
            {
                "time": okx_r["bar_dt"].dt.tz_convert("UTC").dt.tz_localize(None),
                "z_smooth:okx": okx_r["z_vol_smooth"].astype(float),
            }
        ).dropna()
    )
    print(
        f"chart_path_ok spans={n_spans} on_bars={n_on_bars} "
        f"overlay_stats={overlay_stats} "
        f"package=lightweight-charts JupyterChart histogram_overlay"
    )
    if n_spans <= 0 or n_on_bars <= 0:
        raise SystemExit("smoke_fail: expected regime overlays for BTC window")
    print("smoke_ok")


if __name__ == "__main__":
    main()
