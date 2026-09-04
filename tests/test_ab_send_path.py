"""Hermetic tests for A/B send-path stages and contour branching.

No live network, no secrets, no VPS. Covers stage labels, contour skips,
p50/p95, JSON/CSV, dry A vs B critical-path branching, and live gate refusals.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


class StageLabelTests(unittest.TestCase):
    def test_known_stages_and_reject_unknown(self) -> None:
        from app.bot.private.ab_send_path_stages import (
            STAGE_LABELS,
            StageLabelError,
            assert_contour,
            assert_known_stage,
        )

        self.assertIn("warm_ready", STAGE_LABELS)
        self.assertIn("signal", STAGE_LABELS)
        self.assertIn("first_request_sent", STAGE_LABELS)
        self.assertIn("terminal_flat", STAGE_LABELS)
        self.assertEqual(assert_known_stage("operator_approval"), "operator_approval")
        self.assertEqual(assert_contour("b"), "B")
        with self.assertRaises(StageLabelError):
            assert_known_stage("not_a_stage")
        with self.assertRaises(StageLabelError):
            assert_contour("C")

    def test_contour_b_skips_manager_stages(self) -> None:
        from app.bot.private.ab_send_path_stages import StageTrace

        t = StageTrace(trial_id=1, contour="B", send_enabled=False)
        t.mark("warm_ready", mono_ns=1_000_000_000)
        t.mark("signal", mono_ns=1_000_100_000)
        t.apply_contour_skips()
        t.mark("first_request_sent", mono_ns=1_000_200_000)
        t.mark("second_request_sent", mono_ns=1_000_210_000)
        t.mark("terminal_flat", mono_ns=1_005_000_000)
        self.assertIn("recover", t.skipped)
        self.assertIn("operator_approval", t.skipped)
        self.assertIn("lease", t.skipped)
        self.assertIn("order_prepared", t.skipped)
        intervals = t.intervals_ms()
        self.assertAlmostEqual(intervals["signal_to_first_request_sent"], 0.1, places=6)
        self.assertNotIn("signal_to_order_prepared", intervals)

    def test_percentile_and_summarize(self) -> None:
        from app.bot.private.ab_send_path_stages import percentile, summarize_ms

        vals = [10.0, 20.0, 30.0, 40.0, 100.0]
        self.assertEqual(percentile(vals, 0.50), 30.0)
        self.assertAlmostEqual(percentile(vals, 0.95), 88.0, places=9)
        s = summarize_ms(vals)
        self.assertEqual(s["n"], 5)
        self.assertEqual(s["p50"], 30.0)
        self.assertEqual(summarize_ms([])["n"], 0)


class ResultsWriterTests(unittest.TestCase):
    def test_json_csv_roundtrip_and_delta(self) -> None:
        from app.bot.private.ab_send_path_stages import (
            AbSendPathReport,
            StageTrace,
            write_results_csv,
            write_results_json,
            write_summary_csv,
        )

        report = AbSendPathReport(
            status="ok",
            n_requested=2,
            contour="AB",
            dry_run=True,
            warm_ready=True,
        )
        for contour in ("A", "B"):
            for i in range(2):
                t = StageTrace(
                    trial_id=i + 1,
                    contour=contour,
                    send_enabled=False,
                )
                base = 2_000_000_000 + i * 10_000_000
                t.mark("warm_ready", mono_ns=base)
                t.mark("signal", mono_ns=base + 100_000)
                if contour == "A":
                    t.mark("recover", mono_ns=base + 150_000)
                    t.mark("operator_approval", mono_ns=base + 200_000)
                    t.mark("lease", mono_ns=base + 250_000)
                    t.mark("order_prepared", mono_ns=base + 800_000)
                    t.mark("first_request_sent", mono_ns=base + 900_000)
                else:
                    t.apply_contour_skips()
                    t.mark("first_request_sent", mono_ns=base + 200_000)
                t.mark("second_request_sent", mono_ns=base + 950_000)
                t.mark("terminal_flat", mono_ns=base + 2_000_000)
                report.add_trial(t)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jpath = write_results_json(report, root / "ab_send_path_results.json")
            cpath = write_results_csv(report, root / "ab_send_path_trials.csv")
            spath = write_summary_csv(report, root / "ab_send_path_summary.csv")
            body = json.loads(jpath.read_text(encoding="utf-8"))
            self.assertEqual(body["experiment"], "ab_send_path")
            self.assertEqual(body["schema_version"], "ab_send_path_results.v1")
            self.assertEqual(body["n_completed"], 4)
            self.assertFalse(body["else_bybit_ws_on_branch"])
            self.assertIn("signal_to_first_request_sent", body["contour_ab_delta_ms"])
            delta = body["contour_ab_delta_ms"]["signal_to_first_request_sent"]
            self.assertGreater(delta["A_minus_B_p50_ms"], 0.0)
            self.assertTrue(cpath.exists())
            self.assertTrue(spath.exists())


class DryContourBranchTests(unittest.TestCase):
    def test_parse_cli_defaults(self) -> None:
        from app.bot.private.ws_ab_send_path import parse_ab_send_path_cli_args

        cli = parse_ab_send_path_cli_args(
            ["--ab-n=5", "--ab-contour=B", "--ab-send=false"]
        )
        self.assertEqual(cli.n, 5)
        self.assertEqual(cli.contour, "B")
        self.assertFalse(cli.send_enabled)
        self.assertEqual(cli.hold_sec, 0.0)

    def test_dry_a_stamps_manager_stages(self) -> None:
        from app.bot.private.ws_ab_send_path import run_contour_a_dry_trial

        with tempfile.TemporaryDirectory() as td:
            t = run_contour_a_dry_trial(trial_id=1, data_root=Path(td), hold_sec=0.0)
        self.assertEqual(t.contour, "A")
        for name in (
            "warm_ready",
            "signal",
            "recover",
            "operator_approval",
            "lease",
            "order_prepared",
            "first_request_sent",
            "second_request_sent",
            "terminal_flat",
        ):
            self.assertIn(name, t.stamps_ns, msg=name)
        self.assertIn("signal_to_first_request_sent", t.intervals_ms())
        self.assertEqual(t.notes.get("send_result_status"), "prepared_not_dispatched")
        self.assertTrue(t.notes.get("parallel_open"))
        self.assertTrue(t.notes.get("parallel_flatten"))
        self.assertEqual(
            t.stamps_ns["first_request_sent"],
            t.stamps_ns["second_request_sent"],
        )
        self.assertEqual(
            t.stamps_ns["close_first_request_sent"],
            t.stamps_ns["close_second_request_sent"],
        )

    def test_dry_b_skips_manager_and_enqueues_both(self) -> None:
        from app.bot.private.ws_ab_send_path import run_contour_b_dry_trial

        t = run_contour_b_dry_trial(trial_id=1, hold_sec=0.0)
        self.assertEqual(t.contour, "B")
        self.assertIn("first_request_sent", t.stamps_ns)
        self.assertIn("second_request_sent", t.stamps_ns)
        self.assertIn("recover", t.skipped)
        self.assertIn("operator_approval", t.skipped)
        self.assertIn("order_prepared", t.skipped)
        self.assertLess(
            t.intervals_ms()["signal_to_first_request_sent"],
            50.0,
        )

    def test_dry_a_manager_slower_than_b_queue(self) -> None:
        from app.bot.private.ws_ab_send_path import (
            run_contour_a_dry_trial,
            run_contour_b_dry_trial,
        )

        with tempfile.TemporaryDirectory() as td:
            a = run_contour_a_dry_trial(trial_id=1, data_root=Path(td), hold_sec=0.0)
        b = run_contour_b_dry_trial(trial_id=1, hold_sec=0.0)
        a_ms = a.intervals_ms()["signal_to_first_request_sent"]
        b_ms = b.intervals_ms()["signal_to_first_request_sent"]
        self.assertGreater(a_ms, b_ms)

    def test_run_dry_n_and_live_helper_refuses(self) -> None:
        from app.bot.private.ws_ab_send_path import (
            parse_ab_send_path_cli_args,
            run_ab_send_path_experiment,
        )

        cli = parse_ab_send_path_cli_args(
            ["--ab-n=2", "--ab-contour=A", "--ab-send=false"]
        )
        with tempfile.TemporaryDirectory() as td:
            report = run_ab_send_path_experiment(cli=cli, data_root=Path(td))
        self.assertEqual(report.status, "ok")
        self.assertEqual(report.n_completed, 2)
        self.assertTrue(all(c.contour == "A" for c in report.trials))

        live_cli = parse_ab_send_path_cli_args(
            ["--ab-n=1", "--ab-contour=B", "--ab-send=true"]
        )
        refused = run_ab_send_path_experiment(cli=live_cli)
        self.assertEqual(refused.status, "live_requires_cli_main")


class GateAndMainTests(unittest.TestCase):
    def test_gate_requires_opt_in_and_live(self) -> None:
        from app.bot.private.ws_gates import (
            WsProfileGateError,
            assert_ws_ab_send_path_gates,
            is_live_send_ws_profile_gate,
        )

        with self.assertRaises(WsProfileGateError):
            assert_ws_ab_send_path_gates({"VENUE": "live", "LIVE_ORDERS": "1"})
        with self.assertRaises(WsProfileGateError):
            assert_ws_ab_send_path_gates(
                {
                    "VENUE": "live",
                    "LIVE_ORDERS": "0",
                    "BBOT_PRIVATE_AB_SEND": "1",
                }
            )
        self.assertTrue(is_live_send_ws_profile_gate(assert_ws_ab_send_path_gates))

    def test_live_n_cap(self) -> None:
        from app.bot.private.ws_ab_send_path import (
            AbSendPathError,
            parse_ab_send_path_cli_args,
        )

        with self.assertRaises(AbSendPathError):
            parse_ab_send_path_cli_args(
                ["--ab-n=20", "--ab-send=true", "--ab-contour=A"]
            )
        cli = parse_ab_send_path_cli_args(
            ["--ab-n=5", "--ab-send=true", "--ab-contour=B"]
        )
        self.assertEqual(cli.n, 5)
        self.assertEqual(cli.hold_sec, 5.0)

    def test_print_vps_recipe(self) -> None:
        from app.bot.private.ws_ab_send_path import (
            main_ab_send_path,
            print_vps_live_recipe,
        )

        text = print_vps_live_recipe()
        self.assertIn("root@38.180.94.108", text)
        self.assertIn("/root/spread_staging", text)
        self.assertIn("BBOT_PRIVATE_AB_SEND=1", text)
        self.assertIn("TRUMP", text)
        self.assertIn("--ab-n=1", text)
        self.assertIn("NEVER", text)
        code = main_ab_send_path(["--ab-send-path", "--ab-print-vps-recipe"])
        self.assertEqual(code, 0)

    def test_main_dry_writes_outputs(self) -> None:
        from app.bot.private.ws_ab_send_path import main_ab_send_path

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            code = main_ab_send_path(
                [
                    "--ab-send-path",
                    "--ab-contour=B",
                    "--ab-n=2",
                    "--ab-send=false",
                    f"--ab-out={out}",
                ]
            )
            self.assertEqual(code, 0)
            body = json.loads(
                (out / "ab_send_path_results.json").read_text(encoding="utf-8")
            )
            self.assertEqual(body["status"], "ok")
            self.assertEqual(body["n_completed"], 2)
            self.assertEqual(body["contour"], "B")
            self.assertTrue((out / "ab_send_path_trials.csv").is_file())
            self.assertTrue((out / "ab_send_path_summary.csv").is_file())

    def test_main_live_without_approve_or_opt_in(self) -> None:
        from app.bot.private.secrets import LIVE_KEY_NAMES
        from app.bot.private.ws_ab_send_path import main_ab_send_path

        with tempfile.TemporaryDirectory() as td:
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
            code = main_ab_send_path(
                [
                    "--ab-send-path",
                    "--ab-n=1",
                    "--ab-send=true",
                    "--ab-contour=A",
                    "--ab-approve-one-shot",
                ],
                env=env,
            )
            self.assertEqual(code, 1)

            env["BBOT_PRIVATE_AB_SEND"] = "1"
            code2 = main_ab_send_path(
                [
                    "--ab-send-path",
                    "--ab-n=1",
                    "--ab-send=true",
                    "--ab-contour=A",
                ],
                env=env,
            )
            self.assertEqual(code2, 1)

    def test_harness_dispatches_flag(self) -> None:
        from app.bot.private.harness_readonly import main

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            code = main(
                [
                    "--ab-send-path",
                    "--ab-contour=A",
                    "--ab-n=1",
                    "--ab-send=false",
                    f"--ab-out={out}",
                ]
            )
            self.assertEqual(code, 0)
            body = json.loads(
                (out / "ab_send_path_results.json").read_text(encoding="utf-8")
            )
            self.assertEqual(body["contour"], "A")
            self.assertIn("operator_approval", body["trials"][0]["stamps_ns"])


class PrimitivePayloadTests(unittest.TestCase):
    def test_w6_payloads_match_profile(self) -> None:
        from app.bot.private.ws_ab_primitive_send import build_w6_dual_payloads
        from app.bot.private.ws_w6_dual_leg import W6_LEGS

        b, o, _, _ = build_w6_dual_payloads(phase="open")
        self.assertEqual(b["op"], "order.create")
        self.assertEqual(b["args"][0]["symbol"], W6_LEGS["bybit"]["symbol"])
        self.assertEqual(b["args"][0]["qty"], W6_LEGS["bybit"]["qty"])
        self.assertNotIn("reduceOnly", b["args"][0])
        self.assertEqual(o["op"], "order")
        self.assertEqual(o["args"][0]["instId"], W6_LEGS["okx"]["symbol"])
        cb, co, _, _ = build_w6_dual_payloads(phase="close")
        self.assertTrue(cb["args"][0]["reduceOnly"])
        self.assertTrue(co["args"][0]["reduceOnly"])


class ParallelPlaceContractTests(unittest.TestCase):
    """AB must match production parallel dual-leg, not classic sequential W6."""

    def test_contour_a_live_kwargs_match_live_broker_intent(self) -> None:
        from app.bot.private.ws_ab_send_path import CONTOUR_A_LIVE_W6_KWARGS

        self.assertEqual(
            CONTOUR_A_LIVE_W6_KWARGS,
            {"parallel_open": True, "parallel_flatten": True},
        )

    def test_w6_parallel_flatten_exists_and_defaults_off(self) -> None:
        import inspect

        from app.bot.private.ws_w6_dual_leg import run_w6_dual_leg

        params = inspect.signature(run_w6_dual_leg).parameters
        self.assertIn("parallel_open", params)
        self.assertIn("parallel_flatten", params)
        self.assertIs(params["parallel_open"].default, False)
        self.assertIs(params["parallel_flatten"].default, False)

    def test_contour_b_enqueues_both_legs_without_venue_wait(self) -> None:
        import inspect

        from app.bot.private.ws_ab_primitive_send import (
            PrimitiveDualSender,
            build_w6_dual_payloads,
        )

        src = inspect.getsource(PrimitiveDualSender.enqueue_dual)
        put_b = src.find("_bybit_q.put")
        put_o = src.find("_okx_q.put")
        wait = src.find("_wait_sent")
        self.assertGreater(put_b, 0)
        self.assertGreater(put_o, 0)
        self.assertGreater(wait, 0)
        self.assertLess(put_b, wait)
        self.assertLess(put_o, wait)
        self.assertNotIn("recv_trade_ack", src)
        self.assertNotIn("fill", src.lower())

        sent: list[str] = []

        def send_fn(item) -> None:
            sent.append(item.venue)

        loop = PrimitiveDualSender(send_fn=send_fn)
        try:
            b_pay, o_pay, b_req, o_req = build_w6_dual_payloads(phase="open")
            opened = loop.enqueue_dual(
                bybit_payload=b_pay,
                okx_payload=o_pay,
                bybit_req_id=b_req,
                okx_req_id=o_req,
                phase="open",
            )
        finally:
            loop.close()

        self.assertEqual(set(sent), {"bybit", "okx"})
        self.assertIsNotNone(opened.first_enqueued_ns)
        self.assertIsNotNone(opened.second_enqueued_ns)
        enqueue_gap_ms = (
            abs(opened.second_enqueued_ns - opened.first_enqueued_ns) / 1_000_000
        )
        self.assertLess(enqueue_gap_ms, 10.0)

    def test_vps_recipe_states_parallel_place(self) -> None:
        from app.bot.private.ws_ab_send_path import print_vps_live_recipe

        text = print_vps_live_recipe()
        self.assertIn("parallel_open", text)
        self.assertIn("parallel_flatten", text)
        self.assertNotIn("Bybit must fill before OKX", text)


if __name__ == "__main__":
    unittest.main()
