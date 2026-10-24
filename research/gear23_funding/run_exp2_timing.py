"""Run experiment 2 on the frozen no-ICX trade list.

Does not rescan the ridge and does not change decide().
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import pandas as pd

from research.gear23_funding.exp2_timing import (
    EXP2_SEED,
    TradeClock,
    assert_trade_invariants,
    manual_funding_pp,
    mc_summary,
    overlaps,
    sample_independent,
    sample_k1,
    schedule_funding,
    streams,
)
from research.gear23_funding.run_exp1_joined_ab import SETTLEMENTS, load_book

REPO = Path(__file__).resolve().parents[2]
TABLES = REPO / "research" / "gear23_funding" / "tables"
FIGURES = REPO / "research" / "gear23_funding" / "figures"
TRADES_IN = TABLES / "exp1_no_icx_frozen_trades.csv"
SUMMARY_IN = TABLES / "exp1_no_icx_summary.json"
M = 10_000
SAMPLE_REPLICATES = 5
STORED_SUM = -9.71868909


def load_trades() -> tuple[list[TradeClock], int, int]:
    frame = pd.read_csv(TRADES_IN)
    summary = json.loads(SUMMARY_IN.read_text())
    t0 = int(summary["first_ts"])
    t1 = int(summary["last_ts"])
    trades: list[TradeClock] = []
    for i, row in frame.iterrows():
        trades.append(
            TradeClock(
                trade_id=int(i),
                coin=str(row["coin"]),
                side=str(row["side"]),
                duration_s=int(row["ts_close"]) - int(row["ts_open"]),
                ts_open=int(row["ts_open"]),
                ts_close=int(row["ts_close"]),
                funding_pp=float(row["funding_pp"]),
            )
        )
        if int(row["hold_s"]) != trades[-1].duration_s:
            raise RuntimeError(f"hold_s disagrees with timestamps on row {i}")
        if str(row["missing"]) not in ("False", "false", "0"):
            raise RuntimeError(f"row {i} is missing funding in the source file")
    assert_trade_invariants(trades, t0, t1)
    return trades, t0, t1


def coverage(book, trades: list[TradeClock], t0: int, t1: int) -> list[dict]:
    """Flag a venue series that does not span the experiment window."""
    rows = []
    for coin in sorted({t.coin for t in trades}):
        for venue, table in (("bybit", book.bybit), ("okx", book.okx)):
            series = table[coin]
            gaps = np.diff(series.ts_s.astype(np.int64))
            rows.append(
                {
                    "coin": coin,
                    "venue": venue,
                    "first_ts": int(series.ts_s[0]),
                    "last_ts": int(series.ts_s[-1]),
                    "n": int(series.ts_s.shape[0]),
                    "max_gap_s": int(gaps.max()) if gaps.size else None,
                    "covers_window": bool(series.ts_s[0] <= t0 and series.ts_s[-1] >= t1),
                }
            )
    return rows


def reproduce(book, trades: list[TradeClock]) -> float:
    opens = np.array([t.ts_open for t in trades], dtype=np.int64)
    total, parts = schedule_funding(book, trades, opens)
    worst = 0.0
    worst_id = 0
    for trade, part in zip(trades, parts):
        err = abs(part - trade.funding_pp)
        if err > worst:
            worst = err
            worst_id = trade.trade_id
        manual = manual_funding_pp(book, trade.coin, trade.side, trade.ts_open, trade.ts_close)
        if abs(manual - part) > 1e-9:
            raise RuntimeError(
                f"manual sum disagrees on trade {trade.trade_id}: {manual} vs {part}"
            )
    print(
        f"reproduced funding {total:.8f} stored {sum(t.funding_pp for t in trades):.8f} "
        f"max_abs_trade {worst:.3e} at {worst_id}",
        flush=True,
    )
    stored = float(sum(t.funding_pp for t in trades))
    if abs(total - stored) > 1e-6:
        raise RuntimeError(f"recomputed {total} != stored {stored}")
    if abs(total - STORED_SUM) > 1e-6:
        raise RuntimeError(f"recomputed {total} != experiment-1 sum {STORED_SUM}")
    return total


def draw_mode(mode: str, trades, durations, t0, t1, rng, book) -> np.ndarray:
    samples = np.empty(M, dtype=np.float64)
    for i in range(M):
        if mode == "A":
            opens = sample_independent(durations, t0, t1, rng)
        elif mode == "B":
            opens = sample_k1(durations, t0, t1, rng)
            if overlaps(opens, durations):
                raise RuntimeError(f"mode B replicate {i} overlaps")
        else:
            raise ValueError(mode)
        if int(opens.min()) < t0 or int((opens + durations).max()) > t1:
            raise RuntimeError(f"mode {mode} replicate {i} leaves the window")
        samples[i], _ = schedule_funding(book, trades, opens)
        if i == 0 or (i + 1) % 2000 == 0:
            print(f"  {mode} {i + 1}/{M} last={samples[i]:.3f}", flush=True)
    return samples


def check_sample_schedule(mode: str, trades, durations, t0, t1, book, master_seed: int) -> None:
    """Replay replicate 0 from the published seed and hand-sum three holds."""
    rng = streams(master_seed)[0 if mode == "A" else 1]
    opens = sample_independent(durations, t0, t1, rng) if mode == "A" else sample_k1(durations, t0, t1, rng)
    _, parts = schedule_funding(book, trades, opens)
    for idx in (0, len(trades) // 2, len(trades) - 1):
        close_ts = int(opens[idx]) + trades[idx].duration_s
        manual = manual_funding_pp(
            book, trades[idx].coin, trades[idx].side, int(opens[idx]), close_ts
        )
        if abs(manual - parts[idx]) > 1e-9:
            raise RuntimeError(f"hand sum failed mode {mode} trade {idx}")
        included = []
        for venue in (book.bybit[trades[idx].coin], book.okx[trades[idx].coin]):
            for ts_s in venue.ts_s:
                if int(opens[idx]) < int(ts_s) <= close_ts:
                    included.append(int(ts_s))
        if any(not (int(opens[idx]) < ts <= close_ts) for ts in included):
            raise RuntimeError("included print outside the hold")
    print(f"hand-checked 3 holds in mode {mode} replicate 0", flush=True)


def write_svg(path: Path, samples: np.ndarray, actual: float, title: str) -> None:
    """One histogram with a vertical line at the observed funding sum."""
    lo = float(min(samples.min(), actual))
    hi = float(max(samples.max(), actual))
    pad = 0.05 * (hi - lo if hi > lo else 1.0)
    lo -= pad
    hi += pad
    n_bins = 40
    counts, edges = np.histogram(samples, bins=n_bins, range=(lo, hi))
    width, height = 720, 280
    left, right, top, bottom = 48, 16, 28, 36
    plot_w = width - left - right
    plot_h = height - top - bottom
    peak = int(counts.max())
    bars = []
    for i, count in enumerate(counts):
        if count == 0:
            continue
        x0 = left + (edges[i] - lo) / (hi - lo) * plot_w
        x1 = left + (edges[i + 1] - lo) / (hi - lo) * plot_w
        y1 = top + plot_h
        y0 = y1 - (count / peak) * plot_h
        bars.append(
            f'<rect x="{x0:.2f}" y="{y0:.2f}" width="{max(0.0, x1 - x0):.2f}" '
            f'height="{y1 - y0:.2f}" fill="#c4552a"/>'
        )
    x_act = left + (actual - lo) / (hi - lo) * plot_w
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img">
<title>{title}</title>
<rect width="100%" height="100%" fill="#fff"/>
<text x="{left}" y="18" font-family="sans-serif" font-size="14">{title}</text>
{"".join(bars)}
<line x1="{x_act:.2f}" y1="{top}" x2="{x_act:.2f}" y2="{top + plot_h}" stroke="#222" stroke-dasharray="4 3"/>
<text x="{left}" y="{height - 8}" font-family="sans-serif" font-size="12">funding, п.п.</text>
</svg>
'''
    path.write_text(svg)


def main() -> None:
    trades, t0, t1 = load_trades()
    durations = np.array([t.duration_s for t in trades], dtype=np.int64)
    print(
        f"trades {len(trades)} window {t0}..{t1} slack {(t1 - t0) - int(durations.sum())}",
        flush=True,
    )
    book = load_book(SETTLEMENTS, {t.coin for t in trades}, t0, t1)
    cover = coverage(book, trades, t0, t1)
    # A settlement series is a point process. The first print can sit after t0
    # without a missing record. A multi-day hole inside the loaded series cannot.
    holes = [row for row in cover if row["max_gap_s"] is None or row["max_gap_s"] > 3 * 86400]
    if holes:
        raise RuntimeError(f"funding series has a multi-day hole: {holes}")
    uncovered = [row for row in cover if not row["covers_window"]]
    actual = reproduce(book, trades)
    check_sample_schedule("A", trades, durations, t0, t1, book, EXP2_SEED)
    check_sample_schedule("B", trades, durations, t0, t1, book, EXP2_SEED)

    rng_a, rng_b = streams(EXP2_SEED)
    print("mode A", flush=True)
    samples_a = draw_mode("A", trades, durations, t0, t1, rng_a, book)
    print("mode B", flush=True)
    samples_b = draw_mode("B", trades, durations, t0, t1, rng_b, book)
    # A second pair of streams must repeat the first draw of each mode.
    again_a, again_b = streams(EXP2_SEED)
    first_a = schedule_funding(book, trades, sample_independent(durations, t0, t1, again_a))[0]
    first_b = schedule_funding(book, trades, sample_k1(durations, t0, t1, again_b))[0]
    if abs(first_a - float(samples_a[0])) > 1e-9 or abs(first_b - float(samples_b[0])) > 1e-9:
        raise RuntimeError("seed did not reproduce the first replicate")

    stats_a = mc_summary(samples_a, actual)
    stats_b = mc_summary(samples_b, actual)
    print("A", json.dumps(stats_a), flush=True)
    print("B", json.dumps(stats_b), flush=True)

    TABLES.mkdir(parents=True, exist_ok=True)
    FIGURES.mkdir(parents=True, exist_ok=True)
    trade_rows = []
    opens = np.array([t.ts_open for t in trades], dtype=np.int64)
    _, parts = schedule_funding(book, trades, opens)
    for trade, part in zip(trades, parts):
        trade_rows.append(
            {
                "trade_id": trade.trade_id,
                "coin": trade.coin,
                "side": trade.side,
                "duration_s": trade.duration_s,
                "ts_open": trade.ts_open,
                "ts_close": trade.ts_close,
                "funding_pp_stored": trade.funding_pp,
                "funding_pp_recomputed": part,
                "notional": "equal_leg_rate_pct",
            }
        )
    pd.DataFrame(trade_rows).to_csv(TABLES / "exp2_trades.csv", index=False)
    pd.DataFrame({"replicate": np.arange(M), "funding_pp": samples_a}).to_csv(
        TABLES / "exp2_mc_a.csv", index=False
    )
    pd.DataFrame({"replicate": np.arange(M), "funding_pp": samples_b}).to_csv(
        TABLES / "exp2_mc_b.csv", index=False
    )

    sample_rows = []
    snap_a, snap_b = streams(EXP2_SEED)
    for mode, rng, draw in (
        ("A", snap_a, sample_independent),
        ("B", snap_b, sample_k1),
    ):
        for replicate in range(SAMPLE_REPLICATES):
            placed = draw(durations, t0, t1, rng)
            _, piece = schedule_funding(book, trades, placed)
            for trade, open_ts, funding_pp in zip(trades, placed, piece):
                sample_rows.append(
                    {
                        "mode": mode,
                        "replicate": replicate,
                        "trade_id": trade.trade_id,
                        "coin": trade.coin,
                        "side": trade.side,
                        "duration_s": trade.duration_s,
                        "ts_open": int(open_ts),
                        "ts_close": int(open_ts) + trade.duration_s,
                        "funding_pp": funding_pp,
                    }
                )
    pd.DataFrame(sample_rows).to_csv(TABLES / "exp2_schedule_sample.csv", index=False)
    write_svg(
        FIGURES / "exp2_funding_a.svg",
        samples_a,
        actual,
        "A. Independent timing",
    )
    write_svg(
        FIGURES / "exp2_funding_b.svg",
        samples_b,
        actual,
        "B. Portfolio K=1",
    )
    summary = {
        "experiment": "exp2_funding_timing",
        "seed": EXP2_SEED,
        "m": M,
        "t0": t0,
        "t1": t1,
        "n_trades": len(trades),
        "slack_s": (t1 - t0) - int(durations.sum()),
        "actual_funding_pp": actual,
        "mode_A": {
            **stats_a,
            "distribution": "independent uniform open seconds; overlaps allowed",
            "rejected": 0,
        },
        "mode_B": {
            **stats_b,
            "distribution": (
                "uniform over integer-second K=1 schedules: "
                "uniform order times uniform gap composition"
            ),
            "rejected": 0,
            "draws": M,
        },
        "series_starting_after_t0": uncovered,
        "max_gap_s": max(row["max_gap_s"] for row in cover),
    }
    (TABLES / "exp2_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"wrote {TABLES / 'exp2_summary.json'}", flush=True)


if __name__ == "__main__":
    main()
