"""Hermetic tests for Warm-Lat stage instrumentation and dry harness.

No live network, no secrets, no VPS. Covers stage labels, interval math,
p50/p95 summaries, JSON/CSV writers, Path A/B dry cycles, and live gate
refusals.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


class StageLabelTests(unittest.TestCase):
    def test_known_stages_and_reject_unknown(self) -> None:
        from app.bot.private.warm_latency_stages import (
            STAGE_LABELS,
            StageLabelError,
            assert_known_stage,
        )

        self.assertIn("warm_ready", STAGE_LABELS)
        self.assertIn("order_prepared", STAGE_LABELS)
        self.assertIn("request_sent", STAGE_LABELS)
        self.assertEqual(assert_known_stage("ack"), "ack")
        with self.assertRaises(StageLabelError):
            assert_known_stage("not_a_stage")

    def test_trace_intervals_and_skipped(self) -> None:
        from app.bot.private.warm_latency_stages import StageTrace

        t = StageTrace(
            cycle_id=1,
            path="B",
            venue="bybit",
            open_mode="serial",
            send_enabled=False,
        )
        t.mark("warm_ready", mono_ns=1_000_000_000)
        t.mark("intent", mono_ns=1_000_100_000)
        t.mark("approval", mono_ns=1_000_200_000)
        t.mark("lease", mono_ns=1_000_300_000)
        t.mark("profile", mono_ns=1_000_400_000)
        t.mark("order_prepared", mono_ns=1_001_000_000)
        t.mark_skipped("request_sent")
        t.mark_skipped("ack")
        t.mark_skipped("terminal")

        intervals = t.intervals_ms()
        self.assertAlmostEqual(intervals["warm_ready_to_intent"], 0.1, places=6)
        self.assertAlmostEqual(intervals["intent_to_approval"], 0.1, places=6)
        self.assertAlmostEqual(
            intervals["profile_to_order_prepared"], 0.6, places=6
        )
        self.assertNotIn("order_prepared_to_request_sent", intervals)
        self.assertNotIn("warm_ready_to_terminal", intervals)
        self.assertNotIn("approval", t.skipped)
        self.assertIn("request_sent", t.skipped)

    def test_percentile_and_summarize(self) -> None:
        from app.bot.private.warm_latency_stages import percentile, summarize_ms

        vals = [10.0, 20.0, 30.0, 40.0, 100.0]
        self.assertEqual(percentile(vals, 0.50), 30.0)
        self.assertAlmostEqual(percentile(vals, 0.95), 88.0, places=9)
        s = summarize_ms(vals)
        self.assertEqual(s["n"], 5)
        self.assertEqual(s["p50"], 30.0)
        self.assertIn("p95", s)
        self.assertEqual(summarize_ms([])["n"], 0)


class ResultsWriterTests(unittest.TestCase):
    def test_json_csv_roundtrip_shape(self) -> None:
        from app.bot.private.warm_latency_stages import (
            StageTrace,
            WarmLatencyReport,
            write_results_csv,
            write_results_json,
            write_summary_csv,
        )

        report = WarmLatencyReport(
            status="ok",
            n_requested=2,
            path="AB",
            open_mode="serial",
            dry_run=True,
            warm_ready=True,
        )
        for path in ("A", "B"):
            for i in range(2):
                t = StageTrace(
                    cycle_id=i + 1,
                    path=path,
                    venue="bybit",
                    open_mode="serial",
                    send_enabled=False,
                )
                base = 2_000_000_000 + i * 10_000_000
                t.mark("warm_ready", mono_ns=base)
                t.mark("intent", mono_ns=base + 100_000)
                if path == "A":
                    for s in ("approval", "lease", "profile", "order_prepared"):
                        t.mark_skipped(s)
                    t.mark("request_sent", mono_ns=base + 200_000)
                    t.mark("ack", mono_ns=base + 300_000)
                    t.mark("terminal", mono_ns=base + 400_000)
                else:
                    t.mark("approval", mono_ns=base + 150_000)
                    t.mark("lease", mono_ns=base + 160_000)
                    t.mark("profile", mono_ns=base + 170_000)
                    t.mark("order_prepared", mono_ns=base + 500_000)
                    t.mark_skipped("request_sent")
                    t.mark_skipped("ack")
                    t.mark_skipped("terminal")
                report.add_cycle(t)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jpath = write_results_json(report, root / "warm_lat_results.json")
            cpath = write_results_csv(report, root / "warm_lat_cycles.csv")
            spath = write_summary_csv(report, root / "warm_lat_summary.csv")
            body = json.loads(jpath.read_text(encoding="utf-8"))
            self.assertEqual(body["experiment"], "Warm-Lat")
            self.assertEqual(body["n_completed"], 4)
            self.assertIn("summary", body)
            self.assertIn("path_ab_delta_ms", body)
            self.assertFalse(body["else_bybit_ws_on_branch"])
            self.assertTrue(cpath.exists())
            self.assertTrue(spath.exists())
            lines = cpath.read_text(encoding="utf-8").strip().splitlines()
            self.assertGreater(len(lines), 1)
            self.assertIn("interval", lines[0])


class DryHarnessTests(unittest.TestCase):
    def test_parse_cli_and_dry_ab(self) -> None:
        from app.bot.private.ws_warm_latency import (
            parse_warm_lat_cli_args,
            run_warm_latency_experiment,
        )

        cli = parse_warm_lat_cli_args(
            [
                "--warm-lat-n=3",
                "--warm-lat-path=AB",
                "--warm-lat-mode=serial",
                "--warm-lat-send=false",
                "--warm-lat-venue=bybit",
            ]
        )
        self.assertEqual(cli.n, 3)
        self.assertEqual(cli.path, "AB")
        self.assertFalse(cli.send_enabled)

        with tempfile.TemporaryDirectory() as td:
            report = run_warm_latency_experiment(
                cli=cli,
                data_root=Path(td),
                warm_ready=True,
                warm_ready_ns=5_000_000_000,
            )
        self.assertEqual(report.status, "ok")
        self.assertTrue(report.dry_run)
        paths = {c.path for c in report.cycles}
        self.assertEqual(paths, {"A", "B"})
        # bybit-only → 3 A + 3 B
        self.assertEqual(report.n_completed, 6)
        for c in report.cycles:
            self.assertIn("warm_ready", c.stamps_ns)
            self.assertIn("intent", c.stamps_ns)
            if c.path == "A":
                self.assertIn("request_sent", c.stamps_ns)
                self.assertIn("approval", c.skipped)
            else:
                self.assertIn("order_prepared", c.stamps_ns)
                self.assertIn("request_sent", c.skipped)
                self.assertEqual(
                    c.notes.get("send_result_status"), "prepared_not_dispatched"
                )

        summary = report.aggregate_by_path_venue()
        self.assertTrue(any("path_A" in k for k in summary))
        self.assertTrue(any("path_B" in k for k in summary))
        delta = report.path_ab_delta_ms()
        # Shared interval warm_ready_to_intent should exist for both paths.
        self.assertIn("warm_ready_to_intent", delta)

    def test_live_helper_refuses_without_cli_main(self) -> None:
        from app.bot.private.ws_warm_latency import (
            parse_warm_lat_cli_args,
            run_warm_latency_experiment,
        )

        cli = parse_warm_lat_cli_args(
            ["--warm-lat-n=1", "--warm-lat-send=true", "--warm-lat-path=B"]
        )
        report = run_warm_latency_experiment(cli=cli, warm_ready=True)
        self.assertEqual(report.status, "live_requires_cli_main")

    def test_main_dry_writes_outputs(self) -> None:
        from app.bot.private.ws_warm_latency import main_ws_warm_latency

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            code = main_ws_warm_latency(
                [
                    "--ws-warm-latency",
                    "--warm-lat-n=2",
                    "--warm-lat-path=AB",
                    "--warm-lat-venue=dual",
                    "--warm-lat-send=false",
                    f"--warm-lat-out={out}",
                ]
            )
            self.assertEqual(code, 0)
            self.assertTrue((out / "warm_lat_results.json").is_file())
            self.assertTrue((out / "warm_lat_cycles.csv").is_file())
            self.assertTrue((out / "warm_lat_summary.csv").is_file())
            body = json.loads((out / "warm_lat_results.json").read_text(encoding="utf-8"))
            self.assertEqual(body["status"], "ok")
            # dual × 2 paths × n=2 = 8 cycles
            self.assertEqual(body["n_completed"], 8)


class WarmLatGateTests(unittest.TestCase):
    def test_gate_requires_opt_in_and_live(self) -> None:
        from app.bot.private.ws_gates import (
            WsProfileGateError,
            assert_ws_warm_lat_gates,
            is_live_send_ws_profile_gate,
        )

        with self.assertRaises(WsProfileGateError):
            assert_ws_warm_lat_gates(
                {"VENUE": "live", "LIVE_ORDERS": "1"}
            )
        with self.assertRaises(WsProfileGateError):
            assert_ws_warm_lat_gates(
                {
                    "VENUE": "live",
                    "LIVE_ORDERS": "0",
                    "BBOT_PRIVATE_WARM_LAT": "1",
                }
            )
        self.assertTrue(is_live_send_ws_profile_gate(assert_ws_warm_lat_gates))

    def test_main_live_without_approve_or_opt_in(self) -> None:
        from app.bot.private.ws_warm_latency import main_ws_warm_latency

        with tempfile.TemporaryDirectory() as td:
            from app.bot.private.secrets import LIVE_KEY_NAMES

            live_env = Path(td) / "live.env"
            live_env.write_text(
                "\n".join(f"{n}=v{i}" for i, n in enumerate(LIVE_KEY_NAMES)) + "\n",
                encoding="utf-8",
            )
            env = {
                "VENUE": "live",
                "LIVE_ORDERS": "1",
                "BBOT_PRIVATE_ENV_FILE": str(live_env),
                "BBOT_PRIVATE_DATA_ROOT": str(Path(td) / "data"),
            }
            # Missing BBOT_PRIVATE_WARM_LAT
            code = main_ws_warm_latency(
                [
                    "--ws-warm-latency",
                    "--warm-lat-n=1",
                    "--warm-lat-send=true",
                    "--warm-lat-path=B",
                    "--warm-lat-approve-one-shot",
                ],
                env=env,
            )
            self.assertEqual(code, 1)

            env["BBOT_PRIVATE_WARM_LAT"] = "1"
            code2 = main_ws_warm_latency(
                [
                    "--ws-warm-latency",
                    "--warm-lat-n=1",
                    "--warm-lat-send=true",
                    "--warm-lat-path=B",
                    # missing approve-one-shot
                ],
                env=env,
            )
            self.assertEqual(code2, 1)


class HarnessDispatchTests(unittest.TestCase):
    def test_cli_flag_dispatches_warm_latency(self) -> None:
        from app.bot.private.harness_readonly import main

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            code = main(
                [
                    "--ws-warm-latency",
                    "--warm-lat-n=1",
                    "--warm-lat-path=A",
                    "--warm-lat-venue=okx",
                    "--warm-lat-send=false",
                    f"--warm-lat-out={out}",
                ]
            )
            self.assertEqual(code, 0)
            body = json.loads((out / "warm_lat_results.json").read_text(encoding="utf-8"))
            self.assertEqual(body["path"], "A")
            self.assertEqual(body["n_completed"], 1)


if __name__ == "__main__":
    unittest.main()
