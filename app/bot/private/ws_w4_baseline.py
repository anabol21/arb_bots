"""W4 flat baseline: position + open orders must be flat before send.

Injectable probe for tests. Live uses signed GET on an allowlisted path set
only; returns categorical flat / not_flat — never logs sizes/IDs/account data.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Optional, Protocol

from app.bot.private.order_sign import LiveCredentials
from app.bot.private.venue import VenueEndpoints, endpoints_for_venue

# Allowlist only — never place/cancel/amend.
_BYBIT_POS = "/v5/position/list"
_BYBIT_OPEN = "/v5/order/realtime"
_OKX_POS = "/api/v5/account/positions"
_OKX_OPEN = "/api/v5/trade/orders-pending"


class BaselineError(RuntimeError):
    """Non-flat or inconclusive position/open-order baseline."""


@dataclass(frozen=True)
class FlatBaselineResult:
    exchange: str
    symbol: str
    flat: bool
    open_orders_flat: bool
    position_flat: bool

    @property
    def ok(self) -> bool:
        return bool(self.flat and self.open_orders_flat and self.position_flat)


class FlatBaselinePort(Protocol):
    def check(self, *, exchange: str, symbol: str) -> FlatBaselineResult:
        ...


@dataclass
class FakeFlatBaseline:
    """Test-only baseline. Default flat; flip flags to block."""

    flat: bool = True
    open_orders_flat: bool = True
    position_flat: bool = True

    def check(self, *, exchange: str, symbol: str) -> FlatBaselineResult:
        ok = self.flat and self.open_orders_flat and self.position_flat
        return FlatBaselineResult(
            exchange=exchange,
            symbol=symbol,
            flat=ok,
            open_orders_flat=self.open_orders_flat,
            position_flat=self.position_flat,
        )


BaselineProbeFn = Callable[..., FlatBaselineResult]


def _dec(raw: Any) -> Decimal:
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise BaselineError("baseline numeric parse failed") from exc


def _http_get_json(url: str, headers: Mapping[str, str], *, timeout_sec: float) -> Mapping[str, Any]:
    req = urllib.request.Request(url, headers=dict(headers), method="GET")
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:  # noqa: S310
        body = resp.read()
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, Mapping):
        raise BaselineError("baseline response malformed")
    return data


def _bybit_signed_get(
    *,
    credentials: LiveCredentials,
    base: str,
    path: str,
    query: str,
    timeout_sec: float = 15.0,
    http_get_json: Optional[Callable[..., Mapping[str, Any]]] = None,
) -> Mapping[str, Any]:
    if path not in {_BYBIT_POS, _BYBIT_OPEN}:
        raise BaselineError("bybit baseline path not allowlisted")
    ts = str(int(time.time() * 1000))
    recv = 5000
    payload = f"{ts}{credentials.api_key}{recv}{query}"
    sign = hmac.new(
        credentials.api_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    url = f"{base}{path}"
    if query:
        url = f"{url}?{query}"
    headers = {
        "X-BAPI-API-KEY": credentials.api_key,
        "X-BAPI-SIGN": sign,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": str(recv),
        "Content-Type": "application/json",
    }
    getter = http_get_json or _http_get_json
    return getter(url, headers, timeout_sec=timeout_sec)


def _okx_signed_get(
    *,
    credentials: LiveCredentials,
    base: str,
    path_with_query: str,
    timeout_sec: float = 15.0,
    http_get_json: Optional[Callable[..., Mapping[str, Any]]] = None,
) -> Mapping[str, Any]:
    """Signed OKX GET using R1 live header/signing shape (UA/Accept/ISO ts).

    Path allowlist stays local so trade/orders-pending remains reachable without
    weakening ``rest_readonly`` deny fragments.
    """
    from app.bot.private.rest_readonly import (
        assert_okx_headers_for_venue,
        build_okx_readonly_headers,
    )

    path_only = path_with_query.split("?", 1)[0]
    if path_only not in {_OKX_POS, _OKX_OPEN}:
        raise BaselineError("okx baseline path not allowlisted")
    if not credentials.passphrase:
        raise BaselineError("okx baseline requires passphrase")
    headers = build_okx_readonly_headers(
        api_key=credentials.api_key,
        api_secret=credentials.api_secret,
        passphrase=credentials.passphrase,
        path=path_with_query,
        simulated_trading=False,
    )
    assert_okx_headers_for_venue(headers, "live")
    getter = http_get_json or _http_get_json
    return getter(f"{base}{path_with_query}", headers, timeout_sec=timeout_sec)


def _bybit_position_flat(data: Mapping[str, Any], symbol: str) -> bool:
    if data.get("retCode") not in (0, "0"):
        raise BaselineError("bybit position GET rejected")
    rows = ((data.get("result") or {}).get("list")) or []
    if not isinstance(rows, list):
        raise BaselineError("bybit position malformed")
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("symbol") or "") != symbol:
            continue
        size = abs(_dec(row.get("size") or "0"))
        if size != 0:
            return False
    return True


def _bybit_open_orders_flat(data: Mapping[str, Any], symbol: str) -> bool:
    if data.get("retCode") not in (0, "0"):
        raise BaselineError("bybit open-order GET rejected")
    rows = ((data.get("result") or {}).get("list")) or []
    if not isinstance(rows, list):
        raise BaselineError("bybit open-order malformed")
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("symbol") or "") != symbol:
            continue
        # Any matching open row → not flat.
        return False
    return True


def _okx_position_flat(data: Mapping[str, Any], symbol: str) -> bool:
    if str(data.get("code", "")) != "0":
        raise BaselineError("okx position GET rejected")
    rows = data.get("data") or []
    if not isinstance(rows, list):
        raise BaselineError("okx position malformed")
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("instId") or "") != symbol:
            continue
        pos = abs(_dec(row.get("pos") or "0"))
        if pos != 0:
            return False
    return True


def _okx_open_orders_flat(data: Mapping[str, Any], symbol: str) -> bool:
    if str(data.get("code", "")) != "0":
        raise BaselineError("okx open-order GET rejected")
    rows = data.get("data") or []
    if not isinstance(rows, list):
        raise BaselineError("okx open-order malformed")
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("instId") or "") != symbol:
            continue
        return False
    return True


@dataclass
class SignedRestFlatBaseline:
    """Signed GET position + open-orders emptiness (categorical)."""

    exchange: str
    credentials: LiveCredentials
    endpoints: Optional[VenueEndpoints] = None
    probe_fn: Optional[BaselineProbeFn] = None
    # Test-only: inject signed-response transport (never used by default CLI).
    http_get_json: Optional[Callable[..., Mapping[str, Any]]] = None

    def check(self, *, exchange: str, symbol: str) -> FlatBaselineResult:
        if exchange != self.exchange:
            raise BaselineError("baseline exchange mismatch")
        if self.probe_fn is not None:
            return self.probe_fn(
                exchange=exchange, symbol=symbol, credentials=self.credentials
            )
        ep = self.endpoints or endpoints_for_venue("live")
        try:
            if exchange == "bybit":
                pos = _bybit_signed_get(
                    credentials=self.credentials,
                    base=ep.bybit_rest,
                    path=_BYBIT_POS,
                    query=f"category=linear&symbol={symbol}&settleCoin=USDT",
                    http_get_json=self.http_get_json,
                )
                opens = _bybit_signed_get(
                    credentials=self.credentials,
                    base=ep.bybit_rest,
                    path=_BYBIT_OPEN,
                    query=f"category=linear&symbol={symbol}&openOnly=0&limit=50",
                    http_get_json=self.http_get_json,
                )
                pos_flat = _bybit_position_flat(pos, symbol)
                open_flat = _bybit_open_orders_flat(opens, symbol)
            else:
                pos = _okx_signed_get(
                    credentials=self.credentials,
                    base=ep.okx_rest,
                    path_with_query=f"{_OKX_POS}?instId={symbol}",
                    http_get_json=self.http_get_json,
                )
                opens = _okx_signed_get(
                    credentials=self.credentials,
                    base=ep.okx_rest,
                    path_with_query=f"{_OKX_OPEN}?instId={symbol}&instType=SWAP",
                    http_get_json=self.http_get_json,
                )
                pos_flat = _okx_position_flat(pos, symbol)
                open_flat = _okx_open_orders_flat(opens, symbol)
        except (BaselineError, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise BaselineError(f"baseline check failed: {type(exc).__name__}") from exc
        return FlatBaselineResult(
            exchange=exchange,
            symbol=symbol,
            flat=pos_flat and open_flat,
            open_orders_flat=open_flat,
            position_flat=pos_flat,
        )


def assert_flat(result: FlatBaselineResult) -> None:
    if not result.ok:
        raise BaselineError("position or open orders not flat")


def plan_runtime_exchange(plan: Any) -> str:
    """Map OrderPlan.venue → runtime exchange id (bybit|okx)."""
    venue = str(getattr(plan, "venue", "") or "").lower()
    if venue.startswith("okx"):
        return "okx"
    return "bybit"


def plan_matches_runtime_exchange(plan: Any, runtime_exchange: str) -> bool:
    """True when recovery WS cancel may use this runtime's trade socket."""
    return plan_runtime_exchange(plan) == str(runtime_exchange).lower()


