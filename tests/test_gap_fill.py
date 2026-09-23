"""Fill-across-gap helper: flag off preserves baseline; flag on blocks jumps."""

from __future__ import annotations

import unittest

from research.gap_fill import (
    DEFAULT_GAP_FILL_SLACK_MS,
    fill_delay_exceeds_slack,
    reject_fill_across_gap,
)


def _resolve_fill_index(
    signal_i: int,
    ts_ms: list[float],
    *,
    trade_lat_ms: float,
    reject_across_gap: bool = False,
    slack_ms: float = DEFAULT_GAP_FILL_SLACK_MS,
    gaps: list[dict] | None = None,
) -> int | None:
    delay = float(trade_lat_ms)
    if delay <= 0:
        return signal_i
    target = float(ts_ms[signal_i]) + delay
    j = signal_i + 1
    n = len(ts_ms)
    while j < n and ts_ms[j] < target:
        j += 1
    if j >= n:
        return None
    if reject_fill_across_gap(
        float(ts_ms[signal_i]),
        float(ts_ms[j]),
        trade_lat_ms=trade_lat_ms,
        slack_ms=slack_ms,
        enabled=reject_across_gap,
        gaps=gaps,
    ):
        return None
    return j


class GapFillTests(unittest.TestCase):
    def test_flag_off_allows_jump_and_matches_baseline(self) -> None:
        ts = [0.0, 50.0, 5100.0]
        off = _resolve_fill_index(0, ts, trade_lat_ms=100.0, reject_across_gap=False)
        self.assertEqual(off, 2)
        self.assertFalse(
            fill_delay_exceeds_slack(
                0.0, 5100.0, trade_lat_ms=100.0, enabled=False
            )
        )

    def test_flag_on_rejects_jump_much_larger_than_trade_lat(self) -> None:
        ts = [0.0, 50.0, 5100.0]
        on = _resolve_fill_index(0, ts, trade_lat_ms=100.0, reject_across_gap=True)
        self.assertIsNone(on)
        self.assertTrue(
            fill_delay_exceeds_slack(
                0.0, 5100.0, trade_lat_ms=100.0, enabled=True
            )
        )

    def test_flag_on_keeps_fill_within_slack(self) -> None:
        ts = [0.0, 250.0]
        on = _resolve_fill_index(0, ts, trade_lat_ms=100.0, reject_across_gap=True)
        self.assertEqual(on, 1)

    def test_recorded_gap_overlap_rejects_when_enabled(self) -> None:
        gaps = [{"t_down_ms": 50, "t_up_ms": 4000}]
        self.assertTrue(
            reject_fill_across_gap(
                0.0,
                200.0,
                trade_lat_ms=100.0,
                slack_ms=1000.0,
                enabled=True,
                gaps=gaps,
            )
        )
        self.assertFalse(
            reject_fill_across_gap(
                0.0,
                200.0,
                trade_lat_ms=100.0,
                enabled=False,
                gaps=gaps,
            )
        )


if __name__ == "__main__":
    unittest.main()
