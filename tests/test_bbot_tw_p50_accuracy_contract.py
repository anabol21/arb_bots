"""Independent TW accuracy contract; capped reservoirs are approximate."""

from __future__ import annotations

import json
import math
import random
import tempfile
import unittest
from pathlib import Path
from typing import Sequence

from app.bot.floor_watcher import LiveFloorObserver
from app.bot.theta_screener import LiveThetaScreener, ThetaJournalWriter
from app.bot.tw_p50_watcher import (
    LiveTwP50Observer,
    TwP50JournalWriter,
    tw_p50_window,
)


def uncapped_tw_median(
    samples: Sequence[tuple[int, float]], *, now_ms: int, window_ms: int = 60_000
) -> tuple[float | None, int, float]:
    """Clip hold intervals, then invert their value-duration histogram.

    Inputs are finite ticks; there is no cap, reservoir or production helper.
    Stable timestamp ordering makes the final equal-timestamp tick hold.
    """
    if window_ms <= 0:
        return None, 0, 0.0
    left = now_ms - window_ms
    ordered = sorted(samples, key=lambda sample: sample[0])
    count = sum(left < ts <= now_ms for ts, _ in ordered)
    durations: dict[float, int] = {}
    for index, (ts, value) in enumerate(ordered):
        end = ordered[index + 1][0] if index + 1 < len(ordered) else now_ms
        overlap = max(0, min(end, now_ms) - max(ts, left))
        if overlap:
            durations[value] = durations.get(value, 0) + overlap
    total = sum(durations.values())
    cumulative = 0
    for value in sorted(durations):
        cumulative += durations[value]
        # Equality selects the lower median; never interpolate or average.
        if 2 * cumulative >= total:
            return value, count, total / window_ms
    return None, count, 0.0


# All fixtures use [0, 60_000]; the right-edge tick counts but has zero mass.
WINDOW_CASES = (
    ("empty", [], (None, 0, 0.0)),
    ("future_only", [(60_001, 9.0)], (None, 0, 0.0)),
    ("right_only", [(60_000, 9.0)], (None, 1, 0.0)),
    ("left_is_carry", [(0, 0.0)], (0.0, 0, 1.0)),
    ("latest_carry", [(-2, 9.0), (-1, -3.0)], (-3.0, 0, 1.0)),
    ("partial_coverage", [(30_000, 4.0)], (4.0, 1, 0.5)),
    ("carry_gap", [(-5_000, 1.0), (20_000, 5.0)], (5.0, 1, 1.0)),
    ("just_below_half", [(0, 1.0), (29_999, 9.0)], (9.0, 1, 1.0)),
    ("lower_at_half", [(0, 9.0), (30_000, 1.0)], (1.0, 1, 1.0)),
    ("just_above_half", [(0, 1.0), (30_001, 9.0)], (1.0, 1, 1.0)),
    (
        "both_edges_and_future",
        [(-1, 9.0), (0, -2.0), (60_000, 99.0), (60_001, 100.0)],
        (-2.0, 1, 1.0),
    ),
    (
        "equal_timestamp_last_holds",
        [(10_000, 9.0), (10_000, 1.0), (40_000, 3.0)],
        (1.0, 3, 5 / 6),
    ),
)


