"""Private synthetic place path: qty, leg map, injected sender. No sockets."""

from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from app.bot.private.coin_qty import CoinQtyError, shared_coin_qty, shared_from_meta
from app.bot.private.journal_v1 import PrivateJournalWriter
from app.bot.paths import theta_step_chrono_jsonl_path, theta_trades_jsonl_path
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.okx_inst_id import lookup_okx_inst_id_code, prefetch_okx_inst_id_codes
from app.bot.private.order_preflight import LiveHttpMetadataProvider
from app.bot.private.place_send import (
    drain_trade_fill,
    place_live,
    read_warm_trade_frame,
)
from app.bot.private.private_leg_up import clear_all, leg_up, set_exchange_coins
from app.bot.private.step_chrono import StepChrono
from app.bot.private.ws_private import PrivateStreamRuntime, SubscriptionReadiness
from app.bot.theta_trade_manager import ThetaTradeConfig, ThetaTradeManager


def _book(px: float, sz: float = 1000.0) -> dict:
    return {
        "bid_price": px,
        "ask_price": px,
        "bid_size": sz,
        "ask_size": sz,
    }


def _meta(**overrides):
    raw = dict(
        base_coin="BTC",
        okx_symbol="BTC-USDT-SWAP",
        bybit_symbol="BTCUSDT",
        okx_lot_size=Decimal("1"),
        okx_min_size=Decimal("1"),
        okx_ct_val=Decimal("1"),
        bybit_qty_step=Decimal("1"),
        bybit_min_order_qty=Decimal("1"),
        bybit_min_notional_value=Decimal("5"),
    )
    raw.update(overrides)
    return SimpleNamespace(**raw)


class _Ws:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


class _Sender:
    def __init__(self, ws: _Ws) -> None:
        self.ws = ws

    def enqueue_dual(self, *, bybit_text: str, okx_text: str, **_kwargs) -> None:
        self.ws.send(bybit_text)
        self.ws.send(okx_text)


def _creds() -> LiveCredentials:
    return LiveCredentials(api_key="k", api_secret="s", passphrase="p")


