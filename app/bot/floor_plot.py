"""Offline canary plotter for live floor metrics journal.

Reads ``{BBOT_DATA_ROOT}/floor/**/metrics.jsonl`` and writes one HTML page
per coin (SMA-3, floor, tw_p05–tw_p95 corridor fill). Optional PNG when
matplotlib is installed.

Usage::

    PYTHONPATH=. python -m app.bot.floor_plot \\
      --data-root /data/bbot-gear2 \\
      --out /tmp/floor-canary-plots \\
      --coins BTC,ETH,SOL,XRP
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence


def discover_metrics_files(data_root: Path) -> list[Path]:
    root = Path(data_root) / "floor"
    if not root.is_dir():
        return []
    return sorted(root.glob("event_date=*/metrics.jsonl"))


def load_floor_rows(paths: Sequence[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                rows.append(rec)
    return rows


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def filter_series(
    rows: Iterable[dict[str, Any]],
    *,
    coin: str,
    side: str,
) -> list[dict[str, Any]]:
    coin_u = coin.upper()
    side_l = side.lower()
    out = [
        r
        for r in rows
        if str(r.get("base_coin", "")).upper() == coin_u
        and str(r.get("side", "")).lower() == side_l
    ]
    out.sort(key=lambda r: int(r.get("bar_end_ms") or 0))
    return out


def _iso_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _series_xy(
    rows: Sequence[dict[str, Any]], key: str
) -> tuple[list[str], list[Optional[float]]]:
    xs: list[str] = []
    ys: list[Optional[float]] = []
    for r in rows:
        bar_end = int(r.get("bar_end_ms") or 0)
        xs.append(_iso_utc(bar_end))
        ys.append(_finite(r.get(key)))
    return xs, ys


def render_coin_html(
    *,
    coin: str,
    long_rows: Sequence[dict[str, Any]],
    short_rows: Sequence[dict[str, Any]],
) -> str:
    """Self-contained Plotly HTML (CDN). Corridor intent matches gear22 floor panel."""

    def panel(side: str, rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
        x, sma3 = _series_xy(rows, "sma3")
        _, floor = _series_xy(rows, "floor_tf_select_a25")
        _, p05 = _series_xy(rows, "tw_p05")
        _, p95 = _series_xy(rows, "tw_p95")
        # Plotly fill-between: p95 then p05 with fill=tonexty.
        return {
            "side": side,
            "x": x,
            "sma3": sma3,
            "floor": floor,
            "p05": p05,
            "p95": p95,
        }

    payload = {
        "coin": coin.upper(),
        "long": panel("long", long_rows),
        "short": panel("short", short_rows),
    }
    payload_json = json.dumps(payload, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>floor canary — {coin.upper()}</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 16px; background: #f7f8fa; color: #222; }}
  h1 {{ font-size: 1.25rem; margin: 0 0 8px; }}
  p.meta {{ color: #555; font-size: 0.9rem; margin: 0 0 16px; }}
  .plot {{ width: 100%; height: 420px; margin-bottom: 24px; background: #fff;
           border: 1px solid #dde1e6; }}
</style>
</head>
<body>
<h1>Gear 2.2 live floor canary — {coin.upper()}</h1>
<p class="meta">SMA-3 · tf-select α25 floor · TW p05–p95 corridor (hold→next). From floor metrics journal only.</p>
<div id="long" class="plot"></div>
<div id="short" class="plot"></div>
<script>
const DATA = {payload_json};

function traces(panel) {{
  const x = panel.x;
  return [
    {{
      x: x, y: panel.p95, name: "tw_p95",
      line: {{color: "rgba(110,140,170,0.55)", width: 1}},
      hoverinfo: "x+y+name"
    }},
    {{
      x: x, y: panel.p05, name: "tw_p05",
      line: {{color: "rgba(110,140,170,0.55)", width: 1}},
      fill: "tonexty",
      fillcolor: "rgba(140,170,200,0.28)",
      hoverinfo: "x+y+name"
    }},
    {{
      x: x, y: panel.sma3, name: "SMA-3",
      line: {{color: "#ff7f0e", width: 1.7}},
      hoverinfo: "x+y+name"
    }},
    {{
      x: x, y: panel.floor, name: "tf-select α25",
      line: {{color: "#9467bd", width: 2.6}},
      hoverinfo: "x+y+name"
    }}
  ];
}}

function draw(divId, panel) {{
  Plotly.newPlot(divId, traces(panel), {{
    title: DATA.coin + " " + panel.side,
    margin: {{t: 40, r: 20, b: 40, l: 50}},
    legend: {{orientation: "h"}},
    xaxis: {{title: "bar_end (UTC)"}},
    yaxis: {{title: "spread %"}},
    hovermode: "x unified"
  }}, {{responsive: true, displayModeBar: true}});
}}

draw("long", DATA.long);
draw("short", DATA.short);
</script>
</body>
</html>
"""


