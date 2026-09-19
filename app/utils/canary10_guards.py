"""Guards for thin listing-wait canary (dry-run discovery, delta vs prod)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .universe_delta import csv_base_coins, read_delta_rows


def assert_dry_run_discovery_summary(
    summary: Mapping[str, Any],
    *,
    min_csv_coins: int = 200,
    require_delta_zero: bool = True,
) -> None:
    """Startup guard: full copy present, no false hot-add from a 10-row CSV slice."""
    csv_coins = int(summary.get("csv_coins", 0))
    delta_rows = int(summary.get("delta_rows", -1))
    new_before_cap = int(summary.get("new_before_cap", -1))
    if csv_coins < min_csv_coins:
        raise ValueError(
            f"csv_coins={csv_coins} < min_csv_coins={min_csv_coins}; "
            "universe copy likely truncated — abort canary"
        )
    if require_delta_zero and delta_rows != 0:
        raise ValueError(
            f"delta_rows={delta_rows} (new_before_cap={new_before_cap}); "
            "expected 0 before listeners — fix CSV backfill or intersection"
        )
    if require_delta_zero and new_before_cap != 0:
        raise ValueError(
            f"new_before_cap={new_before_cap}; expected 0 for listing-wait idle start"
        )


def coins_absent_from_csv(coins: Sequence[str], csv_path: Path) -> list[str]:
    known = {c.upper() for c in csv_base_coins(csv_path)}
    return [c for c in coins if c.strip().upper() not in known]


def assert_first_delta_vs_prod(
    delta_path: Path,
    *,
    canary_universe_path: Path,
    prod_universe_path: Path,
) -> list[str]:
    """On first non-empty delta: every coin must be absent from canary copy and prod CSV."""
    rows = read_delta_rows(delta_path)
    if not rows:
        return []
    coins = [str(r["base_coin"]).strip() for r in rows if str(r.get("base_coin", "")).strip()]
    in_canary = [
        c
        for c in coins
        if c.upper() in {x.upper() for x in csv_base_coins(canary_universe_path)}
    ]
    if in_canary:
        raise ValueError(
            f"delta lists coins already in canary universe copy: {in_canary}; "
            "not a true listing — abort"
        )
    in_prod = [
        c
        for c in coins
        if c.upper() in {x.upper() for x in csv_base_coins(prod_universe_path)}
    ]
    if in_prod:
        raise ValueError(
            f"delta lists coins already in prod universe: {in_prod}; "
            "copy drift, not exchange listing — abort"
        )
    return coins


def assert_pairs_jump_guard(
    *,
    pairs: int,
    baseline_pairs: int,
    max_extra: int,
    delta_coins_new_to_canary: Sequence[str],
) -> None:
    """Abort if pairs jumped beyond baseline+max_extra without coins absent from full copy."""
    allowed = baseline_pairs + max_extra
    if pairs <= allowed:
        return
    if not delta_coins_new_to_canary:
        raise ValueError(
            f"pairs={pairs} > allowed={allowed} without delta coins absent from canary CSV"
        )
