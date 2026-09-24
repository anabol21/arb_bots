"""Schema contracts for persisted spread events."""

from app.schema.hl_l1 import (
    HL_L1_BODY_COLS,
    HL_L1_BOOK_COLS,
    HL_L1_EXCLUDED_FROM_BODY,
    HL_L1_SCHEMA_NAME,
    HL_L1_TS_COLS,
    hl_l1_body_is_exact,
)
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
from app.schema.ws_gap import (
    WS_GAP_REQUIRED_FIELDS,
    WS_GAP_SCHEMA_VERSION,
    encode_gap_record,
    gaps_jsonl_path,
    utc_event_date,
    validate_gap_record,
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
    "HL_L1_BODY_COLS",
    "HL_L1_BOOK_COLS",
    "HL_L1_EXCLUDED_FROM_BODY",
    "HL_L1_SCHEMA_NAME",
    "HL_L1_TS_COLS",
    "hl_l1_body_is_exact",
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
    "WS_GAP_REQUIRED_FIELDS",
    "WS_GAP_SCHEMA_VERSION",
    "encode_gap_record",
    "gaps_jsonl_path",
    "utc_event_date",
    "validate_gap_record",
]
