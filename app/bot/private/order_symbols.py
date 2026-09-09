"""Allowlisted live USDT perpetual venues/symbols for R3 planning.

No network. Spot/options and arbitrary symbols are rejected.
BTC remains the W3/W4/W5 default. W6 adds one extra matched pair (TRUMP).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from app.policy.trade_manager import (
    CANARY_WAL_EDEN_COINS as CANARY_WAL_EDEN_COINS,
    GEAR2_WOULD_SEND_COINS as GEAR2_WOULD_SEND_COINS,
    LIVE_SIZE_COINS as LIVE_SIZE_COINS,
    SIGNAL_TEST_COINS as SIGNAL_TEST_COINS,
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


# Public L1 and private WS share this pool. W6 TRUMP is harness-only when no
# profile/runtime coins are set — never a silent leftover on canary/live-size.
_PROFILE_DEFAULT_COINS: Mapping[str, tuple[str, ...]] = {
    "canary_wal_eden": CANARY_WAL_EDEN_COINS,
    "gear2_would_send": GEAR2_WOULD_SEND_COINS,
    "signal_test": SIGNAL_TEST_COINS,
}


@dataclass(frozen=True)
class PrivateSubscribePool:
    """Native Bybit/OKX instruments for one live unit's coin allowlist."""

    coins: tuple[str, ...]
    bybit_symbols: tuple[str, ...]
    okx_symbols: tuple[str, ...]

    @property
    def bybit_symbol(self) -> str:
        return self.bybit_symbols[0]

    @property
    def okx_symbol(self) -> str:
        return self.okx_symbols[0]


def normalize_bbot_profile(raw: object) -> str:
    name = str(raw or "").strip().lower()
    if name in {"", "default"}:
        return "gear1"
    if name == "gear2":
        return "gear2_would_send"
    if name == "canary":
        return "canary_wal_eden"
    return name


def native_bybit_linear(coin: str) -> str:
    return f"{str(coin).strip().upper()}USDT"


def native_okx_swap(coin: str) -> str:
    return f"{str(coin).strip().upper()}-USDT-SWAP"


def parse_runtime_coins(raw: object) -> tuple[str, ...]:
    text = str(raw or "").strip()
    if not text:
        return ()
    return tuple(part.strip().upper() for part in text.split(",") if part.strip())


def coins_from_runtime_env(
    env: Optional[Mapping[str, str]] = None,
    *,
    coins: Optional[Sequence[str]] = None,
) -> tuple[str, ...]:
    """Same coin allowlist as public L1 books for the running profile.

    ``BBOT_COINS`` wins when set. Otherwise the policy-mode profile default
    (canary → WAL+EDEN, gear2 → BTC/ETH/SOL/XRP, live-size contour via
    ``BBOT_COINS=SOL,XRP``). Empty when no profile pool is configured (W6
    harness may then pass TRUMP natives explicitly).
    """
    if coins is not None:
        parsed = tuple(str(c).strip().upper() for c in coins if str(c).strip())
        return _assert_profile_coins(parsed, env)
    e = dict(env or {})
    from_env = parse_runtime_coins(e.get("BBOT_COINS"))
    if from_env:
        return _assert_profile_coins(from_env, e)
    profile = normalize_bbot_profile(e.get("BBOT_PROFILE"))
    mode = str(e.get("BBOT_MODE") or "policy").strip().lower()
    if mode != "policy":
        return ()
    defaults = _PROFILE_DEFAULT_COINS.get(profile)
    if not defaults:
        return ()
    return defaults


def _assert_profile_coins(
    coins: tuple[str, ...],
    env: Optional[Mapping[str, str]],
) -> tuple[str, ...]:
    profile = normalize_bbot_profile((env or {}).get("BBOT_PROFILE"))
    if profile == "canary_wal_eden":
        bad = [c for c in coins if not live_size_coin_allowed(c, profile)]
        if bad:
            raise SymbolGateError(
                f"canary_wal_eden refuses coins {bad}; allowed WAL,EDEN"
            )
    return coins


def resolve_private_subscribe_pool(
    env: Optional[Mapping[str, str]] = None,
    *,
    coins: Optional[Sequence[str]] = None,
    bybit_symbol: Optional[str] = None,
    okx_symbol: Optional[str] = None,
    bybit_symbols: Optional[Sequence[str]] = None,
    okx_symbols: Optional[Sequence[str]] = None,
) -> PrivateSubscribePool:
    """Instrument set for private orders/positions (same coins as public L1).

    Profile/runtime coins are the source of truth. Explicit TRUMP (or any
    other native) is rejected when it is not in that pool. Harness paths
    without a profile pool still accept explicit W6 natives.
    """
    e = dict(env or {})
    pool_coins = coins_from_runtime_env(e, coins=coins)
    if pool_coins:
        bybit = tuple(native_bybit_linear(c) for c in pool_coins)
        okx = tuple(native_okx_swap(c) for c in pool_coins)
        stray: list[str] = []
        for raw, allowed in (
            (bybit_symbol, bybit),
            (okx_symbol, okx),
        ):
            if raw and str(raw).strip() not in allowed:
                stray.append(str(raw).strip())
        for seq, allowed in (
            (bybit_symbols, bybit),
            (okx_symbols, okx),
        ):
            if seq is None:
                continue
            for raw in seq:
                if str(raw).strip() not in allowed:
                    stray.append(str(raw).strip())
        if stray:
            raise SymbolGateError(
                "private subscribe symbols "
                f"{tuple(stray)} are not in the profile/runtime coin pool "
                f"{pool_coins} (public L1 and private WS share one allowlist)"
            )
        return PrivateSubscribePool(
            coins=pool_coins,
            bybit_symbols=bybit,
            okx_symbols=okx,
        )

    if bybit_symbols is not None or okx_symbols is not None:
        bybit = tuple(
            str(s).strip() for s in (bybit_symbols or ()) if str(s).strip()
        )
        okx = tuple(str(s).strip() for s in (okx_symbols or ()) if str(s).strip())
        if not bybit or not okx:
            raise SymbolGateError(
                "harness private subscribe requires both bybit_symbols and okx_symbols"
            )
    else:
        bybit_one = str(bybit_symbol or "").strip() or "TRUMPUSDT"
        okx_one = str(okx_symbol or "").strip() or "TRUMP-USDT-SWAP"
        bybit = (bybit_one,)
        okx = (okx_one,)
    inferred = []
    for native in bybit:
        inferred.append(
            native[:-4] if native.endswith("USDT") and "-" not in native else native
        )
    return PrivateSubscribePool(
        coins=tuple(inferred),
        bybit_symbols=bybit,
        okx_symbols=okx,
    )
