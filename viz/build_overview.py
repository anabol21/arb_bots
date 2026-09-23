"""Build / refresh overview summary.

Default is **incremental** (only new lean files). Full rescan is ``--full`` (hours).

Usage::

  ./venv/bin/python -m viz.build_overview              # fast if already seeded
  ./venv/bin/python -m viz.build_overview --full       # wipe + full pass
  ./venv/bin/python -m viz.build_overview --seed-only  # mark catalog processed, keep summary
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from research.lean_ticks_io import parse_ts_ms
from viz.config import DEFAULT_CATALOG, DEFAULT_TICKS
from viz.overview import OVERVIEW_MAX_TICKS, SUMMARY_PATH, build_summary
from viz.overview_cache import build_incremental, seed_processed_from_catalog


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ticks", type=Path, default=DEFAULT_TICKS)
    p.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    p.add_argument("--out", type=Path, default=SUMMARY_PATH)
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--max-line", type=int, default=OVERVIEW_MAX_TICKS)
    p.add_argument(
        "--full",
        action="store_true",
        help="wipe incremental store and rescan every parquet (very slow)",
    )
    p.add_argument(
        "--legacy-full",
        action="store_true",
        help="old one-shot build_summary without incremental store",
    )
    p.add_argument(
        "--seed-only",
        action="store_true",
        help="mark all catalog files processed; do not read bodies",
    )
    args = p.parse_args(argv)

    ticks = args.ticks.resolve()
    catalog = args.catalog.resolve()

    if args.seed_only:
        n = seed_processed_from_catalog(ticks, catalog)
        print(f"seeded processed files: {n}", flush=True)
        return 0

    if args.legacy_full:
        start_ms = parse_ts_ms(args.start) if args.start else None
        end_ms = parse_ts_ms(args.end) if args.end else None
        build_summary(
            ticks,
            catalog,
            start_ms=start_ms,
            end_ms=end_ms,
            workers=args.workers,
            max_line=args.max_line,
            out_path=args.out.resolve(),
        )
        seed_processed_from_catalog(ticks, catalog)
        return 0

    info = build_incremental(
        ticks,
        catalog,
        workers=args.workers,
        max_line=args.max_line,
        full=bool(args.full),
        seed_if_summary=True,
    )
    print(info, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
