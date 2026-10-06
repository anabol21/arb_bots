from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

# This focused test exercises pure startup helpers; the workspace intentionally
# has no websocket dependency installed and must never open a network socket.
sys.modules.setdefault("websockets", ModuleType("websockets"))

from app.bot.runtime import BotRuntime, _canary29_okx_metadata, _canary29_source_stopped
from app.bot.theta_trade_manager import (
    OpenPosition,
    ThetaDecision,
    ThetaTradeConfig,
    ThetaTradeManager,
)


class Canary29MetadataTests(unittest.TestCase):
    def test_one_snapshot_populates_both_caches_for_full_pool(self) -> None:
        symbols = {f"C{i}-USDT-SWAP" for i in range(29)}
        rows = [
            {
                "instId": symbol,
                "instType": "SWAP",
                "settleCcy": "USDT",
                "ctVal": "1",
                "instIdCode": str(index + 1),
            }
            for index, symbol in enumerate(sorted(symbols))
        ]

        ct_vals, inst_codes = _canary29_okx_metadata(rows, symbols)

        self.assertEqual(set(ct_vals), symbols)
        self.assertEqual(set(inst_codes), symbols)
        self.assertTrue(all(value == Decimal("1") for value in ct_vals.values()))

    def test_incomplete_snapshot_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "okx_canary_metadata_incomplete"):
            _canary29_okx_metadata(
                [{"instId": "A-USDT-SWAP", "instType": "SWAP", "settleCcy": "USDT", "ctVal": "1", "instIdCode": "1"}],
                {"A-USDT-SWAP", "B-USDT-SWAP"},
            )


class Canary29PrivateShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_path_private_shutdown_waits_before_clear(self) -> None:
        runtime = object.__new__(BotRuntime)
        runtime._terminal_private_execution = True
        runtime.stop_event = asyncio.Event()
        runtime._private_stop_event = asyncio.Event()
        runtime._synthetic_sender = None
        runtime._synthetic_sender_session = None
        runtime._private_warm = object()
        runtime.log = Mock()
        started = asyncio.Event()
        finish = asyncio.Event()

        async def blocked_place() -> None:
            started.set()
            await finish.wait()

        task = asyncio.create_task(blocked_place())
        runtime.theta_trade = SimpleNamespace(_terminal_place_task=task)
        runtime.stop_event.set()  # SIGTERM is delivered to the main runtime only.
        await started.wait()
        with (
            patch.object(runtime, "_write_canary_state") as checkpoint,
            patch.object(runtime, "_stop_synthetic_private_send") as stop_private,
        ):
            wait_for_place = asyncio.create_task(runtime._finish_private_shutdown())
            await asyncio.sleep(0)
            self.assertFalse(runtime._private_stop_event.is_set())
            self.assertFalse(wait_for_place.done())

            finish.set()
            self.assertTrue(await wait_for_place)
            checkpoint.assert_called_once_with()
            stop_private.assert_called_once_with()


