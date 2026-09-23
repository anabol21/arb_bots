"""Read one coin × 5-minute spread-tick window from local cache or VPS.

Does not download the backup tree onto the Mac. SSH mode filters on the
remote host and returns only the small coin slice.
"""

from __future__ import annotations

from research.tick_window_viz.delay import message_delay_ms
from research.tick_window_viz.fetch import load_tick_window
from research.tick_window_viz.plot import build_tick_window_figure
from research.tick_window_viz.window import (
    FIVE_MIN_MS,
    compacted_filename,
    compacted_names_covering,
    is_five_min_aligned,
    normalize_coin,
    parse_window_start,
    window_bounds_ms,
)

__all__ = [
    "FIVE_MIN_MS",
    "build_tick_window_figure",
    "compacted_filename",
    "compacted_names_covering",
    "is_five_min_aligned",
    "load_tick_window",
    "message_delay_ms",
    "normalize_coin",
    "parse_window_start",
    "window_bounds_ms",
]
