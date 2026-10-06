"""Set account leverage to 1 once, during synthetic-roll live warmup.

This is not an order. It does not market-close or flatten. It is not called
from ``place`` and it is not inside the measured ``ws_send`` interval.

The only leverage this module posts is ``"1"``. A response that is not 1 is
not recorded, and the place path must not send.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.request import Request

from app.bot.private.order_sign import LiveCredentials
from app.bot.private.rest_readonly import (
    OKX_REST_ACCEPT,
    OKX_REST_USER_AGENT,
    _okx_sign,
)
from app.bot.private.venue import VenueEndpoints

OKX_SET_LEVERAGE_PATH = "/api/v5/account/set-leverage"
BYBIT_SET_LEVERAGE_PATH = "/v5/position/set-leverage"
LEVER_ONE = "1"

# Bybit: leverage already equals the value we posted.
_BYBIT_UNCHANGED = 110043

PostFn = Callable[[str, Mapping[str, str], str], tuple[int, Mapping[str, Any]]]


@dataclass(frozen=True)
class LeverageTarget:
    coin: str
    okx_symbol: str
    bybit_symbol: str


def _is_one(raw: object) -> bool:
    if raw is None or raw == "" or isinstance(raw, bool):
        return False
    try:
        return Decimal(str(raw)) == Decimal(LEVER_ONE)
    except Exception:  # noqa: BLE001 — venue field boundary
        return False


def _assert_body_is_lever_one(body: str) -> dict[str, Any]:
    data = json.loads(body)
    if not isinstance(data, dict):
        raise RuntimeError("refusing lever above 1")
    if "lever" in data:
        if data["lever"] != LEVER_ONE or not _is_one(data["lever"]):
            raise RuntimeError("refusing lever above 1")
    elif "buyLeverage" in data or "sellLeverage" in data:
        if data.get("buyLeverage") != LEVER_ONE or data.get("sellLeverage") != LEVER_ONE:
            raise RuntimeError("refusing lever above 1")
    else:
        raise RuntimeError("leverage body missing lever")
    return data


def _okx_body(symbol: str) -> str:
    body = json.dumps(
        {"instId": symbol, "lever": LEVER_ONE, "mgnMode": "cross"},
        separators=(",", ":"),
    )
    _assert_body_is_lever_one(body)
    return body


def _bybit_body(symbol: str) -> str:
    body = json.dumps(
        {
            "category": "linear",
            "symbol": symbol,
            "buyLeverage": LEVER_ONE,
            "sellLeverage": LEVER_ONE,
        },
        separators=(",", ":"),
    )
    _assert_body_is_lever_one(body)
    return body


def _http_post(url: str, headers: Mapping[str, str], body: str) -> tuple[int, dict]:
    _assert_body_is_lever_one(body)
    req = Request(
        url,
        data=body.encode("utf-8"),
        headers={str(k): str(v) for k, v in headers.items()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = int(getattr(resp, "status", None) or resp.getcode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = int(exc.code)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("leverage_set_failed") from exc
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError("leverage_set_failed") from exc
    if not isinstance(data, dict):
        raise RuntimeError("leverage_set_failed")
    return status, data


def _okx_headers(
    *,
    credentials: LiveCredentials,
    path: str,
    body: str,
) -> dict[str, str]:
    import time

    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    sign = _okx_sign(
        secret=credentials.api_secret,
        timestamp=ts,
        method="POST",
        request_path=path,
        body=body,
    )
    return {
        "OK-ACCESS-KEY": credentials.api_key,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": credentials.passphrase or "",
        "Content-Type": "application/json",
        "Accept": OKX_REST_ACCEPT,
        "User-Agent": OKX_REST_USER_AGENT,
    }


def _bybit_headers(
    *,
    credentials: LiveCredentials,
    body: str,
    recv_window: int = 5000,
) -> dict[str, str]:
    import hashlib
    import hmac
    import time

    ts = str(int(time.time() * 1000))
    payload = f"{ts}{credentials.api_key}{recv_window}{body}"
    sign = hmac.new(
        credentials.api_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-BAPI-API-KEY": credentials.api_key,
        "X-BAPI-SIGN": sign,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": str(recv_window),
        "Content-Type": "application/json",
    }


def _okx_confirmed(data: Mapping[str, Any]) -> bool:
    if str(data.get("code")) != "0":
        return False
    rows = data.get("data") or []
    if not rows or not isinstance(rows[0], Mapping):
        return False
    return _is_one(rows[0].get("lever"))


def _bybit_confirmed(data: Mapping[str, Any]) -> bool:
    ret = data.get("retCode")
    if ret != 0 and str(ret) != "0" and ret != _BYBIT_UNCHANGED and str(ret) != str(_BYBIT_UNCHANGED):
        return False
    result = data.get("result")
    if isinstance(result, Mapping):
        for key in ("leverage", "buyLeverage", "sellLeverage"):
            if key in result and result.get(key) not in (None, "") and not _is_one(result.get(key)):
                return False
    return True


def _live_endpoints(endpoints: VenueEndpoints) -> bool:
    if endpoints.venue != "live" or endpoints.okx_simulated_trading:
        return False
    if "testnet" in endpoints.bybit_rest or "testnet" in endpoints.okx_rest:
        return False
    return True


def set_leverage_one(
    targets: Sequence[LeverageTarget],
    *,
    okx_credentials: LiveCredentials,
    bybit_credentials: LiveCredentials,
    endpoints: VenueEndpoints,
    post_fn: Optional[PostFn] = None,
) -> dict[tuple[str, str], str]:
    """POST leverage 1 for each coin. Returns only symbols confirmed at 1.

    ``post_fn`` is injected in tests. There is no ``lever`` argument: this
    function cannot request a value above 1.
    """
    if not _live_endpoints(endpoints):
        return {}
    if not okx_credentials.passphrase:
        return {}
    poster = post_fn or _http_post
    confirmed: dict[tuple[str, str], str] = {}
    for target in targets:
        okx_symbol = str(target.okx_symbol).strip()
        bybit_symbol = str(target.bybit_symbol).strip()
        if okx_symbol:
            _confirm_okx(
                symbol=okx_symbol,
                credentials=okx_credentials,
                endpoints=endpoints,
                poster=poster,
                confirmed=confirmed,
            )
        if bybit_symbol:
            _confirm_bybit(
                symbol=bybit_symbol,
                credentials=bybit_credentials,
                endpoints=endpoints,
                poster=poster,
                confirmed=confirmed,
            )
    return confirmed


def _confirm_okx(
    *,
    symbol: str,
    credentials: LiveCredentials,
    endpoints: VenueEndpoints,
    poster: PostFn,
    confirmed: dict[tuple[str, str], str],
) -> None:
    path = OKX_SET_LEVERAGE_PATH
    body = _okx_body(symbol)
    headers = _okx_headers(credentials=credentials, path=path, body=body)
    url = f"{endpoints.okx_rest}{path}"
    try:
        _status, data = poster(url, headers, body)
    except Exception:  # noqa: BLE001 — fail closed, no send later
        return
    if _okx_confirmed(data):
        confirmed[("okx", symbol)] = LEVER_ONE


def _confirm_bybit(
    *,
    symbol: str,
    credentials: LiveCredentials,
    endpoints: VenueEndpoints,
    poster: PostFn,
    confirmed: dict[tuple[str, str], str],
) -> None:
    path = BYBIT_SET_LEVERAGE_PATH
    body = _bybit_body(symbol)
    headers = _bybit_headers(credentials=credentials, body=body)
    url = f"{endpoints.bybit_rest}{path}"
    try:
        _status, data = poster(url, headers, body)
    except Exception:  # noqa: BLE001 — fail closed, no send later
        return
    if _bybit_confirmed(data):
        confirmed[("bybit", symbol)] = LEVER_ONE
