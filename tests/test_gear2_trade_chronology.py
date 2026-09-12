"""Hermetic tests for Gear-2 trade chronology helpers (fixture only, no live data)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from research.gear2_trade_chronology import (
    DEFAULT_PAD_MS,
    FIXTURE_INTENT_ID,
    FIXTURE_SIGNAL_TS_MS,
    build_chronology,
    build_markers,
    fixture_root,
    load_intent,
    make_chronology_figure,
    neighborhood_window_ms,
    summarize_intent,
)

REPO = Path(__file__).resolve().parents[1]
FIXTURE = fixture_root(REPO)
EXPECTED = json.loads((FIXTURE / "EXPECTED.json").read_text(encoding="utf-8"))


class Gear2TradeChronologyFixtureTests(unittest.TestCase):
    def test_fixture_files_exist(self) -> None:
        self.assertTrue((FIXTURE / "journal").is_dir())
        self.assertTrue((FIXTURE / "ticks" / "ticks.jsonl").is_file())
        self.assertTrue((FIXTURE / "EXPECTED.json").is_file())

    def test_load_intent_dual_legs(self) -> None:
        intent = load_intent(FIXTURE, FIXTURE_INTENT_ID)
        self.assertEqual(intent.base_coin, "SOL")
        self.assertEqual(intent.spread_side, "open_long")
        self.assertEqual(intent.qty, 0.1)
        self.assertIsNotNone(intent.leg_for("okx"))
        self.assertIsNotNone(intent.leg_for("bybit"))
        self.assertEqual(intent.signal_ts_ms(), EXPECTED["signal_ts_ms"])
        self.assertEqual(
            intent.dual_fill_complete_ms(),
            EXPECTED["dual_fill_complete_ms"],
        )
        fills = intent.fill_ts_ms_by_exchange()
        self.assertEqual(fills["okx"], EXPECTED["fill_ts_ms_okx"])
        self.assertEqual(fills["bybit"], EXPECTED["fill_ts_ms_bybit"])
        self.assertEqual(
            intent.dual_fill_complete_ms(),
            max(EXPECTED["fill_ts_ms_okx"], EXPECTED["fill_ts_ms_bybit"]),
        )

    def test_markers_match_expected_timestamps(self) -> None:
        intent = load_intent(FIXTURE, FIXTURE_INTENT_ID)
        markers = build_markers(intent)
        by_kind: dict[str, list] = {}
        for m in markers:
            by_kind.setdefault(m.kind, []).append(m)

        self.assertTrue(by_kind["signal"])
        self.assertEqual(by_kind["signal"][0].ts_ms, FIXTURE_SIGNAL_TS_MS)

        place_ts = {m.exchange: m.ts_ms for m in by_kind["place"]}
        self.assertEqual(place_ts["okx"], EXPECTED["place_ts_ms"])
        self.assertEqual(place_ts["bybit"], EXPECTED["place_ts_ms"])

        ack_ts = {m.exchange: m.ts_ms for m in by_kind["ack"]}
        self.assertEqual(ack_ts["okx"], EXPECTED["ack_ts_ms"])

        fill_ts = {m.exchange: m.ts_ms for m in by_kind["fill"]}
        self.assertEqual(fill_ts["okx"], EXPECTED["fill_ts_ms_okx"])
        self.assertEqual(fill_ts["bybit"], EXPECTED["fill_ts_ms_bybit"])
        for m in by_kind["fill"]:
            self.assertEqual(m.fill_source, "l1_at_send")
            self.assertIn("l1_at_send", m.display_label())

        dual = [m for m in markers if m.kind == "dual_fill_complete"]
        self.assertEqual(len(dual), 1)
        self.assertEqual(dual[0].ts_ms, EXPECTED["dual_fill_complete_ms"])

        # Private journal markers present and aligned to fixture wall times.
        self.assertTrue(by_kind.get("private_sent"))
        self.assertTrue(by_kind.get("private_terminal"))

    def test_neighborhood_pad(self) -> None:
        intent = load_intent(FIXTURE, FIXTURE_INTENT_ID)
        start, end = neighborhood_window_ms(intent, pad_ms=DEFAULT_PAD_MS)
        self.assertEqual(start, EXPECTED["signal_ts_ms"] - DEFAULT_PAD_MS)
        self.assertEqual(
            end,
            EXPECTED["dual_fill_complete_ms"] + DEFAULT_PAD_MS + 1,
        )

    def test_build_chronology_ticks_and_summary(self) -> None:
        plot = build_chronology(
            FIXTURE,
            FIXTURE_INTENT_ID,
            ticks_root=FIXTURE / "ticks",
            pad_ms=DEFAULT_PAD_MS,
        )
        self.assertGreater(len(plot.ticks), 10)
        self.assertIn("event_local_ts_ms", plot.ticks.columns)
        self.assertTrue(
            (plot.ticks["event_local_ts_ms"] >= plot.window_start_ms).all()
        )
        self.assertTrue(
            (plot.ticks["event_local_ts_ms"] < plot.window_end_ms).all()
        )
        # Signal and both fills land inside the loaded tick span.
        tmin = int(plot.ticks["event_local_ts_ms"].min())
        tmax = int(plot.ticks["event_local_ts_ms"].max())
        self.assertLessEqual(tmin, FIXTURE_SIGNAL_TS_MS)
        self.assertGreaterEqual(tmax, EXPECTED["dual_fill_complete_ms"])

        summary = summarize_intent(plot)
        self.assertEqual(summary["signal_ts_ms"], EXPECTED["signal_ts_ms"])
        self.assertEqual(
            summary["dual_fill_complete_ms"],
            EXPECTED["dual_fill_complete_ms"],
        )
        self.assertEqual(summary["fills"]["okx"]["fill_source"], "l1_at_send")
        self.assertIn("fill", summary["marker_kinds"])
        self.assertIn("signal", summary["marker_kinds"])

        # Relative offset does not change absolute marker timestamps.
        plot2 = build_chronology(
            FIXTURE,
            FIXTURE_INTENT_ID,
            ticks_root=FIXTURE / "ticks",
            offset_ms=FIXTURE_SIGNAL_TS_MS,
        )
        self.assertEqual(
            plot2.marker_ts_by_kind("signal")[0],
            EXPECTED["signal_ts_ms"],
        )
        self.assertEqual(
            plot2.axis_ts_ms(EXPECTED["signal_ts_ms"]),
            0,
        )

    def test_compacted_ticks_root(self) -> None:
        plot = build_chronology(
            FIXTURE,
            FIXTURE_INTENT_ID,
            ticks_root=FIXTURE / "ticks_compacted",
            pad_ms=500,
        )
        self.assertGreater(len(plot.ticks), 0)
        self.assertIn("okx_mid", plot.ticks.columns)
        self.assertIn("bybit_mid", plot.ticks.columns)

    def test_figure_builds(self) -> None:
        plot = build_chronology(
            FIXTURE,
            FIXTURE_INTENT_ID,
            ticks_root=FIXTURE / "ticks",
        )
        fig = make_chronology_figure(plot)
        self.assertIsNotNone(fig)
        # Title must surface fill_source caveat when journal says l1_at_send.
        title = fig.layout.title.text or ""
        self.assertIn("l1_at_send", title)
        self.assertIn(FIXTURE_INTENT_ID, title)


if __name__ == "__main__":
    unittest.main()
