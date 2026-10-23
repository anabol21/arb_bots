#!/usr/bin/env python3
"""Deterministic offline reconciliation of Gear2 trade-observation JSONL."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

ZERO = Decimal(0)
ONE = Decimal(1)
VENUES = ("bybit", "okx")
REQUIRED_COVERAGE = {
    "bybit": {("/v5/execution/list", "execution"),
              ("/v5/account/transaction-log", "TRADE"),
              ("/v5/account/transaction-log", "SETTLEMENT")},
    "okx": {("/api/v5/trade/fills-history", "fills"),
            ("/api/v5/account/bills-archive", "bills")},
}


def _d(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid_decimal:{field}") from exc
    if not result.is_finite():
        raise ValueError(f"nonfinite_decimal:{field}")
    return result


_COVERAGE_TIME_FIELDS = (
    "event_signal_ts_ms", "start_ms", "end_ms", "requested_start_ms", "requested_end_ms",
)


def _unix_ms(value: Any, field: str = "timestamp") -> int:
    """Normalize only Unix-millisecond fields; reject lossy/coerced values."""
    if value is None or isinstance(value, bool):
        raise ValueError(f"invalid_unix_ms:{field}")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid_unix_ms:{field}") from exc
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError(f"invalid_unix_ms:{field}")
    return int(number)


def _normalize_coverage_row(row: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    invalid: list[str] = []
    for field in _COVERAGE_TIME_FIELDS:
        if field not in normalized or normalized[field] is None:
            continue
        try:
            normalized[field] = _unix_ms(normalized[field], field)
        except ValueError:
            normalized[field] = None
            invalid.append(field)
    if invalid:
        normalized["_invalid_coverage_time_fields"] = tuple(invalid)
    return normalized


def _s(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == ZERO:
        return "0"
    return format(value.normalize(), "f")


def _optional_decimal(row: Mapping[str, Any], field: str, venue: str) -> Decimal | None:
    values = row.get(field)
    value = values.get(venue) if isinstance(values, Mapping) else None
    if value in (None, ""):
        return None
    try:
        return _d(value, f"{field}.{venue}")
    except ValueError:
        return None


def _side_sign(position_side: str, venue: str) -> Decimal:
    long = position_side == "long"
    return ONE if (long == (venue == "okx")) else -ONE


def _leg_side(position_side: str, event: str, venue: str) -> str:
    sign = _side_sign(position_side, venue)
    if event == "close":
        sign = -sign
    return "buy" if sign > 0 else "sell"


def _reference_price(observation: Mapping[str, Any], venue: str, event: str) -> Decimal:
    side = str(observation.get("side") or "").lower()
    if side not in {"long", "short"}:
        raise ValueError("position_side_missing_or_invalid")
    book = (observation.get("signal_vector") or {}).get(venue)
    if not isinstance(book, Mapping):
        raise ValueError(f"signal_vector_missing:{venue}")
    price_key = "ask_price" if _leg_side(side, event, venue) == "buy" else "bid_price"
    price = _d(book.get(price_key), f"{venue}.{price_key}")
    if price <= ZERO:
        raise ValueError(f"signal_price_not_positive:{venue}:{event}")
    return price


def _observation_map(records: Iterable[Mapping[str, Any]]) -> tuple[
        dict[str, dict[str, Mapping[str, Any]]], dict[str, list[str]], dict[str, int]]:
    grouped: dict[str, dict[str, list[Mapping[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in records:
        if row.get("record_type") != "trade_observation":
            continue
        trade_id, event = str(row.get("trade_id") or ""), str(row.get("event") or "").lower()
        if trade_id and event in {"open", "close"}:
            grouped[trade_id][event].append(row)
    trades: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    issues_by_trade: dict[str, list[str]] = defaultdict(list)
    duplicate_counts: dict[str, int] = defaultdict(int)
    for trade_id, events in grouped.items():
        for event, rows in events.items():
            exact: dict[str, Mapping[str, Any]] = {}
            for row in rows:
                signature = json.dumps(dict(row), sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=False, default=str)
                if signature in exact:
                    duplicate_counts[trade_id] += 1
                else:
                    exact[signature] = row
            unique_rows = list(exact.values())
            if len(unique_rows) > 1:
                issues_by_trade[trade_id].append("conflicting_duplicate_trade_observation")
            if len({str(row.get("intent_id") or "") for row in unique_rows}) > 1:
                issues_by_trade[trade_id].append("multiple_trade_observation_intents")
            selected = max(unique_rows, key=lambda row: (
                int(row.get("signal_ts_ms") or 0), str(row.get("intent_id") or ""),
                json.dumps(dict(row), sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, default=str),
            ))
            trades[trade_id][event] = selected
    return trades, issues_by_trade, duplicate_counts


def _dedupe_facts(records: Iterable[Mapping[str, Any]], trade_id: str) -> tuple[list[Mapping[str, Any]], list[str], int]:
    kept: dict[str, Mapping[str, Any]] = {}
    issues: list[str] = []
    duplicates = 0
    for row in records:
        if row.get("record_type") != "trade_history_fact" or str(row.get("trade_id") or "") != trade_id:
            continue
        if not row.get("dedupe_key"):
            issues.append("history_fact_missing_dedupe_key")
            continue
        key = str(row["dedupe_key"])
        previous = kept.get(key)
        if previous is None:
            kept[key] = row
        else:
            duplicates += 1
            comparable_previous = {k: v for k, v in previous.items() if k != "first_observed_wall_ms"}
            comparable_row = {k: v for k, v in row.items() if k != "first_observed_wall_ms"}
            if comparable_previous != comparable_row:
                issues.append("conflicting_duplicate_history_fact")
    return [kept[key] for key in sorted(kept)], issues, duplicates


def _coverage(records: Iterable[Mapping[str, Any]], trade_id: str,
              lifecycle: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    rows = [_normalize_coverage_row(row) for row in records
            if row.get("record_type") == "trade_history_coverage"
            and str(row.get("trade_id") or "") == trade_id]
    result: dict[str, Any] = {}
    lifecycle_times = []
    lifecycle_time_invalid = set()
    for event, row in lifecycle.items():
        try:
            lifecycle_times.append(_unix_ms(row.get("signal_ts_ms"), "signal_ts_ms"))
        except ValueError:
            lifecycle_times.append(None)
            lifecycle_time_invalid.add(event)
    ambiguous_event_time = len(lifecycle_times) != len(set(lifecycle_times))
    for venue in VENUES:
        def event_instrument(observation: Mapping[str, Any]) -> Any:
            return observation.get("bybit_symbol") if venue == "bybit" else observation.get("okx_instrument")

        instruments = {str(event_instrument(item) or "") for item in lifecycle.values()}
        instrument_mismatch = len(instruments) > 1 or "" in instruments
        expected_instrument = event_instrument(lifecycle.get("open") or next(iter(lifecycle.values()), {}))
        failures: set[str] = set()
        missing: set[str] = set()
        event_coverage: dict[str, dict[str, Any]] = {}
        for event, observation in lifecycle.items():
            try:
                event_ts = _unix_ms(observation.get("signal_ts_ms"), "signal_ts_ms")
            except ValueError:
                event_ts = -1
            event_result: dict[str, Any] = {"status": "complete", "missing": [], "failures": []}
            event_expected_instrument = event_instrument(observation)
            if event in lifecycle_time_invalid:
                event_result["failures"].append("coverage_event_timestamp_invalid")
            if ambiguous_event_time:
                event_result["failures"].append("coverage_event_timestamp_ambiguous")
            scoped_rows = [row for row in rows
                           if row.get("venue") == venue
                           and (str(row.get("source_endpoint") or ""),
                                str(row.get("history_kind") or "")) in REQUIRED_COVERAGE[venue]
                           and row.get("trade_intent_id") == observation.get("intent_id")
                           and row.get("trade_event") == event]
            for row in scoped_rows:
                invalid_times = set(row.get("_invalid_coverage_time_fields") or ())
                if "event_signal_ts_ms" in invalid_times:
                    event_result["failures"].append("coverage_event_timestamp_invalid")
                if invalid_times.intersection({"start_ms", "end_ms"}):
                    event_result["failures"].append("coverage_range_invalid")
                if invalid_times.intersection({"requested_start_ms", "requested_end_ms"}):
                    event_result["failures"].append("coverage_requested_range_invalid")
            for endpoint, kind in sorted(REQUIRED_COVERAGE[venue]):
                candidates = [row for row in rows
                              if row.get("venue") == venue
                              and str(row.get("source_endpoint") or "") == endpoint
                              and str(row.get("history_kind") or "") == kind
                              and row.get("instrument") == event_expected_instrument
                              and event_expected_instrument is not None
                              and row.get("trade_intent_id") == observation.get("intent_id")
                              and row.get("trade_event") == event
                              and row.get("event_signal_ts_ms") == event_ts
                              and row.get("requested_start_ms") is not None
                              and row.get("requested_end_ms") is not None
                              and row.get("start_ms") is not None and row.get("end_ms") is not None]
                valid_candidates = []
                for row in candidates:
                    try:
                        start_ms, end_ms = int(row["start_ms"]), int(row["end_ms"])
                        requested_start_ms = int(row["requested_start_ms"])
                        requested_end_ms = int(row["requested_end_ms"])
                        if (start_ms > end_ms or requested_start_ms > requested_end_ms
                                or start_ms < requested_start_ms or end_ms > requested_end_ms):
                            raise ValueError("range_outside_request")
                        valid_candidates.append(row)
                    except (TypeError, ValueError):
                        event_result["failures"].append("coverage_range_invalid")
                candidates = valid_candidates
                if any(row.get("venue") == venue
                       and str(row.get("source_endpoint") or "") == endpoint
                       and str(row.get("history_kind") or "") == kind
                       and row.get("instrument") == event_expected_instrument
                       and row.get("trade_intent_id") == observation.get("intent_id")
                       and row.get("trade_event") == event
                       and row.get("event_signal_ts_ms") == event_ts
                       and (row.get("start_ms") is None or row.get("end_ms") is None)
                       for row in rows):
                    event_result["failures"].append(f"coverage_range_invalid:{endpoint}:{kind}")
                if any(row.get("venue") == venue
                       and str(row.get("source_endpoint") or "") == endpoint
                       and str(row.get("history_kind") or "") == kind
                       and row.get("instrument") != event_expected_instrument
                       and (row.get("trade_intent_id") == observation.get("intent_id")
                            or row.get("trade_event") == event
                            or row.get("event_signal_ts_ms") == event_ts)
                       for row in rows):
                    event_result["failures"].append(f"coverage_instrument_mismatch:{endpoint}:{kind}")
                requested_ranges: set[tuple[int, int]] = set()
                for row in candidates:
                    try:
                        requested_ranges.add((int(row["requested_start_ms"]),
                                              int(row["requested_end_ms"])))
                    except (TypeError, ValueError):
                        event_result["failures"].append("coverage_range_invalid")
                if any(row.get("trade_intent_id") == observation.get("intent_id")
                       and row.get("trade_event") == event
                       and row.get("event_signal_ts_ms") == event_ts
                       and row.get("venue") == venue
                       and str(row.get("source_endpoint") or "") == endpoint
                       and str(row.get("history_kind") or "") == kind
                       and row.get("instrument") == event_expected_instrument
                       and (row.get("requested_start_ms") is None
                            or row.get("requested_end_ms") is None)
                       for row in rows):
                    event_result["failures"].append("coverage_requested_range_missing")
                request_starts = {start for start, _ in requested_ranges}
                if len(request_starts) > 1:
                    event_result["failures"].append("coverage_requested_range_conflict")
                selected_request = max(requested_ranges, key=lambda item: item[1]) if requested_ranges else None
                if selected_request is not None and selected_request[1] < selected_request[0]:
                    event_result["failures"].append("coverage_requested_range_invalid")
                    selected_request = None
                # Repeated recovery calls may extend the requested end. The
                # longest interval supersedes earlier complete intervals; each
                # cursor chain is checked only within its own request identity.
                selected_rows = [row for row in candidates
                                 if selected_request is not None
                                 and (int(row["requested_start_ms"]), int(row["requested_end_ms"]))
                                 == selected_request]
                ranges: dict[tuple[int, int], list[Mapping[str, Any]]] = defaultdict(list)
                for row in selected_rows:
                    ranges[(int(row["start_ms"]), int(row["end_ms"]))].append(row)
                versions: dict[tuple[tuple[int, int], int, int, str], Mapping[str, Any]] = {}
                for row in candidates:
                    try:
                        version_key = (
                            (int(row["requested_start_ms"]), int(row["requested_end_ms"])),
                            int(row["start_ms"]), int(row["end_ms"]),
                            str(row.get("cursor_in") or ""),
                        )
                    except (TypeError, ValueError):
                        continue
                    previous = versions.get(version_key)
                    if previous is None:
                        versions[version_key] = row
                    else:
                        comparable_previous = {k: v for k, v in previous.items()
                                               if k != "first_observed_wall_ms"}
                        comparable_row = {k: v for k, v in row.items()
                                          if k != "first_observed_wall_ms"}
                        if comparable_previous != comparable_row:
                            event_result["failures"].append("coverage_conflicting_duplicate_page")
                complete_ranges = []
                for time_range, page_rows in sorted(ranges.items()):
                    by_cursor: dict[str, Mapping[str, Any]] = {}
                    for page_row in page_rows:
                        cursor_key = str(page_row.get("cursor_in") or "")
                        previous = by_cursor.get(cursor_key)
                        if previous is None:
                            by_cursor[cursor_key] = page_row
                        else:
                            comparable_previous = {k: v for k, v in previous.items()
                                                   if k != "first_observed_wall_ms"}
                            comparable_row = {k: v for k, v in page_row.items()
                                              if k != "first_observed_wall_ms"}
                            if comparable_previous != comparable_row:
                                event_result["failures"].append("coverage_conflicting_duplicate_page")
                            # Identical overlaps are harmless; conflicting versions remain incomplete.
                    cursor, seen, chain_complete = "", set(), False
                    while cursor in by_cursor and cursor not in seen:
                        seen.add(cursor)
                        page = by_cursor[cursor]
                        status = str(page.get("status") or "")
                        next_cursor = str(page.get("cursor_out") or "")
                        if status not in {"complete", "page_saved"}:
                            event_result["failures"].append(
                                str(page.get("error_type") or status or "coverage_incomplete"))
                            break
                        if status == "complete":
                            chain_complete = not next_cursor
                            break
                        if not next_cursor or next_cursor == cursor:
                            break
                        cursor = next_cursor
                    if chain_complete:
                        complete_ranges.append(time_range)
                    else:
                        event_result["failures"].append("coverage_page_chain_incomplete")
                ordered_ranges = sorted(complete_ranges)
                if ordered_ranges:
                    requested_start, requested_end = selected_request or (-1, -1)
                    if not requested_start <= event_ts <= requested_end:
                        event_result["failures"].append("coverage_range_does_not_include_event")
                    if ordered_ranges[0][0] != requested_start:
                        event_result["failures"].append("coverage_requested_range_start_missing")
                    previous_end = ordered_ranges[0][1]
                    for range_start, range_end in ordered_ranges[1:]:
                        if range_start != previous_end + 1:
                            event_result["failures"].append("coverage_range_gap")
                            break
                        previous_end = range_end
                    if previous_end != requested_end:
                        event_result["failures"].append("coverage_requested_range_end_missing")
                if not complete_ranges:
                    event_result["missing"].append(f"{endpoint}:{kind}")
            if event_result["missing"] or event_result["failures"]:
                event_result["status"] = "incomplete"
                missing.update(f"{event}:{value}" for value in event_result["missing"])
                failures.update(f"{event}:{value}" for value in event_result["failures"])
            event_coverage[event] = event_result
        if instrument_mismatch:
            failures.add("open_close_instrument_mismatch")
        result[venue] = {"status": "complete" if not missing and not failures and lifecycle
                         else "incomplete", "missing": sorted(missing),
                         "failures": sorted(failures), "by_event": event_coverage,
                         "expected_instrument": expected_instrument}
    return result


def _dedupe_funding(records: Iterable[Mapping[str, Any]], trade_id: str,
                    lifecycle: Mapping[str, Mapping[str, Any]]) -> tuple[list[Mapping[str, Any]], list[str], int]:
    observations = list(lifecycle.values())
    expected_instruments = {
        venue: next((str(row.get("bybit_symbol" if venue == "bybit" else "okx_instrument") or "")
                    for row in observations
                    if row.get("bybit_symbol" if venue == "bybit" else "okx_instrument")), "")
        for venue in VENUES
    }
    kept: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
    issues: list[str] = []
    duplicates = 0
    for row in records:
        if row.get("record_type") != "trade_history_fact" or row.get("fact_type") != "funding":
            continue
        venue, instrument, source_id, fact_type = (str(row.get(key) or "") for key in
                                                   ("venue", "instrument", "source_id", "fact_type"))
        candidates = set(row.get("attribution_candidates") or [])
        candidates.update(row.get("attribution_unknown_boundary_trade_ids") or [])
        candidates.update(row.get("attribution_equal_boundary_trade_ids") or [])
        if (venue not in VENUES or instrument != expected_instruments.get(venue)
                or (trade_id not in candidates and str(row.get("trade_id") or "") != trade_id)):
            continue
        if not all((venue, instrument, source_id, fact_type)):
            issues.append("funding_fact_missing_stable_identity")
            continue
        key = (venue, instrument, source_id, fact_type)
        previous = kept.get(key)
        if previous is None:
            kept[key] = row
            continue
        duplicates += 1
        comparable_previous = {k: v for k, v in previous.items() if k != "first_observed_wall_ms"}
        comparable_row = {k: v for k, v in row.items() if k != "first_observed_wall_ms"}
        if comparable_previous != comparable_row:
            issues.append("conflicting_duplicate_funding_fact")
        else:
            issues.append("funding_duplicate_fact")
    return [kept[key] for key in sorted(kept)], issues, duplicates


def _funding_summary(funding_facts: list[Mapping[str, Any]], trade_id: str) -> dict[str, Any]:
    funding: dict[str, Any] = {"status": "unassigned", "unassigned_cashflow_by_venue": {}}
    for venue in VENUES:
        currencies: dict[str, Decimal] = defaultdict(lambda: ZERO)
        relevant = []
        for row in funding_facts:
            if row.get("venue") != venue:
                continue
            currency = str(row.get("currency") or "unknown")
            amount = row.get("signed_amount")
            if amount not in (None, ""):
                currencies[currency] += _d(amount, "funding.signed_amount")
            relevant.append({"source_id": row.get("source_id"), "currency": currency,
                             "signed_amount": amount,
                             "candidate": trade_id in (row.get("attribution_candidates") or []),
                             "unknown_boundary_candidate": trade_id in (
                                 row.get("attribution_unknown_boundary_trade_ids") or []),
                             "equal_boundary_candidate": trade_id in (
                                 row.get("attribution_equal_boundary_trade_ids") or []),
                             "attribution_status": row.get("attribution_status")})
        funding["unassigned_cashflow_by_venue"][venue] = {
            currency: _s(amount) for currency, amount in sorted(currencies.items())
        }
        funding.setdefault("facts", {})[venue] = relevant
    return funding


def _coin_quantity(row: Mapping[str, Any], observation: Mapping[str, Any], venue: str) -> tuple[Decimal, Decimal, str]:
    quantity = _d(row.get("quantity"), "execution.quantity")
    unit = str(row.get("quantity_unit") or "")
    if unit != "contract":
        raise ValueError(f"execution_quantity_unit_unknown:{venue}:{unit or 'missing'}")
    if venue == "bybit":
        # This collector queries linear USDT contracts; Bybit execution qty is base-coin qty.
        return quantity, ONE, "bybit_linear_execution_quantity_is_base_coin"
    book = (observation.get("signal_vector") or {}).get("okx") or {}
    ct_val = book.get("ct_val")
    if ct_val not in (None, ""):
        multiplier = _d(ct_val, "okx.ct_val")
        source = "signal_vector_ct_val"
    else:
        submitted_contracts = _optional_decimal(
            observation, "planned_submitted_qty_by_venue", "okx")
        if submitted_contracts is not None and submitted_contracts > ZERO:
            contracts = submitted_contracts
            coin_qty = _d(observation.get("planned_submitted_coin_qty")
                           or observation.get("terminal_coin_qty"), "planned_submitted_coin_qty")
            source = "planned_submitted_coin_and_contract_quantity"
        else:
            contracts = _d(observation.get("terminal_okx_filled_qty"), "terminal_okx_filled_qty")
            coin_qty = _d(observation.get("terminal_coin_qty"), "terminal_coin_qty")
            source = "legacy_terminal_coin_and_contract_quantity"
        if contracts <= ZERO or coin_qty <= ZERO:
            raise ValueError("okx_contract_unit_conversion_missing")
        multiplier = coin_qty / contracts
    if multiplier <= ZERO:
        raise ValueError("okx_ct_val_not_positive")
    return quantity * multiplier, multiplier, source


def _event_fills(
    facts: Iterable[Mapping[str, Any]], observation: Mapping[str, Any], venue: str, event: str,
) -> dict[str, Any]:
    rows = [row for row in facts if row.get("fact_type") == "execution"
            and row.get("venue") == venue and row.get("trade_event") == event]
    fill_issues: list[str] = []
    fee_issues: list[str] = []
    details = []
    total_qty, total_value = ZERO, ZERO
    fee_cashflow: Decimal | None = ZERO
    fee_notional = ZERO
    fee_rate_numerator = ZERO
    for row in sorted(rows, key=lambda item: str(item.get("source_id") or "")):
        try:
            expected_instrument = (observation.get("bybit_symbol") if venue == "bybit"
                                   else observation.get("okx_instrument"))
            expected_id = (observation.get("bybit_order_link_id") if venue == "bybit"
                           else observation.get("okx_client_order_id"))
            actual_id = (row.get("order_link_id") if venue == "bybit"
                         else row.get("client_order_id"))
            correlation_basis = "client_order_id" if actual_id not in (None, "") else None
            if row.get("trade_intent_id") != observation.get("intent_id"):
                raise ValueError(f"execution_intent_identity_mismatch:{venue}:{event}")
            if row.get("instrument") != expected_instrument or not expected_instrument:
                raise ValueError(f"execution_instrument_mismatch:{venue}:{event}")
            if actual_id not in (None, "") and str(actual_id) != str(expected_id or ""):
                raise ValueError(f"execution_order_identity_mismatch:{venue}:{event}")
            if expected_id and actual_id in (None, ""):
                order_id = str(row.get("order_id") or "")
                if (venue == "bybit" and row.get("correlation_basis") == "known_order_id"
                        and str(row.get("source_endpoint") or "") == "/v5/execution/list"
                        and order_id):
                    correlation_basis = "known_order_id"
                elif (venue == "okx"
                      and str(row.get("source_endpoint") or "") == "/api/v5/trade/fills-history"
                      and order_id and row.get("source_id")
                      and row.get("correlation_basis") in (None, "")):
                    # Legacy producer rows were persisted only after the raw
                    # fills-history clOrdId had been matched to the intent.
                    correlation_basis = "legacy_verified_clordid_filter"
                else:
                    raise ValueError(f"execution_order_identity_missing:{venue}:{event}")
            quantity, quantity_multiplier, conversion_source = _coin_quantity(row, observation, venue)
            price = _d(row.get("price"), "execution.price")
            if quantity <= ZERO or price <= ZERO:
                raise ValueError("execution_quantity_or_price_not_positive")
            expected_side = _leg_side(str(observation.get("side") or "").lower(), event, venue)
            actual_side = str(row.get("side") or "").lower()
            if actual_side and actual_side != expected_side:
                fill_issues.append(f"execution_side_mismatch:{venue}:{event}")
            elif not actual_side:
                fill_issues.append(f"execution_side_missing:{venue}:{event}")
            total_qty += quantity
            total_value += quantity * price
            fee_value = row.get("fee_signed_amount")
            fee_ccy = str(row.get("fee_currency") or "").upper()
            if fee_value in (None, "") or fee_ccy != "USDT":
                fee_cashflow = None
                fee_issues.append(f"fee_cashflow_unavailable_or_unconverted:{venue}:{event}")
            elif fee_cashflow is not None:
                try:
                    fee = _d(fee_value, "execution.fee_signed_amount")
                    fee_cashflow += fee
                    fee_notional += quantity * price
                    fee_rate_numerator += abs(fee)
                except ValueError as exc:
                    fee_cashflow = None
                    fee_issues.append(str(exc))
            details.append({"source_id": row.get("source_id"), "price": _s(price),
                            "coin_quantity": _s(quantity), "exchange_fill_time_ms": row.get("exchange_fill_time_ms"),
                            "source_quantity": str(row.get("quantity")),
                            "source_quantity_unit": row.get("quantity_unit"),
                            "coin_quantity_multiplier": _s(quantity_multiplier),
                            "quantity_conversion_source": conversion_source,
                            "order_identity_correlation_basis": correlation_basis,
                            "exchange_record_ts_ms": row.get("exchange_record_ts_ms"),
                            "first_observed_wall_ms": row.get("first_observed_wall_ms")})
        except ValueError as exc:
            fill_issues.append(str(exc))
    complete_fills = bool(rows) and not fill_issues
    vwap = total_value / total_qty if complete_fills and total_qty > ZERO else None
    # Fee rate is independently derived from this venue/event's signed fee evidence,
    # never split from the policy's total round-trip percentage.
    fee_rate = (fee_rate_numerator / fee_notional
                if fee_cashflow is not None and fee_rate_numerator > ZERO and fee_notional > ZERO else None)
    return {"fills": details, "count": len(rows), "coin_qty": total_qty if complete_fills else None,
            "vwap": vwap, "fee_cashflow": fee_cashflow, "fee_rate": fee_rate,
            "issues": fill_issues + fee_issues}


def _event_timing(observation: Mapping[str, Any], venue_fills: Mapping[str, Mapping[str, Any]], event: str) -> dict[str, Any]:
    timing = observation.get("timing") or {}
    started, ready, consumed = (timing.get(name) or {} for name in
                                ("cycle_started", "vector_ready", "place_result_consumed"))
    spans = {}
    for label, finish in (("vector_ready", ready), ("place_result_consumed", consumed)):
        try:
            spans[f"cycle_started_to_{label}_ms"] = _s(
                (_d(finish["monotonic_ns"], "timing.monotonic")
                 - _d(started["monotonic_ns"], "timing.monotonic")) / Decimal(1_000_000)
            )
        except (KeyError, TypeError, ValueError):
            spans[f"cycle_started_to_{label}_ms"] = None
    try:
        spans["cycle_started_to_terminal_receive_wall_ms"] = _s(
            _d(observation["place_result_fill_wall_ms"], "place_result_fill_wall_ms")
            - _d(started["wall_ms"], "timing.wall_ms")
        )
    except (KeyError, TypeError, ValueError):
        spans["cycle_started_to_terminal_receive_wall_ms"] = None
    return {
        "cycle_started_wall_ms": started.get("wall_ms"),
        "cycle_started_monotonic_ns": started.get("monotonic_ns"),
        "signal_ts_ms": observation.get("signal_ts_ms"),
        "local_monotonic_spans": spans,
        "place_result_fill_wall_ms": observation.get("place_result_fill_wall_ms"),
        "place_result_fill_wall_source": observation.get("place_result_fill_wall_source"),
        "venue_fill_times": {venue: {
            "exchange_fill_time_ms": [f.get("exchange_fill_time_ms") for f in venue_fills[venue][event]["fills"]],
            "exchange_record_ts_ms": [f.get("exchange_record_ts_ms") for f in venue_fills[venue][event]["fills"]],
            "first_observed_wall_ms": [f.get("first_observed_wall_ms") for f in venue_fills[venue][event]["fills"]],
        } for venue in VENUES},
        "clock_domains_subtracted": [],
    }


def _trade_report(trade_id: str, lifecycle: Mapping[str, Mapping[str, Any]], records: list[Mapping[str, Any]],
                  observation_issues: Iterable[str] = (), duplicate_observation_count: int = 0) -> dict[str, Any]:
    issues: list[str] = []
    if "open" not in lifecycle:
        issues.append("open_observation_missing")
    if "close" not in lifecycle:
        issues.append("close_observation_missing")
    open_row, close_row = lifecycle.get("open"), lifecycle.get("close")
    facts, fact_issues, duplicate_count = _dedupe_facts(records, trade_id)
    issues.extend(fact_issues)
    issues.extend(observation_issues)
    coverage = _coverage(records, trade_id, lifecycle)
    for venue_coverage in coverage.values():
        issues.extend(f"history_coverage:{failure}" for failure in venue_coverage["failures"])
    if any(v["status"] != "complete" for v in coverage.values()):
        issues.append("history_coverage_incomplete")
    funding_facts, funding_issues, funding_duplicate_count = _dedupe_funding(
        records, trade_id, lifecycle)
    issues.extend(funding_issues)
    funding = _funding_summary(funding_facts, trade_id)
    if not open_row or not close_row:
        return {"trade_id": trade_id, "status": "incomplete", "issues": sorted(set(issues)),
                "history_coverage": coverage, "venues": {}, "quantity_coverage": {},
                "funding": funding, "pnl_actual_total": None,
                "duplicate_funding_fact_count": funding_duplicate_count,
                "duplicate_observation_count": duplicate_observation_count}

    if str(open_row.get("side") or "").lower() != str(close_row.get("side") or "").lower():
        issues.append("open_close_position_side_mismatch")
    event_fills = {venue: {event: _event_fills(facts, lifecycle[event], venue, event)
                           for event in ("open", "close")} for venue in VENUES}
    venues: dict[str, Any] = {}
    quantity_coverage: dict[str, Any] = {}
    for venue in VENUES:
        opened, closed = event_fills[venue]["open"], event_fills[venue]["close"]
        issues.extend(opened["issues"] + closed["issues"])
        for event_name, observation in (("open", open_row), ("close", close_row)):
            submitted = observation.get("planned_submitted_coin_qty_by_venue")
            submitted = submitted.get(venue) if isinstance(submitted, Mapping) else None
            if submitted not in (None, ""):
                try:
                    if _d(submitted, f"planned_submitted_coin_qty_by_venue.{venue}") <= ZERO:
                        raise ValueError("quantity_not_positive")
                except ValueError:
                    issues.append(f"planned_submitted_quantity_invalid:{venue}:{event_name}")
        try:
            entry_ref = _reference_price(open_row, venue, "open")
            exit_ref = _reference_price(close_row, venue, "close")
            notional = _d(open_row.get("target_notional_usdt"), "target_notional_usdt")
            target_notional_qty = notional / entry_ref
            submitted_by_venue = open_row.get("planned_submitted_coin_qty_by_venue")
            submitted_qty = (submitted_by_venue.get(venue)
                             if isinstance(submitted_by_venue, Mapping) else None)
            if submitted_qty not in (None, ""):
                theoretical_qty = _d(submitted_qty, f"planned_submitted_coin_qty_by_venue.{venue}")
                quantity_basis = "planned_submitted_coin_qty_by_venue"
            else:
                theoretical_qty = target_notional_qty
                quantity_basis = "target_notional_reference_price_legacy"
            if theoretical_qty <= ZERO or target_notional_qty <= ZERO:
                raise ValueError("theoretical_quantity_not_positive")
            sign = _side_sign(str(open_row.get("side") or "").lower(), venue)
            gross_theory = sign * theoretical_qty * (exit_ref - entry_ref)
            gross_theory_target_notional = sign * target_notional_qty * (exit_ref - entry_ref)
        except ValueError as exc:
            issues.append(str(exc))
            entry_ref = exit_ref = theoretical_qty = gross_theory = None
            target_notional_qty = gross_theory_target_notional = quantity_basis = None

        q_open, q_close = opened["coin_qty"], closed["coin_qty"]
        terminal_open = _terminal_coin_qty(open_row, venue)
        terminal_close = _terminal_coin_qty(close_row, venue)
        residuals = []
        for label, actual, terminal in (("open_vs_terminal", q_open, terminal_open),
                                        ("close_vs_terminal", q_close, terminal_close),
                                        ("entry_vs_exit", q_open, q_close)):
            if actual is None or terminal is None:
                residuals.append({"comparison": label, "amount": None, "status": "unknown"})
            else:
                delta = actual - terminal
                if label == "entry_vs_exit":
                    expected = q_close
                    delta = (actual or ZERO) - (expected or ZERO)
                residuals.append({"comparison": label, "amount": _s(delta),
                                  "status": "matched" if delta == ZERO else "unexplained_residual"})
        quantity_complete = all(row["status"] == "matched" for row in residuals)
        quantity_coverage[venue] = {"open_actual_coin_qty": _s(q_open),
                                    "close_actual_coin_qty": _s(q_close),
                                    "open_planned_submitted_coin_qty": _s(
                                        _optional_decimal(open_row, "planned_submitted_coin_qty_by_venue", venue)),
                                    "close_planned_submitted_coin_qty": _s(
                                        _optional_decimal(close_row, "planned_submitted_coin_qty_by_venue", venue)),
                                    "terminal_open_coin_qty": _s(terminal_open),
                                    "terminal_close_coin_qty": _s(terminal_close),
                                    "unexplained_residuals": [r for r in residuals if r["status"] != "matched"]}
        if not quantity_complete:
            issues.append(f"quantity_coverage_incomplete:{venue}")
        actual_gross = price_slippage = quantity_effect = decomposition_residual = None
        open_contrib = close_contrib = gross_at_theory_qty = None
        if (q_open is not None and q_close is not None and opened["vwap"] is not None
                and closed["vwap"] is not None and q_open == q_close
                and not any(issue.startswith(("execution_side_", "execution_quantity", "okx_"))
                            for issue in opened["issues"] + closed["issues"])):
            sign = _side_sign(str(open_row.get("side") or "").lower(), venue)
            actual_gross = sign * q_open * (closed["vwap"] - opened["vwap"])
            if entry_ref is not None and exit_ref is not None:
                gross_at_theory_qty = sign * theoretical_qty * (closed["vwap"] - opened["vwap"])
                price_slippage = gross_at_theory_qty - gross_theory
                quantity_effect = actual_gross - gross_at_theory_qty
                open_contrib = -sign * theoretical_qty * (opened["vwap"] - entry_ref)
                close_contrib = sign * theoretical_qty * (closed["vwap"] - exit_ref)
                decomposition_residual = (actual_gross - gross_theory) - price_slippage - quantity_effect
                if decomposition_residual != ZERO:
                    issues.append(f"gross_decomposition_residual:{venue}")

        theoretical_fee = target_theoretical_fee = None
        if theoretical_qty is not None and entry_ref is not None and exit_ref is not None:
            if opened["fee_rate"] is not None and closed["fee_rate"] is not None:
                theoretical_fee = -(theoretical_qty * entry_ref * opened["fee_rate"]
                                    + theoretical_qty * exit_ref * closed["fee_rate"])
                target_theoretical_fee = -(target_notional_qty * entry_ref * opened["fee_rate"]
                                           + target_notional_qty * exit_ref * closed["fee_rate"])
            else:
                issues.append(f"theoretical_fee_rate_unavailable:{venue}")
        pnl_theory = gross_theory + theoretical_fee if gross_theory is not None and theoretical_fee is not None else None
        pnl_theory_target_notional = (
            gross_theory_target_notional + target_theoretical_fee
            if gross_theory_target_notional is not None and target_theoretical_fee is not None else None
        )
        fee_actual = (opened["fee_cashflow"] + closed["fee_cashflow"]
                      if opened["fee_cashflow"] is not None and closed["fee_cashflow"] is not None else None)
        actual_status = ("incomplete_unassigned_account_level_funding"
                         if actual_gross is not None and fee_actual is not None
                         else "incomplete_execution_or_fee_evidence")
        venues[venue] = {
            "reference_entry_price": _s(entry_ref), "reference_exit_price": _s(exit_ref),
            "theoretical_coin_quantity": _s(theoretical_qty), "signed_coin_quantity": _s(
                theoretical_qty * _side_sign(str(open_row.get("side") or "").lower(), venue)
                if theoretical_qty is not None else None),
            "theoretical_quantity_basis": quantity_basis,
            "planned_submitted_coin_quantity": _s(
                _optional_decimal(open_row, "planned_submitted_coin_qty_by_venue", venue)),
            "unrounded_computed_coin_quantity": _s(
                _optional_decimal(open_row, "unrounded_computed_coin_qty_by_venue", venue)),
            "target_notional_coin_quantity": _s(target_notional_qty),
            "gross_theory": _s(gross_theory), "theoretical_fee_cashflow": _s(theoretical_fee),
            "gross_theory_target_notional": _s(gross_theory_target_notional),
            "target_notional_theoretical_fee_cashflow": _s(target_theoretical_fee),
            "pnl_theory_target_notional": _s(pnl_theory_target_notional),
            "theoretical_fee_rates_by_event": {
                "open": _s(opened["fee_rate"]), "close": _s(closed["fee_rate"]),
            },
            "policy_theoretical_fee_round_trip_pp_observed": open_row.get("theoretical_fee_round_trip_pp"),
            "theoretical_fee_rate_source": "derived_per_venue_from_matched_execution_fee_and_fill_notional"
                if theoretical_fee is not None else None,
            "pnl_theory": _s(pnl_theory), "gross_actual": _s(actual_gross),
            "actual_fee_cashflow": _s(fee_actual),
            "pnl_actual_fee_only": _s(actual_gross + fee_actual)
                if actual_gross is not None and fee_actual is not None else None,
            "pnl_actual": None,
            "pnl_actual_status": actual_status,
            "gross_at_theoretical_quantity_actual_prices": _s(gross_at_theory_qty),
            "price_slippage_gross": _s(price_slippage),
            "quantity_effect_gross": _s(quantity_effect),
            "gross_decomposition_residual": _s(decomposition_residual),
            "price_slippage_gross_open_contribution": _s(open_contrib),
            "price_slippage_gross_close_contribution": _s(close_contrib),
            "actual_entry_vwap": _s(opened["vwap"]), "actual_exit_vwap": _s(closed["vwap"]),
            "open_fill_count": opened["count"], "close_fill_count": closed["count"],
            "open_fills": opened["fills"], "close_fills": closed["fills"],
        }

    timing = {event: _event_timing(lifecycle[event], event_fills, event) for event in ("open", "close")}
    theory_total = sum((_d(venues[v]["pnl_theory"], "pnl_theory") for v in VENUES
                        if venues[v]["pnl_theory"] is not None), ZERO)
    theory_total_complete = all(venues[v]["pnl_theory"] is not None for v in VENUES)
    actual_total = sum((_d(venues[v]["gross_actual"], "gross_actual")
                        + _d(venues[v]["actual_fee_cashflow"], "actual_fee_cashflow")
                        for v in VENUES if venues[v]["gross_actual"] is not None
                        and venues[v]["actual_fee_cashflow"] is not None), ZERO)
    actual_total_complete = all(venues[v]["gross_actual"] is not None
                                and venues[v]["actual_fee_cashflow"] is not None for v in VENUES)
    unexplained = [{"venue": venue, **residual}
                   for venue, coverage_row in quantity_coverage.items()
                   for residual in coverage_row["unexplained_residuals"]]
    gross_theory_total = sum((_d(venues[v]["gross_theory"], "gross_theory")
                              for v in VENUES if venues[v]["gross_theory"] is not None), ZERO)
    gross_theory_target_notional_total = sum(
        (_d(venues[v]["gross_theory_target_notional"], "gross_theory_target_notional")
         for v in VENUES if venues[v]["gross_theory_target_notional"] is not None), ZERO)
    pnl_theory_target_notional_total = sum(
        (_d(venues[v]["pnl_theory_target_notional"], "pnl_theory_target_notional")
         for v in VENUES if venues[v]["pnl_theory_target_notional"] is not None), ZERO)
    gross_actual_total = sum((_d(venues[v]["gross_actual"], "gross_actual")
                              for v in VENUES if venues[v]["gross_actual"] is not None), ZERO)
    slippage_total = sum((_d(venues[v]["price_slippage_gross"], "price_slippage_gross")
                          for v in VENUES if venues[v]["price_slippage_gross"] is not None), ZERO)
    quantity_effect_total = sum((_d(venues[v]["quantity_effect_gross"], "quantity_effect_gross")
                                 for v in VENUES if venues[v]["quantity_effect_gross"] is not None), ZERO)
    return {
        "trade_id": trade_id,
        "status": "complete" if theory_total_complete
            and all(venues[v]["pnl_actual"] is not None for v in VENUES)
            and all(v["status"] == "complete" for v in coverage.values()) and not issues else "incomplete",
        "issues": sorted(set(issues)), "history_coverage": coverage,
        "venues": venues, "gross_theory_total": _s(gross_theory_total),
        "gross_theory_target_notional_total": _s(gross_theory_target_notional_total),
        "gross_actual_total": _s(gross_actual_total), "price_slippage_gross_total": _s(slippage_total),
        "quantity_effect_gross_total": _s(quantity_effect_total),
        "pnl_theory_total": _s(theory_total) if theory_total_complete else None,
        "pnl_theory_target_notional_total": _s(pnl_theory_target_notional_total),
        "pnl_actual_total": None,
        "pnl_actual_fee_only_total": _s(actual_total) if actual_total_complete else None,
        "pnl_actual_status": "incomplete_unassigned_account_level_funding",
        "quantity_coverage": {"by_venue": quantity_coverage,
                              "unexplained_residuals": unexplained}, "funding": funding,
        "timing": timing, "duplicate_fact_count": duplicate_count,
        "duplicate_observation_count": duplicate_observation_count,
        "duplicate_funding_fact_count": funding_duplicate_count,
        "unexplained_residual": unexplained,
    }


def _terminal_coin_qty(row: Mapping[str, Any], venue: str) -> Decimal | None:
    try:
        actual_qtys = row.get("actual_terminal_filled_qty_by_venue")
        native_qty = (actual_qtys.get(venue) if isinstance(actual_qtys, Mapping) else None)
        if native_qty in (None, ""):
            native_qty = (row.get("terminal_bybit_filled_qty") if venue == "bybit"
                          else row.get("terminal_okx_filled_qty"))
        qty = _d(native_qty, f"terminal_{venue}_filled_qty")
        if venue == "bybit":
            return qty
        book = (row.get("signal_vector") or {}).get("okx") or {}
        ct_val = book.get("ct_val") if isinstance(book, Mapping) else None
        if ct_val in (None, ""):
            planned_coin = row.get("planned_submitted_coin_qty") or row.get("terminal_coin_qty")
            planned_native = _optional_decimal(row, "planned_submitted_qty_by_venue", "okx")
            if planned_native is not None and planned_native > ZERO:
                ct_val = _d(planned_coin, "planned_submitted_coin_qty") / planned_native
            else:
                raise ValueError("okx_contract_value_missing_for_terminal_fill")
        multiplier = _d(ct_val, "okx.ct_val")
        if multiplier <= ZERO:
            return None
        return qty * multiplier
    except ValueError:
        return None


def reconcile_records(records: Iterable[Mapping[str, Any]], input_issues: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    rows = list(records)
    lifecycle, observation_issues, observation_duplicate_counts = _observation_map(rows)
    reports = [_trade_report(trade_id, events, rows, observation_issues.get(trade_id, ()),
                             observation_duplicate_counts.get(trade_id, 0))
               for trade_id, events in sorted(lifecycle.items())]
    issues = sorted((dict(issue) for issue in input_issues), key=lambda item: (item.get("line", 0), item.get("error", "")))
    if issues:
        for report in reports:
            report["status"] = "incomplete"
            report["issues"] = sorted(set(report["issues"]) | {"jsonl_input_incomplete"})
    return {"schema_version": "bbot.trade_reconciliation.v1", "trades": reports,
            "input_issues": issues}


def read_jsonl(path: Path) -> tuple[list[Mapping[str, Any]], list[dict[str, Any]]]:
    records, issues = [], []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, 1):
            if not raw.endswith("\n"):
                issues.append({"line": line_no, "error": "incomplete_jsonl_line"})
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                issues.append({"line": line_no, "error": "malformed_jsonl", "column": exc.colno})
                continue
            if not isinstance(row, Mapping):
                issues.append({"line": line_no, "error": "jsonl_row_not_object"})
                continue
            records.append(row)
    return records, issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jsonl", type=Path, help="existing theta_trades JSONL file")
    args = parser.parse_args(argv)
    try:
        records, input_issues = read_jsonl(args.jsonl)
    except OSError as exc:
        print(json.dumps({"error": "input_read_failed", "error_type": type(exc).__name__},
                         sort_keys=True, separators=(",", ":")))
        return 2
    print(json.dumps(reconcile_records(records, input_issues), sort_keys=True,
                     separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
