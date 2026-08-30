"""Testnet-only Broker mount for after GREEN would_send.

Does not send. Refuses live venue and LIVE_ORDERS. Delegates dual-leg
journal to StubBroker (would_send=true, send=false) until an explicit
testnet ladder is run from the B-private chat.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from app.bot.journal import JournalWriter
from app.bot.private.venue import live_orders_enabled, resolve_venue
from app.bot.stub_broker import StubBroker


def make_testnet_broker(
    *,
    data_root: Path,
    journal: JournalWriter,
    trade_lat_ms: int,
    notional_usdt: float,
    log: Callable[[str], None],
    env: Mapping[str, str],
) -> StubBroker:
    venue = resolve_venue(env)
    if venue == "live":
        raise RuntimeError(
            "BBOT_BROKER=private_testnet refuses VENUE=live; "
            "live send needs a separate explicit user phrase"
        )
    if venue != "testnet":
        raise RuntimeError(
            f"BBOT_BROKER=private_testnet requires VENUE=testnet, got {venue!r}"
        )
    if live_orders_enabled(env):
        raise RuntimeError(
            "BBOT_BROKER=private_testnet refuses LIVE_ORDERS=1; "
            "this mount is would_send / testnet-auth only"
        )
    log("broker_mount | kind=private_testnet | venue=testnet | send=false")
    return StubBroker(
        data_root=data_root,
        journal=journal,
        trade_lat_ms=trade_lat_ms,
        notional_usdt=notional_usdt,
        log=log,
    )
