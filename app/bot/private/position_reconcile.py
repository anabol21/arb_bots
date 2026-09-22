"""Read-only restart reconciliation for the Gear 2.2 K=1 spread slot.

The trade journal describes the position the manager believes it owns.  Four
signed GETs (positions + open orders on both venues) prove whether that state
still matches reality before live decisions are enabled.  Results expose only
categorical sides/health; quantities and account identifiers are never logged.
"""

from __future__ import annotations

import urllib.error
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Optional, Sequence

from app.bot.private.order_sign import LiveCredentials
from app.bot.private.venue import VenueEndpoints, endpoints_for_venue
from app.bot.private.ws_w4_baseline import (
    BaselineError,
    _BYBIT_OPEN,
    _BYBIT_POS,
    _OKX_OPEN,
    _OKX_POS,
    _bybit_signed_get,
    _okx_signed_get,
)
from app.bot.theta_trade_manager import OpenPosition


_FLAT = "flat"
_BUY = "buy"
_SELL = "sell"
_AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class RestartPositionResult:
    matched: bool
    reason: str
    bybit_state: str
    okx_state: str
    bybit_open_orders_flat: bool
    okx_open_orders_flat: bool


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BaselineError("restart position numeric parse failed") from exc


def _one_state(sides: set[str]) -> str:
    if not sides:
        return _FLAT
    if len(sides) != 1:
        return _AMBIGUOUS
    return next(iter(sides))


def _bybit_pool_state(
    data: Mapping[str, Any],
    *,
    symbols: set[str],
) -> tuple[str, Optional[str]]:
    if data.get("retCode") not in (0, "0"):
        raise BaselineError("bybit position GET rejected")
    rows = ((data.get("result") or {}).get("list")) or []
    if not isinstance(rows, list):
        raise BaselineError("bybit position malformed")
    sides: set[str] = set()
    active_symbol: Optional[str] = None
    for row in rows:
        if not isinstance(row, Mapping):
            raise BaselineError("bybit position row malformed")
        symbol = str(row.get("symbol") or "")
        if symbol not in symbols or abs(_decimal(row.get("size") or "0")) == 0:
            continue
        side_raw = str(row.get("side") or "").strip().lower()
        if side_raw == "buy":
            side = _BUY
        elif side_raw == "sell":
            side = _SELL
        else:
            side = _AMBIGUOUS
        sides.add(side)
        if active_symbol is not None and active_symbol != symbol:
            sides.add(_AMBIGUOUS)
        active_symbol = symbol
    return _one_state(sides), active_symbol


def _okx_pool_state(
    data: Mapping[str, Any],
    *,
    symbols: set[str],
) -> tuple[str, Optional[str]]:
    if str(data.get("code", "")) != "0":
        raise BaselineError("okx position GET rejected")
    rows = data.get("data") or []
    if not isinstance(rows, list):
        raise BaselineError("okx position malformed")
    sides: set[str] = set()
    active_symbol: Optional[str] = None
    for row in rows:
        if not isinstance(row, Mapping):
            raise BaselineError("okx position row malformed")
        symbol = str(row.get("instId") or "")
        if symbol not in symbols:
            continue
        qty = _decimal(row.get("pos") or "0")
        if qty == 0:
            continue
        pos_side = str(row.get("posSide") or "net").strip().lower()
        if pos_side == "long":
            side = _BUY
        elif pos_side == "short":
            side = _SELL
        elif pos_side == "net":
            side = _BUY if qty > 0 else _SELL
        else:
            side = _AMBIGUOUS
        sides.add(side)
        if active_symbol is not None and active_symbol != symbol:
            sides.add(_AMBIGUOUS)
        active_symbol = symbol
    return _one_state(sides), active_symbol


def _bybit_pool_orders_flat(data: Mapping[str, Any], *, symbols: set[str]) -> bool:
    if data.get("retCode") not in (0, "0"):
        raise BaselineError("bybit open-order GET rejected")
    result = data.get("result") or {}
    rows = result.get("list") or []
    if not isinstance(rows, list):
        raise BaselineError("bybit open-order malformed")
    if str(result.get("nextPageCursor") or "").strip():
        raise BaselineError("bybit open-order pagination incomplete")
    return not any(
        isinstance(row, Mapping) and str(row.get("symbol") or "") in symbols
        for row in rows
    )


