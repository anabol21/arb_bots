#!/usr/bin/env python3
"""Prepare thin listing-wait canary universe CSV on the VPS.

1. Copy prod universe (read-only source).
2. Backfill every Bybit×OKX intersection coin missing from prod (take=no).
3. Set take=yes on exactly 10 crypto coins (default core BTC ETH SOL XRP + 6 more
   from prod take=yes crypto, in file order).
4. Optional: dry-run discovery against the output (must be delta_rows=0).

Never writes under /data/live or production parquet trees.

Usage (VPS, clone /root/spread_hotadd_canary on PR #53 branch):

  export CANARY_ROOT=/root/spread_hotadd_canary
  export PROD_CSV=/root/spread_staging/bybit_okx_universe.csv
  python3 validation/prep_canary10_universe.py \\
    --prod-universe "$PROD_CSV" \\
    --out "$CANARY_ROOT/bybit_okx_universe_canary10.csv" \\
    --dry-run-discovery \\
    --delta "$CANARY_ROOT/hot_add_delta.csv"
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.discovery.intersection import run_discovery  # noqa: E402
from app.utils.canary10_universe import (  # noqa: E402
    DEFAULT_CORE_COINS,
    build_canary10_universe_rows,
    load_prod_universe,
    write_canary10_universe_csv,
)
from app.utils.universe_delta import assert_delta_path_safe  # noqa: E402
from app.utils.canary10_guards import assert_dry_run_discovery_summary  # noqa: E402

FORBIDDEN_PREFIXES = ("/data/live", "/data/spool", "/data/bars", "/data/compacted")


def _assert_out_safe(path: Path) -> None:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    text = str(resolved)
    for prefix in FORBIDDEN_PREFIXES:
        if text == prefix or text.startswith(prefix + "/"):
            raise SystemExit(f"refusing to write canary universe under {prefix}: {resolved}")


def _fetch_intersection_rows():
    from app.discovery.intersection import (
        build_intersection_rows,
        fetch_bybit_linear_instruments,
        fetch_okx_swap_instruments,
        filter_bybit_live_usdt_linear,
        filter_okx_live_usdt_swap,
        normalize_bybit_item,
        normalize_okx_item,
    )

    bybit_raw = fetch_bybit_linear_instruments()
    okx_raw = fetch_okx_swap_instruments()
    bybit_f = filter_bybit_live_usdt_linear(
        [normalize_bybit_item(item) for item in bybit_raw]
    )
    okx_f = filter_okx_live_usdt_swap([normalize_okx_item(item) for item in okx_raw])
    return build_intersection_rows(okx_f, bybit_f)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Prepare canary10 universe CSV with backfill.")
    p.add_argument(
        "--prod-universe",
        required=True,
        help="Production universe CSV (read-only; e.g. /root/spread_staging/bybit_okx_universe.csv)",
    )
    p.add_argument(
        "--out",
        required=True,
        help="Output canary CSV path (e.g. .../bybit_okx_universe_canary10.csv)",
    )
    p.add_argument(
        "--core-coins",
        default=",".join(DEFAULT_CORE_COINS),
        help="Comma-separated core take=yes coins (default BTC,ETH,SOL,XRP)",
    )
    p.add_argument(
        "--take-yes-count",
        type=int,
        default=10,
        help="Exactly this many take=yes crypto rows (default 10)",
    )
    p.add_argument(
        "--dry-run-discovery",
        action="store_true",
        help="After write, run discovery once; require delta_rows=0",
    )
    p.add_argument(
        "--delta",
        default="hot_add_delta.csv",
        help="Delta path for dry-run discovery (must not be universe CSV)",
    )
    p.add_argument(
        "--min-csv-coins",
        type=int,
        default=200,
        help="Guard: csv_coins must be >= this on dry-run (default 200)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%y-%m-%d %H:%M:%S",
    )
    log = logging.getLogger("prep_canary10")
    args = build_parser().parse_args(argv)
    prod_path = Path(args.prod_universe)
    out_path = Path(args.out)
    delta_path = Path(args.delta)
    _assert_out_safe(out_path)
    assert_delta_path_safe(delta_path, out_path)

    if not prod_path.exists():
        log.error("prod universe missing: %s", prod_path)
        return 1

    prod_rows = load_prod_universe(prod_path)
    log.info("prod_universe | path=%s | rows=%s", prod_path, len(prod_rows))

    intersection = _fetch_intersection_rows()
    log.info("intersection_rest | rows=%s", len(intersection))

    core = [c.strip() for c in args.core_coins.split(",") if c.strip()]
    final_rows, take_yes, backfill_added = build_canary10_universe_rows(
        prod_rows,
        intersection,
        core_coins=core,
        take_yes_count=args.take_yes_count,
    )
    write_canary10_universe_csv(out_path, final_rows)
    log.info(
        "canary10_written | out=%s | total_rows=%s | backfill_added=%s | take_yes=%s",
        out_path,
        len(final_rows),
        backfill_added,
        ",".join(take_yes),
    )
    print(json.dumps({"take_yes": take_yes, "total_rows": len(final_rows), "backfill_added": backfill_added}))

    if args.dry_run_discovery:
        summary = run_discovery(
            universe_path=out_path,
            delta_path=delta_path,
            max_new=8,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        try:
            assert_dry_run_discovery_summary(
                summary,
                min_csv_coins=args.min_csv_coins,
            )
        except ValueError as exc:
            log.error("dry_run_guard_failed | %s", exc)
            return 2
        log.info("dry_run_ok | delta_rows=0 | csv_coins=%s", summary.get("csv_coins"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
