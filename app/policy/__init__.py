"""Portable gear-1.0 policy (shared by historical M and live B-bot).

``model.ipynb`` still keeps its own VARIATION/HYPER and ``run_backtest`` copy
for the frozen historical baseline; this package is the extract for callers.
"""

from app.policy.features import (
    CausalMaWindow,
    MaSample,
    latency_ok_for_avg,
    spread_long_pct,
    spread_short_pct,
)
from app.policy.gear2_market_manager import MarketDecision, MarketState, decide_market_tick
from app.policy.trade_manager import (
    DEFAULT_HYPER,
    DEFAULT_VARIATION,
    GEAR2_WOULD_SEND_COINS,
    GEAR2_WOULD_SEND_HYPER,
    GEAR2_WOULD_SEND_VARIATION,
    SIGNAL_TEST_COINS,
    SIGNAL_TEST_VARIATION,
    BotState,
    Intent,
    IntentAction,
    Side,
    TickView,
    decide,
    hyper_for_profile,
    update_causal_ma,
    variation_for_profile,
)

__all__ = [
    "BotState",
    "CausalMaWindow",
    "DEFAULT_HYPER",
    "DEFAULT_VARIATION",
    "GEAR2_WOULD_SEND_COINS",
    "GEAR2_WOULD_SEND_HYPER",
    "GEAR2_WOULD_SEND_VARIATION",
    "SIGNAL_TEST_COINS",
    "SIGNAL_TEST_VARIATION",
    "Intent",
    "IntentAction",
    "MaSample",
    "MarketDecision",
    "MarketState",
    "Side",
    "TickView",
    "decide",
    "decide_market_tick",
    "hyper_for_profile",
    "latency_ok_for_avg",
    "spread_long_pct",
    "spread_short_pct",
    "update_causal_ma",
    "variation_for_profile",
]
