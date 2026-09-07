"""CLI: whole-market gear 2.2 floor snapshot journal (no orders)."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from app.utils.universe_csv import load_take_yes_base_coins
from research.gear22_floor_watcher.builder import (
    DEFAULT_LOOKBACK_HOURS,
    FORMULA_ID,
    build_market_floor_snapshot,
)
from research.gear22_floor_watcher.journal import (
    journal_max_bar_end_ms,
    merge_snapshot_frames,
    read_journal,
    write_dual,
)
from research.gear22_quiet_regime_viz.load import DEFAULT_SINCE_UTC, parse_since_ms

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_UNIVERSE = REPO_ROOT / "bybit_okx_universe.csv"


def _parse_coins(raw: str) -> list[str]:
    parts = [p.strip().upper() for p in str(raw).split(",")]
    coins = [p for p in parts if p]
    if not coins:
        raise argparse.ArgumentTypeError("coins list is empty")
    return coins


def _parse_formats(raw: str) -> list[str]:
    parts = [p.strip().lower() for p in str(raw).split(",") if p.strip()]
    allowed = {"parquet", "jsonl"}
    if not parts or any(p not in allowed for p in parts):
        raise argparse.ArgumentTypeError(
            "formats must be comma-separated subset of parquet,jsonl"
        )
    # Preserve order, unique.
    out: list[str] = []
    for p in parts:
        if p not in out:
            out.append(p)
    return out


def resolve_coins(
    *,
    universe: Path,
    coins: Optional[Sequence[str]],
) -> list[str]:
    """Explicit ``--coins`` wins; else take=yes from universe CSV."""
    if coins:
        return [c.strip().upper() for c in coins if str(c).strip()]
    return load_take_yes_base_coins(universe)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m research.gear22_floor_watcher",
        description=(
            "Gear 2.2 market floor watcher: take=yes coins → 5m long/short "
            "closes → SMA-12 → tf-select α25 floor → observation journal. "
            "No orders / not a live threshold."
        ),
    )
    p.add_argument(
        "--data-root",
        type=Path,
        required=True,
        help="Tick dump root (compacted spread_*.parquet, hive, or CSV).",
    )
    p.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Journal path stem (writes .parquet and/or .jsonl beside it).",
    )
    p.add_argument(
        "--universe",
        type=Path,
        default=DEFAULT_UNIVERSE,
        help=f"Universe CSV with take column (default: {DEFAULT_UNIVERSE.name}).",
    )
    p.add_argument(
        "--coins",
        type=_parse_coins,
        default=None,
        help="Optional coin override (comma-separated). Default: take=yes.",
    )
    p.add_argument(
        "--since",
        default=None,
        help=(
            "Emit window start (UTC ISO). Default: gear2 restart "
            f"{DEFAULT_SINCE_UTC} for oneshot; for --watch, last journal "
            "bar_end_ms (or that default if journal empty)."
        ),
    )
    p.add_argument(
        "--until",
        default=None,
        help="Emit window end (UTC ISO). Default: now.",
    )
    p.add_argument(
        "--lookback-hours",
        type=float,
        default=DEFAULT_LOOKBACK_HOURS,
        help=(
            "Extra history loaded before --since so SMA-12 / 12h trim can warm "
            f"(default {DEFAULT_LOOKBACK_HOURS})."
        ),
    )
    p.add_argument(
        "--watch",
        action="store_true",
        help=(
            "Incremental mode: merge into existing journal by "
            "(bar_end_ms, base_coin, side). If --since omitted, resume from "
            "last journal bar_end_ms."
        ),
    )
    p.add_argument(
        "--formats",
        type=_parse_formats,
        default=["parquet"],
        help="Output formats: parquet, jsonl, or both (default: parquet).",
    )
    return p


def run_watcher(
    *,
    data_root: Path,
    out: Path,
    universe: Path = DEFAULT_UNIVERSE,
    coins: Optional[Sequence[str]] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    lookback_hours: float = DEFAULT_LOOKBACK_HOURS,
    watch: bool = False,
    formats: Sequence[str] = ("parquet",),
) -> dict:
    """Build snapshot, optionally merge, write journal. Returns run summary."""
    coin_list = resolve_coins(universe=universe, coins=coins)
    until_ms = (
        parse_since_ms(until)
        if until
        else int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    )

    existing = read_journal(Path(out).with_suffix(".parquet"))
    if existing.empty:
        # Also try jsonl sibling when parquet missing.
        existing = read_journal(Path(out).with_suffix(".jsonl"))

    if since is not None:
        since_ms = parse_since_ms(since)
    elif watch:
        last = journal_max_bar_end_ms(existing)
        since_ms = last if last is not None else parse_since_ms(DEFAULT_SINCE_UTC)
    else:
        since_ms = parse_since_ms(DEFAULT_SINCE_UTC)

    run_mode = "watch" if watch else "oneshot"
    incoming = build_market_floor_snapshot(
        data_root,
        coins=coin_list,
        since_ms=since_ms,
        until_ms=until_ms,
        lookback_hours=lookback_hours,
        run_mode=run_mode,
    )

    # Both oneshot and watch: overwrite-by-key so re-runs are idempotent.
    merged = merge_snapshot_frames(existing, incoming)

    written = write_dual(out, merged, formats=formats)
    summary = {
        "formula_id": FORMULA_ID,
        "run_mode": run_mode,
        "n_coins_requested": len(coin_list),
        "n_rows_incoming": int(len(incoming)),
        "n_rows_journal": int(len(merged)),
        "since_ms": int(since_ms),
        "until_ms": int(until_ms),
        "lookback_hours": float(lookback_hours),
        "written": [str(p) for p in written],
        "coins_with_rows": sorted(incoming["base_coin"].unique().tolist())
        if not incoming.empty
        else [],
    }
    print(
        f"floor_watcher mode={run_mode} coins={len(coin_list)} "
        f"incoming={summary['n_rows_incoming']} journal={summary['n_rows_journal']} "
        f"wrote={','.join(summary['written'])}"
    )
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    run_watcher(
        data_root=args.data_root,
        out=args.out,
        universe=args.universe,
        coins=args.coins,
        since=args.since,
        until=args.until,
        lookback_hours=args.lookback_hours,
        watch=args.watch,
        formats=args.formats,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
