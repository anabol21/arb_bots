"""Schema contracts for persisted spread events."""

from app.schema.lean_event import (
    BAR_INTERVAL_MS,
    LEAN_BAR_5M_BODY_COLS,
    LEAN_TICK_BODY_COLS,
    LEAN_TICK_BOOK_COLS,
)
from app.schema.parquet_layout import (
    PARTITION_DATE_COL,
    PARTITION_KEYS,
    spreads_partition_dir,
)
from app.schema.spread_event import (
    SPREAD_EVENT_BODY_COLS,
    SPREAD_EVENT_BOOK_COLS,
    active_tick_body_cols,
    lean_schema_enabled,
    tick_schema_mode,
)

__all__ = [
    "BAR_INTERVAL_MS",
    "LEAN_BAR_5M_BODY_COLS",
    "LEAN_TICK_BODY_COLS",
    "LEAN_TICK_BOOK_COLS",
    "PARTITION_DATE_COL",
    "PARTITION_KEYS",
    "SPREAD_EVENT_BODY_COLS",
    "SPREAD_EVENT_BOOK_COLS",
    "active_tick_body_cols",
    "lean_schema_enabled",
    "spreads_partition_dir",
    "tick_schema_mode",
]
