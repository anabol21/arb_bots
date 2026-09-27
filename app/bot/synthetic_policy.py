"""Synthetic roll policy for profile ``synthetic_roll``.

One roll per tick for the whole pool (not per coin). No I/O and no network.
The manager still applies the K=1 slot and the existing size gate.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence


@dataclass(frozen=True)
class SyntheticDecision:
    """One pool-level action. ``hold`` is not a send."""

    action: str  # open | close | hold
    coin: str = ""
    side: str = ""
    roll: int = -1


def decide_synthetic_roll(
    *,
    slot: object,
    coins: Sequence[str],
    rng: random.Random,
) -> SyntheticDecision:
    """Roll 0..100 inclusive.

    - 17 and slot flat → ``open`` one uniform pool coin, uniform long/short
    - 31 and slot holding a coin → ``close`` that coin and side
    - any other roll, 17 while a position is open, or 31 while flat → ``hold``
    """
    pool = [str(c).strip().upper() for c in coins if str(c).strip()]
    roll = int(rng.randint(0, 100))
    pos = getattr(slot, "position", None)
    pending = bool(getattr(slot, "pending", False))
    flat = pos is None and not pending
    if roll == 17 and flat and pool:
        coin = str(rng.choice(pool))
        side = str(rng.choice(("long", "short")))
        return SyntheticDecision(action="open", coin=coin, side=side, roll=roll)
    if roll == 31 and pos is not None and not pending:
        coin = getattr(pos, "base_coin", None)
        if coin is None:
            coin = getattr(pos, "coin", "")
        side = getattr(pos, "side", "")
        return SyntheticDecision(
            action="close",
            coin=str(coin).strip().upper(),
            side=str(side).strip().lower(),
            roll=roll,
        )
    return SyntheticDecision(action="hold", roll=roll)


def make_synthetic_decide(
    coins: Sequence[str],
    rng: random.Random,
):
    """Closure the manager calls once per tick. ``rng`` is injected (tests pass a seed)."""
    default_pool = tuple(str(c).strip().upper() for c in coins if str(c).strip())

    def decide_fn(
        *,
        slot: object,
        coin_order: Optional[Sequence[str]] = None,
        **_kwargs: object,
    ) -> SyntheticDecision:
        pool = coin_order if coin_order else default_pool
        return decide_synthetic_roll(slot=slot, coins=pool, rng=rng)

    return decide_fn


def synthetic_live_gates(env: Optional[Mapping[str, str]] = None) -> bool:
    """Same fail-closed live flags as Contour B. Off → local sender, no sockets."""
    e = env if env is not None else os.environ
    broker = str(e.get("BBOT_BROKER") or "").strip().lower()
    venue = str(e.get("VENUE") or "").strip().lower()
    live_orders = str(e.get("LIVE_ORDERS") or "0").strip().lower()
    return (
        broker in {"private_live", "live"}
        and venue == "live"
        and live_orders in {"1", "true", "on", "yes"}
    )
