from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from app.bot.private.trade_history import _okx_fill_fact
from validation.summarize_trade_observation import read_jsonl, reconcile_records


ROOT = Path(__file__).resolve().parents[1]


def observation(event: str, *, side: str = "long", timestamp: int = 1_000) -> dict:
    is_open = event == "open"
    return {
        "record_type": "trade_observation", "schema_version": "bbot.trade_observation.v1",
        "trade_id": "t-1", "intent_id": f"i-{event}", "event": event,
        "bybit_symbol": "BTCUSDT", "okx_instrument": "BTC-USDT-SWAP",
        "bybit_order_link_id": f"bybit-{event}", "okx_client_order_id": f"okx-{event}",
        "side": side, "base_coin": "BTC", "signal_ts_ms": timestamp,
        "entry_signal_ts_ms": 1_000, "target_notional_usdt": "10",
        "theoretical_fee_round_trip_pp": "0.30",
        "signal_vector": {
            "okx": {"bid_price": "99" if is_open else "110",
                    "ask_price": "101" if is_open else "111", "ct_val": "0.01"},
            "bybit": {"bid_price": "102" if is_open else "109",
                      "ask_price": "103" if is_open else "110"},
        },
        "terminal_coin_qty": "0.1", "terminal_okx_filled_qty": "10",
        "terminal_bybit_filled_qty": "0.1",
        "timing": {
            "cycle_started": {"wall_ms": timestamp, "monotonic_ns": timestamp * 1_000_000},
            "vector_ready": {"wall_ms": timestamp + 10, "monotonic_ns": (timestamp + 10) * 1_000_000},
            "place_result_consumed": {"wall_ms": timestamp + 100, "monotonic_ns": (timestamp + 100) * 1_000_000},
        },
    }


def fact(venue: str, event: str, source: str, price: str, quantity: str, fee: str,
         *, unit: str = "contract", currency: str = "USDT", position_side: str = "long") -> dict:
    return {
        "record_type": "trade_history_fact", "fact_type": "execution", "trade_id": "t-1",
        "venue": venue, "instrument": "BTC-USDT-SWAP" if venue == "okx" else "BTCUSDT",
        "trade_intent_id": f"i-{event}", "trade_event": event,
        "order_link_id": f"bybit-{event}" if venue == "bybit" else None,
        "client_order_id": f"okx-{event}" if venue == "okx" else None,
        "source_id": source, "dedupe_key": source,
        "price": price, "quantity": quantity, "quantity_unit": unit,
        "fee_signed_amount": fee, "fee_currency": currency,
        "side": "buy" if (((event == "open") == (venue == "okx")) == (position_side == "long")) else "sell",
        "exchange_fill_time_ms": 1_000 if event == "open" else 2_000,
        "first_observed_wall_ms": 3_000,
    }


def coverage(venue: str, endpoint: str, kind: str, *, status: str = "complete",
             event: str = "open", timestamp: int = 1_000) -> dict:
    return {"record_type": "trade_history_coverage", "trade_id": "t-1", "venue": venue,
            "source_endpoint": endpoint, "history_kind": kind, "status": status,
            "instrument": "BTC-USDT-SWAP" if venue == "okx" else "BTCUSDT",
            "trade_intent_id": f"i-{event}", "trade_event": event,
            "event_signal_ts_ms": timestamp, "start_ms": timestamp - 100, "end_ms": timestamp + 100,
            "requested_start_ms": timestamp - 100, "requested_end_ms": timestamp + 100,
            "cursor_in": "", "cursor_out": ""}