class TwP50AccuracyContractTests(unittest.TestCase):
    def assert_window_equal(self, actual, expected) -> None:
        p50, count, coverage = actual
        if expected[0] is None:
            self.assertTrue(p50 is None or math.isnan(p50))
        else:
            self.assertEqual(p50, expected[0])
        self.assertEqual(count, expected[1])
        self.assertAlmostEqual(coverage, expected[2], places=12)

    def test_oracle_known_hold_windows(self) -> None:
        for name, samples, expected in WINDOW_CASES:
            with self.subTest(case=name):
                self.assert_window_equal(
                    uncapped_tw_median(samples, now_ms=60_000), expected
                )

    def test_window_function_matches_independent_oracle(self) -> None:
        for name, samples, _ in WINDOW_CASES:
            with self.subTest(case=name):
                self.assert_window_equal(
                    tw_p50_window(samples, now_ms=60_000, window_ms=60_000),
                    uncapped_tw_median(samples, now_ms=60_000),
                )

    def test_observers_match_oracle_before_cap(self) -> None:
        for engine in ("legacy", "deque"):
            for name, samples, _ in WINDOW_CASES:
                with self.subTest(engine=engine, case=name):
                    observer = LiveTwP50Observer(
                        ["BTC"], sample_cap=16, one_minute_engine=engine
                    )
                    for ts, value in samples:
                        observer.note_spreads("BTC", ts, value, value)
                    snapshots = observer.compute_snapshots(
                        now_ms=60_000, computed_at_ms=60_001
                    )
                    self.assertEqual({snap.side for snap in snapshots}, {"long", "short"})
                    for snap in snapshots:
                        self.assert_window_equal(
                            (snap.p50_1m, snap.n_1m, snap.coverage_1m),
                            uncapped_tw_median(samples, now_ms=60_000),
                        )

    def test_seeded_sparse_and_burst_streams_before_cap(self) -> None:
        for engine in ("legacy", "deque"):
            for seed in (0, 7, 42):
                for max_step in (100, 5_000):
                    with self.subTest(engine=engine, seed=seed, max_step=max_step):
                        rng = random.Random(seed)
                        observer = LiveTwP50Observer(
                            ["BTC"], sample_cap=128, one_minute_engine=engine
                        )
                        history = []
                        ts = 0
                        for index in range(96):
                            ts += rng.randint(0, max_step)
                            value = rng.randint(-20, 20) / 8
                            history.append((ts, value))
                            observer.note_spreads("BTC", ts, value, -value)
                            if index % 11 == 0 or index == 95:
                                for snap in observer.compute_snapshots(
                                    now_ms=ts, computed_at_ms=ts + 1
                                ):
                                    side_history = (
                                        history if snap.side == "long"
                                        else [(t, -v) for t, v in history]
                                    )
                                    self.assert_window_equal(
                                        (snap.p50_1m, snap.n_1m, snap.coverage_1m),
                                        uncapped_tw_median(side_history, now_ms=ts),
                                    )
                        self.assertTrue(observer.memory_bound_ok())

    def test_seeded_overflow_characterizes_error_without_exact_parity(self) -> None:
        # A short high-rate burst forces reservoir sampling within one window.
        samples = [(i * 100, float((i * 17) % 31 - 15)) for i in range(400)]
        exact_p50, exact_count, _ = uncapped_tw_median(samples, now_ms=60_000)
        self.assertEqual(exact_p50, 8.0)
        self.assertEqual(exact_count, 399)
        for engine in ("legacy", "deque"):
            for seed in (0, 7, 42):
                with self.subTest(engine=engine, seed=seed):
                    repeats = []
                    for _ in range(2):
                        observer = LiveTwP50Observer(
                            ["BTC"], sample_cap=16, rng=random.Random(seed),
                            one_minute_engine=engine,
                        )
                        for ts, value in samples:
                            observer.note_spreads("BTC", ts, value, -value)
                        snapshots = observer.compute_snapshots(
                            now_ms=60_000, computed_at_ms=60_001
                        )
                        retained = observer.snapshot_samples()
                        self.assertTrue(observer.memory_bound_ok())
                        for snap in snapshots:
                            kept = retained[("BTC", snap.side)]
                            self.assertLessEqual(len(kept), 17)
                            self.assertLess(snap.n_1m, exact_count)
                            self.assert_window_equal(
                                (snap.p50_1m, snap.n_1m, snap.coverage_1m),
                                uncapped_tw_median(kept, now_ms=60_000),
                            )
                        long = next(s for s in snapshots if s.side == "long")
                        self.assertIsNotNone(long.p50_1m)
                        error = long.p50_1m - exact_p50
                        self.assertTrue(math.isfinite(error))
                        if seed == 7:
                            # This pinned burst exposes approximation, not an error budget.
                            self.assertNotEqual(error, 0.0)
                        repeats.append(([s.as_row() for s in snapshots], retained, error))
                    self.assertEqual(repeats[0], repeats[1])

    def test_json_and_theta_follow_on_keep_existing_contract(self) -> None:
        tw_keys = {
            "schema_version", "base_coin", "side", "ts_ms", "p50_1m",
            "n_1m", "coverage_1m", "computed_at_ms",
        }
        theta_keys = {
            "schema_version", "base_coin", "side", "ts_ms", "p50_1m",
            "floor_tf_select_a25", "theta_1m", "computed_at_ms",
            "coverage_1m", "n_1m",
        }
        # Binary fractions keep the positive / negative / zero checks exact.
        for value, floor in (
            (0.25, 0.125), (-0.25, 0.125), (0.0, 0.0), (None, 0.125)
        ):
            with self.subTest(value=value, floor=floor), tempfile.TemporaryDirectory() as tmp:
                observer = LiveTwP50Observer(["BTC"], sample_cap=16)
                floor_observer = LiveFloorObserver(["BTC"])
                samples = [] if value is None else [(0, value), (30_000, value)]
                for ts, spread in samples:
                    observer.note_spreads("BTC", ts, spread, spread)
                for side in ("long", "short"):
                    floor_observer.seed_last_floor(
                        "BTC", side, floor, bar_end_ms=0, computed_at_ms=1
                    )
                tw = observer.compute_snapshots(now_ms=60_000, computed_at_ms=60_001)
                screener = LiveThetaScreener(
                    ["BTC"], floor_observer=floor_observer, tw_p50_observer=observer
                )
                theta = screener.compute_from_tw_snapshots(tw, computed_at_ms=60_002)
                expected_p50, _, _ = uncapped_tw_median(samples, now_ms=60_000)
                for snap in theta:
                    self.assertEqual(snap.p50_1m, expected_p50)
                    self.assertEqual(snap.floor_tf_select_a25, floor)
                    expected_theta = None if expected_p50 is None else expected_p50 - floor
                    self.assertEqual(snap.theta_1m, expected_theta)
                    self.assertEqual(snap.ts_ms, 60_000)
                    self.assertEqual(snap.computed_at_ms, 60_002)
                    self.assertEqual(screener.get_snapshot("btc", snap.side), snap)
                for snapshots, writer, keys, version in (
                    (tw, TwP50JournalWriter(Path(tmp)), tw_keys, "bbot.tw_p50.v2"),
                    (theta, ThetaJournalWriter(Path(tmp)), theta_keys, "bbot.theta.v2"),
                ):
                    rows = [snap.as_row() for snap in snapshots]
                    for row in rows:
                        self.assertEqual(set(row), keys)
                        self.assertEqual(row["schema_version"], version)
                        json.dumps(row, allow_nan=False)
                    paths = writer.append_rows(rows)
                    self.assertEqual(len(paths), 1)
                    loaded = [json.loads(line) for line in paths[0].read_text().splitlines()]
                    self.assertEqual(loaded, rows)


if __name__ == "__main__":
    unittest.main()
