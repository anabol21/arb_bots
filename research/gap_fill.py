"""Fill honesty across a timestamp jump larger than Trade_Lat.

Default off: baseline trade count is unchanged. When enabled, refuse a fill
if ``fill_ts - signal_ts > Trade_Lat + slack`` (default slack 1 s), or if a
recorded WS gap interval overlaps ``(signal_ts, fill_ts]``.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional

DEFAULT_GAP_FILL_SLACK_MS = 1000.0


def fill_delay_exceeds_slack(
    signal_ts_ms: float,
    fill_ts_ms: float,
    *,
    trade_lat_ms: float,
    slack_ms: float = DEFAULT_GAP_FILL_SLACK_MS,
    enabled: bool = False,
) -> bool:
    """True when the fill would treat a time hole as Trade_Lat."""
    if not enabled:
        return False
    delay = float(fill_ts_ms) - float(signal_ts_ms)
    return delay > (float(trade_lat_ms) + float(slack_ms))


def gap_overlaps_signal_fill(
    signal_ts_ms: float,
    fill_ts_ms: float,
    t_down_ms: float,
    t_up_ms: float,
) -> bool:
    """True when ``[t_down, t_up]`` intersects ``(signal_ts, fill_ts]``."""
    return float(t_down_ms) < float(fill_ts_ms) and float(t_up_ms) > float(signal_ts_ms)


def reject_fill_across_gap(
    signal_ts_ms: float,
    fill_ts_ms: float,
    *,
    trade_lat_ms: float,
    slack_ms: float = DEFAULT_GAP_FILL_SLACK_MS,
    enabled: bool = False,
    gaps: Optional[Iterable[Mapping[str, object]]] = None,
) -> bool:
    """Refuse fill across a jump or a recorded reconnect interval."""
    if not enabled:
        return False
    if fill_delay_exceeds_slack(
        signal_ts_ms,
        fill_ts_ms,
        trade_lat_ms=trade_lat_ms,
        slack_ms=slack_ms,
        enabled=True,
    ):
        return True
    if gaps is None:
        return False
    for gap in gaps:
        t_down = gap.get("t_down_ms")
        t_up = gap.get("t_up_ms")
        if t_down is None or t_up is None:
            continue
        if gap_overlaps_signal_fill(
            signal_ts_ms, fill_ts_ms, float(t_down), float(t_up)
        ):
            return True
    return False
