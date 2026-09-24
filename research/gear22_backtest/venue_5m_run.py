"""Rebuild UTC 5-minute Bybit/OKX equity charts from frozen_steps + L1 books.

Observation monitoring only. Does not retune ``policy.decide``, does not change
realized round totals, and is not a profitability or live claim.

Each bucket label is the UTC 5-minute start; the L1 mark uses the last second
of that bucket (xx:04:59, xx:09:59, …). Flat buckets use post-close (and
post-equalize) cash without a book lookup. Buckets inside an open leg use
cash-after-open plus unrealized PnL at close-side prices. A missing book is a
gap in the line.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from research.gear22_backtest.venue_coin_pnl import coin_pnl_section_html, load_closed_rounds
from research.gear22_backtest.venue_ledger import (
    MODES,
    Mode,
    align_bucket_5m_start,
    approx_vs_cash_usd_5m,
    book_keys_5m,
    bucket_5m_mark_ts,
    coverage_counts_5m,
    equity_series_5m,
    iter_bucket_5m_starts,
    load_last_books,
    steps_from_frozen_csv,
    write_balance_5m_html,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STEPS = REPO_ROOT / "research" / "data" / "venue_balance" / "frozen_steps.csv"
DEFAULT_TICKS = REPO_ROOT / "output" / "lean_ticks"
DEFAULT_BOOKS_1HZ = REPO_ROOT / "output" / "gear22_books_1hz"
DEFAULT_OUT = REPO_ROOT / "research" / "data" / "venue_balance" / "balance_5m.html"

FIRST_BUCKET = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)


def _divergence_note(points: list) -> str:
    diffs = [p[0] - p[1] for p in points if p is not None]
    if not diffs:
        return "нет общих точек"
    abs_d = [abs(d) for d in diffs]
    return (
        f"n={len(diffs)} mean|Δ|={sum(abs_d) / len(abs_d):.3f}$ "
        f"max|Δ|={max(abs_d):.3f}$ end Δ={diffs[-1]:.3f}$"
    )


def _last_event_ts(steps_by_mode: dict[Mode, list]) -> int:
    """Max close or open-at-end stamp across modes."""
    last = int(FIRST_BUCKET.timestamp())
    for steps in steps_by_mode.values():
        for step in steps:
            if step.ts_close is not None:
                last = max(last, int(step.ts_close))
            if step.ts_open is not None:
                last = max(last, int(step.ts_open))
    return last


def _sidecar_last_second(books_dir: Path) -> int | None:
    """Last second of the newest finished ``part-YYYYMMDDTHHMMSSZ`` hour."""
    parts = sorted(p for p in books_dir.glob("part-*.parquet") if p.is_file())
    if not parts:
        return None
    stamp = parts[-1].name[len("part-") : -len(".parquet")]
    start = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    return int(start.timestamp()) + 3599


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "UTC 5-minute equity monitoring from frozen_steps.csv + lean/1Hz L1 "
            "(observation only)."
        )
    )
    parser.add_argument("--steps", type=Path, default=DEFAULT_STEPS)
    parser.add_argument("--ticks", type=Path, default=DEFAULT_TICKS)
    parser.add_argument("--books-1hz", type=Path, default=DEFAULT_BOOKS_1HZ)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--window", default="all")
    args = parser.parse_args(argv)

    if not args.steps.is_file():
        print(f"steps not found: {args.steps}", file=sys.stderr)
        return 2
    if not args.ticks.is_dir():
        print(f"tick dir not found: {args.ticks}", file=sys.stderr)
        return 2

    steps_by_mode: dict[Mode, list] = {}
    for mode in MODES:
        steps_by_mode[mode] = steps_from_frozen_csv(
            args.steps, mode=mode, window=args.window
        )

    first_s = int(FIRST_BUCKET.timestamp())
    last_event = _last_event_ts(steps_by_mode)
    books_1hz = args.books_1hz if args.books_1hz.is_dir() else None
    if books_1hz is not None:
        side_end = _sidecar_last_second(books_1hz)
        if side_end is not None:
            last_event = max(last_event, side_end)
    last_s = align_bucket_5m_start(last_event)
    # Ensure the last sample mark covers the open-at-end / last-close stamp.
    if bucket_5m_mark_ts(last_s) < last_event:
        last_s += 300

    bucket_starts = iter_bucket_5m_starts(first_s, last_s)
    first_lbl = datetime.fromtimestamp(first_s, tz=timezone.utc).isoformat()
    last_lbl = datetime.fromtimestamp(last_s, tz=timezone.utc).isoformat()
    print(f"buckets={len(bucket_starts)} {first_lbl} .. {last_lbl}", flush=True)

    mode_series: list[tuple[str, list[int], list]] = []
    profit_series: list[tuple[str, list[int], list]] = []
    gap_counts: list[tuple[str, int, int, int]] = []
    end_notes: list[str] = []
    div_notes: list[str] = []

    jobs: list[tuple[str, list]] = [(mode, steps_by_mode[mode]) for mode in MODES]
    no_icx_steps = steps_from_frozen_csv(
        args.steps, mode="balance_equalize", window="no_icx"
    )
    if no_icx_steps:
        jobs.append(("balance_equalize, без ICX с начала", no_icx_steps))

    all_keys: set[tuple[str, int]] = set()
    for name, steps in jobs:
        keys = book_keys_5m(steps, bucket_starts)
        all_keys |= keys
        print(f"  {name}: steps={len(steps)} book_keys={len(keys)}", flush=True)

    print(
        f"loading L1 for {len(all_keys)} keys from {args.ticks}"
        + (f" + {books_1hz}" if books_1hz is not None else ""),
        flush=True,
    )
    books = (
        load_last_books(args.ticks, all_keys, books_1hz_dir=books_1hz) if all_keys else {}
    )
    print(f"  books found={len(books)} / {len(all_keys)}", flush=True)

    for name, steps in jobs:
        points = equity_series_5m(steps, books, bucket_starts)
        compared = approx_vs_cash_usd_5m(steps, books, bucket_starts, equity=points)
        n_marked, n_gap, n_flat = coverage_counts_5m(steps, bucket_starts, points)
        gap_counts.append((name, n_marked, n_gap, n_flat))
        mode_series.append((name, bucket_starts, points))
        profit_series.append((name, bucket_starts, compared))
        div_notes.append(f"{name}: {_divergence_note(compared)}")
        last = next((p for p in reversed(points) if p is not None), None)
        last_open = any(s.status == "open" for s in steps)
        if last is not None:
            tag = "cash+unrealized, нога ещё открыта" if last_open else "realized cash"
            end_notes.append(f"{name}: Bybit {last[0]:.4f}, OKX {last[1]:.4f} ({tag})")
        print(
            f"  {name}: marked={n_marked} gap={n_gap} flat={n_flat} "
            f"last={None if last is None else (round(last[0], 4), round(last[1], 4))}",
            flush=True,
        )
        print(f"  {div_notes[-1]}", flush=True)

    notes = [
        (
            "Каждая точка — equity на последней секунде UTC 5-минутного бакета "
            "(xx:04:59, xx:09:59, …). Метка оси X — начало того же бакета. "
            "Внутри позиции: cash после open-fee + unrealized по close-side "
            "(long: Bybit ask / OKX bid; short: Bybit bid / OKX ask). "
            "На бакете закрытия после close/equalize — post-close cash. "
            "Плоские бакеты — carry-forward realized cash без lookup книги. "
            "На первых трёх графиках последняя открытая нога ICX снята."
        ),
        "Конец ряда: " + "; ".join(end_notes) + ".",
        (
            "Нижний график — тот же frozen contour и метод balance_equalize, "
            "но ICX нет в пуле с первой секунды. После каждого закрытия кэш "
            "делится поровну, следующая нога = (Bybit + OKX) / 2."
        ),
        (
            "Приближение прибыли: N × (spread_open + spread противоположной стороны "
            "в этот момент − 0.15 п.п. вход − 0.15 п.п. выход) / 100. "
            "Истина: сумма equity двух площадок минус стартовые $200. "
            "Расхождение = приближение − истина, в долларах."
        ),
        "Расхождение: " + "; ".join(div_notes) + ".",
        (
            "Это monitoring series; realized round totals в frozen_steps не менялись. "
            "Не profitability, не live."
        ),
    ]
    coin_rounds = load_closed_rounds(args.steps, window=args.window, mode="balance_equalize")
    extra_html = coin_pnl_section_html(coin_rounds) if coin_rounds else ""
    write_balance_5m_html(
        args.out,
        mode_series=mode_series,
        notes=notes,
        first_s=first_s,
        last_s=last_s,
        gap_counts=gap_counts,
        profit_series=profit_series,
        extra_html=extra_html,
    )
    print(f"wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
