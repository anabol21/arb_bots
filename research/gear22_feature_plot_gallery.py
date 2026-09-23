"""Static flip-through gallery for gear 2.2 theta / p50 / 5m venue charts.

One page = one (UTC day, coin): 2×2 spread panels, then Bybit 5m candles,
OKX 5m candles, and Bybit vs OKX 5m close.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Sequence

import matplotlib.pyplot as plt
import pandas as pd

GALLERY_DIR = Path("output/gear22_feature_plots")
BYBIT_BARS = Path("output/bybit_bar5m_hist_regime")
OKX_BARS = Path("output/okx_bar5m_hist_regime")
PLOT_COLS = [
    "event_date",
    "theta_1m_long",
    "theta_1m_short",
    "p50_1m_long",
    "p50_1m_short",
    "floor_long",
    "floor_short",
]
_BAR_WIDTH = pd.Timedelta(minutes=3.5)

_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>gear 2.2 · theta / p50 / 5m</title>
  <style>
    :root { color-scheme: light; }
    * { box-sizing: border-box; }
    body {
      margin: 0; font-family: ui-sans-serif, system-ui, sans-serif;
      background: #f4f5f7; color: #1b1f24;
    }
    header {
      position: sticky; top: 0; z-index: 2;
      display: flex; align-items: center; gap: 12px;
      padding: 10px 16px; background: #fff;
      border-bottom: 1px solid #d0d7de;
    }
    header button {
      border: 1px solid #d0d7de; background: #fff; border-radius: 8px;
      padding: 6px 12px; font-size: 16px; cursor: pointer;
    }
    header button:hover { background: #f6f8fa; }
    .meta { display: flex; align-items: center; gap: 8px; flex: 1; flex-wrap: wrap; }
    select { font-size: 14px; padding: 6px 8px; border-radius: 8px; border: 1px solid #d0d7de; }
    #pos { color: #656d76; font-size: 13px; }
    #frame {
      display: block; margin: 12px auto; width: min(1400px, 100%);
      height: auto; background: #fff;
      box-shadow: 0 1px 4px rgb(0 0 0 / 8%);
    }
    .hint { text-align: center; color: #656d76; font-size: 12px; padding: 0 16px 16px; }
    kbd {
      font: 12px ui-monospace, monospace; background: #fff;
      border: 1px solid #d0d7de; border-radius: 4px; padding: 0 4px;
    }
  </style>
</head>
<body>
  <header>
    <button type="button" id="prev" title="Предыдущая монета">◀</button>
    <div class="meta">
      <select id="date"></select>
      <select id="coin"></select>
      <span id="pos"></span>
    </div>
    <button type="button" id="next" title="Следующая монета">▶</button>
  </header>
  <img id="frame" alt="theta / p50 / 5m venue"/>
  <p class="hint">
    Сверху theta и p50+floor · снизу 5m Bybit, 5m OKX, close Bybit vs OKX.
    <kbd>←</kbd> <kbd>→</kbd> монета ·
    <kbd>Shift</kbd>+<kbd>←</kbd> <kbd>→</kbd> день ·
    <kbd>Home</kbd> / <kbd>End</kbd>
  </p>
  <script>
    const PAGES = __PAGES__;
    const dates = [...new Set(PAGES.map(p => p.date))];
    const coins = [...new Set(PAGES.map(p => p.coin))];
    const dateEl = document.getElementById("date");
    const coinEl = document.getElementById("coin");
    const frame = document.getElementById("frame");
    const pos = document.getElementById("pos");
    let i = 0;

    for (const d of dates) dateEl.add(new Option(d, d));
    for (const c of coins) coinEl.add(new Option(c, c));

    function findIndex(date, coin) {
      const exact = PAGES.findIndex(p => p.date === date && p.coin === coin);
      if (exact >= 0) return exact;
      return PAGES.findIndex(p => p.date === date);
    }
    function show(idx) {
      i = (idx + PAGES.length) % PAGES.length;
      const p = PAGES[i];
      frame.src = p.src;
      frame.alt = p.date + " " + p.coin;
      dateEl.value = p.date;
      coinEl.value = p.coin;
      pos.textContent = (i + 1) + " / " + PAGES.length;
      history.replaceState(null, "", "#" + p.date + "/" + p.coin);
    }
    function jumpDate(delta) {
      const di = dates.indexOf(PAGES[i].date);
      const nd = dates[(di + delta + dates.length) % dates.length];
      show(findIndex(nd, PAGES[i].coin));
    }
    document.getElementById("prev").onclick = () => show(i - 1);
    document.getElementById("next").onclick = () => show(i + 1);
    dateEl.onchange = () => show(findIndex(dateEl.value, coinEl.value));
    coinEl.onchange = () => show(findIndex(dateEl.value, coinEl.value));
    document.addEventListener("keydown", (e) => {
      if (e.key === "ArrowRight" && e.shiftKey) jumpDate(1);
      else if (e.key === "ArrowLeft" && e.shiftKey) jumpDate(-1);
      else if (e.key === "ArrowRight") show(i + 1);
      else if (e.key === "ArrowLeft") show(i - 1);
      else if (e.key === "Home") show(0);
      else if (e.key === "End") show(PAGES.length - 1);
      else return;
      e.preventDefault();
    });
    const hash = decodeURIComponent(location.hash.slice(1)).split("/");
    const start = hash.length === 2 ? findIndex(hash[0], hash[1]) : 0;
    show(start < 0 ? 0 : start);
  </script>
</body>
</html>
"""


