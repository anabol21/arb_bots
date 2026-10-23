"""Bounded, read-only execution and funding history for completed theta trades."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlencode

from app.bot.private.order_sign import LiveCredentials
from app.bot.private.venue import VenueEndpoints, endpoints_for_venue
from app.bot.private.ws_w4_baseline import _bybit_signed_get, _okx_signed_get

BYBIT_EXECUTION_PAGE_SIZE = 100
BYBIT_TRANSACTION_PAGE_SIZE = 50
OKX_HISTORY_PAGE_SIZE = 100
MAX_PAGES_PER_WINDOW = 20
MAX_BYBIT_WINDOWS = 14
BYBIT_WINDOW_MS = 7 * 24 * 60 * 60 * 1000
OKX_WINDOW_MS = 90 * 24 * 60 * 60 * 1000


def _number(value: Any) -> str:
    try:
        return str(Decimal(str(value)))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("history_numeric_field_invalid") from exc


def _event_ms(value: Any) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("history_event_time_invalid") from exc


def _optional_event_ms(value: Any) -> Optional[int]:
    return None if value in (None, "") else _event_ms(value)


def _bybit_rows(page: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], str]:
    if page.get("retCode") not in (0, "0"):
        raise RuntimeError("bybit_history_rejected")
    result = page.get("result")
    if not isinstance(result, Mapping) or not isinstance(result.get("list"), list):
        raise ValueError("bybit_history_schema_invalid")
    rows = result["list"]
    if any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("bybit_history_row_invalid")
    return rows, str(result.get("nextPageCursor") or "")


def _okx_rows(page: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if str(page.get("code", "")) != "0" or not isinstance(page.get("data"), list):
        raise RuntimeError("okx_history_rejected_or_malformed")
    rows = page["data"]
    if any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("okx_history_row_invalid")
    return rows


def _bybit_execution_fact(
    row: Mapping[str, Any], *, trade_id: str, symbol: str, order_link_id: str,
    trade_intent_id: str, trade_event: str, observed_ms: int,
    allow_order_id_match: bool = False,
) -> dict[str, Any]:
    source_id = str(row.get("execId") or "")
    row_order_link_id = str(row.get("orderLinkId") or "")
    order_id = str(row.get("orderId") or "")
    if (not source_id or str(row.get("symbol") or "") != symbol
            or (row_order_link_id != order_link_id and not (
                allow_order_id_match and not row_order_link_id and order_id
            ))):
        raise ValueError("bybit_execution_identity_missing_or_mismatched")
    return {
        "record_type": "trade_history_fact", "fact_type": "execution",
        "trade_id": trade_id, "venue": "bybit", "instrument": symbol,
        "trade_intent_id": trade_intent_id, "trade_event": trade_event,
        "source_endpoint": "/v5/execution/list", "source_id": source_id,
        "exchange_event_ts_ms": _event_ms(row.get("execTime")),
        "exchange_fill_time_ms": _event_ms(row.get("execTime")),
        "first_observed_wall_ms": observed_ms,
        "time_provenance": "exchange_execTime_and_local_first_observed_wall_clock",
        "price": _number(row.get("execPrice")), "quantity": _number(row.get("execQty")),
        "quantity_unit": "contract", "fee_amount": _number(row.get("execFee") or "0"),
        "fee_signed_amount": str(-abs(Decimal(_number(row.get("execFee") or "0")))),
        "fee_currency": str(row.get("feeCurrency") or ""),
        "side": row.get("side"), "order_id": order_id or None,
        "order_link_id": row_order_link_id,
        "correlation_basis": "client_order_id" if row_order_link_id else "known_order_id",
        "dedupe_key": f"bybit:{symbol}:{source_id}:execution",
    }


def _bybit_transaction_facts(
    row: Mapping[str, Any], *, trade_id: str, symbol: str, observed_ms: int,
    eligible_order_ids: set[str],
) -> list[dict[str, Any]]:
    source_id = str(row.get("id") or "")
    row_type = str(row.get("type") or "")
    order_id = str(row.get("orderId") or "")
    if not source_id or str(row.get("symbol") or "") != symbol:
        raise ValueError("bybit_transaction_identity_missing_or_mismatched")
    if row_type == "TRADE":
        if not order_id:
            raise ValueError("bybit_transaction_identity_missing_or_mismatched")
        if order_id not in eligible_order_ids:
            # The endpoint is account/instrument/time scoped. A valid trade row
            # for another account order is unrelated to this bot intent.
            return []
    base = {
        "trade_id": trade_id if row_type == "TRADE" else None,
        "attribution_status": "order_id_match" if row_type == "TRADE" else "unassigned_requires_unique_position_interval",
        "venue": "bybit", "instrument": symbol,
        "source_endpoint": "/v5/account/transaction-log", "source_id": source_id,
        "exchange_event_ts_ms": _event_ms(row.get("transactionTime")),
        "first_observed_wall_ms": observed_ms,
        "time_provenance": "exchange_transactionTime_and_local_first_observed_wall_clock",
        "currency": row.get("currency"), "dedupe_key": None,
        "order_id": order_id or None,
    }
    facts = []
    if Decimal(_number(row.get("fee") or "0")) != 0:
        facts.append({
            **base, "record_type": "trade_history_fact", "fact_type": "fee",
            "amount": _number(row.get("fee")),
            "signed_amount": str(-abs(Decimal(_number(row.get("fee"))))),
            "accounting_role": "cross_check_only",
            "dedupe_key": f"bybit:{symbol}:{source_id}:fee",
        })
    if Decimal(_number(row.get("funding") or "0")) != 0:
        amount = _number(row.get("funding"))
        facts.append({
            **base, "record_type": "trade_history_fact", "fact_type": "funding",
            "amount": amount, "signed_amount": amount,
            "dedupe_key": f"bybit:{symbol}:{source_id}:funding",
        })
    if not facts:
        facts.append({
            **base, "record_type": "trade_history_fact", "fact_type": "transaction",
            "amount": _number(row.get("change") or "0"),
            "dedupe_key": f"bybit:{symbol}:{source_id}:transaction",
        })
    return facts


def _okx_fill_fact(
    row: Mapping[str, Any], *, trade_id: str, instrument: str,
    trade_intent_id: str, trade_event: str, observed_ms: int
) -> dict[str, Any]:
    source_id = str(row.get("tradeId") or "")
    bill_id = str(row.get("billId") or "")
    client_order_id = str(row.get("clOrdId") or "")
    if (not source_id or not bill_id or not client_order_id
            or str(row.get("instId") or "") != instrument):
        raise ValueError("okx_fill_identity_missing_or_mismatched")
    fee = _number(row.get("fee") or "0")
    fill_time_ms = _optional_event_ms(row.get("fillTime"))
    record_ts_ms = _optional_event_ms(row.get("ts"))
    if fill_time_ms is None and record_ts_ms is None:
        raise ValueError("okx_fill_time_missing")
    return {
        "record_type": "trade_history_fact", "fact_type": "execution",
        "trade_id": trade_id, "venue": "okx", "instrument": instrument,
        "trade_intent_id": trade_intent_id, "trade_event": trade_event,
        "source_endpoint": "/api/v5/trade/fills-history", "source_id": source_id,
        "bill_id": bill_id,
        # `ts` is the record timestamp; only `fillTime` is an execution boundary.
        "exchange_event_ts_ms": fill_time_ms,
        "exchange_fill_time_ms": fill_time_ms,
        "exchange_record_ts_ms": record_ts_ms,
        "exchange_event_time_source": (
            "fillTime" if fill_time_ms is not None else "fillTime_missing_record_ts_only"
        ),
        "first_observed_wall_ms": observed_ms,
        "time_provenance": "exchange_fillTime_or_ts_and_local_first_observed_wall_clock",
        "price": _number(row.get("fillPx")), "quantity": _number(row.get("fillSz")),
        "quantity_unit": "contract", "fee_amount": fee,
        "fee_signed_amount": fee, "fee_currency": str(row.get("feeCcy") or ""),
        "side": row.get("side"), "order_id": row.get("ordId"),
        "client_order_id": client_order_id,
        "correlation_basis": "client_order_id",
        "dedupe_key": f"okx:{instrument}:{source_id}:execution",
    }


def _okx_bill_fact(
    row: Mapping[str, Any], *, trade_id: str, instrument: str, observed_ms: int
) -> dict[str, Any]:
    source_id = str(row.get("billId") or "")
    if not source_id or str(row.get("instId") or "") != instrument:
        raise ValueError("okx_bill_identity_missing_or_mismatched")
    is_funding = str(row.get("type") or "") == "8"
    return {
        "record_type": "trade_history_fact",
        "fact_type": "funding" if is_funding else "account_bill",
        "trade_id": None if is_funding else trade_id,
        "attribution_status": "unassigned_requires_unique_position_interval" if is_funding else "account_bill_not_allocated",
        "venue": "okx", "instrument": instrument,
        "source_endpoint": "/api/v5/account/bills-archive", "source_id": source_id,
        "exchange_event_ts_ms": _event_ms(row.get("ts")),
        "first_observed_wall_ms": observed_ms,
        "time_provenance": "exchange_ts_and_local_first_observed_wall_clock",
        "type": row.get("type"), "sub_type": row.get("subType"),
        "amount": _number(row.get("balChg") or "0"),
        "signed_amount": _number(row.get("balChg") or "0"),
        "currency": row.get("ccy"),
        "dedupe_key": f"okx:{instrument}:{source_id}:bill",
    }


class TradeHistoryCollector:
    """Read bounded venue history and append each page before advancing its cursor."""

    def __init__(
        self, *, bybit_credentials: LiveCredentials, okx_credentials: LiveCredentials,
        endpoints: VenueEndpoints, journal: Any,
        http_get_json: Optional[Callable[..., Mapping[str, Any]]] = None,
        now_ms: Callable[[], int] = lambda: int(datetime.now(timezone.utc).timestamp() * 1000),
    ) -> None:
        canonical = endpoints_for_venue(endpoints.venue)
        if (endpoints.bybit_rest, endpoints.okx_rest, endpoints.okx_simulated_trading) != (
            canonical.bybit_rest, canonical.okx_rest, canonical.okx_simulated_trading
        ):
            raise RuntimeError("history_endpoint_route_mismatch")
        self.bybit_credentials = bybit_credentials
        self.okx_credentials = okx_credentials
        self.endpoints = endpoints
        self.journal = journal
        self.http_get_json = http_get_json
        self.now_ms = now_ms

    def collect(self, *, trade_id: str, bybit_symbol: str, okx_instrument: str,
                bybit_order_link_id: str, okx_client_order_id: str,
                intent_id: str, trade_event: str, base_coin: str, side: str,
                start_ms: int, end_ms: int, signal_ts_ms: int,
                event_signal_ts_ms: Optional[int] = None) -> None:
        event_signal_ts_ms = signal_ts_ms if event_signal_ts_ms is None else event_signal_ts_ms
        requested_start_ms, requested_end_ms = int(start_ms), int(end_ms)
        if not trade_id or end_ms < start_ms:
            raise ValueError("history_trade_or_time_range_invalid")
        try:
            bybit_windows = _windows(start_ms, end_ms, BYBIT_WINDOW_MS)
        except Exception as exc:
            self._record_incomplete(trade_id, "bybit", "/v5/execution/list", "execution",
                                    bybit_symbol, start_ms, end_ms, "", signal_ts_ms, exc,
                                    trade_intent_id=intent_id, trade_event=trade_event,
                                    event_signal_ts_ms=event_signal_ts_ms,
                                    requested_start_ms=requested_start_ms,
                                    requested_end_ms=requested_end_ms)
            raise
        if not bybit_order_link_id or not okx_client_order_id:
            exc = ValueError("history_client_order_identity_missing")
            self._record_incomplete(trade_id, "bybit", "/v5/execution/list", "execution",
                                    bybit_symbol, start_ms, end_ms, "", signal_ts_ms, exc,
                                    trade_intent_id=intent_id, trade_event=trade_event,
                                    event_signal_ts_ms=event_signal_ts_ms,
                                    requested_start_ms=requested_start_ms,
                                    requested_end_ms=requested_end_ms)
            self._record_incomplete(trade_id, "okx", "/api/v5/trade/fills-history", "fills",
                                    okx_instrument, start_ms, end_ms, "", signal_ts_ms, exc,
                                    trade_intent_id=intent_id, trade_event=trade_event,
                                    event_signal_ts_ms=event_signal_ts_ms,
                                    requested_start_ms=requested_start_ms,
                                    requested_end_ms=requested_end_ms)
            raise exc
        if not intent_id or trade_event not in {"open", "close"}:
            raise ValueError("history_terminal_event_identity_missing")
        for start, end in bybit_windows:
            matched_order_ids = self._bybit_pages(
                "/v5/execution/list", "execution", trade_id, bybit_symbol,
                start, end, signal_ts_ms, order_link_id=bybit_order_link_id,
                trade_intent_id=intent_id, trade_event=trade_event,
                event_signal_ts_ms=event_signal_ts_ms,
                requested_start_ms=requested_start_ms, requested_end_ms=requested_end_ms,
            )
            for txn_type in ("TRADE", "SETTLEMENT"):
                self._bybit_pages("/v5/account/transaction-log", txn_type, trade_id,
                                  bybit_symbol, start, end, signal_ts_ms,
                                  eligible_order_ids=matched_order_ids,
                                  funding_identity=(base_coin, side),
                                  trade_intent_id=intent_id, trade_event=trade_event,
                                  event_signal_ts_ms=event_signal_ts_ms,
                                  requested_start_ms=requested_start_ms,
                                  requested_end_ms=requested_end_ms)
        if end_ms - start_ms + 1 > OKX_WINDOW_MS:
            exc = RuntimeError("okx_history_range_limit")
            self._record_incomplete(trade_id, "okx", "/api/v5/trade/fills-history", "fills",
                                    okx_instrument, start_ms, end_ms, "", signal_ts_ms, exc,
                                    trade_intent_id=intent_id, trade_event=trade_event,
                                    event_signal_ts_ms=event_signal_ts_ms,
                                    requested_start_ms=requested_start_ms,
                                    requested_end_ms=requested_end_ms)
            raise exc
        for start, end in _windows(start_ms, end_ms, OKX_WINDOW_MS, maximum=1):
            self._okx_pages("/api/v5/trade/fills-history", "fills", trade_id,
                            okx_instrument, start, end, signal_ts_ms,
                            client_order_id=okx_client_order_id,
                            trade_intent_id=intent_id, trade_event=trade_event,
                            event_signal_ts_ms=event_signal_ts_ms,
                            requested_start_ms=requested_start_ms,
                            requested_end_ms=requested_end_ms)
            self._okx_pages("/api/v5/account/bills-archive", "bills", trade_id,
                            okx_instrument, start, end, signal_ts_ms,
                            funding_identity=(base_coin, side),
                            trade_intent_id=intent_id, trade_event=trade_event,
                            event_signal_ts_ms=event_signal_ts_ms,
                            requested_start_ms=requested_start_ms,
                            requested_end_ms=requested_end_ms)

    def _bybit_pages(self, path: str, kind: str, trade_id: str, symbol: str,
                     start: int, end: int, signal_ts_ms: int, *,
                     order_link_id: str = "", eligible_order_ids: Optional[set[str]] = None,
                     funding_identity: Optional[tuple[str, str]] = None,
                     trade_intent_id: str = "", trade_event: str = "",
                     event_signal_ts_ms: Optional[int] = None,
                     requested_start_ms: Optional[int] = None,
                     requested_end_ms: Optional[int] = None) -> set[str]:
        event_signal_ts_ms = signal_ts_ms if event_signal_ts_ms is None else event_signal_ts_ms
        requested_start_ms = start if requested_start_ms is None else requested_start_ms
        requested_end_ms = end if requested_end_ms is None else requested_end_ms
        cursor, complete = self.journal.trade_history_cursor(
            trade_id, "bybit", path, kind, symbol, start, end, "", signal_ts_ms,
            trade_intent_id=trade_intent_id, trade_event=trade_event,
            requested_start_ms=requested_start_ms, requested_end_ms=requested_end_ms,
        )
        if complete:
            return self.journal.trade_history_execution_order_ids(
                trade_id=trade_id, venue="bybit", symbol=symbol,
                order_link_id=order_link_id,
            ) if kind == "execution" else set()
        matched_order_ids: set[str] = (
            self.journal.trade_history_execution_order_ids(
                trade_id=trade_id, venue="bybit", symbol=symbol,
                order_link_id=order_link_id,
            ) if kind == "execution" else set()
        )
        seen_cursors = {cursor} if cursor else set()
        for _ in range(MAX_PAGES_PER_WINDOW):
            query_fields = {
                "category": "linear", "symbol": symbol, "startTime": str(start),
                "endTime": str(end),
                "limit": str(BYBIT_TRANSACTION_PAGE_SIZE if kind in {"TRADE", "SETTLEMENT"}
                             else BYBIT_EXECUTION_PAGE_SIZE),
            }
            if kind in {"TRADE", "SETTLEMENT"}:
                query_fields.update({"accountType": "UNIFIED", "currency": "USDT", "type": kind})
            if cursor:
                query_fields["cursor"] = cursor
            query = urlencode(query_fields)
            try:
                page = _bybit_signed_get(
                    credentials=self.bybit_credentials, base=self.endpoints.bybit_rest,
                    path=path, query=query, http_get_json=self.http_get_json,
                )
                rows, next_cursor = _bybit_rows(page)
                if next_cursor and next_cursor in seen_cursors:
                    raise RuntimeError("bybit_history_cursor_repeated")
                observed = self.now_ms()
                facts = []
                if kind == "execution":
                    matched_order_ids.update(
                        str(row.get("orderId") or "") for row in rows
                        if str(row.get("symbol") or "") == symbol
                        and str(row.get("orderLinkId") or "") == order_link_id
                        and row.get("orderId")
                    )
                for row in rows:
                    if kind == "execution":
                        if str(row.get("symbol") or "") != symbol:
                            raise ValueError("bybit_execution_instrument_mismatch")
                        row_order_link_id = str(row.get("orderLinkId") or "")
                        order_id = str(row.get("orderId") or "")
                        if row_order_link_id != order_link_id:
                            if row_order_link_id:
                                if order_id in matched_order_ids:
                                    raise ValueError("bybit_execution_client_order_id_mismatch")
                                continue
                            if not order_id or order_id not in matched_order_ids:
                                # This account-scoped endpoint can include other orders;
                                # an unrelated row may not carry orderLinkId at all.
                                continue
                            allow_order_id_match = True
                        else:
                            allow_order_id_match = False
                        if not order_id:
                            raise ValueError("bybit_execution_order_id_missing")
                        matched_order_ids.add(order_id)
                        facts.append(_bybit_execution_fact(
                            row, trade_id=trade_id, symbol=symbol,
                            order_link_id=order_link_id, trade_intent_id=trade_intent_id,
                            trade_event=trade_event, observed_ms=observed,
                            allow_order_id_match=allow_order_id_match,
                        ))
                    else:
                        if str(row.get("type") or "") != kind:
                            continue
                        txn_facts = _bybit_transaction_facts(
                            row, trade_id=trade_id, symbol=symbol, observed_ms=observed,
                            eligible_order_ids=eligible_order_ids or set(),
                        )
                        if kind == "SETTLEMENT":
                            for fact in txn_facts:
                                fact.update(self._funding_attribution(
                                    trade_id, *funding_identity,
                                    int(fact["exchange_event_ts_ms"]), venue="bybit",
                                    instrument=symbol, trade_event=trade_event,
                                ))
                        facts.extend(txn_facts)
                key = _coverage_key(trade_id, "bybit", path, kind, symbol, start, end, cursor,
                                    trade_intent_id, trade_event, requested_start_ms,
                                    requested_end_ms)
                self.journal.append_trade_history_page(
                    facts, _coverage_row(key, trade_id, "bybit", path, kind, symbol,
                                         start, end, cursor, next_cursor, not next_cursor, observed,
                                         trade_intent_id, trade_event, event_signal_ts_ms,
                                         requested_start_ms, requested_end_ms),
                    signal_ts_ms=signal_ts_ms,
                )
            except Exception as exc:
                self._record_incomplete(trade_id, "bybit", path, kind, symbol,
                                        start, end, cursor, signal_ts_ms, exc,
                                        trade_intent_id=trade_intent_id, trade_event=trade_event,
                                        event_signal_ts_ms=event_signal_ts_ms,
                                        requested_start_ms=requested_start_ms,
                                        requested_end_ms=requested_end_ms)
                raise
            if not next_cursor:
                return matched_order_ids
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        exc = RuntimeError("bybit_history_page_limit")
        self._record_incomplete(trade_id, "bybit", path, kind, symbol,
                                start, end, cursor, signal_ts_ms, exc,
                                trade_intent_id=trade_intent_id, trade_event=trade_event,
                                event_signal_ts_ms=event_signal_ts_ms,
                                requested_start_ms=requested_start_ms,
                                requested_end_ms=requested_end_ms)
        raise exc

    def _okx_pages(self, path: str, kind: str, trade_id: str, instrument: str,
                   start: int, end: int, signal_ts_ms: int, *,
                   client_order_id: str = "",
                   funding_identity: Optional[tuple[str, str]] = None,
                     trade_intent_id: str = "", trade_event: str = "",
                     event_signal_ts_ms: Optional[int] = None,
                     requested_start_ms: Optional[int] = None,
                     requested_end_ms: Optional[int] = None) -> None:
        event_signal_ts_ms = signal_ts_ms if event_signal_ts_ms is None else event_signal_ts_ms
        requested_start_ms = start if requested_start_ms is None else requested_start_ms
        requested_end_ms = end if requested_end_ms is None else requested_end_ms
        cursor, complete = self.journal.trade_history_cursor(
            trade_id, "okx", path, kind, instrument, start, end, "", signal_ts_ms,
            trade_intent_id=trade_intent_id, trade_event=trade_event,
            requested_start_ms=requested_start_ms, requested_end_ms=requested_end_ms,
        )
        if complete:
            return
        seen_cursors = {cursor} if cursor else set()
        for _ in range(MAX_PAGES_PER_WINDOW):
            params = {"instType": "SWAP", "instId": instrument,
                      "begin": str(start), "end": str(end), "limit": str(OKX_HISTORY_PAGE_SIZE)}
            if cursor:
                params["after"] = cursor
            path_query = path + "?" + urlencode(params)
            try:
                page = _okx_signed_get(
                    credentials=self.okx_credentials, base=self.endpoints.okx_rest,
                    path_with_query=path_query, venue=self.endpoints.venue,
                    http_get_json=self.http_get_json,
                )
                rows = _okx_rows(page)
                observed = self.now_ms()
                if kind == "fills":
                    facts = []
                    for row in rows:
                        if str(row.get("instId") or "") != instrument:
                            raise ValueError("okx_fill_instrument_mismatch")
                        row_client_id = str(row.get("clOrdId") or "")
                        if not row_client_id:
                            raise ValueError("okx_fill_client_order_id_missing")
                        if row_client_id != client_order_id:
                            continue
                        facts.append(_okx_fill_fact(
                            row, trade_id=trade_id, instrument=instrument,
                            trade_intent_id=trade_intent_id, trade_event=trade_event,
                            observed_ms=observed
                        ))
                    id_key = "billId"
                else:
                    facts = [_okx_bill_fact(row, trade_id=trade_id, instrument=instrument,
                                            observed_ms=observed) for row in rows]
                    for fact in facts:
                        if fact.get("fact_type") == "funding":
                            fact.update(self._funding_attribution(
                                trade_id, *funding_identity,
                                int(fact["exchange_event_ts_ms"]), venue="okx",
                                instrument=instrument, trade_event=trade_event,
                            ))
                    id_key = "billId"
                next_cursor = str(rows[-1].get(id_key) or "") if len(rows) == OKX_HISTORY_PAGE_SIZE else ""
                if len(rows) == OKX_HISTORY_PAGE_SIZE and not next_cursor:
                    raise ValueError("okx_history_cursor_missing")
                if next_cursor and next_cursor in seen_cursors:
                    raise RuntimeError("okx_history_cursor_repeated")
                key = _coverage_key(trade_id, "okx", path, kind, instrument, start, end, cursor,
                                    trade_intent_id, trade_event, requested_start_ms,
                                    requested_end_ms)
                self.journal.append_trade_history_page(
                    facts, _coverage_row(key, trade_id, "okx", path, kind, instrument,
                                         start, end, cursor, next_cursor, not next_cursor, observed,
                                         trade_intent_id, trade_event, event_signal_ts_ms,
                                         requested_start_ms, requested_end_ms),
                    signal_ts_ms=signal_ts_ms,
                )
            except Exception as exc:
                self._record_incomplete(trade_id, "okx", path, kind, instrument,
                                        start, end, cursor, signal_ts_ms, exc,
                                        trade_intent_id=trade_intent_id, trade_event=trade_event,
                                        event_signal_ts_ms=event_signal_ts_ms,
                                        requested_start_ms=requested_start_ms,
                                        requested_end_ms=requested_end_ms)
                raise
            if not next_cursor:
                return
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        exc = RuntimeError("okx_history_page_limit")
        self._record_incomplete(trade_id, "okx", path, kind, instrument,
                                start, end, cursor, signal_ts_ms, exc,
                                trade_intent_id=trade_intent_id, trade_event=trade_event,
                                event_signal_ts_ms=event_signal_ts_ms,
                                requested_start_ms=requested_start_ms,
                                requested_end_ms=requested_end_ms)
        raise exc

    def _funding_attribution(self, trade_id: str, base_coin: str, side: str,
                             event_ms: int, *, venue: str, instrument: str,
                             trade_event: str) -> dict[str, Any]:
        candidates = self.journal.funding_trade_candidates(
            base_coin=base_coin, side=side, event_ms=event_ms,
            venue=venue, instrument=instrument, trade_event=trade_event,
        )
        return {
            "trade_id": None,
            "attribution_status": "unassigned_account_level_funding",
            "attribution_basis": "persisted_successful_bot_trade_observations_and_matched_exchange_fill_times",
            "account_scope_limitation": "external_positions_or_activity_not_authorized_or_observed",
            "attribution_candidates": candidates["candidate_trade_ids"],
            "attribution_unknown_boundary_trade_ids": candidates["unknown_boundary_trade_ids"],
            "attribution_equal_boundary_trade_ids": candidates["equal_boundary_trade_ids"],
            "funding_venue": venue,
            "funding_instrument": instrument,
        }

    def _record_incomplete(self, trade_id: str, venue: str, path: str, kind: str,
                           instrument: str, start: int, end: int, cursor: str,
                           signal_ts_ms: int, error: Exception, *,
                           trade_intent_id: str = "", trade_event: str = "",
                           event_signal_ts_ms: Optional[int] = None,
                           requested_start_ms: Optional[int] = None,
                           requested_end_ms: Optional[int] = None) -> None:
        observed = self.now_ms()
        event_signal_ts_ms = signal_ts_ms if event_signal_ts_ms is None else event_signal_ts_ms
        requested_start_ms = start if requested_start_ms is None else requested_start_ms
        requested_end_ms = end if requested_end_ms is None else requested_end_ms
        key = _coverage_key(trade_id, venue, path, kind, instrument, start, end, cursor,
                            trade_intent_id, trade_event, requested_start_ms,
                            requested_end_ms)
        failed = _coverage_row(key + "|incomplete|" + str(observed), trade_id, venue,
                               path, kind, instrument, start, end, cursor, "", False, observed,
                               trade_intent_id, trade_event, event_signal_ts_ms,
                               requested_start_ms, requested_end_ms)
        failed.update({"status": "incomplete", "error_type": type(error).__name__})
        try:
            self.journal.append_trade_history_page([], failed, signal_ts_ms=signal_ts_ms)
        except Exception:
            pass


def _windows(start: int, end: int, width: int, *, maximum: int = MAX_BYBIT_WINDOWS) -> list[tuple[int, int]]:
    if end < start or (end - start) // width + 1 > maximum:
        raise RuntimeError("history_window_limit")
    result = []
    left = int(start)
    while left <= end:
        right = min(end, left + width - 1)
        result.append((left, right))
        left = right + 1
    return result


def _coverage_key(trade_id: str, venue: str, path: str, kind: str, instrument: str,
                  start: int, end: int, cursor: str, trade_intent_id: str = "",
                  trade_event: str = "", requested_start_ms: Optional[int] = None,
                  requested_end_ms: Optional[int] = None) -> str:
    return "|".join((trade_id, venue, path, kind, instrument, str(start), str(end),
                     str(start if requested_start_ms is None else requested_start_ms),
                     str(end if requested_end_ms is None else requested_end_ms),
                     trade_intent_id, trade_event, cursor))


def _coverage_row(key: str, trade_id: str, venue: str, path: str, kind: str,
                  instrument: str, start: int, end: int, cursor: str,
                  next_cursor: str, complete: bool, observed: int,
                  trade_intent_id: str = "", trade_event: str = "",
                  event_signal_ts_ms: Optional[int] = None,
                  requested_start_ms: Optional[int] = None,
                  requested_end_ms: Optional[int] = None) -> dict[str, Any]:
    requested_start_ms = start if requested_start_ms is None else requested_start_ms
    requested_end_ms = end if requested_end_ms is None else requested_end_ms
    return {
        "record_type": "trade_history_coverage", "status": "complete" if complete else "page_saved",
        "coverage_key": key, "trade_id": trade_id, "venue": venue,
        "source_endpoint": path, "history_kind": kind, "instrument": instrument,
        "start_ms": start, "end_ms": end, "cursor_in": cursor,
        "cursor_out": next_cursor, "first_observed_wall_ms": observed,
        "requested_start_ms": requested_start_ms, "requested_end_ms": requested_end_ms,
        "trade_intent_id": trade_intent_id, "trade_event": trade_event,
        "event_signal_ts_ms": event_signal_ts_ms,
    }
