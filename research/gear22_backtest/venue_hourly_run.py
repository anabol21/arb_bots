"""Rebuild UTC-hourly Bybit/OKX equity charts from frozen_steps + L1 books.

Observation monitoring only. Does not retune ``policy.decide``, does not change
realized round totals, and is not a profitability or live claim.

Each hour label is the UTC hour start; the L1 mark uses the last second of that
hour (xx:59:59). Flat hours use post-close (and post-equalize) cash. Hours
inside an open leg use cash-after-open plus unrealized PnL at close-side
prices. A missing book is a gap in the line.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from research.gear22_backtest.venue_ledger import (
    MODES,
    Mode,
    hourly_book_keys,
    hourly_coverage_counts,
    hourly_equity_series,
    iter_hour_starts,
    load_last_books,
    steps_from_frozen_csv,
    write_hourly_html,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STEPS = REPO_ROOT / "research" / "data" / "venue_balance" / "frozen_steps.csv"
DEFAULT_TICKS = REPO_ROOT / "output" / "lean_ticks"
DEFAULT_BOOKS_1HZ = REPO_ROOT / "output" / "gear22_books_1hz"
DEFAULT_OUT = REPO_ROOT / "research" / "data" / "venue_balance" / "hourly.html"

FIRST_HOUR = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
LAST_HOUR = datetime(2026, 9, 13, 11, 0, tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "UTC-hourly equity monitoring from frozen_steps.csv + lean/1Hz L1 "
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

    first_s = int(FIRST_HOUR.timestamp())
    last_s = int(LAST_HOUR.timestamp())
    hour_starts = iter_hour_starts(first_s, last_s)
    print(
        f"hours={len(hour_starts)} {FIRST_HOUR.isoformat()} .. {LAST_HOUR.isoformat()}",
        flush=True,
    )

    books_1hz = args.books_1hz if args.books_1hz.is_dir() else None
    mode_series: list[tuple[str, list[int], list]] = []
    gap_counts: list[tuple[str, int, int, int]] = []
    end_notes: list[str] = []

    all_keys: set[tuple[str, int]] = set()
    steps_by_mode: dict[Mode, list] = {}
    for mode in MODES:
        steps = steps_from_frozen_csv(args.steps, mode=mode, window=args.window)
        steps_by_mode[mode] = steps
        keys = hourly_book_keys(steps, hour_starts)
        all_keys |= keys
        print(f"  {mode}: steps={len(steps)} book_keys={len(keys)}", flush=True)

    print(
        f"loading L1 for {len(all_keys)} keys from {args.ticks}"
        + (f" + {books_1hz}" if books_1hz is not None else ""),
        flush=True,
    )
    books = (
        load_last_books(args.ticks, all_keys, books_1hz_dir=books_1hz) if all_keys else {}
    )
    print(f"  books found={len(books)} / {len(all_keys)}", flush=True)

    for mode in MODES:
        steps = steps_by_mode[mode]
        points = hourly_equity_series(steps, books, hour_starts)
        n_marked, n_gap, n_flat = hourly_coverage_counts(steps, hour_starts, points)
        gap_counts.append((mode, n_marked, n_gap, n_flat))
        mode_series.append((mode, hour_starts, points))
        last = next((p for p in reversed(points) if p is not None), None)
        last_open = any(s.status == "open" for s in steps)
        if last is not None:
            tag = "cash+unrealized (open ICX)" if last_open else "realized cash"
            end_notes.append(f"{mode}: Bybit {last[0]:.4f}, OKX {last[1]:.4f} ({tag})")
        print(
            f"  {mode}: marked={n_marked} gap={n_gap} flat={n_flat} "
            f"last={None if last is None else (round(last[0], 4), round(last[1], 4))}",
            flush=True,
        )

    notes = [
        (
            "Каждая точка — equity на последней секунде UTC-часа (xx:59:59). "
            "Метка оси X — начало того же часа. "
            "Внутри позиции: cash после open-fee + unrealized по close-side "
            "(long: Bybit ask / OKX bid; short: Bybit bid / OKX ask). "
            "На часе закрытия после close/equalize — post-close cash. "
            "Открытая в конце ICX-нога не force-close."
        ),
        "Конец ряда: " + "; ".join(end_notes) + ".",
        (
            "Это monitoring series; realized round totals в frozen_steps не менялись. "
            "Не profitability, не live."
        ),
    ]
    write_hourly_html(
        args.out,
        mode_series=mode_series,
        notes=notes,
        first_hour_s=first_s,
        last_hour_s=last_s,
        gap_counts=gap_counts,
    )
    print(f"wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
