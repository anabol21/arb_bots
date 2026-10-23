from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.bot.theta_screener import ThetaSnapshot
from app.bot.theta_trade_manager import ThetaTradeConfig, ThetaTradeJournalWriter, ThetaTradeManager


def _books() -> dict:
    return {
        "BTC": {
            "okx": {
                "bid_price": 100.0, "ask_price": 101.0,
                "bid_size": 500.0, "ask_size": 500.0,
                "ts_exchange": "okx-exchange-time", "local_recv_ts_ms": 100,
                "ct_val": 0.01,
                "bids": [[100.0, 5.0]],
            },
            "bybit": {
                "bid_price": 102.0, "ask_price": 103.0,
                "bid_size": 500.0, "ask_size": 500.0,
                "ts_exchange": "bybit-exchange-time", "local_recv_ts_ms": 101,
            },
        }
    }


def _snapshot() -> list[ThetaSnapshot]:
    return [
        ThetaSnapshot(
            base_coin="BTC", side="long", ts_ms=1_700_000_000_000,
            p50_1m=0.8, floor_tf_select_a25=0.2, theta_1m=0.6,
            computed_at_ms=1_700_000_000_001,
        ),
        ThetaSnapshot(
            base_coin="BTC", side="short", ts_ms=1_700_000_000_000,
            p50_1m=0.1, floor_tf_select_a25=0.05, theta_1m=0.05,
            computed_at_ms=1_700_000_000_001,
        ),
    ]


def _context() -> dict:
    return {
        "trade_observation_enabled": True,
        "cycle_id": "cycle-1", "run_id": "run-1",
        "timing": {
            "cycle_started": {"wall_ms": 1_700_000_000_000, "monotonic_ns": 10},
            "vector_compute_started": {"wall_ms": 1_700_000_000_001, "monotonic_ns": 11},
            "vector_ready": {"wall_ms": 1_700_000_000_002, "monotonic_ns": 12},
        },
    }


