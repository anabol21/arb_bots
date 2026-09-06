"""Hermetic tests for the standing private+trade wire transcript."""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.bot.journal import JournalWriter
from app.bot.private.dual_leg_ack import AckOutcome
from app.bot.private.live_broker import LiveBroker
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.paths import wire_jsonl_path
from app.bot.private.wire_transcript import (
    REDACTED,
    SCHEMA_VERSION,
    WireTranscript,
    attach_process_wire_transcript,
    clear_process_wire_transcript,
    derive_fill_delivery_ms,
    extract_venue_ts_ms,
    redact_payload,
    scan_all_wire_events,
    wrap_socket,
)
from app.bot.private.ws_socket import FakePrivateWsSocket
from app.bot.private.ws_trivial_dual_leg import (
    SEND_PATH_TRIVIAL,
    TrivialDualSender,
    build_signed_place_text,
    send_signed_dual,
)
from app.bot.stub_broker import InstrumentMeta as StubInstrumentMeta


def _creds(*, okx: bool = False) -> LiveCredentials:
    if okx:
        return LiveCredentials(
            api_key="okx-live-key-ABCDEF",
            api_secret="okx-live-secret-XYZ",
            passphrase="okx-passphrase-SECRET",
        )
    return LiveCredentials(
        api_key="bybit-live-key-ABCDEF",
        api_secret="bybit-live-secret-XYZ",
    )


def _meta(coin: str = "SOL") -> StubInstrumentMeta:
    return StubInstrumentMeta(
        base_coin=coin,
        okx_symbol=f"{coin}-USDT-SWAP",
        bybit_symbol=f"{coin}USDT",
        okx_lot_size=1.0,
        okx_min_size=1.0,
        bybit_qty_step=0.1,
        bybit_min_order_qty=0.1,
        bybit_min_notional_value=5.0,
    )


def _book() -> dict:
    return {"bid_price": 1.0, "ask_price": 1.01, "bid_qty": 10.0, "ask_qty": 10.0}


class RedactTests(unittest.TestCase):
    def test_bybit_place_header_secrets_stripped(self) -> None:
        text, req, _ = build_signed_place_text(
            venue="bybit",
            symbol="WALUSDT",
            side="buy",
            qty="10",
            credentials=_creds(),
            req_id="reqBybitPlace01",
            dual_leg_id="duallegid0123456789012345678901",
        )
        raw = json.loads(text)
        red = redact_payload(raw, op="order.create")
        self.assertEqual(red["reqId"], req)
        self.assertEqual(red["op"], "order.create")
        self.assertEqual(red["args"][0]["symbol"], "WALUSDT")
        self.assertEqual(red["args"][0]["orderLinkId"], raw["args"][0]["orderLinkId"])
        header = red["header"]
        self.assertEqual(header["X-BAPI-API-KEY"], REDACTED)
        self.assertEqual(header["X-BAPI-SIGN"], REDACTED)
        self.assertNotEqual(header["X-BAPI-TIMESTAMP"], REDACTED)
        blob = json.dumps(red)
        self.assertNotIn("bybit-live-key-ABCDEF", blob)
        self.assertNotIn(raw["header"]["X-BAPI-SIGN"], blob)

    def test_okx_login_and_bybit_auth_args(self) -> None:
        from app.bot.private.ws_messages import (
            build_bybit_private_auth,
            build_okx_private_login,
        )

        bybit = json.loads(build_bybit_private_auth(_creds(), expires_ms=1_700_000_000_000).text)
        red_b = redact_payload(bybit, op="auth")
        self.assertEqual(red_b["op"], "auth")
        self.assertEqual(red_b["args"][0], REDACTED)
        self.assertEqual(red_b["args"][1], 1_700_000_000_000)
        self.assertEqual(red_b["args"][2], REDACTED)

        okx = json.loads(
            build_okx_private_login(_creds(okx=True), timestamp="1700000000").text
        )
        red_o = redact_payload(okx, op="login")
        args0 = red_o["args"][0]
        self.assertEqual(args0["apiKey"], REDACTED)
        self.assertEqual(args0["passphrase"], REDACTED)
        self.assertEqual(args0["sign"], REDACTED)
        self.assertEqual(args0["timestamp"], "1700000000")
        self.assertNotIn("okx-live-key-ABCDEF", json.dumps(red_o))
        self.assertNotIn("okx-passphrase-SECRET", json.dumps(red_o))


class TranscriptAppendTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_process_wire_transcript(close=True)

    def test_append_ordered_and_correlation_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            logs: list[str] = []

            class _H(logging.Handler):
                def emit(self, record: logging.LogRecord) -> None:
                    logs.append(record.getMessage())

            log = logging.getLogger("bbot.test.wire")
            log.handlers.clear()
            log.addHandler(_H())
            log.setLevel(logging.INFO)
            tr = WireTranscript(root, run_id="run_wire_test_01", async_write=False, logger=log)
            attach_process_wire_transcript(tr)
            sock = wrap_socket(
                FakePrivateWsSocket(),
                transcript=tr,
                venue="bybit",
                socket="trade",
                generation_fn=lambda: 3,
            )
            sock.connect()
            tr.bind_place_correlation(
                req_id="reqA",
                intent_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                dual_leg_id="duallegid0123456789012345678901",
                signal_ts_ms=1_700_000_000_000,
                venue="bybit",
                phase="open",
            )
            sock.send_text(
                json.dumps({"reqId": "reqA", "op": "order.create", "args": [{"symbol": "WALUSDT"}]})
            )
            sock.push_inbound(
                json.dumps(
                    {
                        "reqId": "reqA",
                        "op": "order.create",
                        "retCode": 0,
                        "success": True,
                    }
                )
            )
            sock.recv_text()
            sock.push_inbound(
                json.dumps(
                    {
                        "topic": "execution",
                        "data": [
                            {
                                "symbol": "WALUSDT",
                                "execTime": "1700000000500",
                                "orderLinkId": "link1",
                            }
                        ],
                    }
                )
            )
            sock.recv_text()

            events = scan_all_wire_events(root)
            self.assertEqual(len(events), 3)
            self.assertEqual([e["seq"] for e in events], [1, 2, 3])
            self.assertEqual([e["dir"] for e in events], ["out", "in", "in"])
            self.assertTrue(events[0]["mono_ns"] <= events[1]["mono_ns"] <= events[2]["mono_ns"])
            out = events[0]
            self.assertEqual(out["schema_version"], SCHEMA_VERSION)
            self.assertEqual(out["venue"], "bybit")
            self.assertEqual(out["socket"], "trade")
            self.assertEqual(out["req_id"], "reqA")
            self.assertEqual(out["intent_id"], "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
            self.assertEqual(out["dual_leg_id"], "duallegid0123456789012345678901")
            self.assertEqual(out["reconnect_generation"], 3)
            self.assertIn("signal_to_send_ms", out)
            ack = events[1]
            self.assertEqual(ack["req_id"], "reqA")
            self.assertEqual(ack["intent_id"], out["intent_id"])
            fill = events[2]
            self.assertEqual(fill["venue_ts_ms"], 1_700_000_000_500)
            self.assertEqual(
                fill["fill_delivery_ms"],
                derive_fill_delivery_ms(
                    local_recv_ms=fill["wall_ms"], venue_ts_ms=fill["venue_ts_ms"]
                ),
            )
            self.assertTrue(any(line.startswith("wire_out") for line in logs))
            self.assertTrue(any(line.startswith("wire_in") for line in logs))
            self.assertFalse(any("bybit-live-secret" in line for line in logs))
            tr.close()

    def test_fill_delivery_helper(self) -> None:
        self.assertEqual(
            derive_fill_delivery_ms(local_recv_ms=1005, venue_ts_ms=1000), 5
        )
        self.assertIsNone(derive_fill_delivery_ms(local_recv_ms=1005, venue_ts_ms=None))
        self.assertEqual(extract_venue_ts_ms({"data": [{"fillTime": "1700000001000"}]}), 1_700_000_001_000)
        self.assertEqual(extract_venue_ts_ms({"data": [{"uTime": "1700000002"}]}), 1_700_000_002_000)

    def test_timeout_recv_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tr = WireTranscript(Path(td), run_id="run_to", async_write=False)
            sock = wrap_socket(
                FakePrivateWsSocket(), transcript=tr, venue="okx", socket="private"
            )
            sock.connect()
            with self.assertRaises(TimeoutError):
                sock.recv_text()
            self.assertEqual(scan_all_wire_events(Path(td)), [])
            tr.close()


class ContourBDoesNotWaitTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_process_wire_transcript(close=True)

    def test_trivial_send_does_not_recv_or_wait_fill(self) -> None:
        recvs: list[str] = []

        class _Watch(FakePrivateWsSocket):
            def recv_text(self, *, timeout_sec=None):  # type: ignore[no-untyped-def]
                recvs.append("recv")
                return super().recv_text(timeout_sec=timeout_sec)

        bybit = _Watch()
        okx = _Watch()
        bybit.connect()
        okx.connect()
        # Fill already sitting on the private-equivalent inbox — send must ignore it.
        bybit.push_inbound(json.dumps({"topic": "execution", "data": [{"execTime": "1"}]}))
        okx.push_inbound(json.dumps({"arg": {"channel": "orders"}, "data": [{"fillTime": "1"}]}))

        with tempfile.TemporaryDirectory() as td:
            tr = WireTranscript(Path(td), run_id="run_nofill", async_write=False)
            attach_process_wire_transcript(tr)
            w_bybit = wrap_socket(bybit, transcript=tr, venue="bybit", socket="trade")
            w_okx = wrap_socket(okx, transcript=tr, venue="okx", socket="trade")

            def _send(item):
                sock = w_bybit if item.venue == "bybit" else w_okx
                sock.send_text(item.text)

            sender = TrivialDualSender(send_fn=_send)
            b_text, b_req, _ = build_signed_place_text(
                venue="bybit",
                symbol="WALUSDT",
                side="buy",
                qty="10",
                credentials=_creds(),
                dual_leg_id="duallegid0123456789012345678901",
            )
            o_text, o_req, _ = build_signed_place_text(
                venue="okx",
                symbol="WAL-USDT-SWAP",
                side="sell",
                qty="10",
                credentials=_creds(okx=True),
                inst_id_code=193761,
                dual_leg_id="duallegid0123456789012345678901",
            )
            result = send_signed_dual(
                sender=sender,
                bybit_text=b_text,
                okx_text=o_text,
                bybit_req_id=b_req,
                okx_req_id=o_req,
                phase="open",
                intent_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                dual_leg_id="duallegid0123456789012345678901",
                signal_ts_ms=1_700_000_000_000,
            )
            sender.close()
            self.assertIsNone(result.error)
            self.assertIsNotNone(result.first_sent_ns)
            self.assertIsNotNone(result.second_sent_ns)
            self.assertEqual(recvs, [])
            # Inbound fills were not consumed.
            self.assertEqual(len(bybit._inbox), 1)  # noqa: SLF001
            self.assertEqual(len(okx._inbox), 1)  # noqa: SLF001
            events = scan_all_wire_events(Path(td))
            outs = [e for e in events if e["dir"] == "out"]
            self.assertEqual(len(outs), 2)
            venues = {e["venue"] for e in outs}
            self.assertEqual(venues, {"bybit", "okx"})
            for ev in outs:
                self.assertEqual(ev["intent_id"], "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
                self.assertEqual(ev["req_id"] in {b_req, o_req}, True)
                self.assertEqual(ev["payload"]["header"]["X-BAPI-SIGN"] if ev["venue"] == "bybit" else True, REDACTED if ev["venue"] == "bybit" else True)
            tr.close()

    def test_live_broker_place_does_not_call_recv(self) -> None:
        recvs: list[str] = []

        def _send(item) -> None:
            if getattr(item, "text", None) is None:
                return
            # Deliberately no fill recv on the send path.

        def _ack_ok(venue: str, req_id: str, _timeout: float) -> AckOutcome:
            return AckOutcome(
                venue=venue,
                req_id=req_id,
                accepted=True,
                timed_out=False,
                venue_code="0",
                recv_ns=1,
            )

        with tempfile.TemporaryDirectory() as td:
            broker = LiveBroker(
                data_root=Path(td),
                journal=JournalWriter(Path(td)),
                trade_lat_ms=100,
                notional_usdt=20.0,
                log=lambda _m: None,
                env={"VENUE": "live", "LIVE_ORDERS": "1"},
                send_fn=_send,
                inst_id_codes={"SOL-USDT-SWAP": 193761},
                bybit_credentials=_creds(),
                ack_wait_fn=_ack_ok,
            )
            orig_recv = FakePrivateWsSocket.recv_text

            def _watch(self, *, timeout_sec=None):  # type: ignore[no-untyped-def]
                recvs.append("recv")
                return orig_recv(self, timeout_sec=timeout_sec)

            with patch.object(FakePrivateWsSocket, "recv_text", _watch):
                abort = broker.place(
                    spread_side="open_long",
                    base_coin="SOL",
                    signal_ts_ms=1,
                    okx_book=_book(),
                    bybit_book=_book(),
                    meta=_meta("SOL"),
                )
            self.assertIsNone(abort)
            self.assertEqual(broker.last_send_path, SEND_PATH_TRIVIAL)
            self.assertEqual(recvs, [])
            self.assertIsNone(broker.pending)
            broker.close()


class WarmAndTrivialWireTests(unittest.TestCase):
    def tearDown(self) -> None:
        from app.bot.private.ws_warm_session import clear_process_warm_session

        clear_process_warm_session(stop=True)
        clear_process_wire_transcript(close=True)

    def _live_env(self, td: str) -> dict[str, str]:
        from app.bot.private.secrets import LIVE_KEY_NAMES

        live_env = Path(td) / "bbot-private-live.env"
        live_env.write_text(
            "\n".join(f"{n}=v{i}" for i, n in enumerate(LIVE_KEY_NAMES)) + "\n",
            encoding="utf-8",
        )
        return {
            "VENUE": "live",
            "LIVE_ORDERS": "1",
            "BBOT_PRIVATE_ENV_FILE": str(live_env),
            "BBOT_PRIVATE_DATA_ROOT": str(Path(td) / "data"),
        }

    def _push_hs(self, priv, trade, *, okx: bool) -> None:
        if okx:
            priv.push_inbound(json.dumps({"event": "login", "code": "0"}))
            priv.push_inbound(
                json.dumps(
                    {"event": "subscribe", "code": "0", "arg": {"channel": "orders"}}
                )
            )
            trade.push_inbound(json.dumps({"event": "login", "code": "0"}))
        else:
            priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            trade.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))

    def test_warm_handshake_and_trivial_place_write_wire(self) -> None:
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_trivial_dual_leg import warm_trade_send_fn
        from app.bot.private.ws_warm_session import (
            WarmSocketBundle,
            start_warm_private_session,
        )

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)

            def make() -> WarmSocketBundle:
                bpriv = FakePrivateWsSocket()
                btrade = FakePrivateWsSocket()
                opriv = FakePrivateWsSocket()
                otrade = FakePrivateWsSocket()
                self._push_hs(bpriv, btrade, okx=False)
                self._push_hs(opriv, otrade, okx=True)
                return WarmSocketBundle(
                    bybit_private=bpriv,
                    bybit_trade=btrade,
                    okx_private=opriv,
                    okx_trade=otrade,
                )

            session = start_warm_private_session(
                env=env,
                bybit_credentials=_creds(),
                okx_credentials=_creds(okx=True),
                socket_provider=make,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                attach=True,
            )
            self.assertTrue(session.is_ready())
            self.assertIsNotNone(session.wire)
            session.wire.flush()  # type: ignore[union-attr]
            warm_events = scan_all_wire_events(session.journal.data_root)
            outs = [e for e in warm_events if e["dir"] == "out"]
            ins = [e for e in warm_events if e["dir"] == "in"]
            self.assertGreaterEqual(len(outs), 4)  # auth+sub per venue at least
            self.assertGreaterEqual(len(ins), 4)
            venues = {e["venue"] for e in warm_events}
            sockets = {e["socket"] for e in warm_events}
            self.assertEqual(venues, {"bybit", "okx"})
            self.assertEqual(sockets, {"private", "trade"})
            for ev in warm_events:
                blob = json.dumps(ev)
                self.assertNotIn("bybit-live-key-ABCDEF", blob)
                self.assertNotIn("okx-passphrase-SECRET", blob)
                self.assertEqual(ev["run_id"], session.run_id)

            sender = TrivialDualSender(send_fn=warm_trade_send_fn(session))
            b_text, b_req, _ = build_signed_place_text(
                venue="bybit",
                symbol="WALUSDT",
                side="buy",
                qty="10",
                credentials=_creds(),
                dual_leg_id="duallegid0123456789012345678901",
            )
            o_text, o_req, _ = build_signed_place_text(
                venue="okx",
                symbol="WAL-USDT-SWAP",
                side="sell",
                qty="10",
                credentials=_creds(okx=True),
                inst_id_code=193761,
                dual_leg_id="duallegid0123456789012345678901",
            )
            result = send_signed_dual(
                sender=sender,
                bybit_text=b_text,
                okx_text=o_text,
                bybit_req_id=b_req,
                okx_req_id=o_req,
                phase="open",
                intent_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                dual_leg_id="duallegid0123456789012345678901",
                signal_ts_ms=1_700_000_000_000,
                place_io=session.place_io_section(),
            )
            sender.close()
            self.assertIsNone(result.error)
            session.wire.flush()  # type: ignore[union-attr]
            after = scan_all_wire_events(session.journal.data_root)
            places = [
                e
                for e in after
                if e["dir"] == "out"
                and e["socket"] == "trade"
                and e.get("op") in {"order.create", "order"}
            ]
            self.assertEqual(len(places), 2)
            self.assertEqual({e["req_id"] for e in places}, {b_req, o_req})
            for ev in places:
                self.assertEqual(ev["intent_id"], "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
                self.assertEqual(ev["dual_leg_id"], "duallegid0123456789012345678901")
            dated = wire_jsonl_path(
                session.journal.data_root, places[0]["event_date"]
            )
            self.assertTrue(dated.is_file())
            session.stop()


if __name__ == "__main__":
    unittest.main()