def sample_records(side: str = "long") -> list[dict]:
    rows = [
        observation("open", side=side), observation("close", side=side, timestamp=2_000),
        fact("okx", "open", "o1", "101", "5", "-0.01", position_side=side),
        fact("okx", "open", "o2", "102", "5", "-0.01", position_side=side),
        fact("okx", "close", "c1", "110", "10", "-0.01", position_side=side),
        fact("bybit", "open", "bo1", "102", "0.05", "-0.01", position_side=side),
        fact("bybit", "open", "bo2", "103", "0.05", "-0.01", position_side=side),
        fact("bybit", "close", "bc1", "109", "0.1", "-0.01", position_side=side),
        coverage("bybit", "/v5/execution/list", "execution"),
        coverage("bybit", "/v5/account/transaction-log", "TRADE"),
        coverage("bybit", "/v5/account/transaction-log", "SETTLEMENT"),
        coverage("okx", "/api/v5/trade/fills-history", "fills"),
        coverage("okx", "/api/v5/account/bills-archive", "bills"),
    ]
    rows.extend({**row, "signal_ts_ms": 1_000, "trade_intent_id": "i-close", "trade_event": "close",
                 "event_signal_ts_ms": 2_000, "start_ms": 1_900, "end_ms": 2_100,
                 "requested_start_ms": 1_900, "requested_end_ms": 2_100}
                for row in list(rows) if row.get("record_type") == "trade_history_coverage")
    return rows