def _okx_pool_orders_flat(data: Mapping[str, Any], *, symbols: set[str]) -> bool:
    if str(data.get("code", "")) != "0":
        raise BaselineError("okx open-order GET rejected")
    rows = data.get("data") or []
    if not isinstance(rows, list):
        raise BaselineError("okx open-order malformed")
    return not any(
        isinstance(row, Mapping) and str(row.get("instId") or "") in symbols
        for row in rows
    )


@dataclass
class SignedRestRestartPositionReconciler:
    bybit_credentials: LiveCredentials
    okx_credentials: LiveCredentials
    endpoints: Optional[VenueEndpoints] = None
    http_get_json: Optional[Callable[..., Mapping[str, Any]]] = None

    def check(
        self,
        *,
        expected: Optional[OpenPosition],
        bybit_symbols: Sequence[str],
        okx_symbols: Sequence[str],
        expected_bybit_symbol: Optional[str] = None,
        expected_okx_symbol: Optional[str] = None,
    ) -> RestartPositionResult:
        bybit_set = {str(item) for item in bybit_symbols if str(item)}
        okx_set = {str(item) for item in okx_symbols if str(item)}
        if not bybit_set or not okx_set or len(bybit_set) != len(okx_set):
            return RestartPositionResult(
                False, "invalid_symbol_pool", _AMBIGUOUS, _AMBIGUOUS, False, False
            )
        endpoints = self.endpoints or endpoints_for_venue("live")
        try:
            bybit_positions = _bybit_signed_get(
                credentials=self.bybit_credentials,
                base=endpoints.bybit_rest,
                path=_BYBIT_POS,
                query="category=linear&settleCoin=USDT",
                http_get_json=self.http_get_json,
            )
            bybit_orders = _bybit_signed_get(
                credentials=self.bybit_credentials,
                base=endpoints.bybit_rest,
                path=_BYBIT_OPEN,
                query="category=linear&settleCoin=USDT&openOnly=0&limit=50",
                http_get_json=self.http_get_json,
            )
            okx_positions = _okx_signed_get(
                credentials=self.okx_credentials,
                base=endpoints.okx_rest,
                path_with_query=f"{_OKX_POS}?instType=SWAP",
                http_get_json=self.http_get_json,
            )
            okx_orders = _okx_signed_get(
                credentials=self.okx_credentials,
                base=endpoints.okx_rest,
                path_with_query=f"{_OKX_OPEN}?instType=SWAP",
                http_get_json=self.http_get_json,
            )
            bybit_state, bybit_symbol = _bybit_pool_state(
                bybit_positions, symbols=bybit_set
            )
            okx_state, okx_symbol = _okx_pool_state(okx_positions, symbols=okx_set)
            bybit_orders_flat = _bybit_pool_orders_flat(
                bybit_orders, symbols=bybit_set
            )
            okx_orders_flat = _okx_pool_orders_flat(okx_orders, symbols=okx_set)
        except (
            BaselineError,
            urllib.error.URLError,
            TimeoutError,
            OSError,
            ValueError,
            TypeError,
        ):
            return RestartPositionResult(
                False, "reconciliation_inconclusive", _AMBIGUOUS, _AMBIGUOUS, False, False
            )

        if not bybit_orders_flat or not okx_orders_flat:
            reason = "open_orders_present"
            matched = False
        elif expected is None:
            matched = bybit_state == _FLAT and okx_state == _FLAT
            reason = "matched" if matched else "expected_flat_mismatch"
        else:
            expected_bybit = _SELL if expected.side == "long" else _BUY
            expected_okx = _BUY if expected.side == "long" else _SELL
            if not expected_bybit_symbol or not expected_okx_symbol:
                return RestartPositionResult(
                    False,
                    "expected_symbols_missing",
                    bybit_state,
                    okx_state,
                    bybit_orders_flat,
                    okx_orders_flat,
                )
            matched = (
                bybit_state == expected_bybit
                and okx_state == expected_okx
                and bybit_symbol == expected_bybit_symbol
                and okx_symbol == expected_okx_symbol
            )
            reason = "matched" if matched else "expected_open_mismatch"
        return RestartPositionResult(
            matched=matched,
            reason=reason,
            bybit_state=bybit_state,
            okx_state=okx_state,
            bybit_open_orders_flat=bybit_orders_flat,
            okx_open_orders_flat=okx_orders_flat,
        )


__all__ = ["RestartPositionResult", "SignedRestRestartPositionReconciler"]
