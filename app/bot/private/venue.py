"""Venue gates and REST endpoint table for B-private.

Default is testnet/demo. Live send requires VENUE=live AND LIVE_ORDERS=1.
Stage-1 harness never enables send.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional


DEFAULT_VENUE = "testnet"
DEFAULT_LIVE_ORDERS = "0"

# Explicit allowlists — order/place paths must never appear in stage-1 clients.
BYBIT_TESTNET_REST = "https://api-testnet.bybit.com"
BYBIT_LIVE_REST = "https://api.bybit.com"
OKX_REST = "https://www.okx.com"

# Private / trade WebSocket URLs (construction only; default CLI never connects).
BYBIT_TESTNET_PRIVATE_WS = "wss://stream-testnet.bybit.com/v5/private"
BYBIT_TESTNET_TRADE_WS = "wss://stream-testnet.bybit.com/v5/trade"
BYBIT_LIVE_PRIVATE_WS = "wss://stream.bybit.com/v5/private"
BYBIT_LIVE_TRADE_WS = "wss://stream.bybit.com/v5/trade"
OKX_PRIVATE_WS = "wss://ws.okx.com:8443/ws/v5/private"
OKX_BUSINESS_WS = "wss://ws.okx.com:8443/ws/v5/business"
# Public one-symbol L1 (W4 pricing only; never used as private/trade transport).
BYBIT_LIVE_PUBLIC_LINEAR_WS = "wss://stream.bybit.com/v5/public/linear"
OKX_PUBLIC_WS = "wss://ws.okx.com:8443/ws/v5/public"

BYBIT_ACCOUNT_PATH = "/v5/account/wallet-balance"
OKX_ACCOUNT_PATH = "/api/v5/account/balance"

# Optional single-symbol hint for later harness stages (not subscribed in stage 1).
DEFAULT_SYMBOL_HINT = "BTC"


def _truthy(raw: Optional[str]) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def resolve_venue(env: Optional[Mapping[str, str]] = None) -> str:
    e = env if env is not None else os.environ
    raw = (e.get("VENUE") or DEFAULT_VENUE).strip().lower()
    if raw not in {"testnet", "live"}:
        raise ValueError(f"VENUE must be 'testnet' or 'live', got {raw!r}")
    return raw


def live_orders_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    e = env if env is not None else os.environ
    return _truthy(e.get("LIVE_ORDERS", DEFAULT_LIVE_ORDERS))


def assert_orders_disabled(env: Optional[Mapping[str, str]] = None) -> None:
    """Stage-1 and default runtime: refuse any order-capable flag combination."""
    e = env if env is not None else os.environ
    if live_orders_enabled(e):
        raise RuntimeError(
            "LIVE_ORDERS is enabled; stage-1 read-only harness refuses to run"
        )


def assert_stage1_venue(env: Optional[Mapping[str, str]] = None) -> str:
    """Stage-1 harness: only testnet/demo; never live."""
    e = env if env is not None else os.environ
    venue = resolve_venue(e)
    assert_orders_disabled(e)
    if venue != "testnet":
        raise RuntimeError(
            f"stage-1 harness requires VENUE=testnet (got {venue!r}); "
            "live auth/send is a later gate"
        )
    return venue


def send_allowed(env: Optional[Mapping[str, str]] = None) -> bool:
    """True only when both live venue and LIVE_ORDERS=1. Stage-1 never uses this.

    R0 note: even when True, no order endpoints exist yet; a later approval-bound
    sender (R3) is required before any live place/cancel/amend.
    """
    e = env if env is not None else os.environ
    return resolve_venue(e) == "live" and live_orders_enabled(e)


def assert_live_readonly(env: Optional[Mapping[str, str]] = None) -> str:
    """VENUE=live with LIVE_ORDERS=0: credentials may load; send surface closed."""
    e = env if env is not None else os.environ
    venue = resolve_venue(e)
    if venue != "live":
        raise RuntimeError(
            f"live read-only gate requires VENUE=live (got {venue!r})"
        )
    if live_orders_enabled(e):
        raise RuntimeError(
            "LIVE_ORDERS is enabled; live read-only gate refuses to run"
        )
    if send_allowed(e):
        raise RuntimeError("send_allowed unexpectedly True under live read-only")
    return venue


@dataclass(frozen=True)
class VenueEndpoints:
    venue: str
    bybit_rest: str
    okx_rest: str
    okx_simulated_trading: bool
    bybit_private_ws: str
    bybit_trade_ws: str
    okx_private_ws: str
    okx_business_ws: str
    bybit_public_ws: str = BYBIT_LIVE_PUBLIC_LINEAR_WS
    okx_public_ws: str = OKX_PUBLIC_WS
    bybit_account_path: str = BYBIT_ACCOUNT_PATH
    okx_account_path: str = OKX_ACCOUNT_PATH


def endpoints_for_venue(venue: str) -> VenueEndpoints:
    if venue == "testnet":
        return VenueEndpoints(
            venue="testnet",
            bybit_rest=BYBIT_TESTNET_REST,
            okx_rest=OKX_REST,
            okx_simulated_trading=True,
            bybit_private_ws=BYBIT_TESTNET_PRIVATE_WS,
            bybit_trade_ws=BYBIT_TESTNET_TRADE_WS,
            okx_private_ws=OKX_PRIVATE_WS,
            okx_business_ws=OKX_BUSINESS_WS,
            bybit_public_ws=BYBIT_LIVE_PUBLIC_LINEAR_WS,
            okx_public_ws=OKX_PUBLIC_WS,
        )
    if venue == "live":
        return VenueEndpoints(
            venue="live",
            bybit_rest=BYBIT_LIVE_REST,
            okx_rest=OKX_REST,
            okx_simulated_trading=False,
            bybit_private_ws=BYBIT_LIVE_PRIVATE_WS,
            bybit_trade_ws=BYBIT_LIVE_TRADE_WS,
            okx_private_ws=OKX_PRIVATE_WS,
            okx_business_ws=OKX_BUSINESS_WS,
            bybit_public_ws=BYBIT_LIVE_PUBLIC_LINEAR_WS,
            okx_public_ws=OKX_PUBLIC_WS,
        )
    raise ValueError(f"unknown venue: {venue!r}")


def default_secret_file_for_venue(venue: str) -> str:
    if venue == "testnet":
        return "/etc/spread/bbot-private-testnet.env"
    if venue == "live":
        return "/etc/spread/bbot-private-live.env"
    raise ValueError(f"unknown venue: {venue!r}")


def testnet_alias_secret_file() -> str:
    return "/etc/spread/bbot-private.env"
