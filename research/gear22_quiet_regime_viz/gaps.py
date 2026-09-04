"""Gap detection for gappy L1 tick streams."""

from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd

# Default: mark a hole when consecutive ticks are more than 30s apart.
DEFAULT_GAP_THRESHOLD_MS = 30_000


def detect_gap_intervals(
    ts_ms: Sequence[int] | np.ndarray | pd.Series,
    *,
    gap_threshold_ms: int = DEFAULT_GAP_THRESHOLD_MS,
) -> list[tuple[int, int]]:
    """Return ``[gap_start_ms, gap_end_ms)`` where inter-tick delta exceeds threshold.

    ``gap_start`` is the timestamp of the last tick before the hole;
    ``gap_end`` is the timestamp of the first tick after the hole.
    Empty / single-tick input → no gaps.
    """
    if gap_threshold_ms <= 0:
        raise ValueError("gap_threshold_ms must be > 0")
    ts = np.asarray(ts_ms, dtype="int64")
    if ts.size < 2:
        return []
    order = np.argsort(ts, kind="mergesort")
    ts = ts[order]
    deltas = np.diff(ts)
    out: list[tuple[int, int]] = []
    for i, d in enumerate(deltas):
        if int(d) > int(gap_threshold_ms):
            out.append((int(ts[i]), int(ts[i + 1])))
    return out


def gaps_to_vrect_datetimes(
    gaps: Sequence[tuple[int, int]],
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Convert gap ms intervals to UTC timestamps for Plotly ``vrect``."""
    return [
        (
            pd.to_datetime(a, unit="ms", utc=True),
            pd.to_datetime(b, unit="ms", utc=True),
        )
        for a, b in gaps
    ]
