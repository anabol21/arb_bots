"""Signed live futures request construction for Bybit/OKX (no execution).

Never logs raw signatures, headers, or bodies. No network I/O.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Mapping, Optional

from app.bot.private.order_plan import OrderPlan, OrderPlanError

# Official live futures place endpoints (construction only).
BYBIT_LIVE_PLACE_PATH = "/v5/order/create"
OKX_LIVE_PLACE_PATH = "/api/v5/trade/order"

BYBIT_LIVE_REST = "https://api.bybit.com"
OKX_LIVE_REST = "https://www.okx.com"


@dataclass(frozen=True)
class LiveCredentials:
    """In-memory credentials — never serialize into journals/logs."""

    api_key: str
    api_secret: str
    passphrase: Optional[str] = None


@dataclass(frozen=True)
class SignedRequest:
    venue: str
    method: str
    base_url: str
    path: str
    body: str
    # Private header map kept on the object; public_view redacts it.
    _headers: Mapping[str, str]

    def public_view(self) -> dict[str, object]:
        return {
            "venue": self.venue,
            "method": self.method,
            "path": self.path,
            "body_present": bool(self.body),
            "header_names": sorted(
                k
                for k in self._headers.keys()
                if k.lower()
                not in {
                    "ok-access-sign",
                    "ok-access-key",
                    "ok-access-passphrase",
                    "ok-access-timestamp",
                    "x-bapi-api-key",
                    "x-bapi-sign",
                    "x-bapi-timestamp",
                    "x-bapi-recv-window",
                }
            ),
            "auth_headers_present": True,
            # Never expose full private REST URL in public views.
            "base_url_omitted": True,
        }


@dataclass(frozen=True)
class WsTradeDispatch:
    """Trade/private WS place|cancel token — never a REST signed order request.

    Used when ``journal_transport == \"ws_trade\"`` so ApprovalBoundSender does not
    construct ``/v5/order/create``, ``/v5/order/cancel``, ``/api/v5/trade/order``,
    or ``/api/v5/trade/cancel-order``.
    """

    plan: OrderPlan
    op: str  # "place" | "cancel"

    def public_view(self) -> dict[str, object]:
        return {
            "transport": "ws_trade",
            "op": self.op,
            "venue": self.plan.venue,
            "path_omitted": True,
            "rest_order_api": False,
        }


def is_ws_trade_journal(transport: Optional[str]) -> bool:
    return transport == "ws_trade"


def build_signed_place_request(
    plan: OrderPlan,
    credentials: LiveCredentials,
    *,
    recv_window: int = 5000,
    timestamp_ms: Optional[int] = None,
    okx_timestamp: Optional[str] = None,
) -> SignedRequest:
    """Construct a signed place request without sending it."""
    if plan.venue == "bybit_live":
        return _sign_bybit(
            plan, credentials, recv_window=recv_window, timestamp_ms=timestamp_ms
        )
    if plan.venue == "okx_live":
        if not credentials.passphrase:
            raise OrderPlanError("okx_live requires passphrase")
        return _sign_okx(
            plan, credentials, okx_timestamp=okx_timestamp
        )
    raise OrderPlanError(f"unsupported signing venue {plan.venue!r}")


def _bybit_body(plan: OrderPlan) -> dict[str, object]:
    body: dict[str, object] = {
        "category": "linear",
        "symbol": plan.symbol,
        "side": "Buy" if plan.side == "buy" else "Sell",
        "qty": plan.qty,
        "orderLinkId": plan.order_attempt_id[:36],
    }
    if plan.mode == "market":
        body["orderType"] = "Market"
        body["timeInForce"] = "IOC"
    else:
        body["orderType"] = "Limit"
        body["price"] = plan.price
        body["timeInForce"] = "PostOnly"
    if plan.reduce_only:
        body["reduceOnly"] = True
    return body


def _sign_bybit(
    plan: OrderPlan,
    credentials: LiveCredentials,
    *,
    recv_window: int,
    timestamp_ms: Optional[int],
) -> SignedRequest:
    ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
    body_obj = _bybit_body(plan)
    body = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=False)
    payload = f"{ts}{credentials.api_key}{recv_window}{body}"
    sign = hmac.new(
        credentials.api_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "X-BAPI-API-KEY": credentials.api_key,
        "X-BAPI-SIGN": sign,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": str(recv_window),
        "Content-Type": "application/json",
    }
    return SignedRequest(
        venue="bybit_live",
        method="POST",
        base_url=BYBIT_LIVE_REST,
        path=BYBIT_LIVE_PLACE_PATH,
        body=body,
        _headers=headers,
    )


def _okx_body(plan: OrderPlan) -> dict[str, object]:
    body: dict[str, object] = {
        "instId": plan.symbol,
        "tdMode": "cross",
        "side": plan.side,
        "sz": plan.qty,
        "clOrdId": plan.order_attempt_id.replace("_", "")[:32],
    }
    if plan.position_side in {"long", "short"}:
        body["posSide"] = plan.position_side
    if plan.mode == "market":
        body["ordType"] = "market"
    else:
        body["ordType"] = "post_only"
        body["px"] = plan.price
    if plan.reduce_only:
        body["reduceOnly"] = True
    return body


def _sign_okx(
    plan: OrderPlan,
    credentials: LiveCredentials,
    *,
    okx_timestamp: Optional[str],
) -> SignedRequest:
    if okx_timestamp is None:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    else:
        ts = okx_timestamp
    body_obj = _okx_body(plan)
    body = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=False)
    prehash = f"{ts}POST{OKX_LIVE_PLACE_PATH}{body}"
    digest = hmac.new(
        credentials.api_secret.encode("utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    sign = base64.b64encode(digest).decode("utf-8")
    headers = {
        "OK-ACCESS-KEY": credentials.api_key,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": credentials.passphrase or "",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "spread-bbot-private/0",
    }
    return SignedRequest(
        venue="okx_live",
        method="POST",
        base_url=OKX_LIVE_REST,
        path=OKX_LIVE_PLACE_PATH,
        body=body,
        _headers=headers,
    )


BYBIT_LIVE_CANCEL_PATH = "/v5/order/cancel"
OKX_LIVE_CANCEL_PATH = "/api/v5/trade/cancel-order"


def build_signed_cancel_request(
    plan: OrderPlan,
    credentials: LiveCredentials,
    *,
    recv_window: int = 5000,
    timestamp_ms: Optional[int] = None,
    okx_timestamp: Optional[str] = None,
) -> SignedRequest:
    """Construct a signed cancel request without sending it."""
    if plan.venue == "bybit_live":
        ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
        body_obj = {
            "category": "linear",
            "symbol": plan.symbol,
            "orderLinkId": plan.order_attempt_id[:36],
        }
        body = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=False)
        payload = f"{ts}{credentials.api_key}{recv_window}{body}"
        sign = hmac.new(
            credentials.api_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "X-BAPI-API-KEY": credentials.api_key,
            "X-BAPI-SIGN": sign,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": str(recv_window),
            "Content-Type": "application/json",
        }
        return SignedRequest(
            venue="bybit_live",
            method="POST",
            base_url=BYBIT_LIVE_REST,
            path=BYBIT_LIVE_CANCEL_PATH,
            body=body,
            _headers=headers,
        )
    if plan.venue == "okx_live":
        if not credentials.passphrase:
            raise OrderPlanError("okx_live requires passphrase")
        ts = okx_timestamp or time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        body_obj = {
            "instId": plan.symbol,
            "clOrdId": plan.order_attempt_id.replace("_", "")[:32],
        }
        body = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=False)
        prehash = f"{ts}POST{OKX_LIVE_CANCEL_PATH}{body}"
        digest = hmac.new(
            credentials.api_secret.encode("utf-8"),
            prehash.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        sign = base64.b64encode(digest).decode("utf-8")
        headers = {
            "OK-ACCESS-KEY": credentials.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": credentials.passphrase or "",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "spread-bbot-private/0",
        }
        return SignedRequest(
            venue="okx_live",
            method="POST",
            base_url=OKX_LIVE_REST,
            path=OKX_LIVE_CANCEL_PATH,
            body=body,
            _headers=headers,
        )
    raise OrderPlanError(f"unsupported cancel venue {plan.venue!r}")