class Gear22LongCanaryContractTests(unittest.TestCase):
    def test_terminal_private_rejects_legacy_hot_add_path(self) -> None:
        env = {
            "BBOT_MODE": "policy",
            "BBOT_PROFILE": "gear22_live_canary",
            "BBOT_THETA_EXECUTION": "terminal_private",
            "BBOT_THETA_LIVE_SEND": "1",
            "BBOT_THETA_TRADE": "1",
            "BBOT_BROKER": "private_live",
            "VENUE": "live",
            "LIVE_ORDERS": "1",
            "BBOT_HOT_ADD": "1",
        }
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaisesRegex(
                ValueError, "terminal_private hot-add requires Gear 2.3 private readiness gates"
            ):
                BotRuntime()

    def test_resume_refuses_live_source_pid(self) -> None:
        self.assertFalse(_canary29_source_stopped(os.getpid()))

    def test_expired_open_window_keeps_natural_close_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = ThetaTradeManager(
                data_root=Path(tmp),
                config=ThetaTradeConfig(),
                execution_mode="terminal_private",
                place_fn=lambda **_kwargs: None,
                meta_fn=lambda _coin: None,
                entry_allowed_fn=lambda: False,
            )
            open_decision = manager._record_policy_status(
                ThetaDecision(action="open", base_coin="RVN", side="long", reason="signal"),
                evaluated_at_ms=1,
            )
            close_decision = manager._record_policy_status(
                ThetaDecision(action="close", base_coin="RVN", side="long", reason="signal"),
                evaluated_at_ms=2,
            )
            self.assertEqual(open_decision.reason, "canary_open_window_elapsed")
            self.assertEqual(close_decision.action, "close")

    def test_checkpoint_is_atomic_json_with_latest_policy_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = object.__new__(BotRuntime)
            runtime._terminal_private_execution = True
            runtime.theta_trade = SimpleNamespace(
                slot=SimpleNamespace(position=None, pending=False),
                execution_halt_reason=None,
                last_policy_status={"evaluated_at_ms": 123, "action": "hold"},
            )
            runtime.data_root = Path(tmp)
            runtime._canary29_started_at_ms = 100
            runtime._canary29_deadline_ms = 200
            runtime._canary29_completed_cycles = 0
            runtime._canary29_max_cycles = 0
            runtime._canary29_open_window_hours = 72.0
            runtime._canary29_policy = "gear22"
            runtime._synthetic_roll_halt_reason = None
            runtime._canary29_done_reason = None
            runtime._canary29_state_path = runtime.data_root / "canary_state.json"

            runtime._write_canary_state()

            state = json.loads(runtime._canary29_state_path.read_text())
            self.assertEqual(state["last_policy_status"]["evaluated_at_ms"], 123)
            self.assertFalse(state["pending"])
            self.assertEqual(list(runtime.data_root.glob(".canary_state.*.tmp")), [])

    def _resume_fixture(self, root: Path) -> tuple[BotRuntime, dict, dict]:
        source = root / "source"
        target = root / "target"
        source.mkdir()
        position = OpenPosition(
            trade_id="intent-1",
            base_coin="RVN",
            side="long",
            open_signal_ts_ms=900,
            open_fill_ts_ms=1000,
            open_fill_spread=0.01,
            open_notional=10.0,
            open_theta_1m=0.2,
            fill_spread_pp=0.01,
            okx_filled_qty="374",
            bybit_filled_qty="3740",
            coin_filled_qty="3740",
        )
        (source / "bbot.log").write_text(
            f"x | heartbeat | pending=False | position={position!r} | probe_done=False\n",
            encoding="utf-8",
        )
        journal = source / "theta_trades" / "event_date=2026-10-05" / "trades.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text(
            json.dumps({
                "schema_version": "bbot.synthetic_roll.v1",
                "intent_id": "intent-1",
                "status": "open",
                "event": "open",
                "base_coin": "RVN",
                "side": "long",
                "coin_qty": "3740",
                "okx_filled_qty": "374",
                "bybit_filled_qty": "3740",
                "fill_ts_ms": 1000,
            }) + "\n",
            encoding="utf-8",
        )
        private_events = source / "private" / "journal" / "event_date=2026-10-05" / "events.jsonl"
        private_events.parent.mkdir(parents=True)
        private_events.write_text(
            json.dumps({
                "schema_version": "bbot.private.journal.v1",
                "event_id": "event-1",
                "event_type": "reconciliation",
                "event_date": "2026-10-05",
                "event_ts_utc": "2026-10-05T17:31:44.936Z",
                "event_monotonic_ns": 1,
                "run_id": "run-1",
                "operation_id": "stream-operation-1",
                "event_seq": 1,
                "venue": "okx",
                "environment": "live",
                "outcome": "observed",
                "reconciliation_scope": "private_stream_reseed",
                "reconciliation_state": "inconclusive",
                "observation_source": "private_ws",
                "reconnect_generation": 4,
                "sequence_state": "reseed_required",
                "subscription_readiness": "not_ready",
            }) + "\n",
            encoding="utf-8",
        )
        snapshot = {
            "bybit_positions": [{"symbol": "RVNUSDT", "side": "Sell", "size": "3740", "positionIdx": 0}],
            "okx_positions": [{"instId": "RVN-USDT-SWAP", "posSide": "net", "pos": "374"}],
            "bybit_orders": [],
            "okx_orders": [],
        }
        runtime = object.__new__(BotRuntime)
        runtime._canary29_resume_manifest = {
            "schema_version": "bbot.gear22.canary-state.v1",
            "policy_id": "gear22_frozen_v1",
            "policy_selector": "gear22",
            "execution": "terminal_private",
            "max_cycles": 0,
            "open_window_hours": 72.0,
            "source_pid": 999999999,
            "source_data_root": str(source),
            "pending": False,
            "source_intent_id": "intent-1",
            "position": dict(position.__dict__),
        }
        runtime._terminal_private_execution = True
        runtime._canary29_policy = "gear22"
        runtime._canary29_max_cycles = 0
        runtime._canary29_open_window_hours = 72.0
        runtime.data_root = target
        runtime.theta_trade = SimpleNamespace(
            slot=SimpleNamespace(position=None, pending=False, slot_busy=lambda: False)
        )
        runtime.coins = ("RVN",)
        runtime._meta = lambda _coin: SimpleNamespace(
            okx_symbol="RVN-USDT-SWAP", bybit_symbol="RVNUSDT"
        )
        runtime._okx_ct_vals = {"RVN-USDT-SWAP": Decimal("10")}
        runtime._canary29_read_startup_snapshot = lambda: snapshot
        runtime._private_warm = SimpleNamespace(is_ready=lambda: True)
        runtime.log = Mock()
        return runtime, position.__dict__, snapshot

    def test_resume_hydrates_only_exact_open_and_refuses_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _position, _snapshot = self._resume_fixture(Path(tmp))
            runtime._canary29_resume_position()
            self.assertEqual(runtime.theta_trade.slot.position.trade_id, "intent-1")

        for mutation in (
            "qty", "side", "order", "pending", "api_error", "abort",
            "stream_not_ready", "stream_order_ambiguity", "non_stream_reconciliation",
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                runtime, _position, snapshot = self._resume_fixture(Path(tmp))
                source = Path(runtime._canary29_resume_manifest["source_data_root"])
                if mutation == "qty":
                    snapshot["bybit_positions"][0]["size"] = "3751"
                elif mutation == "side":
                    snapshot["bybit_positions"][0]["side"] = "Buy"
                elif mutation == "order":
                    snapshot["okx_orders"].append({"instId": "RVN-USDT-SWAP", "ordId": "active"})
                elif mutation == "pending":
                    journal = next((source / "theta_trades").glob("event_date=*/trades.jsonl"))
                    with journal.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps({"intent_id": "other", "status": "pending"}) + "\n")
                elif mutation == "api_error":
                    snapshot["okx_positions"][0]["pos"] = "not-a-number"
                elif mutation == "abort":
                    private = source / "private" / "journal" / "event_date=2026-10-05" / "events.jsonl"
                    event = {
                        "schema_version": "bbot.private.journal.v1",
                        "event_id": "event-abort",
                        "event_type": "dual_leg_abort",
                        "event_date": "2026-10-05",
                        "event_ts_utc": "2026-10-05T17:31:45Z",
                        "event_monotonic_ns": 2,
                        "run_id": "run-1",
                        "operation_id": "operation-1",
                        "event_seq": 2,
                        "venue": "okx",
                        "environment": "live",
                        "outcome": "observed",
                        "dual_leg_id": "dual-1",
                        "leg_id": "leg-1",
                        "peer_leg_id": "leg-2",
                        "abort_reason": "test",
                        "request_fingerprint": "fp",
                    }
                    private.write_text(json.dumps(event) + "\n")
                elif mutation == "stream_not_ready":
                    runtime._private_warm = SimpleNamespace(is_ready=lambda: False)
                elif mutation in {"stream_order_ambiguity", "non_stream_reconciliation"}:
                    private = source / "private" / "journal" / "event_date=2026-10-05" / "events.jsonl"
                    event = json.loads(private.read_text().splitlines()[0])
                    if mutation == "stream_order_ambiguity":
                        event["dual_leg_id"] = "dual-1"
                    else:
                        event["reconciliation_scope"] = "post_dispatch_ambiguity"
                    private.write_text(json.dumps(event) + "\n")
                with self.assertRaises(Exception):
                    runtime._canary29_resume_position()
                self.assertIsNone(runtime.theta_trade.slot.position)

    def test_cycle_cap_counts_only_completed_close_with_confirmed_flat(self) -> None:
        runtime = object.__new__(BotRuntime)
        runtime._terminal_private_execution = True
        runtime._canary29_completed_cycles = 0
        runtime._canary29_max_cycles = 1
        runtime._canary29_done = False
        runtime._canary29_done_reason = None
        runtime._canary29_window_elapsed = lambda: False
        runtime._canary29_assert_flat = Mock()
        runtime.log = Mock()
        runtime._canary29_record_close_flat(SimpleNamespace(completed=False), "RVN")
        self.assertEqual(runtime._canary29_completed_cycles, 0)
        completed = SimpleNamespace(completed=True, keep_pending=False, abort=None)
        runtime._canary29_record_close_flat(completed, "RVN")
        self.assertEqual(runtime._canary29_completed_cycles, 1)
        self.assertTrue(runtime._canary29_done)
        self.assertEqual(runtime._canary29_done_reason, "cycle_cap_reached_flat")

        failed = object.__new__(BotRuntime)
        failed._terminal_private_execution = True
        failed._canary29_completed_cycles = 0
        failed._canary29_max_cycles = 1
        failed._canary29_done = False
        failed._canary29_done_reason = None
        failed._canary29_assert_flat = Mock(side_effect=RuntimeError("not_flat"))
        failed.log = Mock()
        failed._synthetic_roll_halt_reason = None
        failed._canary29_record_close_flat(
            pending_result := SimpleNamespace(completed=True, keep_pending=False, abort=None),
            "RVN",
        )
        self.assertEqual(failed._canary29_completed_cycles, 0)
        self.assertFalse(failed._canary29_done)
        self.assertFalse(pending_result.completed)
        self.assertTrue(pending_result.keep_pending)


