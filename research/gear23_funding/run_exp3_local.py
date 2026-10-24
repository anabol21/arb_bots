"""Run experiment 3 on the frozen no-ICX trade list.

Local independent shifts only. Does not rescan the ridge, does not rerun
experiment 2, and does not read October settlements.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from research.gear23_funding.exp2_timing import manual_funding_pp
from research.gear23_funding.exp3_local import (
    EXP3_SEED,
    RADII_H,
    event_key,
    event_set_constant,
    funding_for_opens,
    funding_matches_hold,
    history_bounds,
    local_summary,
    local_support,
    radius_streams,
    sample_local_opens,
)
from research.gear23_funding.run_exp1_joined_ab import SETTLEMENTS, load_book
from research.gear23_funding.run_exp2_timing import STORED_SUM, load_trades

REPO = Path(__file__).resolve().parents[2]
TABLES = REPO / "research" / "gear23_funding" / "tables"
FIGURES = REPO / "research" / "gear23_funding" / "figures"
M = 10_000
OCTOBER = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())
R_MAX = 72 * 3600
MAX_GAP_S = 3 * 86400


def load_history(trades):
    """Settlements covering every ±72h shift, stopping before October."""
    want_lo = min(trade.ts_open for trade in trades) - R_MAX
    want_hi = min(max(trade.ts_close for trade in trades) + R_MAX, OCTOBER - 1)
    # load_book scans one extra hour on each side.
    book = load_book(SETTLEMENTS, {trade.coin for trade in trades}, want_lo + 3600, want_hi - 3600)
    horizon = (want_lo, want_hi)
    for coin in {trade.coin for trade in trades}:
        for venue, table in (("bybit", book.bybit), ("okx", book.okx)):
            series = table.get(coin)
            if series is None or series.ts_s.size == 0:
                raise RuntimeError(f"missing {venue} series for {coin}")
            if int(series.ts_s[-1]) >= OCTOBER or int(series.ts_s[0]) < want_lo:
                raise RuntimeError(f"{coin} {venue} leaves the loaded horizon")
            gaps = np.diff(series.ts_s.astype(np.int64))
            if gaps.size and int(gaps.max()) > MAX_GAP_S:
                raise RuntimeError(f"{coin} {venue} gap {int(gaps.max())}s")
    return book, horizon


def trade_limits(book, trades, horizon):
    open_lo = np.empty(len(trades), dtype=np.int64)
    open_hi = np.empty(len(trades), dtype=np.int64)
    windows = {}
    for trade in trades:
        if trade.coin not in windows:
            windows[trade.coin] = history_bounds(book, trade.coin, horizon[0], horizon[1])
        window = windows[trade.coin]
        lo = window.open_lo
        hi = window.close_hi - trade.duration_s
        if not lo <= trade.ts_open <= hi:
            raise RuntimeError(
                f"trade {trade.trade_id} {trade.coin} open {trade.ts_open} "
                f"outside [{lo}, {hi}] close_hi {window.close_hi}"
            )
        open_lo[trade.trade_id] = lo
        open_hi[trade.trade_id] = hi
    return open_lo, open_hi


def reproduce(book, trades) -> float:
    total = 0.0
    worst = 0.0
    for trade in trades:
        opens = np.array([trade.ts_open], dtype=np.int64)
        value = funding_for_opens(book, trade.coin, trade.side, trade.duration_s, opens)
        funding_matches_hold(book, trade.coin, trade.side, trade.duration_s, opens, value)
        manual = manual_funding_pp(book, trade.coin, trade.side, trade.ts_open, trade.ts_close)
        if abs(float(value[0]) - manual) > 1e-9 or abs(manual - trade.funding_pp) > 1e-9:
            raise RuntimeError(f"trade {trade.trade_id} funding {value[0]} stored {trade.funding_pp}")
        err = abs(float(value[0]) - trade.funding_pp)
        worst = max(worst, err)
        total += float(value[0])
    if abs(total - STORED_SUM) > 1e-6:
        raise RuntimeError(f"recomputed {total} != {STORED_SUM}")
    print(f"reproduced funding {total:.8f} max_abs_trade {worst:.3e}", flush=True)
    return total


def draw_radius(book, trades, radius_s, open_lo, open_hi, rng):
    n = len(trades)
    ts_open = np.array([trade.ts_open for trade in trades], dtype=np.int64)
    placed = np.empty((M, n), dtype=np.int64)
    for replicate in range(M):
        placed[replicate] = sample_local_opens(ts_open, radius_s, open_lo, open_hi, rng)
    parts = np.empty((M, n), dtype=np.float64)
    changed = np.zeros((M, n), dtype=bool)
    for trade in trades:
        i = trade.trade_id
        opens = placed[:, i]
        parts[:, i] = funding_for_opens(book, trade.coin, trade.side, trade.duration_s, opens)
        key = event_key(book, trade.coin, trade.ts_open, trade.ts_close)
        bybit = book.bybit[trade.coin].ts_s
        okx = book.okx[trade.coin].ts_s
        close = opens + np.int64(trade.duration_s)
        changed[:, i] = (
            (np.searchsorted(bybit, opens, side="right") != key[0])
            | (np.searchsorted(bybit, close, side="right") != key[1])
            | (np.searchsorted(okx, opens, side="right") != key[2])
            | (np.searchsorted(okx, close, side="right") != key[3])
        )
    # The observed open is inside every local support, so a zero shift is allowed.
    if np.any(placed < open_lo) or np.any(placed > open_hi):
        raise RuntimeError("a local open left the coin history")
    for trade in trades:
        i = trade.trade_id
        if np.any(np.abs(placed[:, i] - trade.ts_open) > radius_s):
            raise RuntimeError(f"trade {i} shifted by more than R")
    return parts.sum(axis=1), parts, changed


def write_histograms(path: Path, by_radius: dict[int, np.ndarray], actual: float) -> dict:
    """Four histograms. Shared x when the clouds and the fact sit on one scale."""
    spans = {}
    for hours, samples in by_radius.items():
        lo, hi = np.quantile(samples, [0.001, 0.999])
        spans[hours] = (float(lo), float(hi), float(np.quantile(samples, 0.75) - np.quantile(samples, 0.25)))
    # One x-axis for all four panels. The clouds move toward the fact as R
    # shrinks, and the fact sits just left of the 1-hour sample.
    global_lo = min(min(float(samples.min()) for samples in by_radius.values()), actual)
    global_hi = max(max(float(samples.max()) for samples in by_radius.values()), actual)
    pad = 0.04 * (global_hi - global_lo if global_hi > global_lo else 1.0)
    domain = (global_lo - pad, global_hi + pad)
    shared = True
    width, height = 720, 220
    left, right, top, bottom = 52, 16, 28, 32
    plot_w = width - left - right
    plot_h = height - top - bottom
    panels = []
    for hours in RADII_H:
        samples = by_radius[hours]
        median = float(np.median(samples))
        q025, q975 = [float(v) for v in np.quantile(samples, [0.025, 0.975])]
        lo, hi = domain
        fact_here = True
        counts, edges = np.histogram(samples, bins=40, range=(lo, hi))
        peak = max(int(counts.max()), 1)
        bars = []
        for i, count in enumerate(counts):
            if count == 0:
                continue
            x0 = left + (edges[i] - lo) / (hi - lo) * plot_w
            x1 = left + (edges[i + 1] - lo) / (hi - lo) * plot_w
            y1 = top + plot_h
            y0 = y1 - (count / peak) * plot_h
            bars.append(
                f'<rect x="{x0:.2f}" y="{y0:.2f}" width="{max(0.4, x1 - x0 - 0.4):.2f}" '
                f'height="{y1 - y0:.2f}" fill="currentColor" opacity="0.82"/>'
            )
        def xpos(value: float) -> float:
            return left + (value - lo) / (hi - lo) * plot_w

        guides = [
            (median, "0.55", "2 3"),
            (q025, "0.35", "1 3"),
            (q975, "0.35", "1 3"),
        ]
        if fact_here:
            guides.insert(0, (actual, "0.95", "4 3"))
        lines = []
        for value, opacity, dash in guides:
            x = xpos(value)
            lines.append(
                f'<line x1="{x:.2f}" y1="{top}" x2="{x:.2f}" y2="{top + plot_h}" '
                f'stroke="currentColor" stroke-opacity="{opacity}" stroke-dasharray="{dash}"/>'
            )
        fact_note = "факт на графике" if fact_here else "факт −9,72 за пределами оси"
        title = f"R = {hours} ч. Медиана {median:.2f}. {fact_note}"
        panels.append(
            f'<g>'
            f'<text x="{left}" y="18" font-family="sans-serif" font-size="14" fill="currentColor">{title}</text>'
            f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" '
            f'stroke="currentColor" stroke-opacity="0.35"/>'
            f'{"".join(bars)}{"".join(lines)}'
            f"</g>"
        )
    stacked = height * len(RADII_H)
    inner = [
        f'<g transform="translate(0 {i * height})">{panel}</g>' for i, panel in enumerate(panels)
    ]
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {stacked}" role="img">'
        f'<title>Локальный funding при R = 72, 24, 6 и 1 час</title>'
        f'{"".join(inner)}</svg>'
    )
    path.write_text(svg)
    return {"shared_x": shared, "domain": domain, "spans": {str(k): v for k, v in spans.items()}}


def write_delta(path: Path, rows: list[dict], actual: float) -> None:
    """Δ(R) and the actual sum against each radius's 2.5–97.5% interval."""
    width, height = 720, 280
    left, top = 56, 28
    plot_w, plot_h = 300, 200
    ordered = sorted(rows, key=lambda row: row["radius_h"])
    deltas = [row["delta_actual_minus_median"] for row in ordered]
    lo = min(min(deltas), -0.05)
    hi = max(max(deltas), 0.05)
    pad = 0.08 * (hi - lo)

    def y_of(value: float) -> float:
        return top + (hi + pad - value) / (hi - lo + 2 * pad) * plot_h

    xs = [left + (i + 0.5) * plot_w / len(ordered) for i in range(len(ordered))]
    pts = " ".join(f"{x:.1f},{y_of(v):.1f}" for x, v in zip(xs, deltas))
    labels = "".join(
        f'<text x="{x:.1f}" y="{top + plot_h + 22}" text-anchor="middle" '
        f'font-family="sans-serif" font-size="12" fill="currentColor">{row["radius_h"]} ч</text>'
        for x, row in zip(xs, ordered)
    )
    circles = "".join(
        f'<circle cx="{x:.1f}" cy="{y_of(v):.1f}" r="4" fill="currentColor"/>'
        for x, v in zip(xs, deltas)
    )
    # Right panel: quantile interval and the constant actual.
    q_lo = min(row["q025"] for row in ordered)
    q_hi = max(row["q975"] for row in ordered)
    y_lo = min(q_lo, actual)
    y_hi = max(q_hi, actual)
    y_pad = 0.08 * (y_hi - y_lo if y_hi > y_lo else 1.0)
    right = 400

    def y2(value: float) -> float:
        return top + (y_hi + y_pad - value) / (y_hi - y_lo + 2 * y_pad) * plot_h

    rx = [right + (i + 0.5) * plot_w / len(ordered) for i in range(len(ordered))]
    bands = "".join(
        f'<line x1="{x:.1f}" y1="{y2(row["q025"]):.1f}" x2="{x:.1f}" y2="{y2(row["q975"]):.1f}" '
        f'stroke="currentColor" stroke-opacity="0.45" stroke-width="6"/>'
        f'<circle cx="{x:.1f}" cy="{y2(row["median"]):.1f}" r="3" fill="currentColor"/>'
        for x, row in zip(rx, ordered)
    )
    actual_y = y2(actual)
    actual_line = (
        f'<line x1="{right}" y1="{actual_y:.1f}" x2="{right + plot_w}" y2="{actual_y:.1f}" '
        f'stroke="currentColor" stroke-dasharray="4 3"/>'
    )
    right_labels = "".join(
        f'<text x="{x:.1f}" y="{top + plot_h + 22}" text-anchor="middle" '
        f'font-family="sans-serif" font-size="12" fill="currentColor">{row["radius_h"]} ч</text>'
        for x, row in zip(rx, ordered)
    )
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img">
<title>Локальное отклонение funding от радиуса сдвига</title>
<text x="{left}" y="16" font-family="sans-serif" font-size="13" fill="currentColor">Δ = факт − медиана</text>
<polyline points="{pts}" fill="none" stroke="currentColor"/>
{circles}
{labels}
<text x="{right}" y="16" font-family="sans-serif" font-size="13" fill="currentColor">факт и квантили 2,5–97,5%</text>
{bands}
{actual_line}
{right_labels}
</svg>
'''
    path.write_text(svg)


def main() -> None:
    trades, t0, t1 = load_trades()
    if len(trades) != 49:
        raise RuntimeError(f"expected 49 trades, got {len(trades)}")
    book, horizon = load_history(trades)
    open_lo, open_hi = trade_limits(book, trades, horizon)
    actual = reproduce(book, trades)
    print(f"horizon {horizon[0]}..{horizon[1]} october {OCTOBER}", flush=True)

    streams = radius_streams(EXP3_SEED)
    by_radius: dict[int, np.ndarray] = {}
    summaries = []
    trade_rows = []
    mc_frames = []
    for hours in RADII_H:
        radius_s = hours * 3600
        print(f"R={hours}h", flush=True)
        sums, parts, changed = draw_radius(
            book, trades, radius_s, open_lo, open_hi, streams[hours]
        )
        again = radius_streams(EXP3_SEED)[hours]
        ts_open = np.array([trade.ts_open for trade in trades], dtype=np.int64)
        first = sample_local_opens(ts_open, radius_s, open_lo, open_hi, again)
        for trade in trades:
            manual = manual_funding_pp(
                book,
                trade.coin,
                trade.side,
                int(first[trade.trade_id]),
                int(first[trade.trade_id]) + trade.duration_s,
            )
            vector = funding_for_opens(
                book,
                trade.coin,
                trade.side,
                trade.duration_s,
                first[trade.trade_id : trade.trade_id + 1],
            )
            if abs(float(vector[0]) - manual) > 1e-9:
                raise RuntimeError(f"R={hours} trade {trade.trade_id} vector != manual")
            if abs(float(vector[0]) - float(parts[0, trade.trade_id])) > 1e-9:
                raise RuntimeError(f"R={hours} seed did not reproduce trade {trade.trade_id}")
        summary = local_summary(sums, actual)
        summary["radius_h"] = hours
        summary["radius_s"] = radius_s
        summary["share_trades_event_changed"] = float(changed.mean())
        summary["share_schedules_event_unchanged"] = float(np.mean(~changed.any(axis=1)))
        n_invariant = 0
        n_truncated = 0
        for trade in trades:
            lo, hi = local_support(trade.ts_open, radius_s, int(open_lo[trade.trade_id]), int(open_hi[trade.trade_id]))
            invariant = event_set_constant(book, trade.coin, trade.duration_s, lo, hi)
            n_invariant += int(invariant)
            full = hi - lo == 2 * radius_s
            n_truncated += int(not full)
            column = parts[:, trade.trade_id]
            median = float(np.median(column))
            q05, q95 = [float(v) for v in np.quantile(column, [0.05, 0.95])]
            trade_rows.append(
                {
                    "trade_id": trade.trade_id,
                    "coin": trade.coin,
                    "side": trade.side,
                    "duration_s": trade.duration_s,
                    "ts_open": trade.ts_open,
                    "ts_close": trade.ts_close,
                    "funding_actual_pp": trade.funding_pp,
                    "radius_h": hours,
                    "support_lo": lo,
                    "support_hi": hi,
                    "support_n": hi - lo + 1,
                    "full_support": full,
                    "event_invariant": invariant,
                    "median_pp": median,
                    "mean_pp": float(column.mean()),
                    "q05_pp": q05,
                    "q95_pp": q95,
                    "delta_pp": trade.funding_pp - median,
                    "share_event_changed": float(changed[:, trade.trade_id].mean()),
                    "share_value_changed": float(np.mean(np.abs(column - trade.funding_pp) > 1e-9)),
                }
            )
            if invariant and changed[:, trade.trade_id].any():
                raise RuntimeError(f"trade {trade.trade_id} changed despite an invariant support")
        summary["n_event_invariant"] = n_invariant
        summary["n_truncated_support"] = n_truncated
        summaries.append(summary)
        by_radius[hours] = sums
        mc_frames.append(pd.DataFrame({"radius_h": hours, "replicate": np.arange(M), "funding_pp": sums}))
        print(
            f"  median {summary['median']:.4f} delta {summary['delta_actual_minus_median']:.4f} "
            f"share<=actual {summary['share_le_actual']:.4f} exact {summary['share_exact']:.4f} "
            f"event-changed {summary['share_trades_event_changed']:.4f} invariant {n_invariant}",
            flush=True,
        )

    TABLES.mkdir(parents=True, exist_ok=True)
    FIGURES.mkdir(parents=True, exist_ok=True)
    pd.concat(mc_frames, ignore_index=True).to_csv(TABLES / "exp3_mc.csv", index=False)
    pd.DataFrame(trade_rows).to_csv(TABLES / "exp3_trades.csv", index=False)
    figure_meta = write_histograms(FIGURES / "exp3_funding_hist.svg", by_radius, actual)
    write_delta(FIGURES / "exp3_delta.svg", summaries, actual)
    payload = {
        "experiment": "exp3_local_funding_timing",
        "seed": EXP3_SEED,
        "m": M,
        "t0": t0,
        "t1": t1,
        "horizon_lo": horizon[0],
        "horizon_hi": horizon[1],
        "october_excluded_from": OCTOBER,
        "n_trades": len(trades),
        "actual_funding_pp": actual,
        "distribution": (
            "independent uniform integer shifts inside each trade's "
            "[-R, +R] intersected with that coin's funding history; no clipping"
        ),
        "radii": summaries,
        "figures": figure_meta,
    }
    (TABLES / "exp3_summary.json").write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {TABLES / 'exp3_summary.json'}", flush=True)


if __name__ == "__main__":
    main()