def _read(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class CoinQtyTests(unittest.TestCase):
    def test_closest_shared_coin_to_ten_usd(self) -> None:
        # price 3 → coin 3 notionals 9, coin 4 notionals 12. Pick 3.
        sized = shared_coin_qty(
            okx_px=Decimal("3"),
            bybit_px=Decimal("3"),
            ct_val=Decimal("1"),
            okx_lot_sz=Decimal("1"),
            okx_min_sz=Decimal("1"),
            bybit_qty_step=Decimal("1"),
            bybit_min_qty=Decimal("1"),
        )
        self.assertEqual(sized.coin_qty, Decimal("3"))
        self.assertEqual(sized.okx_sz, Decimal("3"))
        self.assertEqual(sized.bybit_qty, Decimal("3"))
        self.assertEqual(sized.okx_notional, Decimal("9"))

        # ctVal 3, bybit step 1, price 1 → shared coins 9 (notional 9) and 12.
        stepped = shared_coin_qty(
            okx_px=Decimal("1"),
            bybit_px=Decimal("1"),
            ct_val=Decimal("3"),
            okx_lot_sz=Decimal("1"),
            okx_min_sz=Decimal("1"),
            bybit_qty_step=Decimal("1"),
            bybit_min_qty=Decimal("1"),
        )
        self.assertEqual(stepped.coin_qty, Decimal("9"))
        self.assertEqual(stepped.okx_sz * stepped.coin_qty / stepped.okx_sz, Decimal("9"))
        self.assertEqual(stepped.bybit_qty, Decimal("9"))
        self.assertEqual(stepped.okx_sz * Decimal("3"), stepped.bybit_qty)

    def test_min_notional_above_band(self) -> None:
        with self.assertRaises(CoinQtyError) as ctx:
            shared_coin_qty(
                okx_px=Decimal("20"),
                bybit_px=Decimal("20"),
                ct_val=Decimal("1"),
                okx_lot_sz=Decimal("1"),
                okx_min_sz=Decimal("1"),
                bybit_qty_step=Decimal("1"),
                bybit_min_qty=Decimal("1"),
            )
        self.assertEqual(ctx.exception.code, "min_notional_above_band")

    def test_missing_ct_val_is_qty_mismatch(self) -> None:
        with self.assertRaises(CoinQtyError) as ctx:
            shared_from_meta(
                meta=_meta(okx_ct_val=None),
                okx_px=Decimal("2"),
                bybit_px=Decimal("2"),
            )
        self.assertEqual(ctx.exception.code, "qty_mismatch")


class LegAndSendTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_all()
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "bbot"
        self.root.mkdir()
        self.ws = _Ws()
        self.sender = _Sender(self.ws)

    def tearDown(self) -> None:
        clear_all()

    def test_missing_leg_is_down(self) -> None:
        self.assertFalse(leg_up("okx", "BTC"))
        self.assertFalse(leg_up("bybit", "BTC"))

    def test_channel_down_sends_nothing(self) -> None:
        result = place_live(
            data_root=self.root,
            spread_side="open_long",
            base_coin="BTC",
            signal_ts_ms=1_700_000_000_000,
            okx_book=_book(2),
            bybit_book=_book(2),
            meta=_meta(),
            sender=self.sender,
            credentials=_creds(),
            inst_id_code=101,
            recv_fn=lambda _v: None,
        )
        self.assertEqual(result.abort, "private_channel_down")
        self.assertEqual(self.ws.sent, [])
        self.assertFalse(result.completed)

    def test_qty_mismatch_and_band_do_not_send(self) -> None:
        set_exchange_coins("okx", ["BTC"], True)
        set_exchange_coins("bybit", ["BTC"], True)
        mismatch = place_live(
            data_root=self.root,
            spread_side="open_long",
            base_coin="BTC",
            signal_ts_ms=1_700_000_000_000,
            okx_book=_book(2),
            bybit_book=_book(2),
            meta=_meta(okx_ct_val=None),
            sender=self.sender,
            credentials=_creds(),
            inst_id_code=101,
        )
        self.assertEqual(mismatch.abort, "qty_mismatch")
        self.assertEqual(self.ws.sent, [])
        band = place_live(
            data_root=self.root,
            spread_side="open_long",
            base_coin="BTC",
            signal_ts_ms=1_700_000_000_000,
            okx_book=_book(20),
            bybit_book=_book(20),
            meta=_meta(),
            sender=self.sender,
            credentials=_creds(),
            inst_id_code=101,
        )
        self.assertEqual(band.abort, "min_notional_above_band")
        self.assertEqual(self.ws.sent, [])

    def test_both_legs_up_two_sends_and_both_fills(self) -> None:
        set_exchange_coins("okx", ["BTC"], True)
        set_exchange_coins("bybit", ["BTC"], True)

        def recv(venue: str) -> str:
            if venue == "okx":
                return json.dumps({"data": [{"fillPx": "2.01"}]})
            return json.dumps({"data": [{"avgPx": "1.99"}]})

        result = place_live(
            data_root=self.root,
            spread_side="open_long",
            base_coin="BTC",
            signal_ts_ms=1_700_000_000_000,
            okx_book=_book(2),
            bybit_book=_book(2),
            meta=_meta(),
            sender=self.sender,
            credentials=_creds(),
            inst_id_code=101,
            recv_fn=recv,
        )
        self.assertEqual(len(self.ws.sent), 2)
        self.assertTrue(result.completed)
        self.assertEqual(result.status, "open")
        self.assertEqual(result.okx_fill_px, "2.01")
        self.assertEqual(result.bybit_fill_px, "1.99")
        self.assertIsNotNone(result.latency_ms)
        self.assertGreaterEqual(result.latency_ms, 0)
        self.assertEqual(result.coin_qty, "5")
        joined = " ".join(self.ws.sent)
        self.assertNotIn("reduceOnly", joined)

    def test_one_fill_does_not_complete(self) -> None:
        set_exchange_coins("okx", ["BTC"], True)
        set_exchange_coins("bybit", ["BTC"], True)

        def recv(venue: str) -> str:
            if venue == "okx":
                return json.dumps({"fillPx": "2.0"})
            return json.dumps({"state": "live"})

        result = place_live(
            data_root=self.root,
            spread_side="open_long",
            base_coin="BTC",
            signal_ts_ms=1_700_000_000_000,
            okx_book=_book(2),
            bybit_book=_book(2),
            meta=_meta(),
            sender=self.sender,
            credentials=_creds(),
            inst_id_code=101,
            recv_fn=recv,
        )
        self.assertEqual(len(self.ws.sent), 2)
        self.assertFalse(result.completed)
        self.assertTrue(result.keep_pending)
        self.assertEqual(result.abort, "partial_fill")
        day = "2023-11-14"
        rows = _read(theta_trades_jsonl_path(self.root, day))
        statuses = [r.get("status") for r in rows]
        self.assertIn("pending", statuses)
        self.assertNotIn("open", statuses)
        self.assertNotIn("closed", statuses)

    def test_close_reduce_only_and_chrono(self) -> None:
        set_exchange_coins("okx", ["ETH"], True)
        set_exchange_coins("bybit", ["ETH"], True)

        def recv(venue: str) -> str:
            if venue == "okx":
                return json.dumps({"avgPx": "2.02"})
            return json.dumps({"fillPx": "2.03"})

        result = place_live(
            data_root=self.root,
            spread_side="close",
            base_coin="ETH",
            signal_ts_ms=1_700_000_000_000,
            okx_book=_book(2),
            bybit_book=_book(2),
            meta=_meta(base_coin="ETH", okx_symbol="ETH-USDT-SWAP", bybit_symbol="ETHUSDT"),
            close_of="open_long",
            sender=self.sender,
            credentials=_creds(),
            inst_id_code=101,
            recv_fn=recv,
        )
        self.assertEqual(result.status, "closed")
        self.assertTrue(result.completed)
        self.assertEqual(len(self.ws.sent), 2)
        self.assertTrue(any("reduceOnly" in text for text in self.ws.sent))
        day = "2023-11-14"
        chrono = _read(theta_step_chrono_jsonl_path(self.root, day))
        for block in (
            "preprocess",
            "channel_check",
            "ws_send",
            "journal_pending",
            "wait_fill",
            "fill_done",
        ):
            enter = next(r for r in chrono if r["block"] == block and r["edge"] == "enter")
            exit_ = next(r for r in chrono if r["block"] == block and r["edge"] == "exit")
            self.assertGreaterEqual(exit_["mono_ns"], enter["mono_ns"])
            self.assertIn("wall_ms", enter)
            self.assertIn("signal_ts_ms", enter)
        venues = [r for r in chrono if r["block"] == "venue_message"]
        self.assertEqual(sorted(r["venue"] for r in venues), ["bybit", "okx"])

    def test_login_and_disconnect_write_the_map(self) -> None:
        journal = PrivateJournalWriter(self.root)
        rt = PrivateStreamRuntime(
            exchange="okx",
            environment="live",
            symbol_alias="BTC-USDT-SWAP",
            journal=journal,
            run_id=journal.run_id,
            credentials=_creds(),
            base_coins=("BTC",),
            authenticated=True,
            subscription_readiness=SubscriptionReadiness.READY,
        )
        rt.journal_auth(success=True)
        rt.publish_private_leg_state()
        self.assertTrue(leg_up("okx", "BTC"))
        rt.mark_reconnect()
        self.assertFalse(leg_up("okx", "BTC"))

    def test_chrono_refuses_d_path(self) -> None:
        with self.assertRaises(RuntimeError):
            StepChrono(Path("/data/bars"), intent_id="x", signal_ts_ms=1)


class LiveCycleTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_all()
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "bbot"
        self.root.mkdir()
        self.ws = _Ws()
        self.sender = _Sender(self.ws)
        set_exchange_coins("okx", ["BTC", "ETH", "SOL"], True)
        set_exchange_coins("bybit", ["BTC", "ETH", "SOL"], True)

    def tearDown(self) -> None:
        clear_all()

    def test_injected_sender_open_then_close_returns_flat(self) -> None:
        def recv(venue: str) -> str:
            if venue == "okx":
                return json.dumps({"fillPx": "2.1"})
            return json.dumps({"avgPx": "2.2"})

        def place(**kwargs):
            return place_live(
                data_root=self.root,
                sender=self.sender,
                credentials=_creds(),
                inst_id_code=101,
                recv_fn=recv,
                **kwargs,
            )

        def decide(*, slot, **_kwargs):
            if slot.position is None and not slot.pending:
                return SimpleNamespace(action="open", coin="ETH", side="long")
            if slot.position is not None and not slot.pending:
                return SimpleNamespace(
                    action="close",
                    coin=slot.position.base_coin,
                    side=slot.position.side,
                )
            return SimpleNamespace(action="hold", coin="", side="")

        mgr = ThetaTradeManager(
            data_root=self.root,
            config=ThetaTradeConfig(notional_usdt=10.0, fill_delay_ms=0),
            decide_fn=decide,
            place_fn=place,
            meta_fn=lambda coin: _meta(
                base_coin=coin,
                okx_symbol=f"{coin}-USDT-SWAP",
                bybit_symbol=f"{coin}USDT",
            ),
        )
        quotes = {
            c: {"okx": _book(2), "bybit": _book(2)} for c in ("BTC", "ETH", "SOL")
        }
        mgr.on_theta_snapshots([], quotes=quotes, coin_order=["ETH"], now_ms=1_700_000_000_000)
        self.assertIsNotNone(mgr.slot.position)
        self.assertEqual(mgr.slot.position.base_coin, "ETH")
        self.assertFalse(mgr.slot.pending)
        sends_after_open = len(self.ws.sent)
        self.assertEqual(sends_after_open, 2)
        mgr.on_theta_snapshots([], quotes=quotes, coin_order=["ETH"], now_ms=1_700_000_001_000)
        self.assertIsNone(mgr.slot.position)
        self.assertFalse(mgr.slot.pending)
        self.assertEqual(len(self.ws.sent), 4)
        rows = _read(theta_trades_jsonl_path(self.root, "2023-11-14"))
        opened = next(r for r in rows if r.get("status") == "open")
        closed = next(r for r in rows if r.get("status") == "closed")
        self.assertEqual(opened["coin_qty"], "5")
        self.assertIsNotNone(opened["latency_ms"])
        self.assertIsNotNone(closed["latency_ms"])
        self.assertEqual(opened["okx_fill_px"], "2.1")
        self.assertEqual(opened["bybit_fill_px"], "2.2")
        # Notional 5 coins * 2 USD = 10, closer than 4*2=8 or 6*2=12.
        self.assertEqual(Decimal(opened["coin_qty"]) * Decimal("2"), Decimal("10"))


class _QueueSock:
    """Trade inbound queue. ``recv`` is absent so a raw websocket read fails."""

    def __init__(self, frames: list[str]) -> None:
        self.frames = list(frames)
        self.calls = 0

    def recv_text(self, *, timeout_sec: float | None = None) -> str:
        del timeout_sec
        self.calls += 1
        if not self.frames:
            raise TimeoutError("empty")
        return self.frames.pop(0)


class _FrameRuntime:
    def __init__(self, stash: list[str], sock: _QueueSock, exchange: str) -> None:
        self._stash = list(stash)
        self.trade_socket = sock
        self.exchange = exchange

    def _pop_trade_inbound(self) -> str | None:
        if not self._stash:
            return None
        return self._stash.pop(0)


class FillWaitTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_all()
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "bbot"
        self.root.mkdir()
        self.ws = _Ws()
        self.sender = _Sender(self.ws)
        set_exchange_coins("okx", ["BTC"], True)
        set_exchange_coins("bybit", ["BTC"], True)

    def tearDown(self) -> None:
        clear_all()

    def test_stash_before_inbound_queue(self) -> None:
        cl = "o50db25403a434bb999725beda0d6a5"
        ack = json.dumps(
            {"op": "order", "code": "0", "data": [{"sCode": "0", "clOrdId": cl}]}
        )
        fill = json.dumps(
            {
                "arg": {"channel": "orders", "instId": "BTC-USDT-SWAP"},
                "data": [{"clOrdId": cl, "fillPx": "2.5"}],
            }
        )
        sock = _QueueSock([fill])
        runtime = _FrameRuntime(["ping", ack], sock, "okx")
        waited = drain_trade_fill(
            lambda timeout_sec: read_warm_trade_frame(runtime, timeout_sec),
            exchange="okx",
            timeout_sec=1.0,
        )
        self.assertEqual(waited.ack_body, ack)
        self.assertEqual(waited.fill_body, fill)
        self.assertEqual(waited.verdict, "accept")
        self.assertIsInstance(waited.fill_wall_ms, int)
        self.assertEqual(sock.calls, 1)
        self.assertFalse(hasattr(sock, "recv"))

    def test_reject_is_returned_before_a_later_fill(self) -> None:
        reject = json.dumps(
            {"code": "1", "data": [{"sCode": "51000", "sMsg": "Parameter clOrdId error"}]}
        )
        sock = _QueueSock([json.dumps({"fillPx": "2.5", "clOrdId": "x"})])
        runtime = _FrameRuntime([reject], sock, "okx")
        waited = drain_trade_fill(
            lambda timeout_sec: read_warm_trade_frame(runtime, timeout_sec),
            exchange="okx",
            timeout_sec=1.0,
        )
        self.assertEqual(waited.verdict, "reject")
        self.assertEqual(waited.ack_body, reject)
        self.assertIsNone(waited.fill_body)
        self.assertEqual(sock.calls, 0)

    def test_orders_push_after_ack_completes_with_that_fill_px(self) -> None:
        okx_cl = "o50db25403a434bb999725beda0d6a5"
        okx_ord = "998877"
        bybit_link = "b50db25403a434bb999725beda0d6a5d0"
        okx_ack = json.dumps(
            {
                "id": "1",
                "op": "order",
                "code": "0",
                "data": [{"sCode": "0", "clOrdId": okx_cl, "ordId": okx_ord}],
            }
        )
        other = json.dumps(
            {
                "arg": {"channel": "orders", "instId": "ETH-USDT-SWAP"},
                "data": [{"clOrdId": "someone-else", "ordId": "1", "fillPx": "9.9"}],
            }
        )
        okx_fill = json.dumps(
            {
                "arg": {
                    "channel": "orders",
                    "instType": "SWAP",
                    "instId": "BTC-USDT-SWAP",
                },
                "data": [
                    {
                        "clOrdId": okx_cl,
                        "ordId": okx_ord,
                        "fillPx": "2.1",
                        "avgPx": "2.1",
                        "accFillSz": "5",
                        "state": "filled",
                    }
                ],
            }
        )
        bybit_ack = json.dumps(
            {
                "op": "order.create",
                "retCode": 0,
                "retMsg": "OK",
                "data": {"orderId": "77", "orderLinkId": bybit_link},
            }
        )
        bybit_fill = json.dumps(
            {
                "topic": "execution",
                "data": [
                    {
                        "orderId": "77",
                        "orderLinkId": bybit_link,
                        "execPrice": "2.2",
                    }
                ],
            }
        )
        queues = {
            "okx": ["pong", okx_ack, other, okx_fill],
            "bybit": [json.dumps({"op": "pong"}), bybit_ack, bybit_fill],
        }

        def recv(venue: str) -> object:
            frames = queues[venue]

            def read(_timeout: float) -> str:
                if not frames:
                    raise TimeoutError("empty")
                return frames.pop(0)

            return drain_trade_fill(read, exchange=venue, timeout_sec=1.0)

        signal_ts = 1_700_000_000_000
        result = place_live(
            data_root=self.root,
            spread_side="open_long",
            base_coin="BTC",
            signal_ts_ms=signal_ts,
            okx_book=_book(2),
            bybit_book=_book(2),
            meta=_meta(),
            sender=self.sender,
            credentials=_creds(),
            inst_id_code=101,
            recv_fn=recv,
        )
        self.assertTrue(result.completed)
        self.assertIsNone(result.abort)
        self.assertEqual(result.okx_fill_px, "2.1")
        self.assertEqual(result.bybit_fill_px, "2.2")
        self.assertEqual(result.latency_ms, result.fill_ts_ms - signal_ts)
        self.assertEqual(len(self.ws.sent), 2)
        rows = _read(theta_trades_jsonl_path(self.root, "2023-11-14"))
        self.assertIn("open", [r.get("status") for r in rows])
        okx_ack_row = next(
            r for r in rows if r.get("venue") == "okx" and r.get("venue_verdict") == "accept"
        )
        self.assertEqual(okx_ack_row["body"], okx_ack)
        self.assertNotIn("fillPx", okx_ack_row.get("fields", {}))
        okx_fill_row = next(
            r for r in rows if r.get("venue") == "okx" and r.get("body") == okx_fill
        )
        self.assertEqual(okx_fill_row["fields"]["fillPx"], "2.1")
        self.assertEqual(okx_fill_row["fields"]["accFillSz"], "5")
        self.assertIsInstance(okx_fill_row["wall_ms"], int)
        self.assertGreaterEqual(result.fill_ts_ms, okx_fill_row["wall_ms"])

    def test_accepted_without_fill_px_is_not_partial_fill(self) -> None:
        okx_body = json.dumps({"op": "order", "code": "0", "data": [{"sCode": "0"}]})
        bybit_body = json.dumps({"retCode": 0, "retMsg": "OK", "op": "order.create"})

        def recv(venue: str) -> str:
            if venue == "okx":
                return okx_body
            return bybit_body

        result = place_live(
            data_root=self.root,
            spread_side="open_long",
            base_coin="BTC",
            signal_ts_ms=1_700_000_000_000,
            okx_book=_book(2),
            bybit_book=_book(2),
            meta=_meta(),
            sender=self.sender,
            credentials=_creds(),
            inst_id_code=101,
            recv_fn=recv,
        )
        self.assertIsNone(result.abort)
        self.assertFalse(result.completed)
        self.assertTrue(result.keep_pending)
        self.assertEqual(result.status, "accepted")
        self.assertNotEqual(result.abort, "partial_fill")
        rows = _read(theta_trades_jsonl_path(self.root, "2023-11-14"))
        statuses = [r.get("status") for r in rows]
        self.assertIn("pending", statuses)
        self.assertNotIn("open", statuses)
        self.assertNotIn("closed", statuses)
        okx_msg = next(r for r in rows if r.get("venue") == "okx")
        bybit_msg = next(r for r in rows if r.get("venue") == "bybit")
        self.assertEqual(okx_msg["body"], okx_body)
        self.assertEqual(okx_msg["venue_verdict"], "accept")
        self.assertEqual(okx_msg["fields"]["code"], "0")
        self.assertEqual(okx_msg["fields"]["sCode"], "0")
        self.assertNotIn("fillPx", okx_msg["fields"])
        self.assertNotIn("avgPx", okx_msg["fields"])
        self.assertEqual(bybit_msg["body"], bybit_body)
        self.assertEqual(bybit_msg["venue_verdict"], "accept")
        self.assertEqual(bybit_msg["fields"]["retCode"], 0)
        self.assertEqual(bybit_msg["fields"]["retMsg"], "OK")

    def test_reject_keeps_the_venue_message(self) -> None:
        okx_body = json.dumps(
            {
                "code": "0",
                "data": [{"sCode": "51000", "sMsg": "Parameter clOrdId error"}],
            }
        )
        bybit_body = json.dumps({"retCode": 0, "retMsg": "OK"})

        def recv(venue: str) -> str:
            if venue == "okx":
                return okx_body
            return bybit_body

        result = place_live(
            data_root=self.root,
            spread_side="open_long",
            base_coin="BTC",
            signal_ts_ms=1_700_000_000_000,
            okx_book=_book(2),
            bybit_book=_book(2),
            meta=_meta(),
            sender=self.sender,
            credentials=_creds(),
            inst_id_code=101,
            recv_fn=recv,
        )
        self.assertEqual(result.abort, "venue_reject")
        self.assertFalse(result.completed)
        self.assertTrue(result.keep_pending)
        rows = _read(theta_trades_jsonl_path(self.root, "2023-11-14"))
        statuses = [r.get("status") for r in rows]
        self.assertNotIn("open", statuses)
        self.assertNotIn("closed", statuses)
        okx_msg = next(r for r in rows if r.get("venue") == "okx")
        self.assertEqual(okx_msg["body"], okx_body)
        self.assertEqual(okx_msg["venue_verdict"], "reject")
        self.assertEqual(okx_msg["fields"]["sCode"], "51000")
        self.assertEqual(okx_msg["fields"]["sMsg"], "Parameter clOrdId error")

    def test_timeout_partial_fill_does_not_send_again(self) -> None:
        calls = {"n": 0}

        def recv(venue: str) -> str | None:
            def read(_timeout: float) -> str:
                raise TimeoutError("empty")

            return drain_trade_fill(read, exchange=venue, timeout_sec=0.05)

        def place(**kwargs):
            calls["n"] += 1
            return place_live(
                data_root=self.root,
                sender=self.sender,
                credentials=_creds(),
                inst_id_code=101,
                recv_fn=recv,
                **kwargs,
            )

        def decide(*, slot, **_kwargs):
            del slot
            return SimpleNamespace(action="open", coin="BTC", side="long")

        mgr = ThetaTradeManager(
            data_root=self.root,
            config=ThetaTradeConfig(notional_usdt=10.0, fill_delay_ms=0),
            decide_fn=decide,
            place_fn=place,
            meta_fn=lambda coin: _meta(base_coin=coin),
        )
        quotes = {"BTC": {"okx": _book(2), "bybit": _book(2)}}
        mgr.on_theta_snapshots([], quotes=quotes, coin_order=["BTC"], now_ms=1_700_000_000_000)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(len(self.ws.sent), 2)
        self.assertTrue(mgr.slot.pending)
        self.assertIsNone(mgr.slot.position)
        mgr.on_theta_snapshots([], quotes=quotes, coin_order=["BTC"], now_ms=1_700_000_001_000)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(len(self.ws.sent), 2)


class InstIdCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_all()
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "bbot"
        self.root.mkdir()
        self.ws = _Ws()
        self.sender = _Sender(self.ws)
        set_exchange_coins("okx", ["SOL", "XRP"], True)
        set_exchange_coins("bybit", ["SOL", "XRP"], True)

    def tearDown(self) -> None:
        clear_all()

    def _recv(self, venue: str) -> str:
        if venue == "okx":
            return json.dumps({"fillPx": "2"})
        return json.dumps({"avgPx": "2"})

    def test_instruments_lookup_reads_inst_id_code(self) -> None:
        seen: list[str] = []

        def http_get_json(url: str, _headers: object) -> dict:
            seen.append(url)
            if "SOL-USDT-SWAP" in url:
                inst = "SOL-USDT-SWAP"
                code = 193761
            else:
                inst = "XRP-USDT-SWAP"
                code = 188237
            return {
                "code": "0",
                "data": [
                    {
                        "instId": inst,
                        "instType": "SWAP",
                        "settleCcy": "USDT",
                        "instIdCode": code,
                    }
                ],
            }

        provider = LiveHttpMetadataProvider(http_get_json=http_get_json)
        codes = prefetch_okx_inst_id_codes(
            ["SOL-USDT-SWAP", "XRP-USDT-SWAP"],
            fetch_fn=provider.okx_inst_id_code,
        )
        self.assertEqual(codes["SOL-USDT-SWAP"], 193761)
        self.assertEqual(codes["XRP-USDT-SWAP"], 188237)
        self.assertTrue(all("instruments" in url and "ticker" not in url for url in seen))
        self.assertEqual(len(seen), 2)

    def test_missing_code_does_not_call_sender(self) -> None:
        result = place_live(
            data_root=self.root,
            spread_side="open_long",
            base_coin="SOL",
            signal_ts_ms=1_700_000_000_000,
            okx_book=_book(2),
            bybit_book=_book(2),
            meta=_meta(
                base_coin="SOL",
                okx_symbol="SOL-USDT-SWAP",
                bybit_symbol="SOLUSDT",
            ),
            sender=self.sender,
            credentials=_creds(),
            inst_id_code=None,
            recv_fn=self._recv,
        )
        self.assertEqual(result.abort, "okx_inst_id_code_missing")
        self.assertFalse(result.completed)
        self.assertEqual(self.ws.sent, [])

    def test_prefetched_code_is_passed_into_the_frame(self) -> None:
        hits: list[str] = []

        def fetch(symbol: str) -> int:
            hits.append(symbol)
            return {"SOL-USDT-SWAP": 193761, "XRP-USDT-SWAP": 188237}[symbol]

        codes = prefetch_okx_inst_id_codes(
            ["SOL-USDT-SWAP", "XRP-USDT-SWAP"],
            env={"BBOT_OKX_INST_ID_CODES": "BTC-USDT-SWAP:1"},
            fetch_fn=fetch,
        )
        self.assertEqual(hits, ["SOL-USDT-SWAP", "XRP-USDT-SWAP"])
        self.assertEqual(lookup_okx_inst_id_code(codes, "BTC-USDT-SWAP"), 1)

        for symbol, coin, code in (
            ("SOL-USDT-SWAP", "SOL", 193761),
            ("XRP-USDT-SWAP", "XRP", 188237),
        ):
            before = len(self.ws.sent)
            result = place_live(
                data_root=self.root,
                spread_side="open_long",
                base_coin=coin,
                signal_ts_ms=1_700_000_000_000,
                okx_book=_book(2),
                bybit_book=_book(2),
                meta=_meta(
                    base_coin=coin,
                    okx_symbol=symbol,
                    bybit_symbol=f"{coin}USDT",
                ),
                sender=self.sender,
                credentials=_creds(),
                inst_id_code=lookup_okx_inst_id_code(codes, symbol),
                recv_fn=self._recv,
            )
            self.assertTrue(result.completed, result.abort)
            okx_frame = json.loads(self.ws.sent[before + 1])
            self.assertEqual(okx_frame["args"][0]["instIdCode"], code)
            self.assertIsInstance(okx_frame["args"][0]["instIdCode"], int)
        self.assertEqual(hits, ["SOL-USDT-SWAP", "XRP-USDT-SWAP"])


class ClOrdIdTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_all()
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "bbot"
        self.root.mkdir()
        self.ws = _Ws()
        self.sender = _Sender(self.ws)
        set_exchange_coins("okx", ["SOL"], True)
        set_exchange_coins("bybit", ["SOL"], True)

    def tearDown(self) -> None:
        clear_all()

    def _place(self, intent_id: str):
        def recv(venue: str) -> str:
            if venue == "okx":
                return json.dumps({"fillPx": "2"})
            return json.dumps({"avgPx": "2"})

        return place_live(
            data_root=self.root,
            spread_side="open_long",
            base_coin="SOL",
            signal_ts_ms=1_700_000_000_000,
            okx_book=_book(2),
            bybit_book=_book(2),
            meta=_meta(
                base_coin="SOL",
                okx_symbol="SOL-USDT-SWAP",
                bybit_symbol="SOLUSDT",
            ),
            sender=self.sender,
            credentials=_creds(),
            inst_id_code=193761,
            intent_id=intent_id,
            recv_fn=recv,
        )

    def test_uuid_intent_cl_ord_id_is_alphanumeric(self) -> None:
        intent = "50db2540-3a43-4bb9-9972-5beda0d6a5d0"
        dual = intent.replace("-", "")[:32]
        result = self._place(intent)
        self.assertTrue(result.completed, result.abort)
        self.assertEqual(len(self.ws.sent), 2)
        okx = json.loads(self.ws.sent[1])
        cl = okx["args"][0]["clOrdId"]
        self.assertEqual(cl, ("o" + dual)[:32])
        self.assertTrue(cl.isalnum())
        self.assertGreaterEqual(len(cl), 1)
        self.assertLessEqual(len(cl), 32)
        self.assertNotIn("-", cl)
        bybit = json.loads(self.ws.sent[0])
        link = bybit["args"][0]["orderLinkId"]
        self.assertEqual(link, ("b" + dual)[:36])
        self.assertNotIn("-", link)

    def test_illegal_cl_ord_id_does_not_send(self) -> None:
        result = self._place("bad id!!")
        self.assertEqual(result.abort, "okx_cl_ord_id_illegal")
        self.assertFalse(result.completed)
        self.assertEqual(self.ws.sent, [])


if __name__ == "__main__":
    unittest.main()
