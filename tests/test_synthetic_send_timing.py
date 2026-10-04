"""Synthetic send timing propagation and JSONL persistence; no sockets."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.bot.paths import theta_step_chrono_jsonl_path, theta_trades_jsonl_path
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.place_send import place_live
from app.bot.private.private_leg_up import clear_all, set_exchange_coins
from app.bot.private.send_legs import send_long, send_short
from app.bot.private.step_chrono import StepChrono
from app.bot.private.ws_trivial_dual_leg import (
    TrivialDualSender, TrivialSendItem, TrivialSendResult,
)


SIGNAL_TS = 1_700_000_000_000
INTENT = "50db2540-3a43-4bb9-9972-5beda0d6a5d0"
FIELDS = (
    "queue_enqueued_ns", "dequeued_ns", "callback_started_ns",
    "callback_returned_ns", "owner_ws_send_started_ns", "owner_ws_send_returned_ns",
)


def _result() -> TrivialSendResult:
    result = TrivialSendResult(
        first_enqueued_ns=1,
        second_enqueued_ns=2,
        items=[TrivialSendItem(
            venue="bybit", text="SECRET_FRAME", req_id="RAW_ORDER_ID",
            phase="open", enqueued_ns=1,
        )],
        timings={
            venue: {name: offset + i for i, name in enumerate(FIELDS)}
            for venue, offset in (("bybit", 100), ("okx", 200))
        },
    )
    return result


class _Sender:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def enqueue_dual(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


class SyntheticSendTimingTests(unittest.TestCase):
    def setUp(self):
        clear_all()
        for venue in ("bybit", "okx"):
            set_exchange_coins(venue, ["BTC"], True)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(clear_all)
        self.root = Path(self.temp.name) / "bbot"
        self.credentials = LiveCredentials(
            api_key="SECRET_KEY", api_secret="SECRET_CREDENTIAL", passphrase="SECRET_PASS",
        )

    def _place(self, sender, **overrides):
        kwargs = dict(
            data_root=self.root, spread_side="open_long", base_coin="BTC",
            signal_ts_ms=SIGNAL_TS, intent_id=INTENT,
            okx_book={"bid_price": 2, "ask_price": 2},
            bybit_book={"bid_price": 2, "ask_price": 2},
            meta=SimpleNamespace(
                okx_symbol="BTC-USDT-SWAP", bybit_symbol="BTCUSDT",
                okx_lot_size=Decimal("1"), okx_min_size=Decimal("1"),
                okx_ct_val=Decimal("1"), bybit_qty_step=Decimal("1"),
                bybit_min_order_qty=Decimal("1"), bybit_min_notional_value=Decimal("5"),
            ),
            sender=sender, credentials=self.credentials,
            leverage_one=True, inst_id_code=101,
            recv_fn=lambda venue: json.dumps(
                {"fillPx" if venue == "okx" else "avgPx": "2"}
            ),
        )
        kwargs.update(overrides)
        return place_live(**kwargs)

    def _rows(self):
        path = theta_step_chrono_jsonl_path(self.root, "2023-11-14")
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_send_long_and_short_retain_original_dual_result(self):
        for send in (send_long, send_short):
            with self.subTest(send=send.__name__):
                raw = _result()
                sender = _Sender(raw)
                sent = send(
                    coin="BTC", okx_symbol="BTC-USDT-SWAP", bybit_symbol="BTCUSDT",
                    okx_sz="5", bybit_qty="5", sender=sender,
                    credentials=self.credentials, inst_id_code=101,
                    intent_id=INTENT, signal_ts_ms=SIGNAL_TS,
                )
                self.assertIs(sent.send_result, raw)
                self.assertEqual((sent.abort, sent.sent), (None, 2))
                self.assertEqual(len(sender.calls), 1)

    def test_both_venue_timings_persist_after_send_for_open_and_close(self):
        for side, close_of in (("open_long", None), ("open_short", None), ("close", "open_long")):
            with self.subTest(side=side):
                raw = _result()
                raw.timings["bybit"]["frame"] = "SECRET_FRAME"
                raw.timings["unexpected_venue"] = {"req_id": "RAW_ORDER_ID"}
                sender = _Sender(raw)
                original = StepChrono.send_timing

                def capture(chrono, timings, *, phase):
                    self.assertEqual(chrono._rows[-1]["block"], "ws_send")
                    self.assertEqual(chrono._rows[-1]["edge"], "exit")
                    self.assertEqual(len(sender.calls), 1)
                    original(chrono, timings, phase=phase)

                with patch.object(StepChrono, "send_timing", capture):
                    placed = self._place(sender, spread_side=side, close_of=close_of)
                self.assertTrue(placed.completed)
                self.assertEqual(placed.status, "closed" if close_of else "open")
                rows = self._rows()
                timing = [row for row in rows if row["block"] == "send_timing"][-1]
                self.assertEqual(timing["intent_id"], INTENT)
                self.assertEqual(timing["signal_ts_ms"], SIGNAL_TS)
                self.assertEqual(timing["phase"], "close" if close_of else "open")
                self.assertEqual(timing["send_timing_monotonic_ns"], {
                    venue: {name: raw.timings[venue][name] for name in FIELDS}
                    for venue in ("bybit", "okx")
                })
                ws_exit = [r for r in rows if r["block"] == "ws_send" and r["edge"] == "exit"][-1]
                self.assertGreaterEqual(timing["mono_ns"], ws_exit["mono_ns"])
                text = json.dumps(timing)
                for secret in ("SECRET_FRAME", "RAW_ORDER_ID", "SECRET_KEY", "SECRET_CREDENTIAL", "SECRET_PASS", "req_id"):
                    self.assertNotIn(secret, text)

    def test_absent_sender_and_sender_without_timing_keep_existing_behavior(self):
        self.assertEqual(self._place(None).abort, "private_channel_down")
        placed = self._place(_Sender())
        self.assertTrue(placed.completed)
        self.assertFalse(any(r["block"] == "send_timing" for r in self._rows()))

    def test_real_dual_queue_markers_survive_place_and_serialize(self):
        def callback(item):
            # Inject the owner stamps without a websocket or exchange call.
            item.timing.owner_ws_send_started_ns = time.monotonic_ns()
            item.timing.owner_ws_send_returned_ns = time.monotonic_ns()

        sender = TrivialDualSender(send_fn=callback)
        self.addCleanup(sender.close)
        enqueue = sender.enqueue_dual
        captured = []

        def capture(**kwargs):
            result = enqueue(**kwargs)
            captured.append(result)
            return result

        with patch.object(sender, "enqueue_dual", capture):
            placed = self._place(sender)
        self.assertTrue(placed.completed)
        timing = next(r for r in self._rows() if r["block"] == "send_timing")
        self.assertEqual(timing["send_timing_monotonic_ns"], captured[0].timings)
        for venue in ("bybit", "okx"):
            markers = timing["send_timing_monotonic_ns"][venue]
            stamps = [markers[name] for name in FIELDS]
            self.assertTrue(all(type(value) is int for value in stamps))
            self.assertLessEqual(markers["queue_enqueued_ns"], markers["dequeued_ns"])
            self.assertLessEqual(markers["dequeued_ns"], markers["callback_started_ns"])
            self.assertLessEqual(markers["callback_started_ns"], markers["owner_ws_send_started_ns"])
            self.assertLessEqual(markers["owner_ws_send_started_ns"], markers["owner_ws_send_returned_ns"])
            self.assertLessEqual(markers["owner_ws_send_returned_ns"], markers["callback_returned_ns"])

    def test_returned_sender_error_does_not_change_existing_send_outcome(self):
        raw = _result()
        raw.error = "bybit:RuntimeError"
        raw.timings["bybit"]["owner_ws_send_returned_ns"] = None
        placed = self._place(_Sender(raw))
        self.assertTrue(placed.completed)
        timing = next(r for r in self._rows() if r["block"] == "send_timing")
        self.assertIsNone(timing["send_timing_monotonic_ns"]["bybit"]["owner_ws_send_returned_ns"])

    def test_raised_sender_failure_still_propagates_without_pending_trade(self):
        failure = RuntimeError("injected send failed")
        with self.assertRaises(RuntimeError) as caught:
            self._place(_Sender(error=failure))
        self.assertIs(caught.exception, failure)
        self.assertFalse(theta_trades_jsonl_path(self.root, "2023-11-14").exists())
        self.assertFalse(any(r["block"] == "send_timing" for r in self._rows()))

    def test_journal_failure_is_not_swallowed_after_send(self):
        sender = _Sender(_result())
        with patch("app.bot.private.step_chrono.os.fsync", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                self._place(sender)
        self.assertEqual(len(sender.calls), 1)


if __name__ == "__main__":
    unittest.main()
