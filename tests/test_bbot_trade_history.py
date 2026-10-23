from __future__ import annotations

import json
import asyncio
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.bot.private.order_sign import LiveCredentials
from app.bot.private.trade_history import TradeHistoryCollector, _okx_fill_fact
from app.bot.private.venue import endpoints_for_venue
from app.bot.private.ws_w4_baseline import BaselineError, _bybit_signed_get, _okx_signed_get
from app.bot.theta_trade_manager import ThetaTradeJournalWriter
from app.bot.theta_trade_manager import ThetaTradeConfig, ThetaTradeManager
from app.bot.theta_screener import ThetaSnapshot


class TradeHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.writer = ThetaTradeJournalWriter(self.root)
        self.creds = LiveCredentials("key", "secret", "pass")
        self.start = 1_700_000_000_000
        self.end = self.start + 5000
        self.calls: list[tuple[str, dict[str, str]]] = []

    def _getter(self, url: str, headers: dict[str, str], *, timeout_sec: float) -> dict:
        del timeout_sec
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        self.calls.append((url, headers))
        if parsed.path == "/v5/execution/list":
            page = query.get("cursor", [""])[0]
            rows = [
                # An unrelated row may appear before any of this trade's fills.
                {"execId": "unlinked-exec", "symbol": "BTCUSDT",
                 "execTime": str(self.start + 3), "execPrice": "98", "execQty": "2",
                 "execFee": "0", "feeCurrency": "USDT", "orderId": "unlinked-order"},
                # Continuation executions for a known order may omit orderLinkId.
                {"execId": "exec-1-no-link", "symbol": "BTCUSDT",
                 "execTime": str(self.start + 2), "execPrice": "100.30", "execQty": "0.02",
                 "execFee": "0.01", "feeCurrency": "USDT", "orderId": "bybit-order"},
                {"execId": "exec-1", "symbol": "BTCUSDT", "orderLinkId": "bintent",
                 "execTime": str(self.start + 1),
                 "execPrice": "100.25", "execQty": "0.01", "execFee": "0.02",
                 "feeCurrency": "USDT", "orderId": "bybit-order"},
                {"execId": "manual-exec", "symbol": "BTCUSDT", "orderLinkId": "bmanual",
                 "execTime": str(self.start + 2), "execPrice": "99", "execQty": "5",
                 "execFee": "0", "feeCurrency": "USDT", "orderId": "manual-order"},
            ]
            return {"retCode": 0, "result": {"list": rows,
                    "nextPageCursor": "page-2" if not page else ""}}
        if parsed.path == "/v5/account/transaction-log":
            return {"retCode": 0, "result": {"list": [
                {"id": "txn-1", "symbol": "BTCUSDT", "type": "TRADE",
                 "orderId": "bybit-order", "transactionTime": str(self.start + 2),
                 "fee": "0.03", "funding": "0", "currency": "USDT"},
                {"id": "txn-external", "symbol": "BTCUSDT", "type": "TRADE",
                 "orderId": "external-account-order", "transactionTime": str(self.start + 2),
                 "fee": "0.90", "funding": "0", "currency": "USDT"},
                {"id": "txn-funding", "symbol": "BTCUSDT", "type": "SETTLEMENT",
                 "transactionTime": str(self.start + 2), "fee": "0", "funding": "0.01",
                 "currency": "USDT"}
            ], "nextPageCursor": ""}}
        if parsed.path == "/api/v5/trade/fills-history":
            return {"code": "0", "data": [
                {"tradeId": "trade-1", "billId": "bill-fill-1",
                 "instId": "BTC-USDT-SWAP", "clOrdId": "ointent",
                 "fillTime": str(self.start + 3),
                 "ts": str(self.start + 30), "fillPx": "100.5", "fillSz": "1",
                 "fee": "-0.01", "feeCcy": "USDT", "ordId": "okx-order"},
                {"tradeId": "manual-trade", "billId": "bill-manual",
                 "instId": "BTC-USDT-SWAP", "clOrdId": "omanual",
                 "fillTime": str(self.start + 3), "ts": str(self.start + 30),
                 "fillPx": "90", "fillSz": "5", "fee": "-1",
                 "feeCcy": "USDT", "ordId": "manual-order"}
            ]}
        if parsed.path == "/api/v5/account/bills-archive":
            return {"code": "0", "data": [
                {"billId": "bill-1", "instId": "BTC-USDT-SWAP", "ts": str(self.start + 4),
                 "type": "8", "subType": "173", "balChg": "-0.04", "ccy": "USDT"}
            ]}
        raise AssertionError(f"unexpected history path: {parsed.path}")

    def _collector(self, venue: str = "live") -> TradeHistoryCollector:
        return TradeHistoryCollector(
            bybit_credentials=self.creds, okx_credentials=self.creds,
            endpoints=endpoints_for_venue(venue), journal=self.writer,
            http_get_json=self._getter, now_ms=lambda: self.start + 10,
        )

    def _path(self) -> Path:
        return self.root / "theta_trades/event_date=2023-11-14/trades.jsonl"

    def _rows(self) -> list[dict]:
        return [json.loads(line) for line in self._path().read_text().splitlines()]

    def test_collects_fixed_get_history_and_resumes_without_duplicate_facts(self) -> None:
        args = dict(trade_id="t-1", bybit_symbol="BTCUSDT", okx_instrument="BTC-USDT-SWAP",
                    bybit_order_link_id="bintent", okx_client_order_id="ointent",
                    intent_id="intent-1", trade_event="open",
                    base_coin="BTC", side="long",
                    start_ms=self.start, end_ms=self.end, signal_ts_ms=self.start)
        self._collector().collect(**args)
        first_calls = len(self.calls)
        self.assertGreater(first_calls, 4)  # Bybit execution cursor has two pages.
        self._collector().collect(**args)
        self.assertEqual(len(self.calls), first_calls)
        rows = self._rows()
        facts = [row for row in rows if row.get("record_type") == "trade_history_fact"]
        coverages = [row for row in rows if row.get("record_type") == "trade_history_coverage"]
        self.assertEqual(sum(row.get("source_id") == "exec-1" for row in facts), 1)
        self.assertEqual(sum(row.get("source_id") == "exec-1-no-link" for row in facts), 1)
        unlinked_target = next(row for row in facts if row.get("source_id") == "exec-1-no-link")
        self.assertEqual(unlinked_target["order_link_id"], "")
        self.assertEqual(unlinked_target["correlation_basis"], "known_order_id")
        self.assertFalse(any(row.get("source_id") == "txn-external" for row in facts))
        self.assertFalse(any(row.get("source_id") == "manual-exec" for row in facts))
        self.assertFalse(any(row.get("source_id") == "manual-trade" for row in facts))
        matched_bybit = next(row for row in facts if row.get("source_id") == "exec-1")
        self.assertEqual(matched_bybit["order_link_id"], "bintent")
        self.assertEqual(matched_bybit["order_id"], "bybit-order")
        bybit_fee = next(row for row in facts if row.get("fact_type") == "fee" and row["venue"] == "bybit")
        bybit_funding = next(row for row in facts if row.get("fact_type") == "funding")
        okx_fee = next(row for row in facts if row.get("venue") == "okx" and row.get("fact_type") == "execution")
        self.assertEqual(okx_fee["source_id"], "trade-1")
        self.assertEqual(okx_fee["bill_id"], "bill-fill-1")
        self.assertEqual(okx_fee["client_order_id"], "ointent")
        self.assertEqual(okx_fee["correlation_basis"], "client_order_id")
        okx_funding = next(row for row in facts if row.get("fact_type") == "funding" and row["venue"] == "okx")
        self.assertEqual(bybit_fee["signed_amount"], "-0.03")
        self.assertEqual(bybit_funding["signed_amount"], "0.01")
        self.assertEqual(bybit_funding["attribution_status"], "unassigned_account_level_funding")
        self.assertIsNone(bybit_funding["trade_id"])
        self.assertEqual(okx_fee["fee_signed_amount"], "-0.01")
        self.assertEqual(okx_fee["exchange_fill_time_ms"], self.start + 3)
        self.assertEqual(okx_fee["exchange_record_ts_ms"], self.start + 30)
        self.assertEqual(okx_funding["signed_amount"], "-0.04")
        self.assertEqual(okx_funding["attribution_status"], "unassigned_account_level_funding")
        self.assertIsNone(okx_funding["trade_id"])
        self.assertNotEqual(okx_fee["exchange_event_ts_ms"], okx_fee["first_observed_wall_ms"])
        self.assertTrue(all(row["status"] in {"complete", "page_saved"} for row in coverages))
        self.assertTrue(any(row["status"] == "complete" for row in coverages))
        transaction_coverages = [row for row in coverages
                                 if row["source_endpoint"] == "/v5/account/transaction-log"]
        self.assertEqual({row["history_kind"] for row in transaction_coverages},
                         {"TRADE", "SETTLEMENT"})
        self.assertTrue(all(row["status"] == "complete" for row in transaction_coverages))
        self.assertTrue(any(row.get("source_id") == "bill-1" for row in facts))
        self.assertTrue(any(row.get("venue") == "okx" for row in facts))
        self.assertTrue(all(urlparse(url).scheme == "https" for url, _ in self.calls))
        self.assertTrue(all(urlparse(url).path in {
            "/v5/execution/list", "/v5/account/transaction-log",
            "/api/v5/trade/fills-history", "/api/v5/account/bills-archive",
        } for url, _ in self.calls))
        self.assertTrue(any(h.get("X-BAPI-SIGN") for _, h in self.calls))
        self.assertTrue(any(h.get("OK-ACCESS-SIGN") for _, h in self.calls))
        transaction_queries = [parse_qs(urlparse(url).query) for url, _ in self.calls
                               if urlparse(url).path == "/v5/account/transaction-log"]
        self.assertTrue(transaction_queries)
        self.assertTrue(all(q.get("limit") == ["50"] for q in transaction_queries))
        self.assertTrue(all(row.get("trade_intent_id") == "intent-1"
                            and row.get("trade_event") == "open"
                            and row.get("event_signal_ts_ms") == self.start
                            for row in coverages))
        close_args = {**args, "intent_id": "intent-1-close", "trade_event": "close",
                      "event_signal_ts_ms": self.start + 1_000}
        self._collector().collect(**close_args)
        rows = self._rows()
        close_coverage = [row for row in rows if row.get("record_type") == "trade_history_coverage"
                          and row.get("trade_event") == "close"]
        self.assertTrue(close_coverage)
        self.assertTrue(all(row["trade_intent_id"] == "intent-1-close"
                            and row["event_signal_ts_ms"] == self.start + 1_000
                            and row["requested_start_ms"] == self.start
                            and row["requested_end_ms"] == self.end
                            and row["signal_ts_ms"] == self.start for row in close_coverage))
        self.assertTrue(all("intent-1-close|close|" in row["coverage_key"] for row in close_coverage))
        self.assertEqual(self.writer.trade_history_cursor(
            "t-1", "bybit", "/v5/execution/list", "execution", "BTCUSDT",
            self.start, self.end, "", self.start,
            trade_intent_id="intent-1", trade_event="open"), ("", True))
        self.assertEqual(self.writer.trade_history_cursor(
            "t-1", "bybit", "/v5/execution/list", "execution", "BTCUSDT",
            self.start, self.end, "", self.start,
            trade_intent_id="intent-1-close", trade_event="close"), ("", True))

    def test_unlinked_unrelated_execution_does_not_abort_history_collection(self) -> None:
        self._collector().collect(
            trade_id="t-2", bybit_symbol="BTCUSDT", okx_instrument="BTC-USDT-SWAP",
            bybit_order_link_id="bintent", okx_client_order_id="ointent",
            intent_id="intent-2-close", trade_event="close", base_coin="BTC",
            side="long", start_ms=self.start, end_ms=self.end,
            signal_ts_ms=self.start,
        )
        rows = self._rows()
        facts = [row for row in rows if row.get("record_type") == "trade_history_fact"]
        coverages = [row for row in rows if row.get("record_type") == "trade_history_coverage"]
        self.assertIn("exec-1", {row.get("source_id") for row in facts})
        self.assertNotIn("unlinked-exec", {row.get("source_id") for row in facts})
        self.assertTrue(all(row["status"] in {"complete", "page_saved"}
                            for row in coverages))

    def test_testnet_route_and_host_override_fail_closed(self) -> None:
        collector = self._collector("testnet")
        collector.collect(trade_id="t", bybit_symbol="BTCUSDT", okx_instrument="BTC-USDT-SWAP",
                          bybit_order_link_id="bintent", okx_client_order_id="ointent",
                          intent_id="intent-1", trade_event="open",
                          base_coin="BTC", side="long",
                          start_ms=self.start, end_ms=self.end, signal_ts_ms=self.start)
        bybit_urls = [url for url, _ in self.calls if url.startswith("https://api-testnet.bybit.com/")]
        self.assertTrue(all(url.startswith("https://api-testnet.bybit.com/") for url in bybit_urls))
        okx_headers = [headers for url, headers in self.calls if "/api/v5/" in url]
        self.assertTrue(all(h.get("x-simulated-trading") == "1" for h in okx_headers))
        bad = endpoints_for_venue("live")
        from dataclasses import replace
        with self.assertRaisesRegex(RuntimeError, "endpoint_route_mismatch"):
            TradeHistoryCollector(bybit_credentials=self.creds, okx_credentials=self.creds,
                                  endpoints=replace(bad, bybit_rest="https://evil.example"),
                                  journal=self.writer)

    def test_signed_helpers_reject_non_allowlisted_paths(self) -> None:
        with self.assertRaises(BaselineError):
            _bybit_signed_get(credentials=self.creds, base="https://api.bybit.com",
                              path="/v5/order/create", query="", http_get_json=self._getter)
        with self.assertRaises(BaselineError):
            _okx_signed_get(credentials=self.creds, base="https://www.okx.com",
                            path_with_query="/api/v5/trade/order?instId=BTC-USDT-SWAP",
                            http_get_json=self._getter)

    def test_page_append_is_concurrent_idempotent_and_cursor_survives_writer_reopen(self) -> None:
        fact = {"record_type": "trade_history_fact", "fact_type": "execution",
                "trade_id": "t-1", "venue": "bybit", "instrument": "BTCUSDT",
                "source_id": "exec-1", "dedupe_key": "bybit:BTCUSDT:exec-1:execution"}
        coverage = {"record_type": "trade_history_coverage", "status": "complete",
                    "coverage_key": (f"t-1|bybit|/v5/execution/list|execution|BTCUSDT|{self.start}|{self.end}|"
                                     f"{self.start}|{self.end}|||"),
                    "trade_id": "t-1", "venue": "bybit", "source_endpoint": "/v5/execution/list",
                    "history_kind": "execution", "instrument": "BTCUSDT", "start_ms": self.start,
                    "end_ms": self.end, "cursor_in": "", "cursor_out": "", "first_observed_wall_ms": self.start,
                    "requested_start_ms": self.start, "requested_end_ms": self.end,
                    "trade_intent_id": "", "trade_event": "", "event_signal_ts_ms": self.start}
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _n: self.writer.append_trade_history_page(
                [fact], coverage, signal_ts_ms=self.start), range(16)))
        self.writer = ThetaTradeJournalWriter(self.root)
        self.assertEqual(sum(r.get("dedupe_key") == fact["dedupe_key"] for r in self._rows()), 1)
        self.assertEqual(self.writer.trade_history_cursor(
            "t-1", "bybit", "/v5/execution/list", "execution", "BTCUSDT",
            self.start, self.end, "", self.start), ("", True))

    def _append_interval(self, trade_id: str, *, open_ms: int | None,
                         close_ms: int | None, open_wall_ms: int | None = None,
                         close_wall_ms: int | None = None) -> None:
        for event, intent, exchange_ms, wall_ms in (
            ("open", f"{trade_id}-open", open_ms, open_wall_ms),
            ("close", f"{trade_id}-close", close_ms, close_wall_ms),
        ):
            if exchange_ms is None:
                continue
            self.writer.append_rows([
                {"record_type": "trade_observation", "trade_id": trade_id,
                 "intent_id": intent, "event": event, "base_coin": "BTC",
                 "bybit_symbol": "BTCUSDT", "okx_instrument": "BTC-USDT-SWAP",
                 "side": "long", "signal_ts_ms": wall_ms or self.start},
                {"record_type": "trade_history_fact", "fact_type": "execution",
                 "trade_id": trade_id, "venue": "okx", "instrument": "BTC-USDT-SWAP",
                 "trade_intent_id": intent, "trade_event": event,
                 "source_id": f"{intent}-fill", "exchange_event_ts_ms": exchange_ms,
                 "exchange_fill_time_ms": exchange_ms,
                 "first_observed_wall_ms": wall_ms or self.start + 1,
                 "dedupe_key": f"okx:BTC-USDT-SWAP:{intent}-fill:execution"},
            ])

    def test_funding_unique_candidate_stays_unassigned_without_exclusive_account_proof(self) -> None:
        # Local receipt times bracket the funding time while exchange fills do
        # not: exchange timestamps and local wall clocks are separate domains.
        self._append_interval("one", open_ms=self.start + 100, close_ms=self.start + 200,
                              open_wall_ms=self.start + 1, close_wall_ms=self.start + 999)
        candidates = self.writer.funding_trade_candidates(
            base_coin="BTC", side="long", event_ms=self.start + 50,
            venue="okx", instrument="BTC-USDT-SWAP", trade_event="close")
        self.assertEqual(candidates["candidate_trade_ids"], [])
        attribution = self._collector()._funding_attribution(
            "one", "BTC", "long", self.start + 150, venue="okx",
            instrument="BTC-USDT-SWAP", trade_event="close")
        self.assertEqual(attribution["attribution_status"], "unassigned_account_level_funding")
        self.assertIsNone(attribution["trade_id"])
        self.assertEqual(attribution["attribution_candidates"], ["one"])

    def test_funding_candidate_exchange_boundaries_unknown_and_ambiguous(self) -> None:
        self._append_interval("one", open_ms=self.start + 10, close_ms=self.start + 90)
        self._append_interval("two", open_ms=self.start + 20, close_ms=None)
        candidates = self.writer.funding_trade_candidates(
            base_coin="BTC", side="long", event_ms=self.start + 50,
            venue="okx", instrument="BTC-USDT-SWAP", trade_event="close")
        self.assertEqual(candidates["candidate_trade_ids"], ["one", "two"])
        self.assertEqual(candidates["unknown_boundary_trade_ids"], [])
        equal = self.writer.funding_trade_candidates(
            base_coin="BTC", side="long", event_ms=self.start + 10,
            venue="okx", instrument="BTC-USDT-SWAP", trade_event="close")
        self.assertEqual(equal["candidate_trade_ids"], [])
        self.assertEqual(equal["equal_boundary_trade_ids"], ["one"])
        close_equal = self.writer.funding_trade_candidates(
            base_coin="BTC", side="long", event_ms=self.start + 90,
            venue="okx", instrument="BTC-USDT-SWAP", trade_event="close")
        self.assertNotIn("one", close_equal["candidate_trade_ids"])
        self.assertIn("one", close_equal["equal_boundary_trade_ids"])
        unknown_writer = ThetaTradeJournalWriter(Path(tempfile.mkdtemp()))
        unknown_writer.append_rows([{
            "record_type": "trade_observation", "trade_id": "missing-close",
            "intent_id": "unknown-open", "event": "open", "base_coin": "BTC",
            "okx_instrument": "BTC-USDT-SWAP", "side": "long",
        }, {
            "record_type": "trade_observation", "trade_id": "missing-close",
            "intent_id": "unknown-close", "event": "close", "base_coin": "BTC",
            "okx_instrument": "BTC-USDT-SWAP", "side": "long",
        }, {
            "record_type": "trade_history_fact", "fact_type": "execution",
            "trade_id": "missing-close", "venue": "okx", "instrument": "BTC-USDT-SWAP",
            "trade_intent_id": "unknown-open", "trade_event": "open",
            "exchange_event_ts_ms": self.start + 10,
            "exchange_fill_time_ms": self.start + 10,
        }])
        unknown = unknown_writer.funding_trade_candidates(
            base_coin="BTC", side="long", event_ms=self.start + 50,
            venue="okx", instrument="BTC-USDT-SWAP", trade_event="close")
        self.assertEqual(unknown["candidate_trade_ids"], [])
        self.assertEqual(unknown["unknown_boundary_trade_ids"], ["missing-close"])
        wrong_instrument = self.writer.funding_trade_candidates(
            base_coin="BTC", side="long", event_ms=self.start + 50,
            venue="okx", instrument="BTC-USD-SWAP", trade_event="close")
        self.assertEqual(wrong_instrument["candidate_trade_ids"], [])

    def test_okx_record_ts_without_fill_time_cannot_establish_funding_boundary(self) -> None:
        fact = _okx_fill_fact({
            "tradeId": "trade-ts-only", "billId": "bill-ts-only",
            "instId": "BTC-USDT-SWAP", "clOrdId": "okx-ts-only",
            "fillTime": "", "ts": str(self.start + 10),
            "fillPx": "100", "fillSz": "1", "fee": "-0.01", "feeCcy": "USDT",
            "ordId": "order-1",
        }, trade_id="ts-only", instrument="BTC-USDT-SWAP",
            trade_intent_id="intent-ts-only", trade_event="open", observed_ms=self.start + 999)
        self.assertIsNone(fact["exchange_event_ts_ms"])
        self.assertIsNone(fact["exchange_fill_time_ms"])
        self.assertEqual(fact["exchange_record_ts_ms"], self.start + 10)
        self.assertEqual(fact["client_order_id"], "okx-ts-only")
        self.assertEqual(fact["correlation_basis"], "client_order_id")
        self.assertEqual(fact["exchange_event_time_source"], "fillTime_missing_record_ts_only")
        self.writer.append_rows([{
            "record_type": "trade_observation", "trade_id": "ts-only",
            "intent_id": "intent-ts-only", "event": "open", "base_coin": "BTC",
            "okx_instrument": "BTC-USDT-SWAP", "side": "long",
        }, fact])
        candidates = self.writer.funding_trade_candidates(
            base_coin="BTC", side="long", event_ms=self.start + 20,
            venue="okx", instrument="BTC-USDT-SWAP", trade_event="close")
        self.assertEqual(candidates["candidate_trade_ids"], [])
        self.assertEqual(candidates["unknown_boundary_trade_ids"], ["ts-only"])

    def test_okx_fill_fact_requires_the_raw_client_order_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "okx_fill_identity_missing_or_mismatched"):
            _okx_fill_fact({
                "tradeId": "no-client-id", "billId": "bill-no-client-id",
                "instId": "BTC-USDT-SWAP", "ordId": "order-no-client-id",
                "fillTime": str(self.start), "fillPx": "100", "fillSz": "1",
            }, trade_id="t", instrument="BTC-USDT-SWAP", trade_intent_id="i",
                trade_event="open", observed_ms=self.start)

    def test_recovery_context_is_idempotent_and_restores_interval_after_restart(self) -> None:
        resumed_root = Path(tempfile.mkdtemp())
        recovered_writer = ThetaTradeJournalWriter(resumed_root)
        source_observation = {
            "record_type": "trade_observation", "trade_id": "open-1",
            "intent_id": "intent-1", "event": "open", "base_coin": "BTC",
            "bybit_symbol": "BTCUSDT", "okx_instrument": "BTC-USDT-SWAP",
            "side": "long", "entry_signal_ts_ms": self.start,
            "signal_ts_ms": self.start, "place_result_fill_wall_ms": self.start + 10,
            "bybit_order_link_id": "bintent", "okx_client_order_id": "ointent",
        }
        source = str(self.root / "source-run")
        recovered_writer.append_trade_history_recovery_context(
            source_observation, source_data_root=source
        )
        recovered_writer.append_trade_history_recovery_context(
            source_observation, source_data_root=source
        )
        recovered_writer = ThetaTradeJournalWriter(resumed_root)
        rows = [json.loads(line) for line in (
            resumed_root / "theta_trades/event_date=2023-11-14/trades.jsonl"
        ).read_text().splitlines()]
        contexts = [row for row in rows if row.get("record_type") == "trade_history_recovery_context"]
        self.assertEqual(len(contexts), 1)
        before_history = recovered_writer.funding_trade_candidates(
            base_coin="BTC", side="long", event_ms=self.start + 20,
            venue="okx", instrument="BTC-USDT-SWAP", trade_event="close")
        self.assertEqual(before_history["unknown_boundary_trade_ids"], ["open-1"])
        recovered_writer.append_rows([{
            "record_type": "trade_history_fact", "fact_type": "execution",
            "trade_id": "open-1", "venue": "okx", "instrument": "BTC-USDT-SWAP",
            "trade_intent_id": "intent-1", "trade_event": "open",
            "source_id": "open-fill", "exchange_event_ts_ms": self.start + 15,
            "exchange_fill_time_ms": self.start + 15,
            "first_observed_wall_ms": self.start + 500,
        }])
        self.assertEqual(recovered_writer.funding_trade_candidates(
            base_coin="BTC", side="long", event_ms=self.start + 20,
            venue="okx", instrument="BTC-USDT-SWAP", trade_event="close")[
                "candidate_trade_ids"], ["open-1"])
        recovered_writer.append_trade_observation({
            "record_type": "trade_observation", "trade_id": "open-1",
            "intent_id": "close-1", "event": "close", "base_coin": "BTC",
            "bybit_symbol": "BTCUSDT", "okx_instrument": "BTC-USDT-SWAP",
            "side": "long", "signal_ts_ms": self.start + 90,
            "place_result_fill_wall_ms": self.start + 100,
        })
        recovered_writer.append_rows([{
            "record_type": "trade_history_fact", "fact_type": "execution",
            "trade_id": "open-1", "venue": "okx", "instrument": "BTC-USDT-SWAP",
            "trade_intent_id": "close-1", "trade_event": "close",
            "source_id": "close-fill", "exchange_event_ts_ms": self.start + 90,
            "exchange_fill_time_ms": self.start + 90,
            "first_observed_wall_ms": self.start + 1,
        }])
        self.assertEqual(recovered_writer.funding_trade_candidates(
            base_coin="BTC", side="long", event_ms=self.start + 101,
            venue="okx", instrument="BTC-USDT-SWAP", trade_event="close")[
                "candidate_trade_ids"], [])

    def test_missing_source_id_records_incomplete_coverage_without_advancing(self) -> None:
        def malformed(url: str, headers: dict[str, str], *, timeout_sec: float) -> dict:
            del headers, timeout_sec
            if urlparse(url).path == "/v5/execution/list":
                return {"retCode": 0, "result": {"list": [{
                    "symbol": "BTCUSDT", "orderLinkId": "bintent",
                    "orderId": "bybit-order", "execTime": str(self.start),
                }]}}
            raise AssertionError("unexpected request")
        collector = TradeHistoryCollector(
            bybit_credentials=self.creds, okx_credentials=self.creds,
            endpoints=endpoints_for_venue("live"), journal=self.writer,
            http_get_json=malformed, now_ms=lambda: self.start + 10,
        )
        with self.assertRaisesRegex(ValueError, "identity_missing_or_mismatched"):
            collector.collect(trade_id="t", bybit_symbol="BTCUSDT", okx_instrument="BTC-USDT-SWAP",
                              bybit_order_link_id="bintent", okx_client_order_id="ointent",
                              intent_id="intent-1", trade_event="open",
                              base_coin="BTC", side="long",
                              start_ms=self.start, end_ms=self.end, signal_ts_ms=self.start)
        failed = [r for r in self._rows() if r.get("record_type") == "trade_history_coverage"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["status"], "incomplete")
        self.assertEqual(failed[0]["cursor_out"], "")

    def test_page_write_failure_leaves_cursor_unadvanced_and_records_incomplete(self) -> None:
        original = self.writer.append_trade_history_page
        def fail_successful_page(facts, coverage, *, signal_ts_ms):
            if coverage.get("status") in {"complete", "page_saved"}:
                raise OSError("disk unavailable")
            return original(facts, coverage, signal_ts_ms=signal_ts_ms)
        self.writer.append_trade_history_page = fail_successful_page
        with self.assertRaisesRegex(OSError, "disk unavailable"):
            self._collector().collect(
                trade_id="t-1", bybit_symbol="BTCUSDT", okx_instrument="BTC-USDT-SWAP",
                bybit_order_link_id="bintent", okx_client_order_id="ointent",
                intent_id="intent-1", trade_event="open",
                base_coin="BTC", side="long", start_ms=self.start,
                end_ms=self.end, signal_ts_ms=self.start,
            )
        rows = self._rows()
        incomplete = [r for r in rows if r.get("record_type") == "trade_history_coverage"
                      and r.get("status") == "incomplete"]
        self.assertTrue(incomplete)
        self.assertTrue(all(r.get("cursor_out") == "" for r in incomplete))
        self.assertEqual(self.writer.trade_history_cursor(
            "t-1", "bybit", "/v5/execution/list", "execution", "BTCUSDT",
            self.start, self.end, "", self.start), ("", False))

    def test_okx_after_cursor_and_duplicate_ids_across_pages(self) -> None:
        calls: list[str] = []
        def paged(url: str, headers: dict[str, str], *, timeout_sec: float) -> dict:
            del headers, timeout_sec
            parsed = urlparse(url)
            query = parse_qs(parsed.query)
            if parsed.path == "/v5/execution/list":
                return {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}
            if parsed.path == "/v5/account/transaction-log":
                return {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}
            if parsed.path == "/api/v5/trade/fills-history":
                cursor = query.get("after", [""])[0]
                calls.append(cursor)
                rows = ([{"tradeId": f"f{i}", "billId": f"b{i}",
                          "instId": "BTC-USDT-SWAP", "clOrdId": "ointent",
                          "fillTime": str(self.start + i), "fillPx": "1", "fillSz": "1",
                          "fee": "0", "feeCcy": "USDT"} for i in range(100)]
                        if not cursor else
                        [{"tradeId": "f99", "billId": "b99",
                          "instId": "BTC-USDT-SWAP", "clOrdId": "ointent",
                          "fillTime": str(self.start + 99), "fillPx": "1", "fillSz": "1",
                          "fee": "0", "feeCcy": "USDT"}])
                return {"code": "0", "data": rows}
            return {"code": "0", "data": []}
        collector = TradeHistoryCollector(
            bybit_credentials=self.creds, okx_credentials=self.creds,
            endpoints=endpoints_for_venue("live"), journal=self.writer,
            http_get_json=paged, now_ms=lambda: self.start + 200,
        )
        collector.collect(trade_id="t", bybit_symbol="BTCUSDT", okx_instrument="BTC-USDT-SWAP",
                          bybit_order_link_id="bintent", okx_client_order_id="ointent",
                          intent_id="intent-1", trade_event="open",
                          base_coin="BTC", side="long",
                          start_ms=self.start, end_ms=self.end, signal_ts_ms=self.start)
        self.assertEqual(calls, ["", "b99"])
        okx_fills = [r for r in self._rows() if r.get("record_type") == "trade_history_fact"
                     and r.get("venue") == "okx" and r.get("fact_type") == "execution"]
        self.assertEqual(len(okx_fills), 100)


class TradeHistoryIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_worker_starts_after_terminal_task_and_slot_release(self) -> None:
        root = Path(tempfile.mkdtemp())
        manager = None
        observed_state: list[tuple[bool, bool]] = []
        def history(_row: dict) -> None:
            observed_state.append((bool(manager.slot.pending), manager._terminal_place_task is not None))
            raise RuntimeError("read failed")
        books = {
            "BTC": {"okx": {"bid_price": 100.0, "ask_price": 101.0,
                             "bid_size": 500.0, "ask_size": 500.0, "ct_val": 0.01},
                    "bybit": {"bid_price": 102.0, "ask_price": 103.0,
                              "bid_size": 500.0, "ask_size": 500.0}}
        }
        snapshots = [
            ThetaSnapshot(base_coin="BTC", side="long", ts_ms=1_700_000_000_000,
                          p50_1m=0.8, floor_tf_select_a25=0.2, theta_1m=0.6,
                          computed_at_ms=1_700_000_000_001),
            ThetaSnapshot(base_coin="BTC", side="short", ts_ms=1_700_000_000_000,
                          p50_1m=0.1, floor_tf_select_a25=0.05, theta_1m=0.05,
                          computed_at_ms=1_700_000_000_001),
        ]
        context = {"trade_observation_enabled": True, "run_id": "r", "cycle_id": "c",
                   "timing": {"cycle_started": {"wall_ms": 1_700_000_000_000, "monotonic_ns": 1}}}
        manager = ThetaTradeManager(
            data_root=root, config=ThetaTradeConfig(notional_usdt=10.0),
            place_fn=lambda **_kwargs: type("Result", (), {
                "completed": True, "keep_pending": False, "fill_ts_ms": 1_700_000_000_010,
                "okx_filled_qty": "1", "bybit_filled_qty": "1", "coin_qty": "1",
            })(),
            meta_fn=lambda _coin: object(),
            decide_fn=lambda **_kwargs: type("Decision", (), {
                "action": "open", "base_coin": "BTC", "side": "long", "reason": "ok",
                "theta_1m": 0.6, "opposite_theta_1m": 0.05, "potential_pp": 0.4,
            })(),
            execution_mode="terminal_private", trade_history_fn=history,
        )
        await manager.on_theta_snapshots_async(snapshots, quotes=books, coin_order=("BTC",),
                                               now_ms=1_700_000_000_000, cycle_context=context)
        for _ in range(300):
            if manager._terminal_place_task is None and not manager._history_tasks:
                break
            await asyncio.sleep(0.002)
        self.assertEqual(observed_state, [(False, False)])
        self.assertIsNotNone(manager.slot.position)
        self.assertFalse(manager.slot.pending)
        self.assertIsNone(manager.execution_halt_reason)


if __name__ == "__main__":
    unittest.main()
