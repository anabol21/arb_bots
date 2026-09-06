"""Self-contained HTML chronometry page. No CDN. No invented ticks."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

_KIND_COLORS = {
    "signal": "#f5c542",
    "send": "#7aa2f7",
    "ack": "#9ece6a",
    "fill": "#f7768e",
}


def _esc(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _fmt_num(value: Any, *, digits: int = 6) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return _esc(value)


def _fmt_ms(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{int(value)}"
    except (TypeError, ValueError):
        return _esc(value)


def _fmt_wall(ms: Any) -> str:
    if ms is None:
        return "—"
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return _esc(ms)
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


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


def _svg_series(
    *,
    title: str,
    series_id: str,
    points: Sequence[tuple[int, float]],
    markers: Sequence[Mapping[str, Any]],
    width: int = 960,
    height: int = 240,
    y_unit: str = "",
) -> str:
    pad_l, pad_r, pad_t, pad_b = 64, 24, 28, 36
    inner_w = width - pad_l - pad_r
    inner_h = height - pad_t - pad_b
    if not points:
        return (
            f'<section class="chart" data-series="{_esc(series_id)}">'
            f"<h2>{_esc(title)}</h2>"
            '<p class="empty">No public L1 ticks in this window. '
            "Tape was not retained — not reconstructed.</p>"
            "</section>"
        )
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if x1 <= x0:
        x1 = x0 + 1
    if y1 <= y0:
        pad = abs(y0) * 0.001 if y0 else 0.001
        y0 -= pad
        y1 += pad
    else:
        span = y1 - y0
        y0 -= span * 0.08
        y1 += span * 0.08

    def _x(ms: int) -> float:
        return pad_l + (float(ms) - x0) / (x1 - x0) * inner_w

    def _y(val: float) -> float:
        return pad_t + (1.0 - (float(val) - y0) / (y1 - y0)) * inner_h

    poly = " ".join(f"{_x(x):.2f},{_y(y):.2f}" for x, y in points)
    marker_svg: list[str] = []
    for m in markers:
        wall = m.get("wall_ms")
        if wall is None:
            continue
        try:
            mx = _x(int(wall))
        except (TypeError, ValueError):
            continue
        kind = str(m.get("kind") or "")
        color = _KIND_COLORS.get(kind, "#c0caf5")
        label = kind
        if m.get("venue"):
            label = f"{kind} {m['venue']}"
        marker_svg.append(
            f'<line class="marker marker-{_esc(kind)}" x1="{mx:.2f}" y1="{pad_t}" '
            f'x2="{mx:.2f}" y2="{height - pad_b}" stroke="{color}" '
            'stroke-dasharray="4 3" stroke-width="1.2"/>'
            f'<text class="marker-label" x="{mx:.2f}" y="{pad_t - 6}" '
            f'fill="{color}" text-anchor="middle" font-size="10">{_esc(label)}</text>'
        )
        price = _finite(m.get("price"))
        if price is not None and y0 <= price <= y1:
            marker_svg.append(
                f'<circle class="marker-dot marker-{_esc(kind)}" cx="{mx:.2f}" '
                f'cy="{_y(price):.2f}" r="4" fill="{color}"/>'
            )

    y_ticks = []
    for i in range(5):
        frac = i / 4
        val = y1 - frac * (y1 - y0)
        yy = pad_t + frac * inner_h
        y_ticks.append(
            f'<line x1="{pad_l}" y1="{yy:.2f}" x2="{width - pad_r}" y2="{yy:.2f}" '
            'class="grid"/>'
            f'<text x="{pad_l - 8}" y="{yy + 3:.2f}" class="axis" '
            f'text-anchor="end">{_esc(_fmt_num(val, digits=5))}</text>'
        )
    x_labels = []
    for i in range(4):
        frac = i / 3
        ms = int(x0 + frac * (x1 - x0))
        xx = pad_l + frac * inner_w
        x_labels.append(
            f'<text x="{xx:.2f}" y="{height - 10}" class="axis" '
            f'text-anchor="middle">{_esc(_fmt_wall(ms)[-13:])}</text>'
        )
    return (
        f'<section class="chart" data-series="{_esc(series_id)}">'
        f"<h2>{_esc(title)}</h2>"
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
        f'aria-label="{_esc(title)}">'
        f'<rect x="0" y="0" width="{width}" height="{height}" class="plot-bg"/>'
        + "".join(y_ticks)
        + f'<polyline class="series" fill="none" stroke="#7dcfff" '
        f'stroke-width="1.6" points="{poly}"/>'
        + "".join(marker_svg)
        + "".join(x_labels)
        + (
            f'<text x="12" y="18" class="axis">{_esc(y_unit)}</text>'
            if y_unit
            else ""
        )
        + "</svg></section>"
    )


def _latency_rows(latency: Mapping[str, Any]) -> str:
    rows = [
        ("signal→send", "signal_to_send"),
        ("send→ack", "send_to_ack"),
        ("signal→fill", "signal_to_fill"),
        ("fill_delivery", "fill_delivery"),
    ]
    html = [
        "<table class='lat'><thead><tr>"
        "<th>interval</th><th>bybit ms</th><th>okx ms</th>"
        "</tr></thead><tbody>"
    ]
    for label, key in rows:
        rec = latency.get(key) or {}
        html.append(
            "<tr>"
            f"<td>{_esc(label)}</td>"
            f"<td>{_esc(_fmt_ms(rec.get('bybit')))}</td>"
            f"<td>{_esc(_fmt_ms(rec.get('okx')))}</td>"
            "</tr>"
        )
    html.append("</tbody></table>")
    return "".join(html)


def render_dashboard_html(artifact: Mapping[str, Any]) -> str:
    """Self-contained dashboard. Empty tape stays empty — never faked."""
    coin = artifact.get("base_coin") or ""
    intent = artifact.get("intent_id") or ""
    side = artifact.get("spread_side") or ""
    kind = artifact.get("spread_kind") or ""
    sell_venue = artifact.get("sell_venue") or ""
    buy_venue = artifact.get("buy_venue") or ""
    ticks = list(artifact.get("ticks") or [])
    markers = list(artifact.get("markers") or [])
    spread_key = "spread_long_pct" if kind == "long" else "spread_short_pct"

    sell_points: list[tuple[int, float]] = []
    buy_points: list[tuple[int, float]] = []
    spread_points: list[tuple[int, float]] = []
    for tick in ticks:
        wall = tick.get("wall_ms")
        if wall is None:
            continue
        try:
            wall_i = int(wall)
        except (TypeError, ValueError):
            continue
        venue = tick.get("venue")
        if venue == sell_venue:
            bid = _finite(tick.get("bid"))
            if bid is not None:
                sell_points.append((wall_i, bid))
        if venue == buy_venue:
            ask = _finite(tick.get("ask"))
            if ask is not None:
                buy_points.append((wall_i, ask))
        spr = _finite(tick.get(spread_key))
        if spr is not None:
            spread_points.append((wall_i, spr))

    sell_title = f"Sell {sell_venue} bid"
    buy_title = f"Buy {buy_venue} ask"
    if side == "open_long":
        sell_title = "Sell Bybit bid (open_long)"
        buy_title = "Buy OKX ask (open_long)"
    elif side == "open_short":
        sell_title = "Sell OKX bid (open_short)"
        buy_title = "Buy Bybit ask (open_short)"

    notes = artifact.get("notes") or []
    notes_html = "".join(f"<li>{_esc(n)}</li>" for n in notes)
    snap = artifact.get("signal_book") or {}
    fills = artifact.get("fill_prices") or {}
    signal_spread = artifact.get("signal_spread_pct")
    fill_spread = artifact.get("fill_spread_pct")
    delta = None
    if signal_spread is not None and fill_spread is not None:
        try:
            delta = float(fill_spread) - float(signal_spread)
        except (TypeError, ValueError):
            delta = None

    charts = (
        _svg_series(
            title=sell_title,
            series_id="sell_bid",
            points=sell_points,
            markers=markers,
            y_unit="price",
        )
        + _svg_series(
            title=buy_title,
            series_id="buy_ask",
            points=buy_points,
            markers=markers,
            y_unit="price",
        )
        + _svg_series(
            title=f"Spread {kind} %",
            series_id="spread",
            points=spread_points,
            markers=markers,
            y_unit="pct",
        )
    )

    marker_rows = []
    for m in markers:
        marker_rows.append(
            "<tr>"
            f"<td>{_esc(m.get('kind'))}</td>"
            f"<td>{_esc(m.get('venue') or '—')}</td>"
            f"<td>{_esc(_fmt_wall(m.get('wall_ms')))}</td>"
            f"<td>{_esc(_fmt_num(m.get('price')))}</td>"
            f"<td>{_esc(m.get('req_id') or '')}</td>"
            "</tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Chronometry {_esc(coin)} {_esc(intent)}</title>
<style>
:root {{ color-scheme: dark; }}
body {{
  margin: 0; padding: 24px 28px 48px;
  font-family: ui-sans-serif, system-ui, sans-serif;
  background: #1a1b26; color: #c0caf5;
}}
h1 {{ font-size: 1.35rem; margin: 0 0 8px; }}
h2 {{ font-size: 1rem; margin: 16px 0 8px; color: #bb9af7; }}
.meta, .notes {{ color: #a9b1d6; font-size: 0.92rem; }}
.compare {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px; margin: 16px 0;
}}
.card {{
  background: #24283b; border: 1px solid #414868; border-radius: 10px;
  padding: 12px 14px;
}}
.card strong {{ display: block; color: #7aa2f7; font-size: 0.8rem; }}
.card span {{ font-size: 1.25rem; }}
.chart {{ margin: 18px 0 8px; }}
svg {{ background: #1f2335; border-radius: 8px; }}
.plot-bg {{ fill: #1f2335; }}
.grid {{ stroke: #3b4261; stroke-width: 0.6; }}
.axis {{ fill: #565f89; font-size: 10px; font-family: ui-monospace, monospace; }}
.series {{ stroke-linejoin: round; stroke-linecap: round; }}
table {{ border-collapse: collapse; width: 100%; font-size: 0.9rem; }}
th, td {{ border-bottom: 1px solid #3b4261; padding: 6px 8px; text-align: left; }}
th {{ color: #7aa2f7; }}
.empty {{ color: #f7768e; }}
.legend span {{ margin-right: 14px; }}
.dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; }}
</style>
</head>
<body>
<h1>Contour B chronometry — {_esc(coin)} {_esc(side)}</h1>
<p class="meta">
intent <code>{_esc(intent)}</code> · phase {_esc(artifact.get("phase"))} ·
kind {_esc(kind)} · signal {_esc(_fmt_wall(artifact.get("signal_ts_ms")))} ·
ticks {_esc(artifact.get("tick_count"))}
</p>
<div class="legend">
  <span><i class="dot" style="background:#f5c542"></i>signal</span>
  <span><i class="dot" style="background:#7aa2f7"></i>send</span>
  <span><i class="dot" style="background:#9ece6a"></i>ack</span>
  <span><i class="dot" style="background:#f7768e"></i>fill</span>
</div>
<div class="compare">
  <div class="card"><strong>signal spread %</strong><span>{_esc(_fmt_num(signal_spread))}</span></div>
  <div class="card"><strong>fill spread %</strong><span>{_esc(_fmt_num(fill_spread))}</span></div>
  <div class="card"><strong>fill − signal %</strong><span>{_esc(_fmt_num(delta))}</span></div>
  <div class="card"><strong>signal Bybit bid / OKX ask</strong>
    <span>{_esc(_fmt_num(snap.get("bybit_bid")))} / {_esc(_fmt_num(snap.get("okx_ask")))}</span>
  </div>
  <div class="card"><strong>fill Bybit / OKX</strong>
    <span>{_esc(_fmt_num(fills.get("bybit")))} / {_esc(_fmt_num(fills.get("okx")))}</span>
  </div>
</div>
{charts}
<h2>Latency intervals (ms)</h2>
{_latency_rows(artifact.get("latency_ms") or {})}
<h2>Markers</h2>
<table><thead><tr><th>kind</th><th>venue</th><th>wall UTC</th><th>price</th><th>req_id</th></tr></thead>
<tbody>{"".join(marker_rows) or "<tr><td colspan='5'>none</td></tr>"}</tbody></table>
<h2>Notes</h2>
<ul class="notes">{notes_html or "<li>none</li>"}</ul>
<p class="meta">schema {_esc(artifact.get("schema_version"))}.
Self-contained page. Public L1 from the canary ring, not /data/live.</p>
</body>
</html>
"""