class TradeObservationReconciliationTests(unittest.TestCase):
    def test_integral_float_unix_ms_timestamps_match_jsonl_integer_coverage(self) -> None:
        integer = reconcile_records(sample_records())["trades"][0]
        records = sample_records()
        timestamp_fields = ("signal_ts_ms", "event_signal_ts_ms", "start_ms", "end_ms",
                            "requested_start_ms", "requested_end_ms")
        for row in records:
            for field in timestamp_fields:
                if isinstance(row.get(field), int):
                    row[field] = float(row[field])
        floating = reconcile_records(records)["trades"][0]
        self.assertEqual(floating["history_coverage"], integer["history_coverage"])

    def test_fractional_coverage_timestamp_stays_incomplete(self) -> None:
        records = sample_records()
        target = next(row for row in records if row.get("record_type") == "trade_history_coverage"
                      and row.get("trade_event") == "open")
        target["start_ms"] = target["start_ms"] + 0.5
        coverage_result = reconcile_records(records)["trades"][0]["history_coverage"]["bybit"]
        self.assertEqual(coverage_result["status"], "incomplete")
        self.assertTrue(any("coverage_range_invalid" in error
                            for error in coverage_result["failures"]))

    def test_invalid_unix_ms_values_and_float_page_chain_stay_incomplete(self) -> None:
        for invalid in (None, float("nan"), float("inf"), "not-a-time"):
            records = sample_records()
            target = next(row for row in records
                          if row.get("record_type") == "trade_history_coverage"
                          and row.get("trade_event") == "open")
            target["event_signal_ts_ms"] = invalid
            venue_coverage = reconcile_records(records)["trades"][0]["history_coverage"]["bybit"]
            self.assertEqual(venue_coverage["status"], "incomplete", repr(invalid))

        records = sample_records()
        for row in records:
            if row.get("record_type") == "trade_history_coverage":
                for field in ("event_signal_ts_ms", "start_ms", "end_ms",
                              "requested_start_ms", "requested_end_ms"):
                    row[field] = float(row[field])
        page = next(row for row in records if row.get("record_type") == "trade_history_coverage"
                    and row.get("venue") == "bybit" and row.get("trade_event") == "open"
                    and row.get("history_kind") == "execution")
        page["status"] = "page_saved"
        page["cursor_out"] = "next-page"
        venue_coverage = reconcile_records(records)["trades"][0]["history_coverage"]["bybit"]
        self.assertEqual(venue_coverage["status"], "incomplete")
        self.assertTrue(any("coverage_page_chain_incomplete" in failure
                            for failure in venue_coverage["failures"]))

    def test_producer_shaped_okx_fill_identity_reconciles_end_to_end(self) -> None:
        records = [row for row in sample_records()
                   if not (row.get("fact_type") == "execution" and row.get("venue") == "okx")]
        for event, client_id, px, side in (
            ("open", "okx-open", "101", "buy"),
            ("close", "okx-close", "110", "sell"),
        ):
            records.append(_okx_fill_fact(
                {"tradeId": f"{event}-trade", "billId": f"{event}-bill",
                 "instId": "BTC-USDT-SWAP", "clOrdId": client_id,
                 "ordId": f"{event}-order", "fillTime": "1500", "ts": "1500",
                 "fillPx": px, "fillSz": "10", "fee": "-0.01", "feeCcy": "USDT",
                 "side": side},
                trade_id="t-1", instrument="BTC-USDT-SWAP",
                trade_intent_id=f"i-{event}", trade_event=event, observed_ms=1600,
            ))
        result = reconcile_records(records)["trades"][0]
        self.assertEqual(result["venues"]["okx"]["actual_entry_vwap"], "101")
        self.assertNotIn("execution_order_identity_missing:okx:open", result["issues"])

    def test_legacy_okx_fact_requires_producer_provenance_and_order_id(self) -> None:
        records = sample_records()
        open_row = next(row for row in records if row.get("fact_type") == "execution"
                        and row.get("venue") == "okx" and row.get("trade_event") == "open")
        open_row.pop("client_order_id")
        open_row.update({"source_endpoint": "/api/v5/trade/fills-history",
                         "order_id": "known-order"})
        result = reconcile_records(records)["trades"][0]
        self.assertEqual(result["venues"]["okx"]["open_fill_count"], 2)
        open_row.pop("source_endpoint")
        rejected = reconcile_records(records)["trades"][0]
        self.assertIn("execution_order_identity_missing:okx:open", rejected["issues"])

    def test_known_bybit_order_id_basis_is_accepted_but_wrong_client_id_is_not(self) -> None:
        records = sample_records()
        bybit_open = next(row for row in records if row.get("fact_type") == "execution"
                          and row.get("venue") == "bybit" and row.get("trade_event") == "open")
        bybit_open["order_link_id"] = ""
        bybit_open["order_id"] = "producer-verified-order"
        bybit_open["correlation_basis"] = "known_order_id"
        bybit_open["source_endpoint"] = "/v5/execution/list"
        accepted = reconcile_records(records)["trades"][0]
        self.assertEqual(len(accepted["venues"]["bybit"]["open_fills"]), 2)
        self.assertNotIn("execution_order_identity_missing:bybit:open", accepted["issues"])
        bybit_open["order_link_id"] = "wrong-link"
        rejected = reconcile_records(records)["trades"][0]
        self.assertIn("execution_order_identity_mismatch:bybit:open", rejected["issues"])

    def test_submitted_quantity_is_primary_theory_and_partial_fill_is_quantity_delta(self) -> None:
        records = sample_records()
        for row in records:
            if row.get("record_type") == "trade_observation":
                row["planned_submitted_coin_qty_by_venue"] = {"okx": "0.1", "bybit": "0.1"}
        for row in records:
            if row.get("fact_type") == "execution":
                if row["trade_event"] == "open":
                    row["quantity"] = "4" if row["venue"] == "okx" else "0.04"
                else:
                    row["quantity"] = "8" if row["venue"] == "okx" else "0.08"
        for row in records:
            if row.get("record_type") == "trade_observation":
                row["terminal_okx_filled_qty"] = "8"
                row["terminal_bybit_filled_qty"] = "0.08"
                row["actual_terminal_filled_qty_by_venue"] = {
                    "okx": "8", "bybit": "0.08"}
        result = reconcile_records(records)["trades"][0]
        okx = result["venues"]["okx"]
        self.assertEqual(okx["theoretical_coin_quantity"], "0.1")
        self.assertEqual(okx["target_notional_coin_quantity"],
                         "0.09900990099009900990099009901")
        self.assertEqual(okx["quantity_effect_gross"], "-0.17")
        self.assertEqual(okx["planned_submitted_coin_quantity"], "0.1")

    def test_long_vwap_fee_math_and_deterministic_output(self) -> None:
        records = sample_records()
        first = reconcile_records(records)
        self.assertEqual(first, reconcile_records(records))
        trade = first["trades"][0]
        self.assertEqual(trade["status"], "incomplete")  # account-level funding is not allocated
        self.assertEqual(trade["venues"]["okx"]["reference_entry_price"], "101")
        self.assertEqual(trade["venues"]["bybit"]["reference_entry_price"], "102")
        self.assertEqual(trade["venues"]["okx"]["actual_entry_vwap"], "101.5")
        self.assertEqual(trade["venues"]["okx"]["gross_theory"], "0.8910891089108910891089108911")
        self.assertEqual(trade["venues"]["okx"]["gross_actual"], "0.85")
        self.assertEqual(trade["venues"]["okx"]["price_slippage_gross"], "-0.0495049504950495049504950495")
        self.assertEqual(trade["venues"]["okx"]["quantity_effect_gross"], "0.0084158415841584158415841584")
        self.assertEqual(trade["venues"]["okx"]["gross_decomposition_residual"], "0")
        self.assertIsNotNone(trade["venues"]["okx"]["pnl_theory"])
        self.assertIsNone(trade["venues"]["okx"]["pnl_actual"])
        self.assertEqual(trade["funding"]["status"], "unassigned")
        self.assertIn("cycle_started_to_vector_ready_ms", trade["timing"]["open"]["local_monotonic_spans"])
        self.assertIn("exchange_fill_time_ms", trade["timing"]["open"]["venue_fill_times"]["okx"])
        self.assertEqual(trade["gross_actual_total"], "0.2")

    def test_short_side_uses_opposite_signal_sides_and_signed_quantity(self) -> None:
        result = reconcile_records(sample_records("short"))["trades"][0]
        self.assertEqual(result["venues"]["okx"]["reference_entry_price"], "99")
        self.assertEqual(result["venues"]["bybit"]["reference_entry_price"], "103")
        self.assertEqual(result["venues"]["okx"]["signed_coin_quantity"], "-0.101010101010101010101010101")
        self.assertEqual(result["venues"]["bybit"]["signed_coin_quantity"], "0.09708737864077669902912621359")

    def test_duplicate_ids_deduplicate_and_conflicting_duplicates_are_visible(self) -> None:
        rows = sample_records()
        rows.extend([rows[2], rows[2]])
        result = reconcile_records(rows)["trades"][0]
        self.assertEqual(result["venues"]["okx"]["open_fill_count"], 2)
        self.assertEqual(result["duplicate_fact_count"], 2)
        rows.append({**rows[2], "price": "999"})
        conflicted = reconcile_records(rows)["trades"][0]
        self.assertIn("conflicting_duplicate_history_fact", conflicted["issues"])

    def test_missing_unit_currency_fee_rate_and_quantity_residual_are_not_zeroed(self) -> None:
        rows = sample_records()
        rows = [({**row, "quantity_unit": "unknown"} if row.get("venue") == "okx"
                 and row.get("fact_type") == "execution" else row) for row in rows]
        rows = [({**row, "fee_currency": "BTC", "fee_signed_amount": None}
                 if row.get("fact_type") == "execution" and row.get("venue") == "bybit" else row)
                for row in rows]
        result = reconcile_records(rows)["trades"][0]
        self.assertIsNone(result["venues"]["okx"]["gross_actual"])
        self.assertIsNone(result["venues"]["bybit"]["pnl_theory"])
        self.assertTrue(result["quantity_coverage"]["unexplained_residuals"])

    def test_funding_is_displayed_but_never_allocated_to_trade(self) -> None:
        rows = sample_records()
        rows.append({"record_type": "trade_history_fact", "fact_type": "funding", "trade_id": None,
                     "venue": "okx", "instrument": "BTC-USDT-SWAP", "currency": "USDT", "signed_amount": "0.4",
                     "source_id": "bill-funding", "attribution_candidates": ["t-1"]})
        result = reconcile_records(rows)["trades"][0]
        self.assertEqual(result["funding"]["unassigned_cashflow_by_venue"]["okx"]["USDT"], "0.4")
        self.assertIsNone(result["venues"]["okx"]["pnl_actual"])

    def test_price_and_quantity_effect_decompose_actual_minus_theory(self) -> None:
        rows = sample_records()
        # Keep the evidence internally complete while using an actual quantity unlike Q_theory.
        rows = [({**row, "quantity": ("6" if row.get("source_id") == "o1" else
                                      "5" if row.get("source_id") == "o2" else "11")
                  if row.get("venue") == "okx" else "0.11"}
                 if row.get("fact_type") == "execution" else row) for row in rows]
        rows = [({**row, "terminal_okx_filled_qty": "11", "terminal_coin_qty": "0.11"}
                 if row.get("record_type") == "trade_observation" else row) for row in rows]
        result = reconcile_records(rows)["trades"][0]["venues"]["okx"]
        self.assertEqual(Decimal(result["gross_actual"]) - Decimal(result["gross_theory"]),
                         Decimal(result["price_slippage_gross"]) + Decimal(result["quantity_effect_gross"]))
        self.assertEqual(Decimal(result["price_slippage_gross"]),
                         Decimal(result["price_slippage_gross_open_contribution"])
                         + Decimal(result["price_slippage_gross_close_contribution"]))

    def test_execution_identity_instrument_and_expected_order_mismatches_excluded(self) -> None:
        for field, wrong in (("trade_intent_id", "other-intent"), ("instrument", "ETH-USDT-SWAP"),
                             ("client_order_id", "other-client")):
            with self.subTest(field=field):
                rows = sample_records()
                index = next(i for i, row in enumerate(rows) if row.get("venue") == "okx"
                             and row.get("fact_type") == "execution" and row.get("trade_event") == "open")
                rows[index] = {**rows[index], field: wrong}
                result = reconcile_records(rows)["trades"][0]
                self.assertIsNone(result["venues"]["okx"]["gross_actual"])
                self.assertTrue(any("execution_" in issue and ("identity" in issue or "instrument" in issue
                                                                  or "order" in issue)
                                    for issue in result["issues"]))

    def test_funding_overlap_is_deduplicated_and_conflict_stays_visible(self) -> None:
        rows = sample_records()
        base = {"record_type": "trade_history_fact", "fact_type": "funding", "trade_id": None,
                "venue": "okx", "instrument": "BTC-USDT-SWAP", "currency": "USDT",
                "signed_amount": "0.4", "source_id": "bill-1", "attribution_candidates": ["t-1"]}
        rows.extend([base, {**base, "signed_amount": "0.4"},
                     {**base, "fact_type": "fee", "signed_amount": "-0.1"}])
        result = reconcile_records(rows)["trades"][0]
        self.assertEqual(result["funding"]["unassigned_cashflow_by_venue"]["okx"]["USDT"], "0.4")
        self.assertIn("funding_duplicate_fact", result["issues"])
        rows.append({**base, "signed_amount": "0.5"})
        conflicted = reconcile_records(rows)["trades"][0]
        self.assertIn("conflicting_duplicate_funding_fact", conflicted["issues"])

    def test_duplicate_lifecycle_rows_count_exact_duplicates_and_flag_conflicts(self) -> None:
        rows = sample_records()
        opened = next(row for row in rows if row.get("record_type") == "trade_observation"
                      and row.get("event") == "open")
        rows.append(dict(opened))
        changed_vector = {**opened, "signal_vector": {
            **opened["signal_vector"],
            "bybit": {**opened["signal_vector"]["bybit"], "bid_price": "90"},
        }}
        rows.extend([changed_vector, {**changed_vector, "intent_id": "i-open-retry"}])
        trade = reconcile_records(rows)["trades"][0]
        self.assertEqual(trade["duplicate_observation_count"], 1)
        self.assertIn("conflicting_duplicate_trade_observation", trade["issues"])
        self.assertIn("multiple_trade_observation_intents", trade["issues"])
        self.assertEqual(trade["status"], "incomplete")
        reversed_trade = reconcile_records(list(reversed(rows)))["trades"][0]
        self.assertEqual(trade["venues"]["bybit"]["reference_entry_price"],
                         reversed_trade["venues"]["bybit"]["reference_entry_price"])

    def test_unrelated_funding_duplicates_do_not_contaminate_other_trade(self) -> None:
        rows = sample_records()
        rows.extend([
            {"record_type": "trade_history_fact", "fact_type": "funding", "venue": "okx",
             "instrument": "ETH-USDT-SWAP", "currency": "USDT", "signed_amount": "0.4",
             "source_id": "eth-bill", "attribution_candidates": ["other-trade"]},
            {"record_type": "trade_history_fact", "fact_type": "funding", "venue": "okx",
             "instrument": "ETH-USDT-SWAP", "currency": "USDT", "signed_amount": "0.5",
             "source_id": "eth-bill", "attribution_candidates": ["other-trade"]},
            {"record_type": "trade_history_fact", "fact_type": "funding", "venue": "okx",
             "instrument": "BTC-USDT-SWAP", "currency": "USDT", "signed_amount": "0.4",
             "source_id": "other-btc-bill", "attribution_candidates": ["other-trade"]},
            {"record_type": "trade_history_fact", "fact_type": "funding", "venue": "okx",
             "instrument": "BTC-USDT-SWAP", "currency": "USDT", "signed_amount": "0.5",
             "source_id": "other-btc-bill", "attribution_candidates": ["other-trade"]},
        ])
        trade = reconcile_records(rows)["trades"][0]
        self.assertNotIn("funding_duplicate_fact", trade["issues"])
        self.assertNotIn("conflicting_duplicate_funding_fact", trade["issues"])
        self.assertEqual(trade["duplicate_funding_fact_count"], 0)
        self.assertEqual(trade["funding"]["unassigned_cashflow_by_venue"]["okx"], {})

    def test_open_only_trade_still_reports_matching_unassigned_funding(self) -> None:
        rows = [row for row in sample_records()
                if not (row.get("record_type") == "trade_observation" and row.get("event") == "close")]
        rows.append({"record_type": "trade_history_fact", "fact_type": "funding", "trade_id": None,
                     "venue": "okx", "instrument": "BTC-USDT-SWAP", "currency": "USDT",
                     "signed_amount": "0.17", "source_id": "bill-open-only",
                     "attribution_candidates": ["t-1"]})
        trade = reconcile_records(rows)["trades"][0]
        self.assertEqual(trade["status"], "incomplete")
        self.assertIsNone(trade["pnl_actual_total"])
        self.assertEqual(trade["funding"]["unassigned_cashflow_by_venue"]["okx"]["USDT"], "0.17")
        self.assertEqual(trade["funding"]["facts"]["okx"][0]["source_id"], "bill-open-only")

    def test_coverage_requires_expected_instrument_event_range_and_page_chain(self) -> None:
        rows = sample_records()
        rows = [({**row, "signal_ts_ms": 1_000, "start_ms": 900, "end_ms": 1_100}
                 if row.get("record_type") == "trade_history_coverage" else row) for row in rows]
        # A close observation must be covered by its own terminal event collection.
        rows = [row for row in rows if row.get("record_type") != "trade_history_coverage"]
        rows.extend(coverage(venue, endpoint, kind) for venue in ("bybit", "okx")
                    for endpoint, kind in (("/v5/execution/list", "execution"),
                                          ("/v5/account/transaction-log", "TRADE"),
                                          ("/v5/account/transaction-log", "SETTLEMENT"))
                    if venue == "bybit")
        rows.extend(coverage("okx", endpoint, kind) for endpoint, kind in (
            ("/api/v5/trade/fills-history", "fills"), ("/api/v5/account/bills-archive", "bills")))
        rows = [({**row, "signal_ts_ms": 2_000} if row.get("record_type") == "trade_observation"
                 and row.get("event") == "close" else row) for row in rows]
        result = reconcile_records(rows)["trades"][0]
        self.assertIn("history_coverage_incomplete", result["issues"])

    def test_open_entry_partition_tag_cannot_substitute_for_close_event_coverage(self) -> None:
        rows = sample_records()
        rows = [row for row in rows if not (row.get("record_type") == "trade_history_coverage"
                                             and row.get("trade_event") == "close")]
        result = reconcile_records(rows)["trades"][0]
        self.assertEqual(result["history_coverage"]["bybit"]["by_event"]["open"]["status"],
                         "complete")
        self.assertEqual(result["history_coverage"]["bybit"]["by_event"]["close"]["status"],
                         "incomplete")
        self.assertIn("history_coverage_incomplete", result["issues"])

    def test_recovery_coverage_accepts_monotonic_requested_end_extension(self) -> None:
        rows = sample_records()
        rows.append({**coverage("bybit", "/v5/execution/list", "execution",
                                event="close", timestamp=2_000),
                     "end_ms": 2_200, "requested_end_ms": 2_200})
        result = reconcile_records(rows)["trades"][0]
        self.assertEqual(result["history_coverage"]["bybit"]["by_event"]["close"]["status"],
                         "complete")
        conflicting = {**rows[-1], "cursor_out": "unexpected-next"}
        rows.append(conflicting)
        conflicted = reconcile_records(rows)["trades"][0]
        self.assertEqual(conflicted["history_coverage"]["bybit"]["by_event"]["close"]["status"],
                         "incomplete")
        self.assertIn("history_coverage:close:coverage_conflicting_duplicate_page",
                      conflicted["issues"])

    def test_coverage_accepts_complete_multi_window_cursor_chains_per_event(self) -> None:
        rows = sample_records()
        execution_endpoint = ("/v5/execution/list", "execution")
        for event, timestamp, low in (("open", 1_000, 900), ("close", 2_000, 1_900)):
            rows = [row for row in rows if not (row.get("record_type") == "trade_history_coverage"
                                                 and row.get("venue") == "bybit"
                                                 and (row.get("source_endpoint"), row.get("history_kind"))
                                                 == execution_endpoint
                                                 and row.get("trade_event") == event)]
            rows.extend([
                {**coverage("bybit", *execution_endpoint, event=event, timestamp=timestamp),
                 "start_ms": low, "end_ms": low + 49, "cursor_in": "", "cursor_out": "page-2",
                 "requested_start_ms": low, "requested_end_ms": low + 100, "status": "page_saved"},
                {**coverage("bybit", *execution_endpoint, event=event, timestamp=timestamp),
                 "start_ms": low, "end_ms": low + 49, "cursor_in": "page-2", "cursor_out": "",
                 "requested_start_ms": low, "requested_end_ms": low + 100, "status": "complete"},
                {**coverage("bybit", *execution_endpoint, event=event, timestamp=timestamp),
                 "start_ms": low + 50, "end_ms": low + 100, "cursor_in": "", "cursor_out": "",
                 "requested_start_ms": low, "requested_end_ms": low + 100, "status": "complete"},
            ])
        result = reconcile_records(rows)["trades"][0]
        self.assertEqual(result["history_coverage"]["bybit"]["by_event"]["open"]["status"], "complete")
        self.assertEqual(result["history_coverage"]["bybit"]["by_event"]["close"]["status"], "complete")
        conflicting = next(row for row in rows if row.get("record_type") == "trade_history_coverage"
                           and row.get("venue") == "bybit" and row.get("source_endpoint") == execution_endpoint[0]
                           and row.get("trade_event") == "open" and row.get("cursor_in") == "")
        rows.append({**conflicting, "cursor_out": "other-page"})
        conflicted = reconcile_records(rows)["trades"][0]
        self.assertIn("history_coverage:open:coverage_conflicting_duplicate_page", conflicted["issues"])
        # A missing second page is a visible crash gap even when the first page was fsynced.
        rows = [row for row in rows if not (row.get("record_type") == "trade_history_coverage"
                                             and row.get("venue") == "bybit"
                                             and (row.get("source_endpoint"), row.get("history_kind"))
                                             == execution_endpoint
                                             and row.get("trade_event") == "close"
                                             and row.get("cursor_in") == "page-2")]
        result = reconcile_records(rows)["trades"][0]
        self.assertEqual(result["history_coverage"]["bybit"]["by_event"]["close"]["status"], "incomplete")
        # A wholly absent requested middle window is visible from each row's
        # original request bounds, even when every surviving page is complete.
        rows = [row for row in rows if not (row.get("record_type") == "trade_history_coverage"
                                             and row.get("venue") == "bybit"
                                             and (row.get("source_endpoint"), row.get("history_kind"))
                                             == execution_endpoint
                                             and row.get("trade_event") == "close")]
        rows.extend([
            {**coverage("bybit", *execution_endpoint, event="close", timestamp=2_000),
             "start_ms": 1_900, "end_ms": 1_949, "requested_start_ms": 1_900,
             "requested_end_ms": 2_000, "cursor_in": "", "cursor_out": "", "status": "complete"},
            {**coverage("bybit", *execution_endpoint, event="close", timestamp=2_000),
             "start_ms": 1_951, "end_ms": 2_000, "requested_start_ms": 1_900,
             "requested_end_ms": 2_000, "cursor_in": "", "cursor_out": "", "status": "complete"},
        ])
        result = reconcile_records(rows)["trades"][0]
        self.assertIn("history_coverage:close:coverage_range_gap", result["issues"])
        rows = [({**row, "signal_ts_ms": 2_000, "start_ms": 1_900, "end_ms": 2_100}
                 if row.get("record_type") == "trade_history_coverage" else row) for row in rows]
        rows = [({**row, "instrument": "ETHUSDT"} if row.get("record_type") == "trade_history_coverage"
                 and row.get("venue") == "bybit" else row) for row in rows]
        result = reconcile_records(rows)["trades"][0]
        self.assertIn("history_coverage_incomplete", result["issues"])
        # A saved first page without a terminating complete page is a crash gap.
        rows = sample_records()
        rows = [({**row, "start_ms": 1_900, "end_ms": 2_100, "signal_ts_ms": 2_000,
                  "cursor_in": "", "cursor_out": "next", "status": "page_saved"}
                 if row.get("record_type") == "trade_history_coverage" else row) for row in rows]
        result = reconcile_records(rows)["trades"][0]
        self.assertIn("history_coverage_incomplete", result["issues"])

    def test_zero_fee_does_not_invent_a_theoretical_rate_and_terminal_qty_can_prove_ct_val(self) -> None:
        rows = sample_records()
        rows = [({**row, "signal_vector": {**row["signal_vector"],
                                             "okx": {k: v for k, v in row["signal_vector"]["okx"].items()
                                                     if k != "ct_val"}}}
                 if row.get("record_type") == "trade_observation" else row) for row in rows]
        rows = [({**row, "fee_signed_amount": "0"}
                 if row.get("venue") == "okx" and row.get("fact_type") == "execution" else row)
                for row in rows]
        result = reconcile_records(rows)["trades"][0]
        self.assertEqual(result["venues"]["okx"]["gross_actual"], "0.85")
        self.assertIsNone(result["venues"]["okx"]["pnl_theory"])
        self.assertIn("theoretical_fee_rate_unavailable:okx", result["issues"])

    def test_cli_reads_jsonl_and_emits_repeatable_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "trades.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in sample_records()) + "\n")
            cmd = [sys.executable, str(ROOT / "validation/summarize_trade_observation.py"), str(path)]
            first = subprocess.check_output(cmd, text=True)
            second = subprocess.check_output(cmd, text=True)
            self.assertEqual(first, second)
            self.assertEqual(json.loads(first)["trades"][0]["trade_id"], "t-1")

    def test_malformed_and_unterminated_rows_are_visible_and_mark_trade_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "trades.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in sample_records())
                            + "\n{bad json}\n{\"record_type\":\"trade_observation\"")
            records, input_issues = read_jsonl(path)
            self.assertEqual([item["error"] for item in input_issues],
                             ["malformed_jsonl", "incomplete_jsonl_line"])
            result = reconcile_records(records, input_issues)
            self.assertEqual(result["trades"][0]["status"], "incomplete")
            self.assertIn("jsonl_input_incomplete", result["trades"][0]["issues"])


if __name__ == "__main__":
    unittest.main()
