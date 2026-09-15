"""Gear 2.2 dummy backtest: policy (manager) + replay (run). Observation only."""

from __future__ import annotations

from research.gear22_backtest.params_frozen import (
    DEFAULT_OBSERVE_PARAMS,
    FROZEN,
    PREVIOUS,
)
from research.gear22_backtest.policy import (
    Action,
    Decision,
    DummyParams,
    FeatureSnapshot,
    PolicyParams,
    PolicyState,
    Side,
    decide,
    decide_close,
    decide_open,
    potential_profit_pp,
)

try:
    from research.gear22_backtest.replay import (
        ClosedTrade,
        OpenPosition,
        ReplayResult,
        SlotMode,
        replay_frame,
        replay_hive,
        replay_path,
    )
    _REPLAY_AVAILABLE = True
except ImportError:
    _REPLAY_AVAILABLE = False
    ClosedTrade = None  # type: ignore[misc,assignment]
    OpenPosition = None  # type: ignore[misc,assignment]
    ReplayResult = None  # type: ignore[misc,assignment]
    SlotMode = None  # type: ignore[misc,assignment]
    replay_frame = None  # type: ignore[misc,assignment]
    replay_hive = None  # type: ignore[misc,assignment]
    replay_path = None  # type: ignore[misc,assignment]

__all__ = [
    "Action",
    "ClosedTrade",
    "DEFAULT_OBSERVE_PARAMS",
    "Decision",
    "DummyParams",
    "FROZEN",
    "FeatureSnapshot",
    "OpenPosition",
    "PolicyParams",
    "PolicyState",
    "PREVIOUS",
    "ReplayResult",
    "Side",
    "SlotMode",
    "decide",
    "decide_close",
    "decide_open",
    "potential_profit_pp",
    "replay_frame",
    "replay_hive",
    "replay_path",
]
