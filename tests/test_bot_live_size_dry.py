"""S2 dry harness + S5 LiveBroker gates. Default path does not send."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app.bot.broker import make_broker
from app.bot.journal import JournalWriter
from app.bot.private.live_broker import (
    LiveBroker,
    LiveBrokerGateError,
    LiveSendResult,
    default_live_send_pair,
    invoke_live_send_pair,
    make_live_broker,
)
from app.bot.private.order_symbols import live_size_okx_ct_val
from app.bot.sizing import plan_dual_leg_qty
from app.bot.private.order_sender import assert_default_entrypoint_cannot_transport
from app.bot.private.sized_dual_leg import (
    LIVE_SIZE_COINS,
    dry_random_size_plan,
    main_dry_size_plan,
    main_sized_parallel_cycle,
    pick_contour_coin,
    run_sized_parallel_cycle,
)
from app.bot.private.venue import send_allowed
from app.bot.runtime import load_universe
from app.bot.stub_broker import StubBroker


def _books(px: float) -> dict[str, float]:
    return {"ask_price": px, "bid_price": px, "ask_size": 1000.0, "bid_size": 1000.0}


class ContourTests(unittest.TestCase):
    def test_random_pick_is_sol_or_xrp(self) -> None:
        import random

        rng = random.Random(0)
        seen = {pick_contour_coin(rng=rng) for _ in range(20)}
        self.assertTrue(seen <= set(LIVE_SIZE_COINS))
        self.assertFalse(seen & {"BTC", "ETH"})

    def test_rejects_btc(self) -> None:
        with self.assertRaises(Exception):
            pick_contour_coin(coin="BTC")


class DrySizePlanTests(unittest.TestCase):
    def test_dry_sol_prints_fingerprints_and_does_not_send(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = {
                "VENUE": "testnet",
                "LIVE_ORDERS": "0",
                "BBOT_PRIVATE_DATA_ROOT": td,
            }
            report = dry_random_size_plan(env=env, coin="SOL")
            self.assertEqual(report.status, "ok")
            self.assertEqual(report.coin, "SOL")
            self.assertFalse(report.send_allowed)
            self.assertEqual(report.orders_sent, 0)
            self.assertTrue(report.sends_blocked)
            self.assertEqual(report.live_orders, "0")
            self.assertIn("bybit_open", report.fingerprints)
            self.assertIn("okx_open", report.fingerprints)
            self.assertTrue(report.fingerprints["bybit_open"].startswith("fp_"))
            self.assertTrue(report.fingerprints["okx_open"].startswith("fp_"))
            self.assertEqual(report.spec["bybit_symbol"], "SOLUSDT")
            self.assertEqual(report.spec["okx_symbol"], "SOL-USDT-SWAP")
            self.assertEqual(report.spec["coin_qty"], report.spec["okx_qty"])
            self.assertEqual(report.spec["okx_qty"], report.spec["bybit_qty"])
            self.assertTrue(report.spec["depth_ok"])
            self.assertEqual(report.size_plan["coin_qty"], report.size_plan["okx_qty"])
            self.assertTrue(report.size_plan["depth_ok"])
            self.assertNotIn("BTC", json.dumps(report.as_public_dict()))
            self.assertNotIn("ETH", json.dumps(report.as_public_dict()))
            self.assertNotIn("bbot-gear2", report.data_root)
            probe = Path(td) / "probes" / "dry_size_plan.jsonl"
            self.assertTrue(probe.is_file())

    def test_dry_xrp_ok(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = {"LIVE_ORDERS": "0", "BBOT_PRIVATE_DATA_ROOT": td}
            report = dry_random_size_plan(env=env, coin="XRP", write=False)
            self.assertEqual(report.status, "ok")
            self.assertEqual(report.coin, "XRP")
            self.assertEqual(report.orders_sent, 0)

    def test_dry_cli_send_stays_off(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = {"VENUE": "testnet", "LIVE_ORDERS": "0", "BBOT_PRIVATE_DATA_ROOT": td}
            with patch.dict(os.environ, env, clear=False):
                rc = main_dry_size_plan(["--dry-size-plan", "--coin=SOL"], env=env)
            self.assertEqual(rc, 0)
            assert_default_entrypoint_cannot_transport()
            self.assertFalse(send_allowed(env))

    def test_default_cli_does_not_send(self) -> None:
        from app.bot.private.harness_readonly import run_readonly_harness
        from app.bot.private.order_sender import get_runtime_transport

        with tempfile.TemporaryDirectory() as td:
            env = {
                "VENUE": "testnet",
                "LIVE_ORDERS": "0",
                "BBOT_PRIVATE_DATA_ROOT": td,
            }
            with patch.dict(os.environ, env, clear=False):
                report = run_readonly_harness(env, allow_missing_secrets=True)
            self.assertEqual(int(report.get("orders_sent") or 0), 0)
            self.assertFalse(report.get("send_allowed"))
            self.assertEqual(str(report.get("LIVE_ORDERS") or "0"), "0")
            self.assertIsNone(get_runtime_transport())
            assert_default_entrypoint_cannot_transport()


class SizedCycleScaffoldTests(unittest.TestCase):
    def test_cycle_refuses_without_gates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            payload = run_sized_parallel_cycle(
                env={"VENUE": "testnet", "LIVE_ORDERS": "0", "BBOT_PRIVATE_DATA_ROOT": td},
                coin="SOL",
            )
        self.assertEqual(payload["status"], "rejected_before_socket")
        self.assertEqual(payload["orders_sent"], 0)
        self.assertTrue(payload["sends_blocked"])
        self.assertFalse(payload["send_unlocked"])

    def test_cycle_requires_approve_even_with_gates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            payload = run_sized_parallel_cycle(
                env={
                    "VENUE": "live",
                    "LIVE_ORDERS": "1",
                    "BBOT_PRIVATE_SIZED_CYCLE": "1",
                    "BBOT_PRIVATE_DATA_ROOT": td,
                },
                coin="SOL",
                approve_one_shot=False,
            )
        self.assertEqual(payload["status"], "approval_required")
        self.assertEqual(payload["orders_sent"], 0)
        self.assertFalse(payload["send_unlocked"])

    def test_cycle_cli_never_sends(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = {"VENUE": "live", "LIVE_ORDERS": "1", "BBOT_PRIVATE_DATA_ROOT": td}
            rc = main_sized_parallel_cycle(["--sized-parallel-cycle", "--coin=XRP"], env=env)
        self.assertEqual(rc, 1)
        assert_default_entrypoint_cannot_transport()


class LiveBrokerGateTests(unittest.TestCase):
    def _journal(self, tmp: Path) -> JournalWriter:
        return JournalWriter(tmp)

    def test_default_make_broker_is_stub(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        broker = make_broker(
            data_root=tmp,
            journal=self._journal(tmp),
            trade_lat_ms=100,
            notional_usdt=10.0,
            log=lambda _m: None,
            env={"BBOT_BROKER": "stub", "VENUE": "testnet", "LIVE_ORDERS": "0"},
        )
        self.assertIsInstance(broker, StubBroker)

    def test_live_flags_without_private_live_stay_stub(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        broker = make_broker(
            data_root=tmp,
            journal=self._journal(tmp),
            trade_lat_ms=100,
            notional_usdt=10.0,
            log=lambda _m: None,
            env={"BBOT_BROKER": "stub", "VENUE": "live", "LIVE_ORDERS": "1"},
        )
        self.assertIsInstance(broker, StubBroker)

    def test_private_live_refuses_without_live_flags(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        journal = self._journal(tmp)
        with self.assertRaises(LiveBrokerGateError):
            make_broker(
                data_root=tmp,
                journal=journal,
                trade_lat_ms=100,
                notional_usdt=10.0,
                log=lambda _m: None,
                env={"BBOT_BROKER": "private_live", "VENUE": "testnet", "LIVE_ORDERS": "0"},
            )
        with self.assertRaises(LiveBrokerGateError):
            make_broker(
                data_root=tmp,
                journal=journal,
                trade_lat_ms=100,
                notional_usdt=10.0,
                log=lambda _m: None,
                env={"BBOT_BROKER": "private_live", "VENUE": "live", "LIVE_ORDERS": "0"},
            )

    def test_private_live_refuses_without_broker_kind(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        with self.assertRaises(LiveBrokerGateError):
            make_live_broker(
                data_root=tmp,
                journal=self._journal(tmp),
                trade_lat_ms=100,
                notional_usdt=10.0,
                log=lambda _m: None,
                env={"BBOT_BROKER": "stub", "VENUE": "live", "LIVE_ORDERS": "1"},
            )

    def test_private_live_accepts_gear2_data_root(self) -> None:
        tmp = Path(tempfile.mkdtemp()) / "bbot-gear2"
        tmp.mkdir(parents=True)
        broker = make_broker(
            data_root=tmp,
            journal=self._journal(tmp),
            trade_lat_ms=100,
            notional_usdt=10.0,
            log=lambda _m: None,
            env={"BBOT_BROKER": "private_live", "VENUE": "live", "LIVE_ORDERS": "1"},
        )
        self.assertIsInstance(broker, LiveBroker)
        self.assertTrue(broker.send_unlocked)

    def _mock_send(self, **kwargs: object) -> LiveSendResult:
        del kwargs
        return LiveSendResult(
            status="ok",
            orders_sent=2,
            send=True,
            fill_ts_ms=50,
            ack_ts_ms=40,
            fill_okx=150.1,
            fill_bybit=149.9,
        )

    def test_place_sol_sends_immediately_and_rejects_btc_eth(self) -> None:
        tmp = Path(tempfile.mkdtemp()) / "bbot-gear2"
        tmp.mkdir(parents=True)
        journal = self._journal(tmp)
        broker = make_live_broker(
            data_root=tmp,
            journal=journal,
            trade_lat_ms=100,
            notional_usdt=10.0,
            log=lambda _m: None,
            env={"BBOT_BROKER": "private_live", "VENUE": "live", "LIVE_ORDERS": "1"},
            send_pair_fn=self._mock_send,
        )
        self.assertIsInstance(broker, LiveBroker)
        self.assertTrue(broker.send_unlocked)
        uni = load_universe()
        abort_btc = broker.place(
            spread_side="open_long",
            base_coin="BTC",
            signal_ts_ms=1,
            okx_book=_books(100000),
            bybit_book=_books(100000),
            meta=uni["BTC"],
        )
        self.assertEqual(abort_btc, "not_on_live_size_contour")
        abort_eth = broker.place(
            spread_side="open_short",
            base_coin="ETH",
            signal_ts_ms=1,
            okx_book=_books(4000),
            bybit_book=_books(4000),
            meta=uni["ETH"],
        )
        self.assertEqual(abort_eth, "not_on_live_size_contour")
        abort_sol = broker.place(
            spread_side="open_long",
            base_coin="SOL",
            signal_ts_ms=1,
            okx_book=_books(150),
            bybit_book=_books(150),
            meta=uni["SOL"],
        )
        self.assertIsNone(abort_sol)
        self.assertEqual(broker.position, "open_long")
        self.assertEqual(broker.held_coin, "SOL")
        self.assertFalse(broker.has_pending())
        self.assertFalse(
            broker.on_valid_tick(
                base_coin="SOL",
                event_local_ts_ms=2,
                okx_book=_books(150),
                bybit_book=_books(150),
            )
        )
        legs_dir = tmp / "journal"
        files = list(legs_dir.glob("event_date=*/legs.jsonl"))
        self.assertTrue(files)
        text = files[0].read_text(encoding="utf-8")
        self.assertIn('"send":true', text)
        self.assertIn('"would_send":true', text)
        self.assertIn('"base_coin":"SOL"', text)
        self.assertIn('"status":"filled"', text)

    def test_thin_l1_aborts_without_send(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        sent = {"n": 0}

        def refuse_if_called(**kwargs: object) -> LiveSendResult:
            sent["n"] += 1
            return LiveSendResult(status="should_not_run")

        broker = make_live_broker(
            data_root=tmp,
            journal=self._journal(tmp),
            trade_lat_ms=100,
            notional_usdt=10.0,
            log=lambda _m: None,
            env={"BBOT_BROKER": "private_live", "VENUE": "live", "LIVE_ORDERS": "1"},
            send_pair_fn=refuse_if_called,
        )
        uni = load_universe()
        thin = {"ask_price": 150, "bid_price": 150, "ask_size": 0.01, "bid_size": 0.01}
        abort = broker.place(
            spread_side="open_long",
            base_coin="SOL",
            signal_ts_ms=1,
            okx_book=thin,
            bybit_book=thin,
            meta=uni["SOL"],
        )
        self.assertEqual(abort, "l1_depth_thin")
        self.assertEqual(sent["n"], 0)
        self.assertIsNone(broker.position)

    def test_runtime_private_live_refuses_btc_eth_subscribe(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        env = {
            "BBOT_MODE": "policy",
            "BBOT_PROFILE": "gear2_would_send",
            "BBOT_DATA_ROOT": str(tmp),
            "BBOT_LOG_PATH": str(tmp / "bbot.log"),
            "BBOT_COINS": "BTC,ETH,SOL,XRP",
            "BBOT_BROKER": "private_live",
            "VENUE": "live",
            "LIVE_ORDERS": "1",
        }
        with patch.dict(os.environ, env, clear=False):
            from app.bot.runtime import BotRuntime

            with self.assertRaises(RuntimeError) as ctx:
                BotRuntime()
        self.assertIn("SOL,XRP", str(ctx.exception))

    def test_xrp_ctval_1_reproduces_w6_sample_cap(self) -> None:
        """Gear2 miss: universe sizer (ctVal=1) emits 7.1 coin-as-contract.

        Live OKX XRP-USDT-SWAP ctVal=100, so W6 notional is 7.1*100*mark ≫ $20.
        """
        from app.bot.private.order_metadata import InstrumentMetadata
        from app.bot.private.ws_w6_dual_leg import W6ProfileError, assert_w6_notional

        uni = load_universe()
        px_okx = 1.4182
        px_bybit = 1.4179
        books = _books(px_okx)
        books_b = _books(px_bybit)
        miss = plan_dual_leg_qty(
            uni["XRP"],
            px_okx,
            px_bybit,
            target_usdt=10,
            okx_book=books,
            bybit_book=books_b,
            okx_side="sell",
            bybit_side="buy",
            require_l1=True,
        )
        self.assertTrue(miss.depth_ok)
        self.assertTrue(miss.feasible)
        self.assertEqual(miss.okx_qty, Decimal("7.1"))
        self.assertEqual(miss.okx_ct_val, Decimal("1"))

        now = 1
        okx_live = InstrumentMetadata(
            venue="okx_live",
            symbol="XRP-USDT-SWAP",
            min_qty=Decimal("0.01"),
            qty_step=Decimal("0.01"),
            tick_size=Decimal("0.0001"),
            contract_multiplier=Decimal("100"),
            contract_value_ccy="USDT",
            notional_unit="usdt_per_contract",
            mark_price_usdt=Decimal("1.4182"),
            mark_asof_monotonic_ns=now,
        )
        with self.assertRaises(W6ProfileError) as ctx:
            assert_w6_notional(
                okx_live,
                format(miss.okx_qty, "f"),
                max_usd=Decimal("20"),
            )
        self.assertIn("above sample cap", str(ctx.exception))

        sized = plan_dual_leg_qty(
            uni["XRP"],
            px_okx,
            px_bybit,
            target_usdt=10,
            okx_book=books,
            bybit_book=books_b,
            okx_side="sell",
            bybit_side="buy",
            okx_ct_val=live_size_okx_ct_val("XRP"),
            require_l1=True,
        )
        self.assertTrue(sized.feasible)
        self.assertTrue(sized.depth_ok)
        self.assertEqual(sized.okx_ct_val, Decimal("100"))
        self.assertEqual(sized.coin_qty, Decimal("8"))
        self.assertEqual(sized.okx_qty, Decimal("0.08"))
        assert_w6_notional(
            okx_live,
            format(sized.okx_qty, "f"),
            max_usd=Decimal("20"),
        )

    def test_live_broker_xrp_sends_okx_contracts_not_coins(self) -> None:
        tmp = Path(tempfile.mkdtemp()) / "bbot-gear2"
        tmp.mkdir(parents=True)
        captured: dict[str, object] = {}

        def capture_send(**kwargs: object) -> LiveSendResult:
            captured.update(kwargs)
            return LiveSendResult(
                status="ok",
                orders_sent=2,
                send=True,
                fill_ts_ms=50,
                ack_ts_ms=40,
            )

        broker = make_live_broker(
            data_root=tmp,
            journal=self._journal(tmp),
            trade_lat_ms=100,
            notional_usdt=10.0,
            log=lambda _m: None,
            env={"BBOT_BROKER": "private_live", "VENUE": "live", "LIVE_ORDERS": "1"},
            send_pair_fn=capture_send,
        )
        abort = broker.place(
            spread_side="open_short",
            base_coin="XRP",
            signal_ts_ms=1,
            okx_book=_books(1.4182),
            bybit_book=_books(1.4179),
            meta=load_universe()["XRP"],
        )
        self.assertIsNone(abort)
        legs = captured["legs"]
        assert isinstance(legs, dict)
        self.assertEqual(Decimal(str(legs["okx"]["qty"])), Decimal("0.08"))
        self.assertEqual(Decimal(str(legs["bybit"]["qty"])), Decimal("8"))
        self.assertEqual(legs["okx"]["symbol"], "XRP-USDT-SWAP")

    def test_dispatch_logs_inner_w6_message(self) -> None:
        from app.bot.private.ws_w6_dual_leg import W6ProfileError

        logs: list[str] = []
        tmp = Path(tempfile.mkdtemp())
        env = {"BBOT_BROKER": "private_live", "VENUE": "live", "LIVE_ORDERS": "1"}
        with (
            patch(
                "app.bot.private.ws_gates.assert_gear2_live_send_gates",
                return_value="live",
            ),
            patch("app.bot.private.ws_socket.assert_no_default_ws_socket"),
            patch("app.bot.private.ws_socket.unbind_socket_factory"),
            patch(
                "app.bot.private.order_sender.assert_default_entrypoint_cannot_transport"
            ),
            patch(
                "app.bot.private.ws_w6_dual_leg.open_w6_production_bindings",
                side_effect=W6ProfileError("W6 notional above sample cap"),
            ),
        ):
            sent = default_live_send_pair(
                env=env,
                data_root=tmp,
                coin="XRP",
                legs={"bybit": {}, "okx": {}},
                flatten_only=False,
                log=logs.append,
            )
        self.assertEqual(sent.status, "bind_failed")
        self.assertFalse(sent.send)
        self.assertTrue(any("above sample cap" in row for row in logs))
        self.assertEqual(sent.extra.get("dispatch_error_msg"), "W6 notional above sample cap")
        self.assertEqual(sent.extra.get("msg"), "W6 notional above sample cap")

    def _restore_default_loop(self) -> None:
        """Python 3.9 asyncio.Lock() needs a current loop after asyncio.run()."""
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

    def _run_async(self, factory: object) -> None:
        try:
            asyncio.run(factory())  # type: ignore[misc]
        finally:
            self._restore_default_loop()

    def _w6_style_nested_loop_send(self, **kwargs: object) -> LiveSendResult:
        """Current W6 bind path: new loop + run_until_complete (not asyncio.run)."""
        del kwargs
        loop = asyncio.new_event_loop()
        try:

            async def _open() -> bool:
                return True

            loop.run_until_complete(_open())
        finally:
            loop.close()
        return LiveSendResult(status="ok", orders_sent=2, send=True, fill_ts_ms=50, ack_ts_ms=40)

    def test_running_loop_plus_w6_bind_reproduces_nested_loop(self) -> None:
        """Mock Gear2 loop + current place path (direct send, no offload)."""

        async def go() -> None:
            with self.assertRaises(RuntimeError) as ctx:
                self._w6_style_nested_loop_send()
            self.assertIn("another loop is running", str(ctx.exception).lower())

        self._run_async(go)

    def test_place_from_running_loop_does_not_call_asyncio_run(self) -> None:
        tmp = Path(tempfile.mkdtemp()) / "bbot-gear2"
        tmp.mkdir(parents=True)
        run_calls: list[int] = []
        send_threads: list[bool] = []

        def send_ok(**kwargs: object) -> LiveSendResult:
            del kwargs
            try:
                asyncio.get_running_loop()
                on_loop = True
            except RuntimeError:
                on_loop = False
            send_threads.append(on_loop)
            return LiveSendResult(
                status="ok",
                orders_sent=2,
                send=True,
                fill_ts_ms=50,
                ack_ts_ms=40,
            )

        broker = make_live_broker(
            data_root=tmp,
            journal=self._journal(tmp),
            trade_lat_ms=100,
            notional_usdt=10.0,
            log=lambda _m: None,
            env={"BBOT_BROKER": "private_live", "VENUE": "live", "LIVE_ORDERS": "1"},
            send_pair_fn=send_ok,
        )
        real_run = asyncio.run

        def guard_run(*args: object, **kwargs: object) -> object:
            run_calls.append(1)
            raise AssertionError("asyncio.run must not nest on the Gear2 loop")

        async def go() -> None:
            with patch("asyncio.run", side_effect=guard_run):
                abort = await broker.aplace(
                    spread_side="open_long",
                    base_coin="SOL",
                    signal_ts_ms=1,
                    okx_book=_books(150),
                    bybit_book=_books(150),
                    meta=load_universe()["SOL"],
                )
            self.assertIsNone(abort)
            self.assertEqual(broker.held_coin, "SOL")

        self._run_async(go)
        self.assertEqual(run_calls, [])
        self.assertEqual(send_threads, [False])
        del real_run

    def test_place_from_running_loop_survives_w6_style_run_until_complete(self) -> None:
        tmp = Path(tempfile.mkdtemp()) / "bbot-gear2"
        tmp.mkdir(parents=True)
        broker = make_live_broker(
            data_root=tmp,
            journal=self._journal(tmp),
            trade_lat_ms=100,
            notional_usdt=10.0,
            log=lambda _m: None,
            env={"BBOT_BROKER": "private_live", "VENUE": "live", "LIVE_ORDERS": "1"},
            send_pair_fn=self._w6_style_nested_loop_send,
        )

        async def go() -> None:
            abort = broker.place(
                spread_side="open_short",
                base_coin="XRP",
                signal_ts_ms=1,
                okx_book=_books(1.4182),
                bybit_book=_books(1.4179),
                meta=load_universe()["XRP"],
            )
            self.assertIsNone(abort)
            self.assertEqual(broker.held_coin, "XRP")

        self._run_async(go)

    def test_invoke_live_send_pair_offloads_when_loop_running(self) -> None:
        seen: list[str] = []

        def send_fn(**kwargs: object) -> LiveSendResult:
            del kwargs
            try:
                asyncio.get_running_loop()
                seen.append("on_loop")
            except RuntimeError:
                seen.append("worker")
            return LiveSendResult(status="ok", send=True, orders_sent=2)

        async def go() -> None:
            out = invoke_live_send_pair(send_fn)
            self.assertTrue(out.send)
            self.assertEqual(seen, ["worker"])

        self._run_async(go)

    def test_bind_failed_journals_inner_msg(self) -> None:
        tmp = Path(tempfile.mkdtemp()) / "bbot-gear2"
        tmp.mkdir(parents=True)
        inner = "Cannot run the event loop while another loop is running"

        def boom(**kwargs: object) -> LiveSendResult:
            del kwargs
            return LiveSendResult(
                status="bind_failed",
                reason="RuntimeError",
                send=False,
                extra={
                    "dispatch_error": "RuntimeError",
                    "dispatch_error_msg": inner,
                    "msg": inner,
                },
            )

        broker = make_live_broker(
            data_root=tmp,
            journal=self._journal(tmp),
            trade_lat_ms=100,
            notional_usdt=10.0,
            log=lambda _m: None,
            env={"BBOT_BROKER": "private_live", "VENUE": "live", "LIVE_ORDERS": "1"},
            send_pair_fn=boom,
        )
        abort = broker.place(
            spread_side="open_short",
            base_coin="XRP",
            signal_ts_ms=1,
            okx_book=_books(1.4182),
            bybit_book=_books(1.4179),
            meta=load_universe()["XRP"],
        )
        self.assertEqual(abort, "RuntimeError")
        files = list((tmp / "journal").glob("event_date=*/legs.jsonl"))
        self.assertTrue(files)
        rec = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(rec.get("msg"), inner)
        self.assertEqual(rec.get("dispatch_status"), "bind_failed")
        self.assertFalse(rec.get("send"))


if __name__ == "__main__":
    unittest.main()
