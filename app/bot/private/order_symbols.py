"""Allowlisted live USDT perpetual venues/symbols for R3 planning.

No network. Spot/options and arbitrary symbols are rejected.
BTC remains the W3/W4/W5 default. W6 adds one extra matched pair (TRUMP).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

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
