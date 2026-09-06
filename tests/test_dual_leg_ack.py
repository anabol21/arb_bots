"""Contour B post-send dual-leg ACK: both-ok, overnight 60033, timeout.

No network. Proves the canary EDEN failure mode (Bybit retCode=0 + OKX
event=error 60033) no longer marks local ``open_long``.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from app.bot.journal import JournalWriter
from app.bot.private.dual_leg_ack import (
    AckOutcome,
    abort_string,
    flatten_venues,
    resolve_ack_timeout_sec,
    wait_dual_place_acks,
)
from app.bot.private.live_broker import LiveBroker
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.ws_private import PrivateStreamRuntime
from app.bot.private.ws_socket import FakePrivateWsSocket
from app.bot.private.ws_trivial_dual_leg import SEND_PATH_TRIVIAL, assert_signed_place_frame
from app.bot.stub_broker import InstrumentMeta
from tests.test_okx_ws_message_id import WIRE_OKX_INBOUND


def _creds() -> LiveCredentials:
    return LiveCredentials(api_key="k", api_secret="s", passphrase="p")


def _meta(coin: str = "EDEN") -> InstrumentMeta:
    return InstrumentMeta(
        base_coin=coin,
        okx_symbol=f"{coin}-USDT-SWAP",
        bybit_symbol=f"{coin}USDT",
        okx_lot_size=1.0,
        okx_min_size=1.0,
        bybit_qty_step=1.0,
        bybit_min_order_qty=1.0,
        bybit_min_notional_value=5.0,
    )


def _book(*, bid: float = 1.0, ask: float = 1.01) -> dict:
    return {"bid_price": bid, "ask_price": ask, "bid_qty": 200.0, "ask_qty": 200.0}


def _parse_okx(text: str, *, expect_req_id: str):
    rt = object.__new__(PrivateStreamRuntime)
    rt.exchange = "okx"
    return PrivateStreamRuntime.parse_trade_ack_text(
        rt, text, expect_req_id=expect_req_id
    )


def _parse_bybit(text: str, *, expect_req_id: str):
    rt = object.__new__(PrivateStreamRuntime)
    rt.exchange = "bybit"
    return PrivateStreamRuntime.parse_trade_ack_text(
        rt, text, expect_req_id=expect_req_id
    )


def _ok_outcome(venue: str, req_id: str, _timeout: float) -> AckOutcome:
    return AckOutcome(
        venue=venue,
        req_id=req_id,
        accepted=True,
        timed_out=False,
        venue_code="0",
        recv_ns=time.monotonic_ns(),
    )


class ParseOvernight60033Tests(unittest.TestCase):
    def test_okx_event_error_without_id_is_reject(self) -> None:
        obs = _parse_okx(
            json.dumps(WIRE_OKX_INBOUND, separators=(",", ":")),
            expect_req_id="reqc08a00f2f36c481682e7fa5dc998",
        )
        self.assertFalse(obs.accepted)
        self.assertEqual(obs.venue_code, "60033")
        self.assertEqual(obs.ack_state, "received")

    def test_okx_success_still_requires_matching_id(self) -> None:
        frame = json.dumps(
            {
                "id": "abc123",
                "op": "order",
                "code": "0",
                "data": [{"sCode": "0"}],
            }
        )
        obs = _parse_okx(frame, expect_req_id="abc123")
        self.assertTrue(obs.accepted)
        with self.assertRaises(TimeoutError):
            _parse_okx(frame, expect_req_id="other")

    def test_bybit_retcode_zero_accepted(self) -> None:
        frame = json.dumps(
            {
                "reqId": "req_bybit_ok",
                "op": "order.create",
                "retCode": 0,
                "success": True,
            }
        )
        obs = _parse_bybit(frame, expect_req_id="req_bybit_ok")
        self.assertTrue(obs.accepted)
        self.assertEqual(obs.venue_code, "0")


class DualAckHelperTests(unittest.TestCase):
    def test_wait_parallel_both_ok(self) -> None:
        seen: list[str] = []

        def _wait(venue: str, req_id: str, timeout: float) -> AckOutcome:
            seen.append(venue)
            return _ok_outcome(venue, req_id, timeout)

        result = wait_dual_place_acks(
            bybit_req_id="b1",
            okx_req_id="o1",
            timeout_sec=1.0,
            wait_one=_wait,
        )
        self.assertTrue(result.both_accepted)
        self.assertEqual(set(seen), {"bybit", "okx"})

    def test_abort_strings_and_flatten_open_only(self) -> None:
        rejected = wait_dual_place_acks(
            bybit_req_id="b1",
            okx_req_id="o1",
            timeout_sec=0.2,
            wait_one=lambda v, r, t: (
                _ok_outcome(v, r, t)
                if v == "bybit"
                else AckOutcome(
                    venue="okx",
                    req_id=r,
                    accepted=False,
                    timed_out=False,
                    venue_code="60033",
                    recv_ns=1,
                )
            ),
        )
        self.assertFalse(rejected.both_accepted)
        self.assertEqual(abort_string(rejected), "dual_ack_rejected:okx:60033")
        self.assertEqual(flatten_venues(result=rejected, phase="open"), ["bybit"])
        self.assertEqual(flatten_venues(result=rejected, phase="close"), [])

        timed = wait_dual_place_acks(
            bybit_req_id="b1",
            okx_req_id="o1",
            timeout_sec=0.2,
            wait_one=lambda v, r, t: AckOutcome(
                venue=v,
                req_id=r,
                accepted=False,
                timed_out=True,
                error="timeout",
                recv_ns=1,
            ),
        )
        self.assertTrue(timed.any_timeout)
        self.assertEqual(abort_string(timed), "dual_ack_timeout:bybit,okx")
        self.assertEqual(flatten_venues(result=timed, phase="open"), ["bybit", "okx"])

    def test_missing_runtime_is_timeout(self) -> None:
        result = wait_dual_place_acks(
            bybit_req_id="b1",
            okx_req_id="o1",
            timeout_sec=0.05,
        )
        self.assertTrue(result.any_timeout)
        self.assertFalse(result.both_accepted)
        self.assertTrue(abort_string(result).startswith("dual_ack_timeout:"))

    def test_recv_trade_ack_reads_60033_from_fake_socket(self) -> None:
        sock = FakePrivateWsSocket(exchange="okx")
        sock.connect()
        sock.push_inbound(json.dumps(WIRE_OKX_INBOUND, separators=(",", ":")))
        rt = object.__new__(PrivateStreamRuntime)
        rt.exchange = "okx"
        rt.trade_socket = sock
        rt._trade_inbound_stash = []  # noqa: SLF001
        rt.note_trade_activity = lambda: None
        obs = PrivateStreamRuntime.recv_trade_ack(
            rt, expect_req_id="anyInFlightId", timeout_sec=0.5
        )
        self.assertFalse(obs.accepted)
        self.assertEqual(obs.venue_code, "60033")

    def test_timeout_env_default(self) -> None:
        self.assertEqual(resolve_ack_timeout_sec({}), 2.0)
        self.assertEqual(resolve_ack_timeout_sec({"BBOT_PRIVATE_ACK_TIMEOUT_SEC": "1.5"}), 1.5)


class LiveBrokerDualAckTests(unittest.TestCase):
    def _broker(self, td: str, *, ack_wait_fn, sent=None, **kwargs) -> LiveBroker:
        sent = sent if sent is not None else []

        def _send(item) -> None:
            sent.append(item)
            venue = item.venue
            assert_signed_place_frame(
                "bybit_live" if venue == "bybit" else "okx_live",
                item.text,
            )

        broker = LiveBroker(
            data_root=Path(td),
            journal=JournalWriter(Path(td)),
            trade_lat_ms=100,
            notional_usdt=10.0,
            log=lambda _m: None,
            env={"VENUE": "live", "LIVE_ORDERS": "1"},
            send_fn=_send,
            inst_id_codes={"EDEN-USDT-SWAP": 1, "SOL-USDT-SWAP": 193761},
            bybit_credentials=_creds(),
            ack_wait_fn=ack_wait_fn,
            ack_timeout_sec=0.4,
            **kwargs,
        )
        broker._test_sent = sent  # type: ignore[attr-defined]
        return broker

    def test_both_ack_ok_sets_open_long(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sent: list = []
            broker = self._broker(td, ack_wait_fn=_ok_outcome, sent=sent)
            abort = broker.place(
                spread_side="open_long",
                base_coin="EDEN",
                signal_ts_ms=1,
                okx_book=_book(),
                bybit_book=_book(),
                meta=_meta("EDEN"),
            )
            self.assertIsNone(abort)
            self.assertEqual(broker.position, "open_long")
            self.assertEqual(broker.held_coin, "EDEN")
            self.assertIsNone(broker.pending)
            self.assertTrue(broker.last_ack_result.both_accepted)  # type: ignore[union-attr]
            self.assertEqual(broker.last_flatten, [])
            self.assertEqual({i.venue for i in sent}, {"bybit", "okx"})
            broker.close()

    def test_bybit_ok_okx_60033_stays_flat_and_flattens_bybit(self) -> None:
        """Overnight canary: Bybit filled/acked, OKX 60033 → no local open."""

        def _wait(venue: str, req_id: str, timeout: float) -> AckOutcome:
            if venue == "bybit":
                obs = _parse_bybit(
                    json.dumps(
                        {
                            "reqId": req_id,
                            "op": "order.create",
                            "retCode": 0,
                            "success": True,
                        }
                    ),
                    expect_req_id=req_id,
                )
                return AckOutcome(
                    venue="bybit",
                    req_id=req_id,
                    accepted=bool(obs.accepted),
                    timed_out=False,
                    venue_code=obs.venue_code,
                    recv_ns=time.monotonic_ns(),
                )
            obs = _parse_okx(
                json.dumps(WIRE_OKX_INBOUND, separators=(",", ":")),
                expect_req_id=req_id,
            )
            return AckOutcome(
                venue="okx",
                req_id=req_id,
                accepted=bool(obs.accepted),
                timed_out=False,
                venue_code=obs.venue_code,
                recv_ns=time.monotonic_ns(),
            )

        with tempfile.TemporaryDirectory() as td:
            sent: list = []
            broker = self._broker(td, ack_wait_fn=_wait, sent=sent)
            abort = broker.place(
                spread_side="open_long",
                base_coin="EDEN",
                signal_ts_ms=1,
                okx_book=_book(),
                bybit_book=_book(),
                meta=_meta("EDEN"),
            )
            self.assertIsNotNone(abort)
            self.assertTrue(str(abort).startswith("dual_ack_rejected:"))
            self.assertIn("okx:60033", str(abort))
            self.assertIsNone(broker.position)
            self.assertIsNone(broker.held_coin)
            self.assertIsNone(broker.pending)
            self.assertEqual(len(broker.last_flatten), 1)
            self.assertEqual(broker.last_flatten[0].venue, "bybit")
            self.assertEqual(broker.last_flatten[0].reason, "peer_reject")
            self.assertIsNotNone(broker.last_flatten[0].sent_ns)
            flatten_items = [i for i in sent if i.phase == "flatten"]
            self.assertEqual(len(flatten_items), 1)
            self.assertEqual(flatten_items[0].venue, "bybit")
            body = json.loads(flatten_items[0].text)
            self.assertTrue(body["args"][0].get("reduceOnly"))
            broker.close()

    def test_timeout_fail_closed_stays_flat_and_flattens_both(self) -> None:
        def _wait(venue: str, req_id: str, _timeout: float) -> AckOutcome:
            return AckOutcome(
                venue=venue,
                req_id=req_id,
                accepted=False,
                timed_out=True,
                error="timeout",
                recv_ns=time.monotonic_ns(),
            )

        with tempfile.TemporaryDirectory() as td:
            sent: list = []
            broker = self._broker(td, ack_wait_fn=_wait, sent=sent)
            abort = broker.place(
                spread_side="open_short",
                base_coin="EDEN",
                signal_ts_ms=1,
                okx_book=_book(),
                bybit_book=_book(),
                meta=_meta("EDEN"),
            )
            self.assertTrue(str(abort).startswith("dual_ack_timeout:"))
            self.assertIsNone(broker.position)
            self.assertIsNone(broker.held_coin)
            venues = {a.venue for a in broker.last_flatten}
            self.assertEqual(venues, {"bybit", "okx"})
            self.assertEqual({i.venue for i in sent if i.phase == "flatten"}, {"bybit", "okx"})
            broker.close()

    def test_acks_do_not_gate_second_leg_send(self) -> None:
        """Sends stay parallel: both ws.send before any ACK wait."""
        events: list[str] = []

        def _send(item) -> None:
            events.append(f"send:{item.venue}")

        def _wait(venue: str, req_id: str, timeout: float) -> AckOutcome:
            events.append(f"ack:{venue}")
            return _ok_outcome(venue, req_id, timeout)

        with tempfile.TemporaryDirectory() as td:
            broker = LiveBroker(
                data_root=Path(td),
                journal=JournalWriter(Path(td)),
                trade_lat_ms=100,
                notional_usdt=10.0,
                log=lambda _m: None,
                env={"VENUE": "live", "LIVE_ORDERS": "1"},
                send_fn=_send,
                inst_id_codes={"EDEN-USDT-SWAP": 1},
                bybit_credentials=_creds(),
                ack_wait_fn=_wait,
                ack_timeout_sec=0.4,
            )
            abort = broker.place(
                spread_side="open_long",
                base_coin="EDEN",
                signal_ts_ms=1,
                okx_book=_book(),
                bybit_book=_book(),
                meta=_meta("EDEN"),
            )
            self.assertIsNone(abort)
            self.assertEqual(broker.last_send_path, SEND_PATH_TRIVIAL)
            send_idx = [i for i, ev in enumerate(events) if ev.startswith("send:")]
            ack_idx = [i for i, ev in enumerate(events) if ev.startswith("ack:")]
            self.assertEqual(len(send_idx), 2)
            self.assertEqual(len(ack_idx), 2)
            self.assertLess(max(send_idx), min(ack_idx))
            self.assertEqual({events[i] for i in send_idx}, {"send:bybit", "send:okx"})
            broker.close()

    def test_close_ack_fail_keeps_position_and_does_not_reopen(self) -> None:
        def _close_reject_okx(venue: str, req_id: str, timeout: float) -> AckOutcome:
            if venue == "bybit":
                return _ok_outcome(venue, req_id, timeout)
            return AckOutcome(
                venue="okx",
                req_id=req_id,
                accepted=False,
                timed_out=False,
                venue_code="51000",
                recv_ns=1,
            )

        with tempfile.TemporaryDirectory() as td:
            sent: list = []
            broker = self._broker(td, ack_wait_fn=_ok_outcome, sent=sent)
            self.assertIsNone(
                broker.place(
                    spread_side="open_long",
                    base_coin="EDEN",
                    signal_ts_ms=1,
                    okx_book=_book(),
                    bybit_book=_book(),
                    meta=_meta("EDEN"),
                )
            )
            self.assertEqual(broker.position, "open_long")
            broker._ack_wait_fn = _close_reject_okx
            before = len(sent)
            abort = broker.place(
                spread_side="close",
                base_coin="EDEN",
                signal_ts_ms=2,
                okx_book=_book(),
                bybit_book=_book(),
                meta=_meta("EDEN"),
                close_of="open_long",
            )
            self.assertTrue(str(abort).startswith("dual_ack_rejected:"))
            self.assertEqual(broker.position, "open_long")
            self.assertEqual(broker.held_coin, "EDEN")
            self.assertEqual(broker.last_flatten, [])
            self.assertFalse(any(i.phase == "flatten" for i in sent[before:]))
            broker.close()


if __name__ == "__main__":
    unittest.main()
