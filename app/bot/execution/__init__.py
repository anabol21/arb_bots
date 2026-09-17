"""Pure execution v2 domain layer.

No sockets, secrets, production writes, systemd or live broker access.
"""

from app.bot.execution.contracts import (
    SCHEMA_VERSION,
    ContractValidationError,
    ExecutionEvent,
    ExecutionEventType,
    IntentAction,
    LegPlan,
    LegState,
    LegStatus,
    SpreadDirection,
    SpreadState,
    SpreadStatus,
    TradeIntent,
    Venue,
    derive_client_id,
)
from app.bot.execution.state_machine import (
    InvalidTransition,
    apply_event,
    apply_events,
    assert_invariants,
    initial_spread_state,
    is_proven_flat,
    needs_reconciliation,
    opens_allowed,
)

__all__ = [
    "SCHEMA_VERSION",
    "ContractValidationError",
    "ExecutionEvent",
    "ExecutionEventType",
    "IntentAction",
    "InvalidTransition",
    "LegPlan",
    "LegState",
    "LegStatus",
    "SpreadDirection",
    "SpreadState",
    "SpreadStatus",
    "TradeIntent",
    "Venue",
    "apply_event",
    "apply_events",
    "assert_invariants",
    "derive_client_id",
    "initial_spread_state",
    "is_proven_flat",
    "needs_reconciliation",
    "opens_allowed",
]