@dataclass
class SignedRestOrderStateRecon:
    """GET-only order-state recon for W4 recovery (never place/cancel POST).

    - No open orders for plan symbol → ``CANCELLED`` (observed terminal).
    - Open order present → ``WORKING``.
    - HTTP/parse/credential failure → ``UNKNOWN`` (never invent fill).
    """

    bybit_credentials: Optional[LiveCredentials] = None
    okx_credentials: Optional[LiveCredentials] = None
    endpoints: Optional[VenueEndpoints] = None
    http_get_json: Optional[Callable[..., Mapping[str, Any]]] = None
    # When True, also require position flat before CANCELLED.
    require_position_flat: bool = False
    # Test-only probe: return snapshot without network.
    probe_fn: Optional[Callable[[Any], Any]] = None

    def get(self, plan: Any) -> Any:
        from app.bot.private.order_lease import OrderStateSnapshot

        if self.probe_fn is not None:
            try:
                return self.probe_fn(plan)
            except Exception:  # noqa: BLE001
                return OrderStateSnapshot.UNKNOWN

        exchange = plan_runtime_exchange(plan)
        symbol = str(getattr(plan, "symbol", "") or "")
        if not symbol:
            return OrderStateSnapshot.UNKNOWN
        ep = self.endpoints or endpoints_for_venue("live")
        try:
            if exchange == "bybit":
                creds = self.bybit_credentials
                if creds is None:
                    return OrderStateSnapshot.UNKNOWN
                opens = _bybit_signed_get(
                    credentials=creds,
                    base=ep.bybit_rest,
                    path=_BYBIT_OPEN,
                    query=f"category=linear&symbol={symbol}&openOnly=0&limit=50",
                    http_get_json=self.http_get_json,
                )
                open_flat = _bybit_open_orders_flat(opens, symbol)
                if not open_flat:
                    return OrderStateSnapshot.WORKING
                if self.require_position_flat:
                    pos = _bybit_signed_get(
                        credentials=creds,
                        base=ep.bybit_rest,
                        path=_BYBIT_POS,
                        query=f"category=linear&symbol={symbol}&settleCoin=USDT",
                        http_get_json=self.http_get_json,
                    )
                    if not _bybit_position_flat(pos, symbol):
                        return OrderStateSnapshot.WORKING
                return OrderStateSnapshot.CANCELLED

            creds = self.okx_credentials
            if creds is None:
                return OrderStateSnapshot.UNKNOWN
            opens = _okx_signed_get(
                credentials=creds,
                base=ep.okx_rest,
                path_with_query=f"{_OKX_OPEN}?instId={symbol}&instType=SWAP",
                http_get_json=self.http_get_json,
            )
            open_flat = _okx_open_orders_flat(opens, symbol)
            if not open_flat:
                return OrderStateSnapshot.WORKING
            if self.require_position_flat:
                pos = _okx_signed_get(
                    credentials=creds,
                    base=ep.okx_rest,
                    path_with_query=f"{_OKX_POS}?instId={symbol}",
                    http_get_json=self.http_get_json,
                )
                if not _okx_position_flat(pos, symbol):
                    return OrderStateSnapshot.WORKING
            return OrderStateSnapshot.CANCELLED
        except (BaselineError, urllib.error.URLError, TimeoutError, OSError, ValueError, TypeError):
            return OrderStateSnapshot.UNKNOWN


def build_signed_rest_order_recon(
    *,
    bybit_credentials: Optional[LiveCredentials] = None,
    okx_credentials: Optional[LiveCredentials] = None,
    endpoints: Optional[VenueEndpoints] = None,
    http_get_json: Optional[Callable[..., Mapping[str, Any]]] = None,
    require_position_flat: bool = False,
) -> SignedRestOrderStateRecon:
    """Factory for production/tests — GET open-order recon only."""
    return SignedRestOrderStateRecon(
        bybit_credentials=bybit_credentials,
        okx_credentials=okx_credentials,
        endpoints=endpoints,
        http_get_json=http_get_json,
        require_position_flat=require_position_flat,
    )
