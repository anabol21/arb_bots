"""Thin listing-wait canary: full universe copy, take=yes on 10 crypto pairs.

Backfill every Bybit×OKX intersection coin missing from the prod CSV (take=no),
then set take=yes on exactly ``take_yes_count`` crypto coins. Discovery against
the copy must yield delta_rows=0 until a genuinely new listing appears.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Mapping, Optional, Sequence

from research.is_crypto import is_crypto

from .universe_csv import read_universe_dicts

logger = logging.getLogger("canary10")

DEFAULT_CORE_COINS = ("BTC", "ETH", "SOL", "XRP")
DEFAULT_TAKE_YES_COUNT = 10

UNIVERSE_COLUMNS = (
    "base_coin",
    "okx_symbol",
    "bybit_symbol",
    "okx_tick_size",
    "okx_lot_size",
    "okx_min_size",
    "bybit_tick_size",
    "bybit_qty_step",
    "bybit_min_order_qty",
    "bybit_min_notional_value",
    "take",
)


def _norm_coin(coin: str) -> str:
    return str(coin or "").strip().upper()


def intersection_row_to_universe_row(row: Mapping[str, str], *, take: str = "no") -> dict[str, str]:
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
        "take": take,
    }


def pick_take_yes_coins(
    prod_rows: Sequence[Mapping[str, str]],
    *,
    core_coins: Sequence[str] = DEFAULT_CORE_COINS,
    take_yes_count: int = DEFAULT_TAKE_YES_COUNT,
) -> list[str]:
    """Choose exactly ``take_yes_count`` crypto coins; ``core_coins`` first."""
    if take_yes_count <= 0:
        raise ValueError(f"take_yes_count must be > 0, got {take_yes_count}")
    core_u = [_norm_coin(c) for c in core_coins if _norm_coin(c)]
    chosen: list[str] = []
    seen: set[str] = set()

    def _try_add(coin: str) -> None:
        c = _norm_coin(coin)
        if not c or c in seen:
            return
        if not is_crypto(c):
            return
        seen.add(c)
        chosen.append(c)

    for c in core_u:
        _try_add(c)
    if len(chosen) < len(core_u):
        missing = [c for c in core_u if c not in chosen]
        raise ValueError(f"core crypto coins missing or non-crypto: {missing}")

    prod_take_yes_crypto: list[str] = []
    for row in prod_rows:
        take = str(row.get("take", "")).strip().lower()
        if take != "yes":
            continue
        coin = _norm_coin(str(row.get("base_coin", "")))
        if not coin or coin in seen:
            continue
        if not is_crypto(coin):
            continue
        prod_take_yes_crypto.append(coin)

    for coin in prod_take_yes_crypto:
        if len(chosen) >= take_yes_count:
            break
        _try_add(coin)

    if len(chosen) < take_yes_count:
        raise ValueError(
            f"need {take_yes_count} crypto take=yes coins; only resolved {len(chosen)}: {chosen}"
        )
    return chosen[:take_yes_count]


def backfill_rows_from_intersection(
    prod_rows: Sequence[Mapping[str, str]],
    intersection_rows: Sequence[Mapping[str, str]],
) -> tuple[list[dict[str, str]], int]:
    """Merge prod CSV with intersection coins absent from prod (new rows take=no)."""
    by_coin: dict[str, dict[str, str]] = {}
    for row in prod_rows:
        coin = str(row.get("base_coin", "")).strip()
        if not coin:
            continue
        by_coin[_norm_coin(coin)] = {k: str(v) for k, v in row.items()}

    added = 0
    for row in intersection_rows:
        coin = str(row.get("base_coin", "")).strip()
        if not coin:
            continue
        key = _norm_coin(coin)
        if key in by_coin:
            continue
        by_coin[key] = intersection_row_to_universe_row(row, take="no")
        added += 1

    merged = sorted(by_coin.values(), key=lambda r: _norm_coin(r["base_coin"]))
    return merged, added


def apply_take_yes_mask(
    rows: Sequence[Mapping[str, str]],
    take_yes_coins: Sequence[str],
) -> list[dict[str, str]]:
    """Set take=yes only on ``take_yes_coins``; every other row take=no."""
    yes_set = {_norm_coin(c) for c in take_yes_coins}
    if len(yes_set) != len(take_yes_coins):
        raise ValueError("duplicate coins in take_yes_coins")
    for coin in take_yes_coins:
        if not is_crypto(coin):
            raise ValueError(f"take=yes coin must be crypto: {coin!r}")
    out: list[dict[str, str]] = []
    for row in rows:
        coin = _norm_coin(str(row.get("base_coin", "")))
        take = "yes" if coin in yes_set else "no"
        merged = {k: str(row.get(k, "")).strip() for k in UNIVERSE_COLUMNS}
        merged["take"] = take
        out.append(merged)
    yes_count = sum(1 for r in out if r["take"] == "yes")
    if yes_count != len(yes_set):
        missing = yes_set - {_norm_coin(r["base_coin"]) for r in out if r["take"] == "yes"}
        raise ValueError(f"take=yes coins not in merged universe: {sorted(missing)}")
    return out


def write_canary10_universe_csv(path: Path, rows: Sequence[Mapping[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(UNIVERSE_COLUMNS)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: str(row.get(name, "")) for name in fieldnames})
        fh.flush()
    tmp.replace(path)


def build_canary10_universe_rows(
    prod_rows: Sequence[Mapping[str, str]],
    intersection_rows: Sequence[Mapping[str, str]],
    *,
    core_coins: Sequence[str] = DEFAULT_CORE_COINS,
    take_yes_count: int = DEFAULT_TAKE_YES_COUNT,
) -> tuple[list[dict[str, str]], list[str], int]:
    """Backfill, mask take=yes on 10 crypto, return (rows, take_yes_list, backfill_added)."""
    merged, added = backfill_rows_from_intersection(prod_rows, intersection_rows)
    take_yes = pick_take_yes_coins(
        prod_rows,
        core_coins=core_coins,
        take_yes_count=take_yes_count,
    )
    final = apply_take_yes_mask(merged, take_yes)
    return final, take_yes, added


def load_prod_universe(path: Path) -> list[dict[str, str]]:
    return read_universe_dicts(path)


def count_take_yes(rows: Sequence[Mapping[str, str]]) -> int:
    return sum(1 for r in rows if str(r.get("take", "")).strip().lower() == "yes")
