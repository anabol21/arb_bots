"""Pure request contract for the bounded EV2 live synthetic experiment.

Parsing this contract does not open sockets, authorize orders, or bypass the
runtime's ``ev2_live_execution_adapter_not_integrated`` stop gate. The future
fill-driven adapter must enforce it again at the durable send boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping

from app.bot.theta_trade_manager import GEAR22_HTML_TOP30, SYNTHETIC_POLICY_MODE


class LiveSyntheticSourceError(ValueError):
    """Invalid or incompletely armed EV2-13B source request."""


@dataclass(frozen=True)
class LiveSyntheticSourceRequest:
    coin: str
    notional_usdt_per_leg: Decimal
    max_round_trips: int
    max_planned_dual_leg_submissions: int

    @property
    def execution_coin_order(self) -> tuple[str, ...]:
        """Keep the public 30-coin feed but allow this one execution coin."""
        return (self.coin,)


def parse_live_synthetic_source_request(env: Mapping[str, str]) -> LiveSyntheticSourceRequest:
    """Require an exact, explicit six-submission / three-cycle request.

    This is a configuration contract, not an order-capability lease. In
    particular, emergency reduce-only recovery is outside the planned budget
    and requires independent, confirmed residual exposure.
    """
    required = {
        "BBOT_EV2_LIVE_SYNTHETIC_SOURCE": "1",
        "BBOT_POLICY_MODE": SYNTHETIC_POLICY_MODE,
        "BBOT_PROFILE": "gear22_live_canary",
        "BBOT_BROKER": "private_live",
        "VENUE": "live",
        "LIVE_ORDERS": "1",
        "BBOT_THETA_LIVE_SEND": "1",
        "BBOT_SLOT_K": "1",
        "BBOT_EV2_MAX_ROUND_TRIPS": "3",
        "BBOT_EV2_MAX_DUAL_LEG_SUBMISSIONS": "6",
        "BBOT_SYNTHETIC_ROLL_SEED": "7",
    }
    for key, expected in required.items():
        if str(env.get(key) or "").strip() != expected:
            raise LiveSyntheticSourceError(f"ev2_live_source_requires_{key.lower()}")
    for key in ("BBOT_EV2_SHADOW", "BBOT_EV2_AUDIT"):
        if str(env.get(key) or "0").strip().lower() not in {"0", "false", "off", "no"}:
            raise LiveSyntheticSourceError(f"ev2_live_source_forbids_{key.lower()}")

    feed_coins = tuple(str(c).strip().upper() for c in str(env.get("BBOT_COINS") or "").split(","))
    if feed_coins != GEAR22_HTML_TOP30:
        raise LiveSyntheticSourceError("ev2_live_source_requires_frozen_top30")
    coin = str(env.get("BBOT_EV2_LIVE_COIN") or "").strip().upper()
    if coin not in GEAR22_HTML_TOP30:
        raise LiveSyntheticSourceError("ev2_live_source_coin_not_in_top30")
    try:
        notional = Decimal(str(env.get("BBOT_NOTIONAL_USDT") or ""))
    except InvalidOperation as exc:
        raise LiveSyntheticSourceError("ev2_live_source_invalid_notional") from exc
    if notional != Decimal("10"):
        raise LiveSyntheticSourceError("ev2_live_source_requires_ten_usdt_per_leg")
    return LiveSyntheticSourceRequest(
        coin=coin,
        notional_usdt_per_leg=notional,
        max_round_trips=3,
        max_planned_dual_leg_submissions=6,
    )
