"""Gear 2.2 live canary: fail-closed gate, stub unchanged, Contour B place."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

from app.bot.stub_broker import InstrumentMeta
from app.bot.theta_screener import ThetaSnapshot
from app.bot.theta_trade_manager import (
    DEFAULT_LIVE_CANARY_NOTIONAL_USDT,
    GEAR22_HTML_TOP30,
    ThetaLiveSendError,
    ThetaTradeConfig,
    ThetaTradeManager,
    ThetaTradeRecoveryError,
    assert_theta_live_send_gates,
    theta_live_send_requested,
    theta_trade_enabled,
)
from research.gear22_backtest.policy import PolicyParams


def _snap(
    coin: str,
    side: str,
    theta_1m: float | None,
    *,
    floor: float = 0.20,
    p50_1m: float | None = None,
    ts_ms: int = 1_700_000_000_000,
) -> ThetaSnapshot:
    p1 = p50_1m if p50_1m is not None else (
        (floor + theta_1m) if theta_1m is not None else None
    )
    return ThetaSnapshot(
        base_coin=coin,
        side=side,
        ts_ms=ts_ms,
        p50_1m=p1,
        p50_5m=p1,
        floor_tf_select_a25=floor,
        theta_1m=theta_1m,
        theta_5m=(p1 - floor) if p1 is not None else None,
        computed_at_ms=ts_ms + 1,
    )


def _books(*, ask_sz: float = 10.0, bid_sz: float = 10.0) -> dict:
    return {
        "okx": {
            "bid_price": 99.0,
            "ask_price": 100.0,
            "bid_size": bid_sz,
            "ask_size": ask_sz,
            "local_recv_ts_ms": 1,
        },
        "bybit": {
            "bid_price": 100.2,
            "ask_price": 100.5,
            "bid_size": bid_sz,
            "ask_size": ask_sz,
            "local_recv_ts_ms": 1,
        },
    }


def _frozen() -> PolicyParams:
    return PolicyParams(
        theta_open=0.50,
        p50_open=0.60,
        min_profit_pp=0.20,
        min_theta_close=0.05,
        fee_round_trip_pp=0.30,
    )


def _qualify_snaps() -> list[ThetaSnapshot]:
    return [
        _snap("KAITO", "long", 0.60, p50_1m=0.80, floor=0.20),
        _snap("KAITO", "short", 0.01),
    ]


def _meta(coin: str = "KAITO") -> InstrumentMeta:
    return InstrumentMeta(
        base_coin=coin,
        okx_symbol=f"{coin}-USDT-SWAP",
        bybit_symbol=f"{coin}USDT",
        okx_lot_size=1.0,
        okx_min_size=0.01,
        bybit_qty_step=0.01,
        bybit_min_order_qty=0.01,
        bybit_min_notional_value=5.0,
    )


class FakeLiveBroker:
    """Records place() calls; ACK success unless abort_next is set."""

    def __init__(self, notional_usdt: float = 20.0) -> None:
        self.notional_usdt = float(notional_usdt)
        self.calls: list[dict[str, Any]] = []
        self.position: Optional[str] = None
        self.held_coin: Optional[str] = None
        self.abort_next: Optional[str] = None

    def place(self, **kwargs: Any) -> Optional[str]:
        self.calls.append(dict(kwargs))
        if self.abort_next:
            reason = self.abort_next
            self.abort_next = None
            return reason
        side = str(kwargs.get("spread_side") or "")
        if side in ("open_long", "open_short"):
            self.position = side
            self.held_coin = str(kwargs.get("base_coin") or "").upper()
        elif side == "close":
            self.position = None
            self.held_coin = None
        return None


class LiveGateTests(unittest.TestCase):
    def test_gate_off_for_stub_would_send(self) -> None:
        env = {
            "BBOT_PROFILE": "gear22_would_send",
            "BBOT_BROKER": "stub",
        }
        self.assertFalse(theta_live_send_requested("gear22_would_send", env))
        assert_theta_live_send_gates("gear22_would_send", env)

    def test_profile_live_canary_requests_send(self) -> None:
        self.assertTrue(theta_live_send_requested("gear22_live_canary", {}))
        self.assertTrue(theta_live_send_requested("gear22_live", {}))
        self.assertTrue(theta_trade_enabled("gear22_live_canary", {}))

    def test_env_flag_on_would_send_profile_still_requests(self) -> None:
        env = {"BBOT_THETA_LIVE_SEND": "1", "BBOT_BROKER": "stub"}
        self.assertTrue(theta_live_send_requested("gear22_would_send", env))

    def test_fail_closed_without_live_orders(self) -> None:
        env = {
            "BBOT_PROFILE": "gear22_live_canary",
            "BBOT_BROKER": "private_live",
            "VENUE": "live",
        }
        with self.assertRaises(ThetaLiveSendError) as ctx:
            assert_theta_live_send_gates("gear22_live_canary", env)
        self.assertIn("LIVE_ORDERS=1", str(ctx.exception))

    def test_fail_closed_stub_broker(self) -> None:
        env = {
            "BBOT_THETA_LIVE_SEND": "1",
            "BBOT_BROKER": "stub",
            "VENUE": "live",
            "LIVE_ORDERS": "1",
        }
        with self.assertRaises(ThetaLiveSendError) as ctx:
            assert_theta_live_send_gates("gear22_would_send", env)
        self.assertIn("private_live", str(ctx.exception))

    def test_fail_closed_wrong_venue(self) -> None:
        env = {
            "BBOT_THETA_LIVE_SEND": "1",
            "BBOT_BROKER": "private_live",
            "VENUE": "testnet",
            "LIVE_ORDERS": "1",
        }
        with self.assertRaises(ThetaLiveSendError):
            assert_theta_live_send_gates("gear22_would_send", env)

    def test_gate_ok_when_fully_armed(self) -> None:
        env = {
            "BBOT_PROFILE": "gear22_live_canary",
            "BBOT_BROKER": "private_live",
            "VENUE": "live",
            "LIVE_ORDERS": "1",
        }
        assert_theta_live_send_gates("gear22_live_canary", env)

    def test_live_notional_is_capped_at_ten_per_leg(self) -> None:
        env = {
            "BBOT_PROFILE": "gear22_live_canary",
            "BBOT_BROKER": "private_live",
            "VENUE": "live",
            "LIVE_ORDERS": "1",
            "BBOT_NOTIONAL_USDT": "20",
        }
        with self.assertRaises(ThetaLiveSendError):
            assert_theta_live_send_gates("gear22_live_canary", env)
        env["BBOT_NOTIONAL_USDT"] = "10"
        assert_theta_live_send_gates("gear22_live_canary", env)

    def test_live_send_without_place_fn_raises(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        with self.assertRaises(ThetaLiveSendError):
            ThetaTradeManager(
                data_root=tmp,
                config=ThetaTradeConfig(notional_usdt=20.0),
                live_send=True,
            )

    def test_html_top30_locked(self) -> None:
        self.assertEqual(len(GEAR22_HTML_TOP30), 30)
        self.assertEqual(GEAR22_HTML_TOP30[0], "KAITO")
        self.assertIn("WAL", GEAR22_HTML_TOP30)
        self.assertIn("GIGGLE", GEAR22_HTML_TOP30)
        self.assertEqual(DEFAULT_LIVE_CANARY_NOTIONAL_USDT, 10.0)

    def test_policy_profile_accepts_live_canary(self) -> None:
        from app.policy.trade_manager import (
            variation_for_profile,
            uses_gear2_market_manager,
        )

        variation_for_profile("gear22_live_canary")
        self.assertFalse(uses_gear2_market_manager("gear22_live_canary"))


class StubPathUnchangedTests(unittest.TestCase):
    def test_would_send_still_sleeps_70ms_and_does_not_place(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        slept: list[float] = []
        broker = FakeLiveBroker(notional_usdt=10.0)

        def _sleep(seconds: float) -> None:
            slept.append(seconds)

        mgr = ThetaTradeManager(
            data_root=tmp,
            config=ThetaTradeConfig(
                fill_delay_ms=70,
                notional_usdt=10.0,
                policy_params=_frozen(),
            ),
            sleep_fn=_sleep,
            live_send=False,
            place_fn=broker.place,
            meta_fn=_meta,
        )
        rows = mgr.on_theta_snapshots(
            _qualify_snaps(),
            quotes={"KAITO": _books()},
            now_ms=1_000_000,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "open")
        self.assertEqual(rows[0]["fill_ts_ms"], 1_000_070)
        self.assertEqual(rows[0]["latency_ms"], 70)
        self.assertTrue(rows[0]["would_send"])
        self.assertFalse(rows[0]["send"])
        self.assertFalse(rows[0]["live_send"])
        self.assertEqual(rows[0]["fill_model"], "synthetic_delay")
        self.assertEqual(slept, [0.07])
        self.assertEqual(broker.calls, [])

    def test_insufficient_size_does_not_place_on_live_manager(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        broker = FakeLiveBroker()
        slept: list[float] = []
        mgr = ThetaTradeManager(
            data_root=tmp,
            config=ThetaTradeConfig(
                fill_delay_ms=70,
                notional_usdt=20.0,
                policy_params=_frozen(),
            ),
            sleep_fn=lambda s: slept.append(s),
            live_send=True,
            live_recovery_confirmed=True,
            place_fn=broker.place,
            meta_fn=_meta,
        )
        quotes = {"KAITO": _books(ask_sz=0.0001, bid_sz=0.0001)}
        rows = mgr.on_theta_snapshots(
            _qualify_snaps(), quotes=quotes, now_ms=5_000
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "skip")
        self.assertEqual(rows[0]["reject_reason"], "insufficient_size")
        self.assertEqual(broker.calls, [])
        self.assertEqual(slept, [])
        self.assertIsNone(mgr.slot.position)


class LiveSendPathTests(unittest.TestCase):
    def test_live_manager_refuses_decisions_before_reconciliation(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        broker = FakeLiveBroker(notional_usdt=20.0)
        mgr = ThetaTradeManager(
            data_root=tmp,
            config=ThetaTradeConfig(
                notional_usdt=20.0,
                policy_params=_frozen(),
            ),
            live_send=True,
            place_fn=broker.place,
            meta_fn=_meta,
        )
        with self.assertRaisesRegex(
            ThetaTradeRecoveryError, "live_reconciliation_required"
        ):
            mgr.on_theta_snapshots(
                _qualify_snaps(), quotes={"KAITO": _books()}, now_ms=1_000
            )
        self.assertEqual(broker.calls, [])
        mgr.confirm_live_reconciliation(matched=True, reason="matched")
        rows = mgr.on_theta_snapshots(
            _qualify_snaps(), quotes={"KAITO": _books()}, now_ms=2_000
        )
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["send"])

    def test_failed_live_reconciliation_latches_block(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        broker = FakeLiveBroker(notional_usdt=20.0)
        mgr = ThetaTradeManager(
            data_root=tmp,
            config=ThetaTradeConfig(
                notional_usdt=20.0,
                policy_params=_frozen(),
            ),
            live_send=True,
            place_fn=broker.place,
            meta_fn=_meta,
        )
        with self.assertRaisesRegex(
            ThetaTradeRecoveryError, "live_reconciliation_failed"
        ):
            mgr.confirm_live_reconciliation(
                matched=False, reason="expected_open_mismatch"
            )
        with self.assertRaisesRegex(
            ThetaTradeRecoveryError, "trade_history_unhealthy"
        ):
            mgr.confirm_live_reconciliation(matched=True, reason="matched")

    def test_restart_replays_live_open_and_checks_broker_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            broker = FakeLiveBroker(notional_usdt=20.0)
            config = ThetaTradeConfig(
                fill_delay_ms=70,
                notional_usdt=20.0,
                policy_params=_frozen(),
            )
            first = ThetaTradeManager(
                data_root=root,
                config=config,
                live_send=True,
                live_recovery_confirmed=True,
                place_fn=broker.place,
                meta_fn=_meta,
            )
            rows = first.on_theta_snapshots(
                _qualify_snaps(), quotes={"KAITO": _books()}, now_ms=2_000_000
            )
            trade_id = rows[0]["trade_id"]

            restarted = ThetaTradeManager(
                data_root=root,
                config=config,
                live_send=True,
                live_recovery_confirmed=True,
                place_fn=broker.place,
                meta_fn=_meta,
            )
            self.assertIsNotNone(restarted.slot.position)
            assert restarted.slot.position is not None
            self.assertEqual(restarted.slot.position.trade_id, trade_id)
            restarted.assert_local_broker_state(
                broker_position=broker.position,
                held_coin=broker.held_coin,
            )
            with self.assertRaisesRegex(
                ThetaTradeRecoveryError, "local_broker_state_mismatch"
            ):
                restarted.assert_local_broker_state(
                    broker_position=None,
                    held_coin=None,
                )

    def test_live_place_notional_20_shared_trade_id_no_sleep(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        broker = FakeLiveBroker(notional_usdt=20.0)

        def _boom(_seconds: float) -> None:
            raise AssertionError("70ms fill sleep must not run on live send")

        mgr = ThetaTradeManager(
            data_root=tmp,
            config=ThetaTradeConfig(
                fill_delay_ms=70,
                notional_usdt=20.0,
                policy_params=_frozen(),
            ),
            sleep_fn=_boom,
            live_send=True,
            live_recovery_confirmed=True,
            place_fn=broker.place,
            meta_fn=_meta,
        )
        rows = mgr.on_theta_snapshots(
            _qualify_snaps(),
            quotes={"KAITO": _books()},
            now_ms=2_000_000,
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["event"], "open")
        self.assertEqual(row["notional_usdt"], 20.0)
        self.assertTrue(row["would_send"])
        self.assertTrue(row["send"])
        self.assertTrue(row["live_send"])
        self.assertEqual(row["fill_model"], "venue_ack")
        self.assertEqual(row["signal_ts_ms"], 2_000_000)
        self.assertNotEqual(row["fill_ts_ms"], 2_000_070)
        self.assertEqual(len(broker.calls), 1)
        call = broker.calls[0]
        self.assertEqual(call["spread_side"], "open_long")
        self.assertEqual(call["base_coin"], "KAITO")
        self.assertEqual(call["intent_id"], row["trade_id"])
        self.assertEqual(call["extra"]["trade_id"], row["trade_id"])
        self.assertEqual(row["intent_id"], row["trade_id"])
        self.assertEqual(broker.notional_usdt, 20.0)
        self.assertIsNotNone(mgr.slot.position)
        self.assertEqual(mgr.slot.position.trade_id, row["trade_id"])

    def test_live_abort_keeps_slot_flat_and_send_false(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        broker = FakeLiveBroker(notional_usdt=20.0)
        broker.abort_next = "okx_inst_id_code_missing"
        mgr = ThetaTradeManager(
            data_root=tmp,
            config=ThetaTradeConfig(
                fill_delay_ms=70,
                notional_usdt=20.0,
                policy_params=_frozen(),
            ),
            sleep_fn=lambda _s: (_ for _ in ()).throw(
                AssertionError("no sleep on live abort")
            ),
            live_send=True,
            live_recovery_confirmed=True,
            place_fn=broker.place,
            meta_fn=_meta,
        )
        rows = mgr.on_theta_snapshots(
            _qualify_snaps(),
            quotes={"KAITO": _books()},
            now_ms=3_000_000,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "open")
        self.assertFalse(rows[0]["send"])
        self.assertTrue(rows[0]["would_send"])
        self.assertEqual(rows[0]["live_abort"], "okx_inst_id_code_missing")
        self.assertIsNone(mgr.slot.position)

    def test_async_live_path_skips_sleep(self) -> None:
        import asyncio

        tmp = Path(tempfile.mkdtemp())
        broker = FakeLiveBroker(notional_usdt=20.0)

        async def _boom(_seconds: float) -> None:
            raise AssertionError("async live send must not await fill delay")

        mgr = ThetaTradeManager(
            data_root=tmp,
            config=ThetaTradeConfig(
                fill_delay_ms=70,
                notional_usdt=20.0,
                policy_params=_frozen(),
            ),
            sleep_fn=_boom,
            live_send=True,
            live_recovery_confirmed=True,
            place_fn=broker.place,
            meta_fn=_meta,
        )
        rows = asyncio.run(
            mgr.on_theta_snapshots_async(
                _qualify_snaps(),
                quotes={"KAITO": _books()},
                now_ms=4_000_000,
            )
        )
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["send"])
        self.assertEqual(len(broker.calls), 1)
        self.assertEqual(broker.calls[0]["intent_id"], rows[0]["trade_id"])


class RuntimeLiveCanaryTests(unittest.TestCase):
    @unittest.skipUnless(
        __import__("importlib").util.find_spec("websockets") is not None,
        "websockets not installed",
    )
    def test_runtime_refuses_legacy_dual_ack_live_path_even_when_armed(self) -> None:
        env = {
            "BBOT_MODE": "policy",
            "BBOT_PROFILE": "gear22_live_canary",
            "BBOT_BROKER": "private_live",
            "BBOT_THETA_LIVE_SEND": "1",
            "LIVE_ORDERS": "1",
            "VENUE": "live",
        }
        with patch.dict("os.environ", env, clear=False):
            from app.bot.runtime import BotRuntime

            with self.assertRaisesRegex(
                RuntimeError, "ev2_live_execution_adapter_not_integrated"
            ):
                BotRuntime()

    @unittest.skipUnless(
        __import__("importlib").util.find_spec("websockets") is not None,
        "websockets not installed",
    )
    def test_runtime_refuses_live_canary_without_live_orders(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        env = {
            "BBOT_MODE": "policy",
            "BBOT_PROFILE": "gear22_live_canary",
            "BBOT_BROKER": "stub",
            "BBOT_COINS": "KAITO,WAL",
            "BBOT_DATA_ROOT": str(tmp / "bbot"),
            "BBOT_LOG_PATH": str(tmp / "bbot.log"),
            "BBOT_FLOOR_WARM": "0",
            "LIVE_ORDERS": "0",
            "VENUE": "testnet",
            "BBOT_THETA_LIVE_SEND": "1",
        }
        with patch.dict("os.environ", env, clear=False):
            from app.bot.runtime import BotRuntime

            with self.assertRaises(ThetaLiveSendError):
                BotRuntime()

    @unittest.skipUnless(
        __import__("importlib").util.find_spec("websockets") is not None,
        "websockets not installed",
    )
    def test_runtime_stub_would_send_still_enables_theta_without_place(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        env = {
            "BBOT_MODE": "policy",
            "BBOT_PROFILE": "gear22_would_send",
            "BBOT_BROKER": "stub",
            "BBOT_COINS": "KAITO,WAL",
            "BBOT_DATA_ROOT": str(tmp / "bbot"),
            "BBOT_LOG_PATH": str(tmp / "bbot.log"),
            "BBOT_THETA_TRADE": "1",
            "BBOT_FLOOR_WATCH": "1",
            "BBOT_TW_P50_WATCH": "1",
            "BBOT_THETA_WATCH": "1",
            "BBOT_FLOOR_WARM": "0",
            "LIVE_ORDERS": "0",
            "BBOT_THETA_LIVE_SEND": "0",
        }
        with patch.dict("os.environ", env, clear=False):
            from app.bot.runtime import BotRuntime

            rt = BotRuntime()
            self.assertEqual(rt.profile, "gear22_would_send")
            self.assertTrue(rt.theta_trade_enabled)
            self.assertIsNotNone(rt.theta_trade)
            assert rt.theta_trade is not None
            self.assertFalse(rt.theta_trade.live_send)
            self.assertIsNone(rt.theta_trade._place_fn)  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
