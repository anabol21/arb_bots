"""Gear 2.2 whole-market floor metric watcher (observation / would_send journal).

Builds the locked tf-select α25 floor for every take=yes coin, long and short
independently. No orders, no private broker, not a live threshold.

Floor formula: ``research.gear22_quiet_regime_viz.floors.compute_chosen_floor``.
See ``docs/gear22-floor-watcher.md`` and ``docs/gear22-floor-metric.md``.
"""

from __future__ import annotations

from research.gear22_floor_watcher.builder import (
    FORMULA_ID,
    SNAPSHOT_COLUMNS,
    build_market_floor_snapshot,
    build_side_floor_rows,
)
from research.gear22_floor_watcher.journal import (
    JOURNAL_KEY_COLS,
    merge_snapshot_frames,
    read_journal,
    write_journal,
)

__all__ = [
    "FORMULA_ID",
    "JOURNAL_KEY_COLS",
    "SNAPSHOT_COLUMNS",
    "build_market_floor_snapshot",
    "build_side_floor_rows",
    "merge_snapshot_frames",
    "read_journal",
    "write_journal",
]
