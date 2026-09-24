"""take=yes coins intersected with Hyperliquid perp names.

Match is exact string equality after strip. No kPEPE / 1000PEPE aliases.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from app.utils.universe_csv import UniversePath, load_take_yes_pairs

HL_INFO_URL = "https://api.hyperliquid.xyz/info"


@dataclass(frozen=True)
class HlUniverseSelection:
    matched: tuple[str, ...]
    unmatched: tuple[str, ...]


def fetch_perp_meta(
    *,
    url: str = HL_INFO_URL,
    timeout_sec: float = 20.0,
) -> dict:
    """POST info ``{"type": "meta"}``. Perp universe only, not ``spotMeta``."""
    body = json.dumps({"type": "meta"}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_sec) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("universe"), list):
        raise ValueError("hyperliquid perp meta response has no universe list")
    return payload


def perp_names_from_meta(payload: Mapping[str, object]) -> list[str]:
    """Exact ``universe[].name`` values, first-seen order, no case folding."""
    universe = payload.get("universe")
    if not isinstance(universe, list):
        raise ValueError("hyperliquid perp meta universe is not a list")
    names: list[str] = []
    seen: set[str] = set()
    for item in universe:
        if not isinstance(item, dict):
            continue
        raw = item.get("name")
        if not isinstance(raw, str):
            continue
        name = raw.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def select_exact_coins(
    take_yes_base_coins: Sequence[str],
    hl_names: Iterable[str],
) -> HlUniverseSelection:
    """Keep take=yes coins whose name is in the Hyperliquid perp set."""
    hl_set = {str(name).strip() for name in hl_names if str(name).strip()}
    matched: list[str] = []
    unmatched: list[str] = []
    seen: set[str] = set()
    for raw in take_yes_base_coins:
        coin = str(raw).strip()
        if not coin or coin in seen:
            continue
        seen.add(coin)
        if coin in hl_set:
            matched.append(coin)
        else:
            unmatched.append(coin)
    return HlUniverseSelection(tuple(matched), tuple(unmatched))


def select_take_yes_pairs(
    pairs: Sequence[Mapping[str, str]],
    hl_names: Iterable[str],
) -> HlUniverseSelection:
    coins = [str(row["base_coin"]) for row in pairs]
    return select_exact_coins(coins, hl_names)


def load_and_select(
    universe_path: UniversePath,
    hl_names: Iterable[str],
) -> HlUniverseSelection:
    pairs = load_take_yes_pairs(universe_path)
    return select_take_yes_pairs(pairs, hl_names)


def default_universe_path() -> Path:
    return Path(__file__).resolve().parents[2] / "bybit_okx_universe.csv"