class TradeObservationTests(unittest.IsolatedAsyncioTestCase):
    async def _wait_worker(self, manager: ThetaTradeManager) -> None:
        for _ in range(200):
            if manager._terminal_place_task is None:
                return
            await asyncio.sleep(0.001)
        self.fail("terminal worker did not finish")

    async def test_only_confirmed_open_and_close_append_observations(self) -> None:
        root = Path(tempfile.mkdtemp())
        actions = iter(("open", "close"))
        calls: list[dict] = []
        decided_quotes: list[float] = []
        quotes = _books()

        def decide(**_kwargs: object) -> SimpleNamespace:
            action = next(actions)
            decided_quotes.append(_kwargs["quotes"]["BTC"]["okx"]["bid_price"])
            quotes["BTC"]["okx"]["bid_price"] = 999.0
            return SimpleNamespace(
                action=action, coin="BTC", base_coin="BTC", side="long",
                reason=f"{action}_reason", theta_1m=0.6,
                opposite_theta_1m=0.05, potential_pp=0.4,
            )

        def place(**kwargs: object) -> SimpleNamespace:
            calls.append({"place_bid": kwargs["okx_book"]["bid_price"], **dict(kwargs)})
            kwargs["okx_book"]["bid_price"] = 999.0
            return SimpleNamespace(
                completed=True, keep_pending=False, fill_ts_ms=1_700_000_000_050,
                okx_filled_qty="10", bybit_filled_qty="0.1", coin_qty="0.1",
                okx_submitted_qty="10", bybit_submitted_qty="0.1",
            )

        manager = ThetaTradeManager(
            data_root=root, config=ThetaTradeConfig(notional_usdt=10.0),
            place_fn=place, meta_fn=lambda _coin: object(), decide_fn=decide,
            execution_mode="terminal_private",
        )
        for _ in range(2):
            quotes["BTC"]["okx"]["bid_price"] = 100.0
            await manager.on_theta_snapshots_async(
                _snapshot(), quotes=quotes, coin_order=("BTC",),
                now_ms=1_700_000_000_000, cycle_context=_context(),
            )
            await self._wait_worker(manager)

        path = root / "theta_trades/event_date=2023-11-14/trades.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines()]
        observations = [row for row in records if row.get("record_type") == "trade_observation"]
        self.assertEqual([row["event"] for row in observations], ["open", "close"])
        self.assertEqual([row["status"] for row in observations], [
            "terminal_completed_history_pending", "terminal_completed_history_pending"
        ])
        self.assertEqual(observations[0]["run_id"], "run-1")
        self.assertEqual(observations[0]["signal_vector"]["okx"]["bid_price"], 100.0)
        self.assertNotIn("bids", observations[0]["signal_vector"]["okx"])
        self.assertIsNotNone(observations[0]["planned_qty"]["okx_planned_qty"])
        self.assertEqual(observations[0]["planned_submitted_coin_qty_by_venue"],
                         {"okx": "0.1", "bybit": "0.1"})
        self.assertEqual(observations[0]["planned_submitted_qty_by_venue"],
                         {"okx": "10", "bybit": "0.1"})
        self.assertEqual(observations[0]["actual_terminal_filled_qty_by_venue"],
                         {"okx": "10", "bybit": "0.1"})
        self.assertEqual(observations[0]["chronometry_ref"]["intent_id"], observations[0]["intent_id"])
        hex_intent = observations[0]["intent_id"].replace("-", "")[:32]
        self.assertEqual(observations[0]["bybit_order_link_id"], f"b{hex_intent}"[:36])
        self.assertEqual(observations[0]["okx_client_order_id"], f"o{hex_intent}"[:32])
        self.assertEqual(observations[0]["place_result_fill_wall_ms"], 1_700_000_000_050)
        self.assertEqual(
            observations[0]["place_result_fill_wall_source"],
            "max_local_receive_wall_ms_from_terminal_frames",
        )
        self.assertEqual(decided_quotes, [100.0, 100.0])
        self.assertEqual([call["place_bid"] for call in calls], [100.0, 100.0])
        timing = observations[0]["timing"]
        self.assertLessEqual(timing["cycle_started"]["monotonic_ns"], timing["vector_ready"]["monotonic_ns"])
        self.assertIn("worker_submitted", timing)
        self.assertIn("worker_started", timing)
        self.assertIn("place_result_consumed", timing)
        self.assertIn("append_started", timing)
        self.assertEqual(observations[1]["close_of_trade_id"], observations[0]["trade_id"])

    async def test_failed_partial_and_observation_write_failure_do_not_change_trade_state(self) -> None:
        root = Path(tempfile.mkdtemp())

        def failed_place(**_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                completed=False, keep_pending=True, send_attempted=True,
                abort="asymmetric_fill",
            )

        manager = ThetaTradeManager(
            data_root=root, config=ThetaTradeConfig(notional_usdt=10.0),
            place_fn=failed_place, meta_fn=lambda _coin: object(),
            decide_fn=lambda **_kwargs: SimpleNamespace(
                action="open", coin="BTC", base_coin="BTC", side="long", reason="open"
            ),
            execution_mode="terminal_private",
        )
        await manager.on_theta_snapshots_async(
            _snapshot(), quotes=_books(), coin_order=("BTC",),
            now_ms=1_700_000_000_000, cycle_context=_context(),
        )
        await self._wait_worker(manager)
        self.assertTrue(manager.slot.pending)
        self.assertEqual(manager.execution_halt_reason, "asymmetric_fill")
        path = root / "theta_trades/event_date=2023-11-14/trades.jsonl"
        self.assertFalse(path.exists())

        class FailingWriter(ThetaTradeJournalWriter):
            def append_trade_observation(self, _row: object) -> list[Path]:
                raise OSError("disk unavailable")

        success = ThetaTradeManager(
            data_root=root / "isolated", config=ThetaTradeConfig(notional_usdt=10.0),
            journal=FailingWriter(root / "isolated"),
            place_fn=lambda **_kwargs: SimpleNamespace(
                completed=True, keep_pending=False, fill_ts_ms=1_700_000_000_050,
                okx_filled_qty="0.1", bybit_filled_qty="0.1", coin_qty="0.1",
            ),
            meta_fn=lambda _coin: object(),
            decide_fn=lambda **_kwargs: SimpleNamespace(
                action="open", coin="BTC", base_coin="BTC", side="long", reason="open"
            ),
            execution_mode="terminal_private",
        )
        logs: list[str] = []
        success._log = logs.append
        await success.on_theta_snapshots_async(
            _snapshot(), quotes=_books(), coin_order=("BTC",),
            now_ms=1_700_000_000_000, cycle_context=_context(),
        )
        await self._wait_worker(success)
        self.assertIsNotNone(success.slot.position)
        self.assertFalse(success.slot.pending)
        self.assertIsNone(success.execution_halt_reason)
        self.assertTrue(any("trade_observation_append | status=fail" in row for row in logs))

    async def test_noneligible_manager_keeps_quote_and_cycle_context_untouched(self) -> None:
        quotes = _books()
        context = {"trade_observation_enabled": False, "marker": "keep"}
        seen_quotes: list[bool] = []
        manager = ThetaTradeManager(
            data_root=Path(tempfile.mkdtemp()),
            config=ThetaTradeConfig(notional_usdt=10.0),
            place_fn=lambda **_kwargs: None,
            meta_fn=lambda _coin: object(),
            decide_fn=lambda **kwargs: (
                seen_quotes.append(kwargs["quotes"] is quotes)
                or SimpleNamespace(action="skip", reason="no_signal")
            ),
            execution_mode="terminal_private",
        )
        with patch(
            "app.bot.theta_trade_manager._freeze_quotes",
            side_effect=AssertionError("noneligible manager froze quotes"),
        ) as freeze:
            with patch(
                "app.bot.theta_trade_manager._observation_cycle_context",
                side_effect=AssertionError("noneligible manager created observation metadata"),
            ) as add_observation_metadata:
                await manager.on_theta_snapshots_async(
                    _snapshot(), quotes=quotes, coin_order=("BTC",),
                    now_ms=1_700_000_000_000, cycle_context=context,
                )
        freeze.assert_not_called()
        add_observation_metadata.assert_not_called()
        self.assertEqual(seen_quotes, [True])
        self.assertEqual(context, {"trade_observation_enabled": False, "marker": "keep"})

    def test_runtime_enables_context_only_for_terminal_gear23_profile(self) -> None:
        with patch.dict(sys.modules, {"websockets": SimpleNamespace(connect=None)}):
            from app.bot.runtime import BotRuntime

        runtime = BotRuntime.__new__(BotRuntime)
        runtime._terminal_private_execution = True
        runtime.profile = "gear22_live_canary"
        runtime._canary29_policy = "gear22"
        runtime._private_warm = SimpleNamespace(run_id="run-1")
        context = runtime._trade_observation_cycle_context()
        self.assertTrue(context["trade_observation_enabled"])
        self.assertEqual(context["run_id"], "run-1")
        runtime._canary29_policy = "synthetic"
        self.assertEqual(runtime._trade_observation_cycle_context(), {})
        runtime.profile = "synthetic_roll"
        self.assertEqual(runtime._trade_observation_cycle_context(), {})


if __name__ == "__main__":
    unittest.main()
