"""Allowlisted live USDT perpetual venues/symbols for R3 planning.

No network. Spot/options and arbitrary symbols are rejected.
BTC remains the W3/W4/W5 default. W6 adds one extra matched pair (TRUMP).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from app.policy.trade_manager import (
    CANARY_WAL_EDEN_COINS as CANARY_WAL_EDEN_COINS,
    LIVE_SIZE_COINS as LIVE_SIZE_COINS,
    live_size_coin_allowed,
    live_size_coins_for_profile,
)

ORDER_VENUES = frozenset({"bybit_live", "okx_live"})

# Default pair for W3/W4/W5 (alias → per-venue native symbols).
PLANNED_PAIR_ALIAS = "BTC-USDT-PERP"
# Extra W6 dual-leg pair only. Not a default; not a whole-market allowlist.
W6_PAIR_ALIAS = "TRUMP-USDT-PERP"


@dataclass(frozen=True)
class AllowedFuturesSymbol:
    venue: str
    symbol: str
    symbol_alias: str
    instrument_class: str  # must be linear_perpetual
    quote_ccy: str


def _fut(venue: str, symbol: str, alias: str) -> AllowedFuturesSymbol:
    return AllowedFuturesSymbol(
        venue=venue,
        symbol=symbol,
        symbol_alias=alias,
        instrument_class="linear_perpetual",
        quote_ccy="USDT",
    )


_DEFAULT: Mapping[str, AllowedFuturesSymbol] = {
    "bybit_live": _fut("bybit_live", "BTCUSDT", PLANNED_PAIR_ALIAS),
    "okx_live": _fut("okx_live", "BTC-USDT-SWAP", PLANNED_PAIR_ALIAS),
}

_ALLOWED_ROWS: tuple[AllowedFuturesSymbol, ...] = (
    _DEFAULT["bybit_live"],
    _DEFAULT["okx_live"],
    _fut("bybit_live", "TRUMPUSDT", W6_PAIR_ALIAS),
    _fut("okx_live", "TRUMP-USDT-SWAP", W6_PAIR_ALIAS),
)

_BY_NATIVE: Mapping[tuple[str, str], AllowedFuturesSymbol] = {
    (row.venue, row.symbol): row for row in _ALLOWED_ROWS
}
_BY_ALIAS: Mapping[tuple[str, str], AllowedFuturesSymbol] = {
    (row.venue, row.symbol_alias): row for row in _ALLOWED_ROWS
}

# LIVE_SIZE contour (SOL/XRP) and canary (WAL/EDEN). Separate from W6 BTC/TRUMP.
# Contour B send does not use resolve_allowed_futures_symbol; this list is the
# private-symbol gate for those live-size / canary coins.
_LIVE_SIZE_ROWS: tuple[AllowedFuturesSymbol, ...] = (
    _fut("bybit_live", "SOLUSDT", "SOL-USDT-PERP"),
    _fut("okx_live", "SOL-USDT-SWAP", "SOL-USDT-PERP"),
    _fut("bybit_live", "XRPUSDT", "XRP-USDT-PERP"),
    _fut("okx_live", "XRP-USDT-SWAP", "XRP-USDT-PERP"),
    _fut("bybit_live", "WALUSDT", "WAL-USDT-PERP"),
    _fut("okx_live", "WAL-USDT-SWAP", "WAL-USDT-PERP"),
    _fut("bybit_live", "EDENUSDT", "EDEN-USDT-PERP"),
    _fut("okx_live", "EDEN-USDT-SWAP", "EDEN-USDT-PERP"),
)
_LIVE_SIZE_BY_NATIVE: Mapping[tuple[str, str], AllowedFuturesSymbol] = {
    (row.venue, row.symbol): row for row in _LIVE_SIZE_ROWS
}
_LIVE_SIZE_BY_ALIAS: Mapping[tuple[str, str], AllowedFuturesSymbol] = {
    (row.venue, row.symbol_alias): row for row in _LIVE_SIZE_ROWS
}
_LIVE_SIZE_BASE: Mapping[str, str] = {
    "SOLUSDT": "SOL",
    "SOL-USDT-SWAP": "SOL",
    "SOL-USDT-PERP": "SOL",
    "XRPUSDT": "XRP",
    "XRP-USDT-SWAP": "XRP",
    "XRP-USDT-PERP": "XRP",
    "WALUSDT": "WAL",
    "WAL-USDT-SWAP": "WAL",
    "WAL-USDT-PERP": "WAL",
    "EDENUSDT": "EDEN",
    "EDEN-USDT-SWAP": "EDEN",
    "EDEN-USDT-PERP": "EDEN",
}


class SymbolGateError(ValueError):
    """Futures symbol/venue allowlist violation."""


def assert_order_venue(venue: str) -> str:
    v = str(venue).strip()
    if v not in ORDER_VENUES:
        raise SymbolGateError(
            f"order venue must be exactly bybit_live or okx_live, got {venue!r}"
        )
    return v


def resolve_allowed_futures_symbol(venue: str, symbol: str) -> AllowedFuturesSymbol:
    """Accept only the planned USDT linear perpetual mapping for the venue."""
    v = assert_order_venue(venue)
    sym = str(symbol).strip()
    found = _BY_NATIVE.get((v, sym)) or _BY_ALIAS.get((v, sym))
    if found is None and sym == PLANNED_PAIR_ALIAS:
        found = _DEFAULT[v]
    if found is not None:
        if found.instrument_class != "linear_perpetual":
            raise SymbolGateError("only linear_perpetual futures are allowed")
        return found
    lowered = sym.lower()
    if "option" in lowered or "spot" in lowered:
        raise SymbolGateError("spot/options instruments are rejected")
    raise SymbolGateError(f"symbol {symbol!r} not on allowlist for {v}")


def allowed_native_symbol(venue: str) -> str:
    """W3 default native symbol (BTC). W6 must pass TRUMP natives explicitly."""
    return _DEFAULT[assert_order_venue(venue)].symbol


def _base_coin_from_live_size_symbol(symbol: str) -> Optional[str]:
    return _LIVE_SIZE_BASE.get(str(symbol).strip().upper())


def resolve_live_size_futures_symbol(
    venue: str,
    symbol: str,
    *,
    contour: str = "live_size",
) -> AllowedFuturesSymbol:
    """Allowlist for LIVE_SIZE (SOL/XRP) or canary (WAL/EDEN). Not W6 BTC/TRUMP."""
    v = assert_order_venue(venue)
    sym = str(symbol).strip()
    found = _LIVE_SIZE_BY_NATIVE.get((v, sym)) or _LIVE_SIZE_BY_ALIAS.get((v, sym))
    if found is None:
        lowered = sym.lower()
        if "option" in lowered or "spot" in lowered:
            raise SymbolGateError("spot/options instruments are rejected")
        raise SymbolGateError(f"symbol {symbol!r} not on live-size allowlist for {v}")
    base = _base_coin_from_live_size_symbol(found.symbol) or _base_coin_from_live_size_symbol(
        found.symbol_alias
    )
    profile = "canary_wal_eden" if str(contour).strip().lower() in {
        "canary_wal_eden",
        "canary",
    } else "live_size"
    if base is None or not live_size_coin_allowed(base, profile):
        raise SymbolGateError(
            f"symbol {symbol!r} not allowed on {profile} live-size contour "
            f"(coins={live_size_coins_for_profile(profile)})"
        )
    return found
