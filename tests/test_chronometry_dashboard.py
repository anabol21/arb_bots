"""Chronometry dashboard: fixture tape, fill vs signal spread, OKX id."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.bot.journal import JournalWriter
from app.bot.private.chronometry import (
    ChronometryContext,
    build_chronometry_artifact,
    chronometry_enabled,
    persist_signal_book_on_place,
    spread_kind_for_side,
    write_chronometry_files,
)
from app.bot.private.chronometry_dashboard import render_dashboard_html
from app.bot.private.dual_leg_ack import AckOutcome
from app.bot.private.l1_tick_ring import (
    L1Tick,
    capture_signal_book,
    clear_process_rings,
    fill_spread_pct,
)
from app.bot.private.live_broker import LiveBroker
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.ws_messages import new_okx_ws_id, okx_ws_id_is_legal
from app.bot.private.ws_trivial_dual_leg import assert_signed_place_frame
from app.bot.stub_broker import InstrumentMeta
from tests.test_okx_ws_message_id import WIRE_OKX_OUTBOUND_ID


def _tick(wall_ms: int, venue: str, bid: float, ask: float) -> L1Tick:
    return L1Tick(
        wall_ms=wall_ms,
        mono_ns=wall_ms * 1_000_000,
        venue=venue,
        bid=bid,
        ask=ask,
        bid_size=20.0,
        ask_size=21.0,
        event_local_ts_ms=wall_ms,
        spread_long_pct=(bid - ask) / bid * 100.0 if venue == "bybit" else None,
    )


def _fixture_ticks() -> list[L1Tick]:
    ticks: list[L1Tick] = []
    # 5s before signal through fills. signal at 1_000_000.
    for i in range(20):
        wall = 995_000 + i * 500
        bybit_bid = 10.0 + i * 0.01
        okx_ask = 9.90 + i * 0.008
        ticks.append(
            L1Tick(
                wall_ms=wall,
                mono_ns=wall * 1_000_000,
                venue="bybit",
                bid=bybit_bid,
                ask=bybit_bid + 0.02,
                bid_size=30.0,
                ask_size=31.0,
                event_local_ts_ms=wall,
                spread_long_pct=(bybit_bid - okx_ask) / bybit_bid * 100.0,
                spread_short_pct=None,
            )
        )
        ticks.append(
            L1Tick(
                wall_ms=wall + 10,
                mono_ns=(wall + 10) * 1_000_000,
                venue="okx",
                bid=okx_ask - 0.02,
                ask=okx_ask,
                bid_size=40.0,
                ask_size=41.0,
                event_local_ts_ms=wall + 10,
                spread_long_pct=(bybit_bid - okx_ask) / bybit_bid * 100.0,
                spread_short_pct=None,
            )
        )
    return ticks


def _artifact(**overrides: object) -> dict:
    signal_ts = 1_000_000
    snap = capture_signal_book(
        {"bid_price": 9.94, "ask_price": 9.96, "bid_size": 10, "ask_size": 10},
        {"bid_price": 10.10, "ask_price": 10.12, "bid_size": 10, "ask_size": 10},
        event_local_ts_ms=signal_ts,
        wall_ms=signal_ts,
    )
    wire = [
        {
            "dir": "out",
            "venue": "bybit",
            "socket": "trade",
            "wall_ms": signal_ts + 2,
            "req_id": "babc123",
            "intent_id": "intent-1",
        },
        {
            "dir": "out",
            "venue": "okx",
            "socket": "trade",
            "wall_ms": signal_ts + 3,
            "req_id": new_okx_ws_id(prefix="o"),
            "intent_id": "intent-1",
        },
        {
            "dir": "in",
            "venue": "bybit",
            "socket": "trade",
            "wall_ms": signal_ts + 40,
            "req_id": "babc123",
            "op": "order.create",
        },
        {
            "dir": "in",
            "venue": "okx",
            "socket": "trade",
            "wall_ms": signal_ts + 45,
            "req_id": "okxack1",
            "op": "order",
        },
        {
            "dir": "in",
            "venue": "bybit",
            "socket": "private",
            "wall_ms": signal_ts + 80,
            "payload": {"data": [{"execPrice": "10.08", "execTime": str(signal_ts + 70)}]},
            "fill_delivery_ms": 10,
            "intent_id": "intent-1",
        },
        {
            "dir": "in",
            "venue": "okx",
            "socket": "private",
            "wall_ms": signal_ts + 90,
            "payload": {"data": [{"fillPx": "9.97", "fillTime": str(signal_ts + 75)}]},
            "fill_delivery_ms": 15,
            "intent_id": "intent-1",
        },
    ]
    ctx = ChronometryContext(
        intent_id="intent-1",
        base_coin="EDEN",
        spread_side="open_long",
        phase="open",
        signal_ts_ms=signal_ts,
        data_root=Path("/tmp/unused-chronometry"),
        signal_book=snap,
        wire_events=wire,
        ticks=_fixture_ticks(),
        lookback_ms=30_000,
        lookahead_ms=15_000,
    )
    art = build_chronometry_artifact(ctx)
    art.update(overrides)
    return art


class DashboardGeneratorTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_process_rings()

    def test_html_has_tapes_markers_and_latency(self) -> None:
        art = _artifact()
        html = render_dashboard_html(art)
        self.assertIn('data-series="sell_bid"', html)
        self.assertIn('data-series="buy_ask"', html)
        self.assertIn('data-series="spread"', html)
        self.assertIn("Sell Bybit bid", html)
        self.assertIn("Buy OKX ask", html)
        self.assertIn("signal→send", html)
        self.assertIn("send→ack", html)
        self.assertIn("signal→fill", html)
        self.assertIn("fill_delivery", html)
        self.assertIn("signal spread", html)
        self.assertIn("fill spread", html)
        self.assertIn("class=\"series\"", html)
        self.assertIn("marker-signal", html)
        self.assertIn("marker-ack", html)
        self.assertIn("marker-fill", html)
        self.assertGreater(art["tick_count"], 10)
        self.assertFalse(art["ticks_missing"])
        self.assertAlmostEqual(art["signal_spread_pct"], (10.10 - 9.96) / 10.10 * 100.0)
        self.assertAlmostEqual(
            art["fill_spread_pct"],
            fill_spread_pct(spread_kind="long", bybit_exec=10.08, okx_exec=9.97),
        )
        lat = art["latency_ms"]
        self.assertEqual(lat["signal_to_send"]["bybit"], 2)
        self.assertEqual(lat["send_to_ack"]["bybit"], 38)
        self.assertEqual(lat["signal_to_fill"]["okx"], 90)
        self.assertEqual(lat["fill_delivery"]["bybit"], 10)

    def test_empty_ring_does_not_invent_ticks(self) -> None:
        art = _artifact()
        art["ticks"] = []
        art["tick_count"] = 0
        art["ticks_missing"] = True
        html = render_dashboard_html(art)
        self.assertIn("No public L1 ticks", html)
        self.assertNotIn('points="', html)

    def test_write_files_under_reports_trades(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            art = _artifact()
            paths = write_chronometry_files(art, data_root=Path(td))
            self.assertTrue(paths["html"].is_file())
            self.assertTrue(paths["json"].is_file())
            self.assertEqual(
                paths["dir"],
                Path(td) / "reports" / "trades" / "intent-1",
            )
            loaded = json.loads(paths["json"].read_text(encoding="utf-8"))
            self.assertEqual(loaded["schema_version"], "bbot.canary.chronometry.v1")
            page = paths["html"].read_text(encoding="utf-8")
            self.assertIn("Contour B chronometry", page)

    def test_close_of_long_uses_short_formula(self) -> None:
        self.assertEqual(spread_kind_for_side("close", open_spread_side="open_long"), "short")
        self.assertEqual(spread_kind_for_side("open_long"), "long")


class FillVsSignalMathTests(unittest.TestCase):
    def test_fill_worse_than_signal_long(self) -> None:
        signal = (100.0 - 99.50) / 100.0 * 100.0
        fill = fill_spread_pct(spread_kind="long", bybit_exec=99.90, okx_exec=99.60)
        assert fill is not None
        self.assertAlmostEqual(signal, 0.5)
        self.assertLess(fill, signal)

    def test_enabled_only_for_canary_or_flag(self) -> None:
        self.assertTrue(chronometry_enabled({"BBOT_PROFILE": "canary_wal_eden"}))
        self.assertTrue(chronometry_enabled({"BBOT_CHRONOMETRY": "1"}))
        self.assertFalse(chronometry_enabled({"BBOT_PROFILE": "gear2_would_send"}))
        self.assertFalse(
            chronometry_enabled({"BBOT_PROFILE": "canary_wal_eden", "BBOT_CHRONOMETRY": "0"})
        )


class OkxIdRegressionTests(unittest.TestCase):
    def test_dashboard_okx_req_ids_stay_legal(self) -> None:
        art = _artifact()
        for marker in art["markers"]:
            if marker.get("venue") == "okx" and marker.get("req_id"):
                self.assertTrue(
                    okx_ws_id_is_legal(marker["req_id"]),
                    msg=marker["req_id"],
                )
        html = render_dashboard_html(art)
        self.assertNotIn(WIRE_OKX_OUTBOUND_ID, html)
        self.assertNotIn("req_c08a00f2", html)

    def test_historical_underscore_id_is_still_illegal(self) -> None:
        self.assertFalse(okx_ws_id_is_legal(WIRE_OKX_OUTBOUND_ID))
        self.assertTrue("_" in WIRE_OKX_OUTBOUND_ID)


class LiveBrokerChronometryHookTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_process_rings()

    def test_successful_dual_ack_writes_dashboard(self) -> None:
        def _ok(venue: str, req_id: str, _timeout: float) -> AckOutcome:
            return AckOutcome(
                venue=venue,
                req_id=req_id,
                accepted=True,
                timed_out=False,
                venue_code="0",
                recv_ns=time.monotonic_ns(),
                wall_ms=int(time.time() * 1000),
            )

        sent: list = []

        def _send(item) -> None:
            sent.append(item)
            assert_signed_place_frame(
                "bybit_live" if item.venue == "bybit" else "okx_live",
                item.text,
            )
            if item.venue == "okx":
                body = json.loads(item.text)
                self.assertTrue(okx_ws_id_is_legal(body["id"]))
                self.assertNotIn("_", body["id"])

        with tempfile.TemporaryDirectory() as td:
            env = {
                "VENUE": "live",
                "LIVE_ORDERS": "1",
                "BBOT_PROFILE": "canary_wal_eden",
                "BBOT_CHRONOMETRY_SYNC": "1",
                "BBOT_CHRONOMETRY_FILL_WAIT_SEC": "0",
            }
            broker = LiveBroker(
                data_root=Path(td),
                journal=JournalWriter(Path(td)),
                trade_lat_ms=100,
                notional_usdt=10.0,
                log=lambda _m: None,
                env=env,
                send_fn=_send,
                inst_id_codes={"WAL-USDT-SWAP": 1},
                bybit_credentials=LiveCredentials(api_key="k", api_secret="s", passphrase="p"),
                ack_wait_fn=_ok,
                ack_timeout_sec=0.4,
            )
            abort = broker.place(
                spread_side="open_long",
                base_coin="WAL",
                signal_ts_ms=1_700_000_000_000,
                okx_book={"bid_price": 0.40, "ask_price": 0.41, "bid_qty": 200, "ask_qty": 200},
                bybit_book={"bid_price": 0.42, "ask_price": 0.43, "bid_qty": 200, "ask_qty": 200},
                meta=InstrumentMeta(
                    base_coin="WAL",
                    okx_symbol="WAL-USDT-SWAP",
                    bybit_symbol="WALUSDT",
                    okx_lot_size=1.0,
                    okx_min_size=1.0,
                    bybit_qty_step=1.0,
                    bybit_min_order_qty=1.0,
                    bybit_min_notional_value=5.0,
                ),
            )
            self.assertIsNone(abort)
            self.assertEqual(broker.position, "open_long")
            extra = persist_signal_book_on_place(
                intent_id="already-stored",
                okx_book={"bid_price": 0.40, "ask_price": 0.41},
                bybit_book={"bid_price": 0.42, "ask_price": 0.43},
                extra={},
            )
            self.assertAlmostEqual(extra.spread_long_pct or 0.0, (0.42 - 0.41) / 0.42 * 100.0)
            reports = list((Path(td) / "reports" / "trades").glob("*/dashboard.html"))
            self.assertEqual(len(reports), 1)
            page = reports[0].read_text(encoding="utf-8")
            self.assertIn("WAL", page)
            self.assertIn("open_long", page)
            payload = json.loads(reports[0].with_name("chronometry.json").read_text())
            self.assertIsNotNone(payload["signal_book"]["bybit_bid"])
            self.assertTrue(payload["ticks_missing"])  # no ring in this test
            self.assertIn("not retained", " ".join(payload["notes"]))
            broker.close()

    def test_emit_is_after_ack_not_on_send(self) -> None:
        events: list[str] = []

        def _send(item) -> None:
            events.append(f"send:{item.venue}")

        def _wait(venue: str, req_id: str, timeout: float) -> AckOutcome:
            events.append(f"ack:{venue}")
            return AckOutcome(
                venue=venue,
                req_id=req_id,
                accepted=True,
                timed_out=False,
                recv_ns=time.monotonic_ns(),
                wall_ms=1,
            )

        with tempfile.TemporaryDirectory() as td, patch(
            "app.bot.private.live_broker.emit_after_dual_ack",
            side_effect=lambda **kw: events.append("chrono"),
        ):
            broker = LiveBroker(
                data_root=Path(td),
                journal=JournalWriter(Path(td)),
                trade_lat_ms=100,
                notional_usdt=10.0,
                log=lambda _m: None,
                env={
                    "VENUE": "live",
                    "LIVE_ORDERS": "1",
                    "BBOT_PROFILE": "canary_wal_eden",
                    "BBOT_CHRONOMETRY_SYNC": "1",
                    "BBOT_CHRONOMETRY_FILL_WAIT_SEC": "0",
                },
                send_fn=_send,
                inst_id_codes={"EDEN-USDT-SWAP": 1},
                bybit_credentials=LiveCredentials(api_key="k", api_secret="s", passphrase="p"),
                ack_wait_fn=_wait,
                ack_timeout_sec=0.4,
            )
            abort = broker.place(
                spread_side="open_long",
                base_coin="EDEN",
                signal_ts_ms=1,
                okx_book={"bid_price": 1.0, "ask_price": 1.01, "bid_qty": 200, "ask_qty": 200},
                bybit_book={"bid_price": 1.0, "ask_price": 1.01, "bid_qty": 200, "ask_qty": 200},
                meta=InstrumentMeta(
                    base_coin="EDEN",
                    okx_symbol="EDEN-USDT-SWAP",
                    bybit_symbol="EDENUSDT",
                    okx_lot_size=1.0,
                    okx_min_size=1.0,
                    bybit_qty_step=1.0,
                    bybit_min_order_qty=1.0,
                    bybit_min_notional_value=5.0,
                ),
            )
            self.assertIsNone(abort)
            self.assertEqual(events[-1], "chrono")
            self.assertLess(events.index("send:bybit"), events.index("ack:bybit"))
            self.assertLess(max(i for i, e in enumerate(events) if e.startswith("ack:")), events.index("chrono"))
            broker.close()


if __name__ == "__main__":
    unittest.main()