def try_write_png(
    path: Path,
    *,
    coin: str,
    side: str,
    rows: Sequence[dict[str, Any]],
) -> Optional[Path]:
    """Write PNG if matplotlib is available; otherwise return None."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    if not rows:
        return None
    x_ms = [int(r["bar_end_ms"]) for r in rows]
    x = [datetime.fromtimestamp(m / 1000.0, tz=timezone.utc) for m in x_ms]
    sma3 = [_finite(r.get("sma3")) for r in rows]
    floor = [_finite(r.get("floor_tf_select_a25")) for r in rows]
    p05 = [_finite(r.get("tw_p05")) for r in rows]
    p95 = [_finite(r.get("tw_p95")) for r in rows]

    fig, ax = plt.subplots(figsize=(10, 4))
    if any(v is not None for v in p05) and any(v is not None for v in p95):
        y05 = [v if v is not None else math.nan for v in p05]
        y95 = [v if v is not None else math.nan for v in p95]
        ax.fill_between(x, y05, y95, color=(0.55, 0.67, 0.78, 0.35), label="tw_p05–p95")
    ax.plot(x, [v if v is not None else math.nan for v in sma3], color="#ff7f0e", lw=1.7, label="SMA-3")
    ax.plot(
        x,
        [v if v is not None else math.nan for v in floor],
        color="#9467bd",
        lw=2.4,
        label="tf-select α25",
    )
    ax.set_title(f"{coin.upper()} {side}")
    ax.set_ylabel("spread %")
    ax.legend(loc="best", fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return path


def write_coin_plots(
    *,
    coin: str,
    rows: Sequence[dict[str, Any]],
    out_dir: Path,
    write_png: bool = True,
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    long_rows = filter_series(rows, coin=coin, side="long")
    short_rows = filter_series(rows, coin=coin, side="short")
    html_path = out_dir / f"{coin.upper()}_floor_canary.html"
    html_path.write_text(
        render_coin_html(coin=coin, long_rows=long_rows, short_rows=short_rows),
        encoding="utf-8",
    )
    written: dict[str, Path] = {"html": html_path}
    if write_png:
        for side, side_rows in (("long", long_rows), ("short", short_rows)):
            png = try_write_png(
                out_dir / f"{coin.upper()}_{side}_floor_canary.png",
                coin=coin,
                side=side,
                rows=side_rows,
            )
            if png is not None:
                written[f"png_{side}"] = png
    return written


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plot live floor canary metrics (SMA-3 / floor / TW p05–p95)."
    )
    parser.add_argument(
        "--data-root",
        required=True,
        help="BBOT data root containing floor/event_date=*/metrics.jsonl",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Output directory for HTML (and PNG if matplotlib present)",
    )
    parser.add_argument(
        "--coins",
        default="",
        help="Comma-separated coins (default: all coins found in journal)",
    )
    parser.add_argument(
        "--no-png",
        action="store_true",
        help="Skip matplotlib PNG even if available",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    data_root = Path(args.data_root)
    out_dir = Path(args.out)
    files = discover_metrics_files(data_root)
    rows = load_floor_rows(files)
    if not rows:
        print(f"no floor metrics under {data_root / 'floor'}")
        return 1

    if args.coins.strip():
        coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    else:
        coins = sorted({str(r.get("base_coin", "")).upper() for r in rows if r.get("base_coin")})

    for coin in coins:
        paths = write_coin_plots(
            coin=coin,
            rows=rows,
            out_dir=out_dir,
            write_png=not args.no_png,
        )
        print(f"{coin}: " + ", ".join(f"{k}={v}" for k, v in paths.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