def png_path(out_dir: Path, event_date: str, coin: str) -> Path:
    return out_dir / "img" / event_date / f"{coin}.png"


def load_day_bars(root: Path, coin: str, event_date: str) -> pd.DataFrame:
    path = root / f"base_coin={coin}" / f"event_date={event_date}" / "part.parquet"
    if not path.is_file():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    if df.empty:
        return df
    df = df.sort_values("bar_start_ts_ms").copy()
    dt = pd.to_datetime(df["bar_start_ts_ms"], unit="ms", utc=True)
    df["bar_dt"] = dt.dt.tz_convert(None)
    return df


def _naive_event_dt(part: pd.DataFrame) -> pd.Series:
    dt = pd.to_datetime(part["event_date"], utc=True, errors="coerce")
    if getattr(dt.dt, "tz", None) is not None:
        return dt.dt.tz_convert(None)
    return pd.to_datetime(part["event_date"])


def _plot_candles(ax, bars: pd.DataFrame, title: str) -> None:
    if bars.empty:
        ax.set_title(title + " (no 5m hist)")
        ax.text(0.5, 0.5, "no bars", ha="center", va="center", transform=ax.transAxes)
        return
    t = bars["bar_dt"]
    o = bars["open"].to_numpy()
    h = bars["high"].to_numpy()
    low = bars["low"].to_numpy()
    c = bars["close"].to_numpy()
    up = c >= o
    ax.vlines(t, low, h, color="0.45", lw=0.5, zorder=1)
    if up.any():
        ax.bar(
            t[up],
            (c - o)[up],
            bottom=o[up],
            width=_BAR_WIDTH,
            color="#2ca02c",
            align="center",
            zorder=2,
        )
    if (~up).any():
        ax.bar(
            t[~up],
            (c - o)[~up],
            bottom=o[~up],
            width=_BAR_WIDTH,
            color="#d62728",
            align="center",
            zorder=2,
        )
    ax.set_title(title)


