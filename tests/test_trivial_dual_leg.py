"""Hermetic tests for the default live-manager trivial dual-leg send path."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.bot.broker import make_broker
from app.bot.journal import JournalWriter
from app.bot.private.live_broker import LiveBroker, LiveBrokerError, make_live_broker
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.ws_gates import WsProfileGateError, assert_ws_trivial_dual_leg_gates
from app.bot.private.ws_trivial_dual_leg import (
    SEND_PATH_TRIVIAL,
    SEND_PATH_W6,
    TrivialDualSender,
    TrivialSendError,
    assert_signed_place_frame,
    build_signed_place_text,
    parse_inst_id_code_env,
    resolve_live_send_path,
    w6_manager_opt_in,
)
from app.bot.stub_broker import InstrumentMeta, StubBroker


def _creds() -> LiveCredentials:
    return LiveCredentials(api_key="k", api_secret="s", passphrase="p")


def _meta(coin: str = "SOL") -> InstrumentMeta:
    return InstrumentMeta(
        base_coin=coin,
        okx_symbol=f"{coin}-USDT-SWAP",
        bybit_symbol=f"{coin}USDT",
        okx_lot_size=1.0,
        okx_min_size=1.0,
        bybit_qty_step=0.1,
        bybit_min_order_qty=0.1,
        bybit_min_notional_value=5.0,
    )


def _book(*, bid: float = 1.0, ask: float = 1.01) -> dict:
    return {"bid_price": bid, "ask_price": ask, "bid_qty": 10.0, "ask_qty": 10.0}


def _live_env(td: str, **extra: str) -> dict[str, str]:
    from app.bot.private.secrets import LIVE_KEY_NAMES

    live_env = Path(td) / "bbot-private-live.env"
    live_env.write_text(
        "\n".join(f"{n}=v{i}" for i, n in enumerate(LIVE_KEY_NAMES)) + "\n",
        encoding="utf-8",
    )
    env = {
        "VENUE": "live",
        "LIVE_ORDERS": "1",
        "BBOT_PRIVATE_ENV_FILE": str(live_env),
        "BBOT_PRIVATE_DATA_ROOT": str(Path(td) / "data"),
        "BBOT_BROKER": "private_live",
    }
    env.update(extra)
    return env


class ResolveSendPathTests(unittest.TestCase):
    def test_default_is_trivial_without_w6_flag(self) -> None:
        self.assertEqual(resolve_live_send_path({}), SEND_PATH_TRIVIAL)
        self.assertEqual(
            resolve_live_send_path({"BBOT_PRIVATE_W6": "1"}), SEND_PATH_TRIVIAL
        )
        self.assertFalse(w6_manager_opt_in({"BBOT_PRIVATE_W6": "1"}))

    def test_w6_requires_explicit_path_and_flag(self) -> None:
        self.assertEqual(
            resolve_live_send_path({"BBOT_PRIVATE_SEND_PATH": "w6"}), SEND_PATH_W6
        )
        self.assertFalse(w6_manager_opt_in({"BBOT_PRIVATE_SEND_PATH": "w6"}))
        self.assertTrue(
            w6_manager_opt_in(
                {"BBOT_PRIVATE_SEND_PATH": "w6", "BBOT_PRIVATE_W6": "1"}
            )
        )

    def test_rejects_unknown(self) -> None:
        with self.assertRaises(TrivialSendError):
            resolve_live_send_path({"BBOT_PRIVATE_SEND_PATH": "faster_w6"})

    def test_parse_inst_id_env(self) -> None:
        self.assertEqual(
            parse_inst_id_code_env("SOL-USDT-SWAP:193761,BTC-USDT-SWAP:2"),
            {"SOL-USDT-SWAP": 193761, "BTC-USDT-SWAP": 2},
        )
        self.assertEqual(parse_inst_id_code_env(""), {})


class SignedFrameTests(unittest.TestCase):
    def test_bybit_has_reqid_hmac_order_link(self) -> None:
        text, req, plan = build_signed_place_text(
            venue="bybit",
            symbol="SOLUSDT",
            side="buy",
            qty="0.5",
            credentials=_creds(),
        )
        data = json.loads(text)
        self.assertEqual(data["reqId"], req)
        self.assertTrue(data["header"]["X-BAPI-SIGN"])
        self.assertTrue(data["args"][0]["orderLinkId"])
        self.assertEqual(data["args"][0]["symbol"], "SOLUSDT")
        self.assertEqual(plan.venue, "bybit_live")
        assert_signed_place_frame("bybit_live", text)

    def test_okx_requires_inst_id_code(self) -> None:
        text, req, _ = build_signed_place_text(
            venue="okx",
            symbol="SOL-USDT-SWAP",
            side="sell",
            qty="1",
            credentials=None,
            inst_id_code=193761,
        )
        data = json.loads(text)
        self.assertEqual(data["id"], req)
        self.assertEqual(data["args"][0]["instIdCode"], 193761)
        self.assertIsInstance(data["args"][0]["instIdCode"], int)
        assert_signed_place_frame("okx_live", text)

    def test_unsigned_primitive_rejected(self) -> None:
        primitive = json.dumps(
            {
                "op": "order.create",
                "header": {"X-BAPI-TIMESTAMP": "1"},
                "args": [{"symbol": "TRUMPUSDT", "qty": "4.0"}],
            }
        )
        with self.assertRaises(TrivialSendError):
            assert_signed_place_frame("bybit_live", primitive)
        okx_bare = json.dumps(
            {"id": "1", "op": "order", "args": [{"instId": "TRUMP-USDT-SWAP"}]}
        )
        with self.assertRaises(TrivialSendError):
            assert_signed_place_frame("okx_live", okx_bare)


class TrivialSenderTests(unittest.TestCase):
    def test_dual_enqueue_fires_both_without_fill_wait(self) -> None:
        sent: list[str] = []

        def _send(item) -> None:
            sent.append(item.venue)

        loop = TrivialDualSender(send_fn=_send)
        try:
            b_text, b_req, _ = build_signed_place_text(
                venue="bybit",
                symbol="SOLUSDT",
                side="buy",
                qty="0.5",
                credentials=_creds(),
            )
            o_text, o_req, _ = build_signed_place_text(
                venue="okx",
                symbol="SOL-USDT-SWAP",
                side="sell",
                qty="1",
                credentials=None,
                inst_id_code=99,
            )
            result = loop.enqueue_dual(
                bybit_text=b_text,
                okx_text=o_text,
                bybit_req_id=b_req,
                okx_req_id=o_req,
                phase="open",
            )
            self.assertIsNone(result.error)
            self.assertIsNotNone(result.first_sent_ns)
            self.assertIsNotNone(result.second_sent_ns)
            self.assertEqual(set(sent), {"bybit", "okx"})
            skew_ns = abs(int(result.second_enqueued_ns) - int(result.first_enqueued_ns))
            self.assertLess(skew_ns, 50_000_000)  # 50 ms enqueue skew
        finally:
            loop.close()


class LiveBrokerPlaceTests(unittest.TestCase):
    def _broker(self, td: str, **kwargs) -> LiveBroker:
        sent: list[str] = []

        def _send(item) -> None:
            sent.append(item.venue)
            assert_signed_place_frame(
                "bybit_live" if item.venue == "bybit" else "okx_live",
                item.text,
            )

        kwargs.setdefault("send_fn", _send)
        kwargs.setdefault("inst_id_codes", {"SOL-USDT-SWAP": 193761})
        env = kwargs.pop("env", None) or {
            "VENUE": "live",
            "LIVE_ORDERS": "1",
        }
        broker = LiveBroker(
            data_root=Path(td),
            journal=JournalWriter(Path(td)),
            trade_lat_ms=100,
            notional_usdt=20.0,
            log=lambda _m: None,
            env=env,
            bybit_credentials=_creds(),
            **kwargs,
        )
        broker._test_sent = sent  # type: ignore[attr-defined]
        return broker

    def test_filters_keep_already_in_position_and_held_coin(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            broker = self._broker(td)
            broker.position = "open_long"
            broker.held_coin = "ETH"
            abort = broker.place(
                spread_side="open_short",
                base_coin="SOL",
                signal_ts_ms=1,
                okx_book=_book(),
                bybit_book=_book(),
                meta=_meta("SOL"),
            )
            self.assertEqual(abort, "already_in_position")
            abort = broker.place(
                spread_side="close",
                base_coin="SOL",
                signal_ts_ms=1,
                okx_book=_book(),
                bybit_book=_book(),
                meta=_meta("SOL"),
                close_of="open_long",
            )
            self.assertEqual(abort, "not_held_coin")
            self.assertEqual(broker._test_sent, [])

    def test_default_place_does_not_call_w6_recover_or_approve(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            broker = self._broker(td)

            def _boom(*_a, **_k):
                raise AssertionError("W6 must not run on the default hot path")

            with (
                patch(
                    "app.bot.private.ws_w6_dual_leg.run_w6_dual_leg",
                    side_effect=_boom,
                ),
                patch(
                    "app.bot.private.ws_w6_dual_leg.open_w6_production_bindings",
                    side_effect=_boom,
                ),
                patch(
                    "app.bot.private.order_sender.ApprovalBoundSender.send_approved",
                    side_effect=_boom,
                ),
            ):
                abort = broker.place(
                    spread_side="open_long",
                    base_coin="SOL",
                    signal_ts_ms=1,
                    okx_book=_book(),
                    bybit_book=_book(),
                    meta=_meta("SOL"),
                )
            self.assertIsNone(abort)
            self.assertEqual(set(broker._test_sent), {"bybit", "okx"})
            self.assertEqual(broker.last_send_path, SEND_PATH_TRIVIAL)
            self.assertEqual(broker.position, "open_long")
            self.assertEqual(broker.held_coin, "SOL")
            self.assertIsNone(broker.pending)
            broker.close()

    def test_open_and_flatten_both_parallel(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            broker = self._broker(td)
            abort = broker.place(
                spread_side="open_short",
                base_coin="SOL",
                signal_ts_ms=1,
                okx_book=_book(),
                bybit_book=_book(),
                meta=_meta("SOL"),
            )
            self.assertIsNone(abort)
            self.assertEqual(broker.position, "open_short")
            first = list(broker._test_sent)
            broker._test_sent.clear()
            abort = broker.place(
                spread_side="close",
                base_coin="SOL",
                signal_ts_ms=2,
                okx_book=_book(),
                bybit_book=_book(),
                meta=_meta("SOL"),
                close_of="open_short",
            )
            self.assertIsNone(abort)
            self.assertIsNone(broker.position)
            self.assertEqual(set(first), {"bybit", "okx"})
            self.assertEqual(set(broker._test_sent), {"bybit", "okx"})
            broker.close()

    def test_inst_id_from_env_and_warmup_is_off_place(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hits: list[str] = []

            def _fetch(symbol: str) -> int:
                hits.append(symbol)
                return 42

            broker = self._broker(
                td,
                env={
                    "VENUE": "live",
                    "LIVE_ORDERS": "1",
                    "BBOT_OKX_INST_ID_CODES": "SOL-USDT-SWAP:193761",
                },
                inst_id_codes={},
            )
            broker.warmup_inst_id_codes(["ETH-USDT-SWAP"], fetch_fn=_fetch)
            self.assertEqual(hits, ["ETH-USDT-SWAP"])
            abort = broker.place(
                spread_side="open_long",
                base_coin="SOL",
                signal_ts_ms=1,
                okx_book=_book(),
                bybit_book=_book(),
                meta=_meta("SOL"),
            )
            self.assertIsNone(abort)
            self.assertEqual(hits, ["ETH-USDT-SWAP"])  # place did not fetch
            broker.close()

    def test_w6_opt_in_is_trump_only_and_not_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            broker = self._broker(
                td,
                env={
                    "VENUE": "live",
                    "LIVE_ORDERS": "1",
                    "BBOT_PRIVATE_SEND_PATH": "w6",
                    "BBOT_PRIVATE_W6": "1",
                },
            )
            abort = broker.place(
                spread_side="open_long",
                base_coin="SOL",
                signal_ts_ms=1,
                okx_book=_book(),
                bybit_book=_book(),
                meta=_meta("SOL"),
            )
            self.assertEqual(abort, "w6_path_trump_only")
            self.assertEqual(broker.last_send_path, SEND_PATH_W6)
            self.assertEqual(broker._test_sent, [])
            broker.close()


class TrivialGateTests(unittest.TestCase):
    def test_gate_does_not_require_w6_flag(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = _live_env(td)
            self.assertEqual(assert_ws_trivial_dual_leg_gates(env), "live")

    def test_gate_refuses_w6_path_and_missing_live_orders(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = _live_env(td, BBOT_PRIVATE_SEND_PATH="w6", BBOT_PRIVATE_W6="1")
            with self.assertRaises(WsProfileGateError):
                assert_ws_trivial_dual_leg_gates(env)
            env2 = _live_env(td)
            env2["LIVE_ORDERS"] = "0"
            with self.assertRaises(WsProfileGateError):
                assert_ws_trivial_dual_leg_gates(env2)


class MakeBrokerLiveTests(unittest.TestCase):
    def test_private_live_refuses_without_live_orders(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            journal = JournalWriter(Path(td))
            with self.assertRaises(LiveBrokerError):
                make_broker(
                    data_root=Path(td),
                    journal=journal,
                    trade_lat_ms=100,
                    notional_usdt=20.0,
                    log=lambda _m: None,
                    env={"BBOT_BROKER": "private_live", "VENUE": "live"},
                )

    def test_private_live_default_path_is_trivial(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            journal = JournalWriter(Path(td))
            broker = make_live_broker(
                data_root=Path(td),
                journal=journal,
                trade_lat_ms=100,
                notional_usdt=20.0,
                log=lambda _m: None,
                env={"VENUE": "live", "LIVE_ORDERS": "1"},
                send_fn=lambda _item: None,
                inst_id_codes={"SOL-USDT-SWAP": 1},
                bybit_credentials=_creds(),
            )
            self.assertIsInstance(broker, LiveBroker)
            self.assertEqual(broker.send_path, SEND_PATH_TRIVIAL)
            self.assertIsInstance(broker, StubBroker)
            broker.close()

    def test_stub_default_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            broker = make_broker(
                data_root=Path(td),
                journal=JournalWriter(Path(td)),
                trade_lat_ms=100,
                notional_usdt=20.0,
                log=lambda _m: None,
                env={"BBOT_BROKER": "stub"},
            )
            self.assertIsInstance(broker, StubBroker)
            self.assertFalse(isinstance(broker, LiveBroker))


if __name__ == "__main__":
    unittest.main()
