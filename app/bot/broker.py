"""Broker factory. Default is the stub (would_send, send=false).

``BBOT_BROKER=stub`` (default): ``StubBroker``.
``BBOT_BROKER=private_testnet``: testnet-only adapter; refuses live venue
and does not enable send.
``BBOT_BROKER=private_live``: live sender. Requires ``VENUE=live`` and
``LIVE_ORDERS=1``. Default send path is trivial dual-leg (Contour B /
bybit_ws queue→ws.send). Full W6 is opt-in via
``BBOT_PRIVATE_SEND_PATH=w6`` + ``BBOT_PRIVATE_W6=1``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from app.bot.journal import JournalWriter
from app.bot.stub_broker import StubBroker


class Broker(Protocol):
    position: Optional[str]
    held_coin: Optional[str]
    pending: Any

    def can_open(self) -> bool: ...
    def has_pending(self) -> bool: ...
    def place(self, **kwargs: Any) -> Optional[str]: ...
    def on_valid_tick(self, **kwargs: Any) -> bool: ...
    def abort_pending(self, **kwargs: Any) -> None: ...


def make_broker(
    *,
    data_root: Path,
    journal: JournalWriter,
    trade_lat_ms: int,
    notional_usdt: float,
    log: Callable[[str], None],
    env: Optional[dict[str, str]] = None,
) -> StubBroker:
    e = env if env is not None else os.environ
    kind = (e.get("BBOT_BROKER") or "stub").strip().lower()
    if kind in ("", "stub"):
        return StubBroker(
            data_root=data_root,
            journal=journal,
            trade_lat_ms=trade_lat_ms,
            notional_usdt=notional_usdt,
            log=log,
        )
    if kind == "private_testnet":
        from app.bot.private.testnet_broker import make_testnet_broker

        return make_testnet_broker(
            data_root=data_root,
            journal=journal,
            trade_lat_ms=trade_lat_ms,
            notional_usdt=notional_usdt,
            log=log,
            env=e,
        )
    if kind in ("private_live", "live"):
        from app.bot.private.live_broker import make_live_broker

        return make_live_broker(
            data_root=data_root,
            journal=journal,
            trade_lat_ms=trade_lat_ms,
            notional_usdt=notional_usdt,
            log=log,
            env=e,
        )
    raise ValueError(
        f"BBOT_BROKER must be stub|private_testnet|private_live, got {kind!r}"
    )