def plot_coin_day(
    part: pd.DataFrame,
    coin: str,
    dest: Path,
    *,
    event_date: Optional[str] = None,
    dpi: int = 110,
    bybit_root: Path = BYBIT_BARS,
    okx_root: Path = OKX_BARS,
) -> Path:
    """Write a tall PNG: 2×2 spread, then Bybit/OKX 5m candles and dual close."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    day = event_date or dest.parent.name
    bybit = load_day_bars(bybit_root, coin, day)
    okx = load_day_bars(okx_root, coin, day)
    x = _naive_event_dt(part)
    # Keep venue panels on the same UTC window as the feature traces
    # (partial last day, e.g. 2026-09-13 ending ~11:45Z).
    if not x.empty:
        lo, hi = x.min(), x.max()
        if not bybit.empty:
            bybit = bybit[(bybit["bar_dt"] >= lo) & (bybit["bar_dt"] <= hi)]
        if not okx.empty:
            okx = okx[(okx["bar_dt"] >= lo) & (okx["bar_dt"] <= hi)]

    fig = plt.figure(figsize=(16, 20), layout="constrained")
    gs = fig.add_gridspec(5, 2, height_ratios=[1.0, 1.0, 1.25, 1.25, 1.15])
    ax_th_l = fig.add_subplot(gs[0, 0])
    ax_th_s = fig.add_subplot(gs[0, 1], sharex=ax_th_l)
    ax_p50_l = fig.add_subplot(gs[1, 0], sharex=ax_th_l)
    ax_p50_s = fig.add_subplot(gs[1, 1], sharex=ax_th_l)
    ax_bb = fig.add_subplot(gs[2, :])
    ax_ok = fig.add_subplot(gs[3, :])
    ax_mid = fig.add_subplot(gs[4, :])

    ax_th_l.plot(x, part["theta_1m_long"], lw=0.6)
    ax_th_s.plot(x, part["theta_1m_short"], lw=0.6)
    ax_p50_l.plot(x, part["p50_1m_long"], lw=0.6, label="p50")
    ax_p50_l.plot(x, part["floor_long"], lw=0.8, color="tab:orange", label="floor")
    ax_p50_s.plot(x, part["p50_1m_short"], lw=0.6, label="p50")
    ax_p50_s.plot(x, part["floor_short"], lw=0.8, color="tab:orange", label="floor")
    ax_th_l.set_title(f"{coin}  {day}  theta_1m_long")
    ax_th_s.set_title("theta_1m_short")
    ax_p50_l.set_title("p50_1m_long + floor")
    ax_p50_s.set_title("p50_1m_short + floor")
    ax_p50_l.legend(loc="upper right", fontsize=8)
    ax_p50_s.legend(loc="upper right", fontsize=8)
    for ax in (ax_th_l, ax_th_s, ax_p50_l, ax_p50_s):
        ax.axhline(0, color="grey", linestyle="--", linewidth=1.0)

    _plot_candles(ax_bb, bybit, "Bybit 5m OHLC")
    _plot_candles(ax_ok, okx, "OKX 5m OHLC")
    ax_bb.set_ylabel("price")
    ax_ok.set_ylabel("price")
    if not bybit.empty:
        ax_mid.plot(bybit["bar_dt"], bybit["close"], color="#2ca02c", lw=0.9, label="Bybit close")
    if not okx.empty:
        ax_mid.plot(okx["bar_dt"], okx["close"], color="#1f77b4", lw=0.9, label="OKX close")
    if bybit.empty and okx.empty:
        ax_mid.text(0.5, 0.5, "no 5m close", ha="center", va="center", transform=ax_mid.transAxes)
    ax_mid.set_title("close Bybit vs OKX (5m kline)")
    ax_mid.set_ylabel("price")
    if not bybit.empty or not okx.empty:
        ax_mid.legend(loc="upper right", fontsize=8)

    fig.savefig(dest, dpi=dpi)
    plt.close(fig)
    return dest


def write_index(out_dir: Path, *, coins: Optional[Sequence[str]] = None) -> Path:
    img_root = out_dir / "img"
    pages: list[dict[str, str]] = []
    if img_root.is_dir():
        dates = sorted(p.name for p in img_root.iterdir() if p.is_dir())
        coin_order = list(coins) if coins else []
        for event_date in dates:
            found = {p.stem: p for p in (img_root / event_date).glob("*.png")}
            ordered = [c for c in coin_order if c in found]
            ordered.extend(sorted(c for c in found if c not in ordered))
            for coin in ordered:
                pages.append(
                    {
                        "date": event_date,
                        "coin": coin,
                        "src": f"img/{event_date}/{coin}.png",
                    }
                )
    html = _HTML.replace("__PAGES__", json.dumps(pages, ensure_ascii=False))
    dest = out_dir / "index.html"
    dest.write_text(html, encoding="utf-8")
    return dest


def rebuild_index(out_dir: Path = GALLERY_DIR) -> Path:
    return write_index(out_dir)


if __name__ == "__main__":
    path = rebuild_index()
    print(path)
