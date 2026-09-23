"""Run the Bybit/OKX cash ledger on the frozen gear-2.2 trade list.

Observation accounting only. Uses ``sweep.store_from_hive`` + ``run_combo``
(the fast 1 Hz contour) on IS/OOS windows or on every hive ``event_date``.
Does not retune ``policy.decide``. Not profitability, not live, not gear 2.5.

Lean ticks are an optional price attachment so each policy round can be split
into Bybit cash and OKX cash. They are not the replay input. Missing local
L1 for a ``(coin, ts_s)`` is a hole (skipped, path continues); a book that
fails the spread match is ``price_mismatch`` (also skipped, path continues).
``potential_pp`` is never substituted as dollars.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from research.gear22_backtest.params_frozen import FROZEN, IS_DATES, OOS_DATES
from research.gear22_backtest.policy import PolicyParams
from research.gear22_backtest.sweep import RunResult, run_combo, store_from_hive
from research.gear22_backtest.venue_ledger import (
    MODES,
    Mode,
    cash_path,
    ledger_status,
    load_last_books,
    one_step_neighbors,
    open_fill_exit_pp,
    price_run,
    run_ledger,
    step_row,
    summary_row,
    tick_coverage,
    timestamp_keys_for_run,
    write_balance_html,
    write_steps_csv,
    write_summary_csv,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HIVE = REPO_ROOT / "output" / "gear22_backtest_features_by_date"
DEFAULT_TICKS = REPO_ROOT / "output" / "lean_ticks"
DEFAULT_BOOKS_1HZ = REPO_ROOT / "output" / "gear22_books_1hz"
DEFAULT_OUT = REPO_ROOT / "research" / "data" / "venue_balance"


def list_hive_event_dates(hive: Path, *, part_name: str = "part-000.parquet") -> list[str]:
    """Sorted UTC ``event_date`` partition names that contain ``part_name``."""
    dates: list[str] = []
    for path in sorted(hive.glob(f"event_date=*/{part_name}")):
        if not path.is_file():
            continue
        parent = path.parent.name
        if not parent.startswith("event_date="):
            continue
        dates.append(parent.split("=", 1)[1])
    return dates


def _ledger_for_run(
    run: RunResult,
    mode: Mode,
    books: dict,
    *,
    open_fill_pp: Optional[float],
    open_exit_pp: Optional[float],
):
    rounds, open_leg = price_run(
        run,
        books,
        open_fill_pp=open_fill_pp,
        open_exit_pp=open_exit_pp,
    )
    return run_ledger(rounds, mode, open_leg=open_leg)


def run_window(
    *,
    hive: Path,
    tick_dir: Path,
    dates: Sequence[str],
    window: str,
    neighbors: bool,
    books_1hz_dir: Optional[Path] = None,
) -> tuple[list[dict], list[dict], list[tuple[str, list[tuple[float, float]]]]]:
    """Load the 1 Hz feature hive, run combos, attach L1 where present."""
    print(f"loading hive window={window} dates={dates[0]}..{dates[-1]} n={len(dates)}", flush=True)
    store = store_from_hive(hive, dates=dates)
    print(f"  rows={store.n_rows} coins={len(store.coins)} span_s={store.span_s}", flush=True)

    labelled = one_step_neighbors(FROZEN) if neighbors else [("frozen", FROZEN)]
    runs: list[tuple[str, PolicyParams, RunResult, Optional[float], Optional[float]]] = []
    all_keys: set[tuple[str, int]] = set()
    for label, params in labelled:
        print(f"  run_combo {label}", flush=True)
        result = run_combo(store, params)
        fill_pp, exit_pp = open_fill_exit_pp(store, result)
        runs.append((label, params, result, fill_pp, exit_pp))
        keys = timestamp_keys_for_run(result)
        all_keys |= keys
        print(
            f"    closed={result.n_closed} open_end={0 if result.open_coin is None else 1} "
            f"keys={len(keys)} sum_pp={result.sum_closed_pp:.4f}",
            flush=True,
        )

    sidecar = books_1hz_dir if books_1hz_dir is not None and books_1hz_dir.is_dir() else None
    print(
        f"loading lean books for {len(all_keys)} keys from {tick_dir}"
        + (f" + 1Hz sidecar {sidecar}" if sidecar is not None else ""),
        flush=True,
    )
    books = (
        load_last_books(tick_dir, all_keys, books_1hz_dir=sidecar) if all_keys else {}
    )
    print(f"  books found={len(books)} / {len(all_keys)}", flush=True)

    step_rows: list[dict] = []
    summary_rows: list[dict] = []
    charts: list[tuple[str, list[tuple[float, float]]]] = []

    for label, params, result, fill_pp, exit_pp in runs:
        for mode in MODES:
            ledger = _ledger_for_run(
                result, mode, books, open_fill_pp=fill_pp, open_exit_pp=exit_pp
            )
            status = ledger_status(ledger)
            summary_rows.append(
                summary_row(
                    window=window,
                    label=label,
                    params=params,
                    mode=mode,
                    status=status,
                    result=ledger,
                    n_policy_closed=result.n_closed,
                    policy_sum_potential_pp=result.sum_closed_pp,
                )
            )
            if label == "frozen":
                for step in ledger.steps:
                    step_rows.append(step_row(window, label, mode, step))
                charts.append((f"{window} frozen {mode}", cash_path(ledger)))
            print(
                f"  {label} {mode}: status={status} "
                f"final=({ledger.cash_bybit:.4f},{ledger.cash_okx:.4f}) "
                f"min=({ledger.min_bybit:.4f},{ledger.min_okx:.4f}) "
                f"applied={ledger.n_applied} hole={ledger.n_skipped_hole} "
                f"mismatch={ledger.n_price_mismatch} "
                f"not_applied={ledger.n_not_applied} policy_closed={result.n_closed}",
                flush=True,
            )
    return step_rows, summary_rows, charts


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bybit/OKX perp cash ledger on frozen gear-2.2 1 Hz trades "
            "(observation accounting, not a PnL claim)."
        )
    )
    parser.add_argument("--hive", type=Path, default=DEFAULT_HIVE)
    parser.add_argument("--ticks", type=Path, default=DEFAULT_TICKS)
    parser.add_argument(
        "--books-1hz",
        type=Path,
        default=DEFAULT_BOOKS_1HZ,
        help="Optional finished part-*.parquet sidecar for L1 after lean gaps.",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--window",
        choices=("is", "oos", "both", "all"),
        default="both",
        help=(
            "Feature windows: is/oos from params_frozen, both, or all hive "
            "event_date partitions from first to last (default: both)."
        ),
    )
    parser.add_argument(
        "--skip-neighbors",
        action="store_true",
        help="Only the frozen cell (three modes), not one-step neighbours.",
    )
    args = parser.parse_args(argv)

    if not args.hive.is_dir():
        print(f"hive not found: {args.hive}", file=sys.stderr)
        return 2
    if not args.ticks.is_dir():
        print(f"tick dir not found: {args.ticks}", file=sys.stderr)
        return 2

    cov = tick_coverage(args.ticks)
    cov_note = "no lean tick windows parsed"
    if cov is not None:
        cov_start, cov_end = cov
        cov_note = f"UTC [{cov_start.isoformat()}, {cov_end.isoformat()})"
        print(f"lean tick coverage {cov_note}", flush=True)
    else:
        print(f"warning: {cov_note} under {args.ticks}", flush=True)

    books_1hz = args.books_1hz if args.books_1hz.is_dir() else None
    if books_1hz is None:
        print(
            f"warning: 1 Hz books dir missing ({args.books_1hz}); lean ticks only",
            flush=True,
        )
    else:
        n_parts = len(list(books_1hz.glob("part-*.parquet")))
        print(f"1 Hz books sidecar {books_1hz} parts={n_parts}", flush=True)

    windows: list[tuple[str, Sequence[str]]] = []
    if args.window == "all":
        all_dates = list_hive_event_dates(args.hive)
        if not all_dates:
            print(f"no event_date=*/part-000.parquet under {args.hive}", file=sys.stderr)
            return 2
        windows.append(("all", all_dates))
        print(
            f"full hive span event_date {all_dates[0]}..{all_dates[-1]} "
            f"n={len(all_dates)}",
            flush=True,
        )
    else:
        if args.window in ("is", "both"):
            windows.append(("is", list(IS_DATES)))
        if args.window in ("oos", "both"):
            windows.append(("oos", list(OOS_DATES)))

    all_steps: list[dict] = []
    all_summaries: list[dict] = []
    all_charts: list[tuple[str, list[tuple[float, float]]]] = []
    for name, dates in windows:
        steps, summaries, charts = run_window(
            hive=args.hive,
            tick_dir=args.ticks,
            dates=dates,
            window=name,
            neighbors=not args.skip_neighbors,
            books_1hz_dir=books_1hz,
        )
        all_steps.extend(steps)
        all_summaries.extend(summaries)
        all_charts.extend(charts)

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    steps_path = out / "frozen_steps.csv"
    neighbors_path = out / "neighbors.csv"
    summary_path = out / "frozen_summary.csv"
    html_path = out / "report.html"

    write_steps_csv(steps_path, [r for r in all_steps if r.get("label") == "frozen"])
    write_summary_csv(neighbors_path, all_summaries)
    write_summary_csv(
        summary_path,
        [r for r in all_summaries if r.get("label") == "frozen"],
    )

    html_rows = list(all_summaries)

    date_span_note = ""
    if windows:
        first_dates = windows[0][1]
        last_dates = windows[-1][1]
        date_span_note = (
            f"Windows: "
            + "; ".join(f"{name} {dates[0]}..{dates[-1]} (n={len(dates)})" for name, dates in windows)
            + f". First date {first_dates[0]}, last date {last_dates[-1]}."
        )

    sidecar_note = (
        f"1 Hz books sidecar: {books_1hz} (lean first, part-*.parquet fills gaps)."
        if books_1hz is not None
        else "No 1 Hz books sidecar; lean ticks only."
    )
    notes = [
        (
            "Replay input is the 1 Hz feature hive "
            f"({args.hive.name}) via sweep.store_from_hive + run_combo. "
            f"{date_span_note} "
            "Lean ticks / 1 Hz books are only an optional L1 attachment for "
            "two-venue cash. Feature hive trade list is not extended past its dates."
        ),
        (
            f"Local lean-tick coverage: {cov_note}. {sidecar_note} "
            "A (coin, ts_s) with no L1 is skipped_hole; a book that fails the "
            "spread match is price_mismatch. Both skip that round and continue; "
            "potential_pp is never booked as dollars."
        ),
        (
            "Cash is a separate perp-wallet ledger. potential_pp is listed beside "
            "cash in the CSV and is never added into these balances. "
            "This is 1 Hz accounting, not profitability, not live."
        ),
        (
            "p50_open=0.50 is the known occupancy cliff on this contour; a "
            "higher cash path there is not evidence of a better policy."
        ),
    ]
    write_balance_html(
        html_path,
        charts=all_charts,
        summary_rows=html_rows,
        notes=notes,
    )

    print(f"wrote {steps_path}", flush=True)
    print(f"wrote {neighbors_path}", flush=True)
    print(f"wrote {summary_path}", flush=True)
    print(f"wrote {html_path}", flush=True)
    print("observation only: 1 Hz accounting, not profitability, not live", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
