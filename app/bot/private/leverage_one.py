"""Set account leverage to 1 once, during synthetic-roll live warmup.

This is not an order. It does not market-close or flatten. It is not called
from ``place`` and it is not inside the measured ``ws_send`` interval.

The only leverage this module posts is ``"1"``. A response that is not 1 is
not recorded, and the place path must not send.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlencode
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

LOG = logging.getLogger("bbot.private.leverage_one")
_REDACT_MARKERS = (
    "OK-ACCESS",
    "X-BAPI",
    "api_secret",
    "api_key",
    "passphrase",
    "Authorization",
    "BEGIN ",
)

PostFn = Callable[[str, Mapping[str, str], str], tuple[int, Mapping[str, Any]]]
GetFn = Callable[..., Mapping[str, Any]]

OKX_LEVERAGE_INFO_PATH = "/api/v5/account/leverage-info"
BYBIT_POSITION_PATH = "/v5/position/list"
BYBIT_OPEN_ORDERS_PATH = "/v5/order/realtime"
OKX_POSITION_PATH = "/api/v5/account/positions"
OKX_OPEN_ORDERS_PATH = "/api/v5/trade/orders-pending"
_MAX_PREFLIGHT_PAGES = 100


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


def _clip(value: object, *, limit: int = 160) -> str:
    if value is None:
        return "-"
    text = " ".join(str(value).split())
    if not text:
        return "-"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _safe_exc_text(exc: BaseException) -> str:
    text = _clip(exc)
    lowered = text.lower()
    if any(marker.lower() in lowered for marker in _REDACT_MARKERS):
        return "<redacted>"
    return text


def _exc_fields(exc: BaseException | None) -> tuple[str, str]:
    if exc is None:
        return "-", "-"
    root = exc
    while root.__cause__ is not None:
        root = root.__cause__
    if root is not exc:
        return f"{type(exc).__name__}/{type(root).__name__}", _safe_exc_text(root)
    return type(exc).__name__, _safe_exc_text(exc)


def _first_response_row(data: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not data:
        return None
    rows = data.get("data")
    if isinstance(rows, list) and rows and isinstance(rows[0], Mapping):
        return rows[0]
    result = data.get("result")
    if isinstance(result, Mapping):
        rows = result.get("list")
        if isinstance(rows, list) and rows and isinstance(rows[0], Mapping):
            return rows[0]
        return result
    return None


def _row_for_symbol(
    data: Mapping[str, Any] | None,
    *,
    symbol: str,
    symbol_key: str,
) -> Mapping[str, Any] | None:
    if not data or not symbol:
        return _first_response_row(data)
    rows: list[Any] = []
    raw_rows = data.get("data")
    if isinstance(raw_rows, list):
        rows = raw_rows
    else:
        result = data.get("result")
        if isinstance(result, Mapping) and isinstance(result.get("list"), list):
            rows = result["list"]
    matching = [
        row
        for row in rows
        if isinstance(row, Mapping) and str(row.get(symbol_key) or "") == symbol
    ]
    if matching:
        return matching[0]
    return _first_response_row(data)


def _field_from_row(row: Mapping[str, Any] | None, *keys: str) -> str:
    if not row:
        return "-"
    for key in keys:
        if row.get(key) not in (None, ""):
            return _clip(row.get(key), limit=32)
    return "-"


def _venue_code(data: Mapping[str, Any] | None) -> str:
    if not data:
        return "-"
    if data.get("code") not in (None, ""):
        return _clip(data.get("code"), limit=32)
    if data.get("retCode") not in (None, ""):
        return _clip(data.get("retCode"), limit=32)
    row = _first_response_row(data)
    if row and row.get("sCode") not in (None, ""):
        return _clip(row.get("sCode"), limit=32)
    return "-"


def _venue_msg(data: Mapping[str, Any] | None) -> str:
    if not data:
        return "-"
    for key in ("msg", "retMsg"):
        if data.get(key) not in (None, ""):
            return _clip(data.get(key))
    row = _first_response_row(data)
    if row and row.get("sMsg") not in (None, ""):
        return _clip(row.get("sMsg"))
    return "-"


def _log_leverage_step_failed(
    *,
    venue: str,
    step: str,
    symbol: str,
    reason: str,
    coin: str = "",
    http_status: object = None,
    response: Mapping[str, Any] | None = None,
    exc: BaseException | None = None,
    requested_leverage: str = LEVER_ONE,
    requested_mgn_mode: str = "-",
    observed_leverage: object = None,
    observed_mgn_mode: object = None,
) -> None:
    """Log a set/readback/preflight failure without secrets or auth headers."""
    if exc is not None and http_status is None:
        http_status = getattr(exc, "http_status", None)
    row = _first_response_row(response)
    if observed_leverage is None:
        observed_leverage = _field_from_row(
            row, "lever", "leverage", "buyLeverage", "sellLeverage"
        )
    if observed_mgn_mode is None:
        observed_mgn_mode = _field_from_row(row, "mgnMode", "tradeMode", "marginMode")
    exc_type, exc_text = _exc_fields(exc)
    LOG.warning(
        "leverage_one_step_failed | venue=%s | step=%s | symbol=%s | coin=%s | "
        "reason=%s | http_status=%s | code=%s | msg=%s | "
        "requested_leverage=%s | requested_mgn_mode=%s | "
        "observed_leverage=%s | observed_mgn_mode=%s | exc_type=%s | exc=%s",
        venue or "-",
        step or "-",
        symbol or "-",
        coin or "-",
        reason or "-",
        _clip(http_status, limit=16) if http_status is not None else "-",
        _venue_code(response),
        _venue_msg(response),
        _clip(requested_leverage, limit=16),
        _clip(requested_mgn_mode, limit=16),
        _clip(observed_leverage, limit=32),
        _clip(observed_mgn_mode, limit=32),
        exc_type,
        exc_text,
    )


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
        err = RuntimeError("leverage_set_failed")
        setattr(err, "http_status", status)
        raise err from exc
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
                coin=target.coin,
                credentials=okx_credentials,
                endpoints=endpoints,
                poster=poster,
                confirmed=confirmed,
            )
        if bybit_symbol:
            _confirm_bybit(
                symbol=bybit_symbol,
                coin=target.coin,
                credentials=bybit_credentials,
                endpoints=endpoints,
                poster=poster,
                confirmed=confirmed,
            )
    return confirmed


def set_and_verify_leverage_one(
    target: LeverageTarget,
    *,
    okx_credentials: LiveCredentials,
    bybit_credentials: LiveCredentials,
    endpoints: VenueEndpoints,
    post_fn: Optional[PostFn] = None,
    get_fn: Optional[GetFn] = None,
) -> bool:
    """Require a flat instrument, set 1x, then verify both venue readbacks."""
    getter = get_fn or _http_get_json
    try:
        _preflight_flat(
            target,
            okx_credentials=okx_credentials,
            bybit_credentials=bybit_credentials,
            endpoints=endpoints,
            get_fn=getter,
        )
    except RuntimeError as exc:
        if str(exc) == "leverage_preflight_not_flat":
            raise
        raise RuntimeError("leverage_preflight_failed") from exc
    except Exception as exc:  # noqa: BLE001 — fail closed before setter
        _log_leverage_step_failed(
            venue="-",
            step="preflight",
            symbol=target.okx_symbol or target.bybit_symbol,
            coin=target.coin,
            reason="preflight_failed",
            exc=exc,
        )
        raise RuntimeError("leverage_preflight_failed") from exc
    confirmed = set_leverage_one(
        [target],
        okx_credentials=okx_credentials,
        bybit_credentials=bybit_credentials,
        endpoints=endpoints,
        post_fn=post_fn,
    )
    if (
        confirmed.get(("okx", target.okx_symbol)) != LEVER_ONE
        or confirmed.get(("bybit", target.bybit_symbol)) != LEVER_ONE
    ):
        raise RuntimeError("leverage_set_unconfirmed")

    okx_path = (
        f"{OKX_LEVERAGE_INFO_PATH}?"
        f"{urlencode({'instId': target.okx_symbol, 'mgnMode': 'cross'})}"
    )
    from app.bot.private.rest_readonly import build_okx_readonly_headers

    okx_headers = build_okx_readonly_headers(
        api_key=okx_credentials.api_key,
        api_secret=okx_credentials.api_secret,
        passphrase=okx_credentials.passphrase or "",
        path=okx_path,
        simulated_trading=endpoints.okx_simulated_trading,
    )
    try:
        okx_data = getter(
            f"{endpoints.okx_rest}{okx_path}", okx_headers, timeout_sec=15.0
        )
    except Exception as exc:  # noqa: BLE001 — fail closed, log venue/step
        _log_leverage_step_failed(
            venue="okx",
            step="readback",
            symbol=target.okx_symbol,
            coin=target.coin,
            reason="readback_failed",
            exc=exc,
            requested_mgn_mode="cross",
        )
        if str(exc) == "leverage_readback_failed":
            raise
        raise RuntimeError("leverage_readback_failed") from exc
    if not _okx_readback_is_one(okx_data, target.okx_symbol):
        row = _row_for_symbol(okx_data, symbol=target.okx_symbol, symbol_key="instId")
        _log_leverage_step_failed(
            venue="okx",
            step="readback",
            symbol=target.okx_symbol,
            coin=target.coin,
            reason="readback_unconfirmed",
            response=okx_data if isinstance(okx_data, Mapping) else None,
            requested_mgn_mode="cross",
            observed_leverage=_field_from_row(row, "lever", "leverage"),
            observed_mgn_mode=_field_from_row(row, "mgnMode"),
        )
        raise RuntimeError("leverage_readback_unconfirmed")

    query = (
        f"category=linear&symbol={target.bybit_symbol}"
        "&settleCoin=USDT&limit=200"
    )
    try:
        bybit_data = _bybit_get_all_pages(
            credentials=bybit_credentials,
            base=endpoints.bybit_rest,
            path=BYBIT_POSITION_PATH,
            query=query,
            get_fn=getter,
        )
    except RuntimeError as exc:
        _log_leverage_step_failed(
            venue="bybit",
            step="readback",
            symbol=target.bybit_symbol,
            coin=target.coin,
            reason="readback_failed",
            exc=exc,
        )
        raise RuntimeError("leverage_readback_failed") from exc
    if not _bybit_readback_is_one(bybit_data, target.bybit_symbol):
        row = _row_for_symbol(
            bybit_data, symbol=target.bybit_symbol, symbol_key="symbol"
        )
        _log_leverage_step_failed(
            venue="bybit",
            step="readback",
            symbol=target.bybit_symbol,
            coin=target.coin,
            reason="readback_unconfirmed",
            response=bybit_data if isinstance(bybit_data, Mapping) else None,
            observed_leverage=_field_from_row(
                row, "leverage", "buyLeverage", "sellLeverage"
            ),
            observed_mgn_mode=_field_from_row(row, "tradeMode", "marginMode"),
        )
        raise RuntimeError("leverage_readback_unconfirmed")
    return True


def _preflight_get(
    venue: str,
    symbol: str,
    coin: str,
    fn: Callable[[], Mapping[str, Any]],
) -> Mapping[str, Any]:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — fail closed, log venue/step
        _log_leverage_step_failed(
            venue=venue,
            step="preflight",
            symbol=symbol,
            coin=coin,
            reason="preflight_get_failed",
            exc=exc,
        )
        raise


def _require_preflight_flat(
    venue: str,
    symbol: str,
    coin: str,
    ok: bool,
    response: Mapping[str, Any] | None,
) -> None:
    if ok:
        return
    _log_leverage_step_failed(
        venue=venue,
        step="preflight",
        symbol=symbol,
        coin=coin,
        reason="not_flat",
        response=response if isinstance(response, Mapping) else None,
    )
    raise RuntimeError("leverage_preflight_not_flat")


def _preflight_flat(
    target: LeverageTarget,
    *,
    okx_credentials: LiveCredentials,
    bybit_credentials: LiveCredentials,
    endpoints: VenueEndpoints,
    get_fn: GetFn,
) -> None:
    from app.bot.private.ws_w4_baseline import (
        _BYBIT_OPEN,
        _BYBIT_POS,
        _OKX_OPEN,
        _OKX_POS,
        _bybit_open_orders_flat,
        _bybit_position_flat,
        _bybit_signed_get,
        _okx_open_orders_flat,
        _okx_position_flat,
        _okx_signed_get,
    )

    bybit_position_query = (
        f"category=linear&symbol={target.bybit_symbol}"
        "&settleCoin=USDT&limit=200"
    )
    bybit_positions = _preflight_get(
        "bybit",
        target.bybit_symbol,
        target.coin,
        lambda: _bybit_get_all_pages(
            credentials=bybit_credentials,
            base=endpoints.bybit_rest,
            path=_BYBIT_POS,
            query=bybit_position_query,
            get_fn=get_fn,
        ),
    )
    _require_preflight_flat(
        "bybit",
        target.bybit_symbol,
        target.coin,
        _bybit_position_flat(bybit_positions, target.bybit_symbol),
        bybit_positions,
    )

    bybit_orders = _preflight_get(
        "bybit",
        target.bybit_symbol,
        target.coin,
        lambda: _bybit_get_all_pages(
            credentials=bybit_credentials,
            base=endpoints.bybit_rest,
            path=_BYBIT_OPEN,
            query=(
                f"category=linear&symbol={target.bybit_symbol}"
                "&openOnly=0&limit=50"
            ),
            get_fn=get_fn,
        ),
    )
    _require_preflight_flat(
        "bybit",
        target.bybit_symbol,
        target.coin,
        _bybit_open_orders_flat(bybit_orders, target.bybit_symbol),
        bybit_orders,
    )

    okx_positions = _preflight_get(
        "okx",
        target.okx_symbol,
        target.coin,
        lambda: _okx_signed_get(
            credentials=okx_credentials,
            base=endpoints.okx_rest,
            path_with_query=(
                f"{_OKX_POS}?{urlencode({'instId': target.okx_symbol, 'instType': 'SWAP'})}"
            ),
            http_get_json=get_fn,
        ),
    )
    _require_preflight_flat(
        "okx",
        target.okx_symbol,
        target.coin,
        _okx_position_flat(okx_positions, target.okx_symbol),
        okx_positions,
    )
    okx_orders = _preflight_get(
        "okx",
        target.okx_symbol,
        target.coin,
        lambda: _okx_signed_get(
            credentials=okx_credentials,
            base=endpoints.okx_rest,
            path_with_query=(
                f"{_OKX_OPEN}?{urlencode({'instId': target.okx_symbol, 'instType': 'SWAP'})}"
            ),
            http_get_json=get_fn,
        ),
    )
    _require_preflight_flat(
        "okx",
        target.okx_symbol,
        target.coin,
        _okx_open_orders_flat(okx_orders, target.okx_symbol),
        okx_orders,
    )


def _bybit_get_all_pages(
    *,
    credentials: LiveCredentials,
    base: str,
    path: str,
    query: str,
    get_fn: GetFn,
) -> Mapping[str, Any]:
    from app.bot.private.ws_w4_baseline import _bybit_signed_get

    cursor = ""
    seen: set[str] = set()
    rows: list[Any] = []
    for _ in range(_MAX_PREFLIGHT_PAGES):
        page_query = query if not cursor else f"{query}&{urlencode({'cursor': cursor})}"
        page = _bybit_signed_get(
            credentials=credentials,
            base=base,
            path=path,
            query=page_query,
            http_get_json=get_fn,
        )
        if page.get("retCode") not in (0, "0"):
            raise RuntimeError("bybit_preflight_get_rejected")
        result = page.get("result")
        if not isinstance(result, Mapping) or not isinstance(
            result.get("list"), list
        ):
            raise RuntimeError("leverage_preflight_failed")
        rows.extend(result["list"])
        next_cursor = str(result.get("nextPageCursor") or "")
        if not next_cursor:
            return {"retCode": 0, "result": {"list": rows}}
        if next_cursor in seen:
            raise RuntimeError("leverage_preflight_failed")
        seen.add(next_cursor)
        cursor = next_cursor
    raise RuntimeError("leverage_preflight_failed")


def _http_get_json(
    url: str, headers: Mapping[str, str], *, timeout_sec: float
) -> Mapping[str, Any]:
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("leverage_readback_failed") from exc
    if not isinstance(data, Mapping):
        raise RuntimeError("leverage_readback_failed")
    return data


def _okx_readback_is_one(data: Mapping[str, Any], symbol: str) -> bool:
    rows = data.get("data")
    if str(data.get("code")) != "0" or not isinstance(rows, list):
        return False
    matching = [
        row for row in rows
        if isinstance(row, Mapping) and str(row.get("instId") or "") == symbol
    ]
    return bool(matching) and all(
        _is_one(row.get("lever")) and str(row.get("mgnMode") or "") == "cross"
        for row in matching
    )


def _bybit_readback_is_one(data: Mapping[str, Any], symbol: str) -> bool:
    if str(data.get("retCode")) != "0":
        return False
    result = data.get("result")
    rows = result.get("list") if isinstance(result, Mapping) else None
    if not isinstance(rows, list):
        return False
    matching = [
        row for row in rows
        if isinstance(row, Mapping) and str(row.get("symbol") or "") == symbol
    ]
    # Bybit may append an empty cursor placeholder for the same symbol; it can
    # carry an old leverage value but has no position status or update time.
    current = [
        row for row in matching
        if row.get("positionStatus") not in (None, "")
        and row.get("updatedTime") not in (None, "")
    ]
    if not current:
        return False
    for row in current:
        leverage_values = [
            row.get(field)
            for field in ("leverage", "buyLeverage", "sellLeverage")
            if row.get(field) not in (None, "")
        ]
        if not leverage_values or any(not _is_one(value) for value in leverage_values):
            return False
        try:
            if Decimal(str(row.get("size") or "0")) != 0:
                return False
        except Exception:  # noqa: BLE001 — venue response boundary
            return False
        if row.get("positionIdx") not in (None, "", 0, "0", 1, "1", 2, "2"):
            return False
    return True


def _confirm_okx(
    *,
    symbol: str,
    credentials: LiveCredentials,
    endpoints: VenueEndpoints,
    poster: PostFn,
    confirmed: dict[tuple[str, str], str],
    coin: str = "",
) -> None:
    path = OKX_SET_LEVERAGE_PATH
    body = _okx_body(symbol)
    headers = _okx_headers(credentials=credentials, path=path, body=body)
    url = f"{endpoints.okx_rest}{path}"
    try:
        status, data = poster(url, headers, body)
    except Exception as exc:  # noqa: BLE001 — fail closed, no send later
        _log_leverage_step_failed(
            venue="okx",
            step="set",
            symbol=symbol,
            coin=coin,
            reason="set_exception",
            exc=exc,
            requested_mgn_mode="cross",
        )
        return
    if isinstance(data, Mapping) and _okx_confirmed(data):
        confirmed[("okx", symbol)] = LEVER_ONE
        return
    row = _row_for_symbol(
        data if isinstance(data, Mapping) else None,
        symbol=symbol,
        symbol_key="instId",
    )
    _log_leverage_step_failed(
        venue="okx",
        step="set",
        symbol=symbol,
        coin=coin,
        reason="set_unconfirmed",
        http_status=status,
        response=data if isinstance(data, Mapping) else None,
        requested_mgn_mode="cross",
        observed_leverage=_field_from_row(row, "lever"),
        observed_mgn_mode=_field_from_row(row, "mgnMode"),
    )


def _confirm_bybit(
    *,
    symbol: str,
    credentials: LiveCredentials,
    endpoints: VenueEndpoints,
    poster: PostFn,
    confirmed: dict[tuple[str, str], str],
    coin: str = "",
) -> None:
    path = BYBIT_SET_LEVERAGE_PATH
    body = _bybit_body(symbol)
    headers = _bybit_headers(credentials=credentials, body=body)
    url = f"{endpoints.bybit_rest}{path}"
    try:
        status, data = poster(url, headers, body)
    except Exception as exc:  # noqa: BLE001 — fail closed, no send later
        _log_leverage_step_failed(
            venue="bybit",
            step="set",
            symbol=symbol,
            coin=coin,
            reason="set_exception",
            exc=exc,
        )
        return
    if isinstance(data, Mapping) and _bybit_confirmed(data):
        confirmed[("bybit", symbol)] = LEVER_ONE
        return
    row = _row_for_symbol(
        data if isinstance(data, Mapping) else None,
        symbol=symbol,
        symbol_key="symbol",
    )
    _log_leverage_step_failed(
        venue="bybit",
        step="set",
        symbol=symbol,
        coin=coin,
        reason="set_unconfirmed",
        http_status=status,
        response=data if isinstance(data, Mapping) else None,
        observed_leverage=_field_from_row(
            row, "leverage", "buyLeverage", "sellLeverage"
        ),
        observed_mgn_mode=_field_from_row(row, "tradeMode", "marginMode"),
    )
