"""Concrete REST seed/reseed for private WS (signed GET only; no order send).

Uses existing read-only account probes plus signed position/instrument GETs.
Returns categorical matched/inconclusive only — never account/order values.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import urllib.error
from dataclasses import dataclass
from typing import Any, Optional

from app.bot.private.order_sign import LiveCredentials
from app.bot.private.rest_readonly import (
    _assert_path_readonly,
    _http_get,
    assert_okx_headers_for_venue,
    build_okx_readonly_headers,
    normalize_http_outcome,
    probe_bybit_wallet,
    probe_okx_balance,
)
from app.bot.private.venue import VenueEndpoints, endpoints_for_venue
from app.bot.private.ws_private import RestReseedPort, RestReseedResult

# Signed GET paths only (no place/cancel/amend).
BYBIT_POSITION_PATH = "/v5/position/list"
BYBIT_INSTRUMENT_PATH = "/v5/market/instruments-info"
OKX_POSITION_PATH = "/api/v5/account/positions"
OKX_INSTRUMENT_PATH = "/api/v5/account/instruments"


def _bybit_get(
    *,
    credentials: LiveCredentials,
    endpoints: VenueEndpoints,
    path: str,
    query: str,
    recv_window: int = 5000,
    timeout_sec: float = 15.0,
    http_get: Optional[Any] = None,
) -> tuple[bool, Optional[int], Optional[str]]:
    _assert_path_readonly(path)
    ts = str(int(time.time() * 1000))
    payload = f"{ts}{credentials.api_key}{recv_window}{query}"
    sign = hmac.new(
        credentials.api_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    url = f"{endpoints.bybit_rest}{path}"
    if query:
        url = f"{url}?{query}"
    headers = {
        "X-BAPI-API-KEY": credentials.api_key,
        "X-BAPI-SIGN": sign,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": str(recv_window),
        "Content-Type": "application/json",
    }
    getter = http_get or _http_get
    try:
        status, data = getter(url, headers, timeout_sec=timeout_sec)
    except (ValueError, urllib.error.URLError, TimeoutError, OSError):
        return False, None, None
    ret = data.get("retCode")
    code_s = None if ret is None else str(ret)
    ok = status == 200 and (ret == 0 or code_s == "0")
    return ok, status, code_s


def _okx_get(
    *,
    credentials: LiveCredentials,
    endpoints: VenueEndpoints,
    path: str,
    timeout_sec: float = 15.0,
    http_get: Optional[Any] = None,
) -> tuple[bool, Optional[int], Optional[str]]:
    if not credentials.passphrase:
        return False, None, None
    _assert_path_readonly(path)
    headers = build_okx_readonly_headers(
        api_key=credentials.api_key,
        api_secret=credentials.api_secret,
        passphrase=credentials.passphrase,
        path=path,
        simulated_trading=endpoints.okx_simulated_trading,
    )
    assert_okx_headers_for_venue(headers, endpoints.venue)
    url = f"{endpoints.okx_rest}{path}"
    getter = http_get or _http_get
    try:
        status, data = getter(url, headers, timeout_sec=timeout_sec)
    except (ValueError, urllib.error.URLError, TimeoutError, OSError):
        return False, None, None
    code = data.get("code")
    code_s = None if code is None else str(code)
    ok = status == 200 and code_s == "0"
    return ok, status, code_s


@dataclass
class SignedRestReseedAdapter:
    """REST seed/reseed via signed GET account + position + instrument.

    Never journals raw responses, balances, or order/account identifiers.
    """

    credentials: LiveCredentials
    exchange: str  # bybit | okx
    endpoints: VenueEndpoints
    timeout_sec: float = 15.0
    # Optional injectable GET probes for fake/no-network tests.
    _probe_fn: Optional[Any] = None
    # Test-only: replace low-level signed HTTP transport (url, headers, timeout)→(status, data).
    _http_get_fn: Optional[Any] = None
    # Test-only: replace account wallet/balance probe (returns object with .ok).
    _account_probe_fn: Optional[Any] = None

    def reseed(
        self,
        *,
        venue: str,
        environment: str,
        reconnect_generation: int,
        symbol_alias: str,
    ) -> RestReseedResult:
        del reconnect_generation  # categorical only; generation tracked by runtime
        if venue != self.exchange:
            return RestReseedResult(matched=False, inconclusive=True)
        if environment != "live" or self.endpoints.venue != "live":
            return RestReseedResult(matched=False, inconclusive=True)
        if self._probe_fn is not None:
            return self._probe_fn(
                venue=venue,
                environment=environment,
                symbol_alias=symbol_alias,
            )
        try:
            account_ok = self._account_ok()
            position_ok = self._position_ok(symbol_alias)
            instrument_ok = self._instrument_ok(symbol_alias)
        except Exception:  # noqa: BLE001 — never leak; categorical only
            return RestReseedResult(matched=False, inconclusive=True)
        if account_ok and position_ok and instrument_ok:
            return RestReseedResult(matched=True, inconclusive=False)
        return RestReseedResult(matched=False, inconclusive=True)

    def _account_ok(self) -> bool:
        if self._account_probe_fn is not None:
            res = self._account_probe_fn()
            return bool(getattr(res, "ok", res))
        if self.exchange == "bybit":
            res = probe_bybit_wallet(
                api_key=self.credentials.api_key,
                api_secret=self.credentials.api_secret,
                endpoints=self.endpoints,
                timeout_sec=self.timeout_sec,
            )
            return bool(res.ok)
        res = probe_okx_balance(
            api_key=self.credentials.api_key,
            api_secret=self.credentials.api_secret,
            passphrase=self.credentials.passphrase or "",
            endpoints=self.endpoints,
            timeout_sec=self.timeout_sec,
        )
        return bool(res.ok)

    def _position_ok(self, symbol_alias: str) -> bool:
        if self.exchange == "bybit":
            query = f"category=linear&symbol={symbol_alias}"
            ok, status, code = _bybit_get(
                credentials=self.credentials,
                endpoints=self.endpoints,
                path=BYBIT_POSITION_PATH,
                query=query,
                timeout_sec=self.timeout_sec,
                http_get=self._http_get_fn,
            )
            _ = normalize_http_outcome(http_status=status, exchange_code=code, ok=ok)
            return ok
        path = f"{OKX_POSITION_PATH}?instId={symbol_alias}"
        ok, status, code = _okx_get(
            credentials=self.credentials,
            endpoints=self.endpoints,
            path=path,
            timeout_sec=self.timeout_sec,
            http_get=self._http_get_fn,
        )
        _ = normalize_http_outcome(http_status=status, exchange_code=code, ok=ok)
        return ok

    def _instrument_ok(self, symbol_alias: str) -> bool:
        if self.exchange == "bybit":
            query = f"category=linear&symbol={symbol_alias}"
            ok, status, code = _bybit_get(
                credentials=self.credentials,
                endpoints=self.endpoints,
                path=BYBIT_INSTRUMENT_PATH,
                query=query,
                timeout_sec=self.timeout_sec,
                http_get=self._http_get_fn,
            )
            _ = normalize_http_outcome(http_status=status, exchange_code=code, ok=ok)
            return ok
        path = f"{OKX_INSTRUMENT_PATH}?instType=SWAP&instId={symbol_alias}"
        ok, status, code = _okx_get(
            credentials=self.credentials,
            endpoints=self.endpoints,
            path=path,
            timeout_sec=self.timeout_sec,
            http_get=self._http_get_fn,
        )
        _ = normalize_http_outcome(http_status=status, exchange_code=code, ok=ok)
        return ok


def build_signed_rest_reseed(
    *,
    exchange: str,
    credentials: LiveCredentials,
    endpoints: Optional[VenueEndpoints] = None,
    probe_fn: Optional[Any] = None,
) -> RestReseedPort:
    ep = endpoints if endpoints is not None else endpoints_for_venue("live")
    return SignedRestReseedAdapter(
        credentials=credentials,
        exchange=exchange,
        endpoints=ep,
        _probe_fn=probe_fn,
    )