class Gear22CheckpointFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_checkpoint_failure_requests_runtime_stop_and_retains_pending(self) -> None:
        runtime = object.__new__(BotRuntime)
        runtime.theta_trade = SimpleNamespace(
            slot=SimpleNamespace(pending=False), execution_halt_reason=None
        )
        runtime._synthetic_roll_halt_reason = None
        runtime.stop_event = asyncio.Event()
        runtime.log = Mock()

        runtime._canary29_latch_checkpoint_failure(OSError("disk"))

        self.assertTrue(runtime.stop_event.is_set())
        self.assertTrue(runtime.theta_trade.slot.pending)
        self.assertEqual(
            runtime.theta_trade.execution_halt_reason,
            "state_checkpoint_failed:OSError",
        )

    async def test_failed_state_callback_leaves_terminal_slot_pending_and_halted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = ThetaTradeManager(
                data_root=Path(tmp),
                config=ThetaTradeConfig(),
                execution_mode="terminal_private",
                place_fn=lambda **_kwargs: None,
                meta_fn=lambda _coin: None,
                state_change_fn=lambda: (_ for _ in ()).throw(OSError("disk")),
            )
            manager._execute_injected_place = Mock(side_effect=RuntimeError("unknown result"))
            manager._schedule_terminal_private_place(
                ThetaDecision(action="open", base_coin="RVN", side="long", reason="signal"),
                signal_ts=1,
                signal_mono_ns=1,
                okx_s={},
                bybit_s={},
            )
            await manager._terminal_place_task
            self.assertTrue(manager.slot.pending)
            self.assertTrue(str(manager.execution_halt_reason).startswith("state_checkpoint_failed:"))


if __name__ == "__main__":
    unittest.main()
