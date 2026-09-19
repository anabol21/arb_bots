#!/usr/bin/env python3
"""Drive hot-add canary experiments A–C by writing delta/drop snapshots only.

Never writes under /data/live, /data/spool, /data/bars, or /data/compacted.
Use with ``spread-collector-hotadd-canary.service`` and a 2-coin universe CSV.

Schedule (one action per minute by default):

| Minute | Action |
|--------|--------|
| 0 | Empty delta and drop; canary starts on 2-coin CSV |
| 1 | Cumulative delta +1 coin (not in bootstrap set) |
| 2 | Cumulative delta +2 more (total +3 beyond bootstrap) |
| 3 | Cumulative delta +10 more (total +13); set SPREAD_HOT_ADD_MAX_EXTRA=16 on canary |
| 4 | Drop snapshot: 1–2 coins from the added set (not bootstrap BTC/ETH) |

Usage (VPS, after deploying branch and enabling canary unit):

  export STAGING=/root/spread_staging
  python3 validation/hot_add_canary_driver.py prepare \\
    --universe $STAGING/bybit_okx_universe.csv \\
    --staging-dir $STAGING/canary-hotadd \\
    --bootstrap BTC,ETH

  # Point canary at 2-coin screen (example unit drop-in or env):
  # SPREAD_UNIVERSE=$STAGING/canary-hotadd/canary_2coin.csv

  python3 validation/hot_add_canary_driver.py run \\
    --staging-dir $STAGING/canary-hotadd \\
    --interval-sec 60
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.utils.universe_delta import (  # noqa: E402
    DeltaPathError,
    assert_delta_path_safe,
    write_delta_atomic,
    write_drop_atomic,
)

FORBIDDEN_WRITE_PREFIXES = (
    "/data/live",
    "/data/spool",
    "/data/bars",
    "/data/compacted",
)


def _assert_staging_safe(path: Path) -> None:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    text = str(resolved)
    for prefix in FORBIDDEN_WRITE_PREFIXES:
        if text == prefix or text.startswith(prefix + "/"):
            raise SystemExit(
                f"refusing to write canary driver files under production tree: {resolved}"
            )


def _read_universe_rows(universe: Path) -> list[dict[str, str]]:
    with universe.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise ValueError(f"universe has no header: {universe}")
        return [dict(row) for row in reader]


def _row_for_coin(
    rows: list[dict[str, str]], coin: str, *, universe: Path
) -> dict[str, str]:
    want = coin.strip().upper()
    for row in rows:
        if str(row.get("base_coin", "")).strip().upper() == want:
            return row
    raise KeyError(f"coin {coin!r} not in universe {universe}")


def _delta_row_from_universe(row: dict[str, str]) -> dict[str, str]:
    return {
        "base_coin": str(row["base_coin"]).strip(),
        "okx_symbol": str(row["okx_symbol"]).strip(),
        "bybit_symbol": str(row["bybit_symbol"]).strip(),
        "okx_tick_size": str(row.get("okx_tick_size", "")).strip(),
        "okx_lot_size": str(row.get("okx_lot_size", "")).strip(),
        "okx_min_size": str(row.get("okx_min_size", "")).strip(),
        "bybit_tick_size": str(row.get("bybit_tick_size", "")).strip(),
        "bybit_qty_step": str(row.get("bybit_qty_step", "")).strip(),
        "bybit_min_order_qty": str(row.get("bybit_min_order_qty", "")).strip(),
        "bybit_min_notional_value": str(row.get("bybit_min_notional_value", "")).strip(),
        "discovered_at_utc": "canary-driver",
    }


def pick_extra_coins(
    universe_rows: list[dict[str, str]],
    *,
    bootstrap: set[str],
    count: int,
) -> list[dict[str, str]]:
    """Real symbols: prefer take=no, then take!=yes, excluding bootstrap."""
    bootstrap_u = {c.upper() for c in bootstrap}
    candidates: list[dict[str, str]] = []
    for row in universe_rows:
        coin = str(row.get("base_coin", "")).strip()
        if not coin or coin.upper() in bootstrap_u:
            continue
        take = str(row.get("take", "")).strip().lower()
        if take == "no":
            candidates.append(row)
    if len(candidates) < count:
        for row in universe_rows:
            coin = str(row.get("base_coin", "")).strip()
            if not coin or coin.upper() in bootstrap_u:
                continue
            take = str(row.get("take", "")).strip().lower()
            if take == "yes":
                continue
            if row in candidates:
                continue
            candidates.append(row)
    if len(candidates) < count:
        raise SystemExit(
            f"need {count} extra coins outside bootstrap; only found {len(candidates)}"
        )
    return [_delta_row_from_universe(r) for r in candidates[:count]]


def write_canary_2coin_csv(
    universe: Path,
    out_csv: Path,
    bootstrap: list[str],
) -> None:
    rows = _read_universe_rows(universe)
    selected = []
    for coin in bootstrap:
        selected.append(_row_for_coin(rows, coin, universe=universe))
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys())
    with out_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in selected:
            out = dict(row)
            out["take"] = "yes"
            writer.writerow(out)


def cmd_prepare(args: argparse.Namespace) -> int:
    staging = Path(args.staging_dir).expanduser()
    _assert_staging_safe(staging)
    universe = Path(args.universe).expanduser()
    bootstrap = [c.strip() for c in args.bootstrap.split(",") if c.strip()]
    if len(bootstrap) != 2:
        raise SystemExit("prepare expects exactly two bootstrap coins (e.g. BTC,ETH)")
    staging.mkdir(parents=True, exist_ok=True)
    canary_csv = staging / "canary_2coin.csv"
    write_canary_2coin_csv(universe, canary_csv, bootstrap)
    delta_path = staging / "hot_add_delta.csv"
    drop_path = staging / "hot_add_drop.csv"
    assert_delta_path_safe(delta_path, universe)
    assert_delta_path_safe(drop_path, universe)
    write_delta_atomic(delta_path, [], universe_path=universe)
    write_drop_atomic(drop_path, [])
    print(f"wrote {canary_csv}")
    print(f"cleared {delta_path}")
    print(f"cleared {drop_path}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    staging = Path(args.staging_dir).expanduser()
    _assert_staging_safe(staging)
    universe = Path(args.universe).expanduser()
    bootstrap = {c.strip().upper() for c in args.bootstrap.split(",") if c.strip()}
    universe_rows = _read_universe_rows(universe)
    extras = pick_extra_coins(universe_rows, bootstrap=bootstrap, count=13)
    delta_path = staging / "hot_add_delta.csv"
    drop_path = staging / "hot_add_drop.csv"
    assert_delta_path_safe(delta_path, universe)
    assert_delta_path_safe(drop_path, universe)

    def step_empty() -> None:
        write_delta_atomic(delta_path, [], universe_path=universe)
        write_drop_atomic(drop_path, [])

    def step_plus(n: int) -> None:
        write_delta_atomic(delta_path, extras[:n], universe_path=universe)

    def step_drop() -> None:
        added = [r["base_coin"] for r in extras[:3]]
        write_drop_atomic(drop_path, added[:2])

    schedule = [
        ("A baseline empty delta/drop", step_empty),
        ("B +1 cumulative", lambda: step_plus(1)),
        ("B +3 cumulative (+2 more)", lambda: step_plus(3)),
        ("B +13 cumulative (+10 more; need MAX_EXTRA=16)", lambda: step_plus(13)),
        ("C drop 2 added coins", step_drop),
    ]
    interval = float(args.interval_sec)
    for label, fn in schedule:
        print(f"step | {label} | delta={delta_path} | drop={drop_path}", flush=True)
        fn()
        if label != schedule[-1][0]:
            print(f"sleep {interval}s before next step", flush=True)
            time.sleep(interval)
    print("schedule complete", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Hot-add canary experiment file driver")
    sub = parser.add_subparsers(dest="cmd", required=True)

    prep = sub.add_parser("prepare", help="Write 2-coin CSV and empty delta/drop")
    prep.add_argument("--universe", required=True)
    prep.add_argument("--staging-dir", required=True)
    prep.add_argument("--bootstrap", default="BTC,ETH")
    prep.set_defaults(func=cmd_prepare)

    run = sub.add_parser("run", help="Run A–C minute schedule against staging paths")
    run.add_argument("--staging-dir", required=True)
    run.add_argument("--universe", default=str(REPO / "bybit_okx_universe.csv"))
    run.add_argument("--bootstrap", default="BTC,ETH")
    run.add_argument("--interval-sec", type=float, default=60.0)
    run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
