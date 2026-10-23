"""Universe: take=yes Bybit∩OKX rows exact-intersected with Hyperliquid perps."""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from app.utils.universe_csv import UniversePath, load_take_yes_pairs

HL_INFO_URL = "https://api.hyperliquid.xyz/info"


@dataclass(frozen=True)
class HlV2Pair:
    base_coin: str
    okx_symbol: str
    bybit_symbol: str


@dataclass(frozen=True)
class HlV2UniverseSelection:
    matched: tuple[HlV2Pair, ...]
    unmatched_coins: tuple[str, ...]


def fetch_perp_meta(
    *,
    url: str = HL_INFO_URL,
    timeout_sec: float = 20.0,
) -> dict:
    """POST info ``{"type": "meta"}``. Perp universe only."""
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


def select_pairs(
    pairs: Sequence[Mapping[str, str]],
    hl_names: Iterable[str],
) -> HlV2UniverseSelection:
    """Keep take=yes pairs whose base_coin is an exact HL perp name."""
    hl_set = {str(name).strip() for name in hl_names if str(name).strip()}
    matched: list[HlV2Pair] = []
    unmatched: list[str] = []
    seen: set[str] = set()
    for row in pairs:
        coin = str(row["base_coin"]).strip()
        if not coin or coin in seen:
            continue
        seen.add(coin)
        if coin in hl_set:
            matched.append(
                HlV2Pair(
                    base_coin=coin,
                    okx_symbol=str(row["okx_symbol"]).strip(),
                    bybit_symbol=str(row["bybit_symbol"]).strip(),
                )
            )
        else:
            unmatched.append(coin)
    return HlV2UniverseSelection(tuple(matched), tuple(unmatched))


def load_and_select(
    universe_path: UniversePath,
    hl_names: Iterable[str],
) -> HlV2UniverseSelection:
    pairs = load_take_yes_pairs(universe_path)
    return select_pairs(pairs, hl_names)


def default_universe_path() -> Path:
    env = (
        os.environ.get("HL_V2_UNIVERSE")
        or os.environ.get("SPREAD_UNIVERSE")
        or ""
    ).strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "bybit_okx_universe.csv"
