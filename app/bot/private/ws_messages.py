"""Private / trade WS message construction for live Bybit and OKX.

Builds auth, subscribe, place, and cancel frames without network I/O.
Public views never expose signature, key, passphrase, order id, or raw frames.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from app.bot.private.order_plan import OrderPlan, OrderPlanError
from app.bot.private.order_sign import LiveCredentials

# OKX trade WS ``id`` (place/cancel): case-sensitive alphanumerics, ≤32.
# Underscore is illegal — live 60033 "Parameter id error" on Contour B
# (2026-09-05, ``id`` = new_opaque_id("req")[:32] = ``req_<hex>``).
OKX_WS_ID_MAX_LEN = 32
OKX_WS_ID_ERROR_CODE = "60033"
OKX_WS_ID_ERROR_MSG = "Parameter id error"
OKX_WS_ID_PARAMETER = "id"
_OKX_WS_ID_RE = re.compile(r"^[A-Za-z0-9]{1,32}$")


class OkxWsIdError(ValueError):
    """Local charset check matching OKX trade WS ``60033`` / Parameter id error."""

    code = OKX_WS_ID_ERROR_CODE
    msg = OKX_WS_ID_ERROR_MSG
    parameter = OKX_WS_ID_PARAMETER

    def __init__(self, req_id: object, *, reason: str) -> None:
        self.req_id = req_id
        self.reason = reason
        super().__init__(
            f"OKX WS {self.parameter} error ({self.code}): {self.msg} ({reason})"
        )


def okx_ws_id_is_legal(req_id: object) -> bool:
    return isinstance(req_id, str) and bool(_OKX_WS_ID_RE.fullmatch(req_id))


def assert_okx_ws_message_id(req_id: object) -> str:
    """Raise ``OkxWsIdError`` (60033 semantics) if ``id`` is not OKX-legal."""
    if not isinstance(req_id, str) or not req_id:
        raise OkxWsIdError(req_id, reason="empty or not a string")
    if not req_id.isascii() or not req_id.isalnum():
        raise OkxWsIdError(
            req_id, reason="must be alphanumeric letters/numbers only (no underscore)"
        )
    if len(req_id) > OKX_WS_ID_MAX_LEN:
        raise OkxWsIdError(
            req_id, reason=f"length {len(req_id)} > {OKX_WS_ID_MAX_LEN}"
        )
    return req_id


def sanitize_okx_ws_id(req_id: object) -> str:
    """Strip non-alnum, truncate to 32. Fail-closed if nothing remains."""
    raw = "" if req_id is None else str(req_id)
    cleaned = "".join(ch for ch in raw if ch.isascii() and ch.isalnum())
    if not cleaned:
        raise OkxWsIdError(req_id, reason="empty after stripping non-alnum")
    return assert_okx_ws_message_id(cleaned[:OKX_WS_ID_MAX_LEN])


def new_okx_ws_id(*, prefix: str = "req") -> str:
    """Generate an OKX-legal trade WS ``id`` (no underscore)."""
    cleaned_prefix = "".join(
        ch for ch in str(prefix) if ch.isascii() and ch.isalnum()
    ) or "req"
    room = OKX_WS_ID_MAX_LEN - len(cleaned_prefix)
    if room < 8:
        cleaned_prefix = cleaned_prefix[: OKX_WS_ID_MAX_LEN - 8]
        room = OKX_WS_ID_MAX_LEN - len(cleaned_prefix)
    return assert_okx_ws_message_id(f"{cleaned_prefix}{uuid.uuid4().hex[:room]}")


@dataclass(frozen=True)
class WsOutboundMessage:
    """Constructed outbound WS text. Keep raw text off logs/journals."""

    venue: str  # bybit_live | okx_live
    channel: str  # private_stream | trade
    op: str
    text: str

    def public_view(self) -> dict[str, object]:
        return {
            "venue": self.venue,
            "channel": self.channel,
            "op": self.op,
            "payload_present": bool(self.text),
            "payload_bytes": len(self.text.encode("utf-8")),
            # Never echo raw frame / signing material.
            "raw_omitted": True,
        }


def _json_compact(obj: Mapping[str, Any] | Sequence[Any]) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


# --- Bybit private stream -------------------------------------------------


def build_bybit_private_auth(
    credentials: LiveCredentials,
    *,
    expires_ms: Optional[int] = None,
) -> WsOutboundMessage:
    """Bybit v5 private WS auth: HMAC over GET/realtime{expires}."""
    exp = int(expires_ms if expires_ms is not None else (time.time() * 1000 + 10_000))
    payload = f"GET/realtime{exp}"
    sign = hmac.new(
        credentials.api_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    body = {"op": "auth", "args": [credentials.api_key, exp, sign]}
    return WsOutboundMessage(
        venue="bybit_live",
        channel="private_stream",
        op="auth",
        text=_json_compact(body),
    )


def build_bybit_private_subscribe(
    *,
    topics: Optional[Sequence[str]] = None,
) -> WsOutboundMessage:
    """Subscribe order/execution/position topics (one-symbol filter is client-side)."""
    args = list(topics) if topics is not None else ["order", "execution", "position"]
    body = {"op": "subscribe", "args": args}
    return WsOutboundMessage(
        venue="bybit_live",
        channel="private_stream",
        op="subscribe",
        text=_json_compact(body),
    )


def build_bybit_ping() -> WsOutboundMessage:
    return WsOutboundMessage(
        venue="bybit_live",
        channel="private_stream",
        op="ping",
        text=_json_compact({"op": "ping"}),
    )


def build_bybit_trade_place(
    plan: OrderPlan,
    credentials: LiveCredentials,
    *,
    req_id: str,
    recv_window: int = 5000,
    timestamp_ms: Optional[int] = None,
) -> WsOutboundMessage:
    """Bybit trade WS order.create — separate from private stream."""
    if plan.venue != "bybit_live":
        raise OrderPlanError("bybit trade place requires bybit_live plan")
    ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
    args_obj: dict[str, object] = {
        "category": "linear",
        "symbol": plan.symbol,
        "side": "Buy" if plan.side == "buy" else "Sell",
        "qty": plan.qty,
        "orderLinkId": plan.order_attempt_id[:36],
    }
    if plan.mode == "market":
        args_obj["orderType"] = "Market"
        args_obj["timeInForce"] = "IOC"
    else:
        args_obj["orderType"] = "Limit"
        args_obj["price"] = plan.price
        args_obj["timeInForce"] = "PostOnly"
    if plan.reduce_only:
        args_obj["reduceOnly"] = True
    # Header-style auth fields ride with trade WS request (not logged).
    body_str = _json_compact(args_obj)
    sign_payload = f"{ts}{credentials.api_key}{recv_window}{body_str}"
    sign = hmac.new(
        credentials.api_secret.encode("utf-8"),
        sign_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    frame = {
        "reqId": req_id,
        "header": {
            "X-BAPI-API-KEY": credentials.api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-SIGN": sign,
            "X-BAPI-RECV-WINDOW": str(recv_window),
        },
        "op": "order.create",
        "args": [args_obj],
    }
    return WsOutboundMessage(
        venue="bybit_live",
        channel="trade",
        op="order.create",
        text=_json_compact(frame),
    )


def build_bybit_trade_cancel(
    plan: OrderPlan,
    credentials: LiveCredentials,
    *,
    req_id: str,
    recv_window: int = 5000,
    timestamp_ms: Optional[int] = None,
) -> WsOutboundMessage:
    if plan.venue != "bybit_live":
        raise OrderPlanError("bybit trade cancel requires bybit_live plan")
    ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
    args_obj: dict[str, object] = {
        "category": "linear",
        "symbol": plan.symbol,
        "orderLinkId": plan.order_attempt_id[:36],
    }
    body_str = _json_compact(args_obj)
    sign_payload = f"{ts}{credentials.api_key}{recv_window}{body_str}"
    sign = hmac.new(
        credentials.api_secret.encode("utf-8"),
        sign_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    frame = {
        "reqId": req_id,
        "header": {
            "X-BAPI-API-KEY": credentials.api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-SIGN": sign,
            "X-BAPI-RECV-WINDOW": str(recv_window),
        },
        "op": "order.cancel",
        "args": [args_obj],
    }
    return WsOutboundMessage(
        venue="bybit_live",
        channel="trade",
        op="order.cancel",
        text=_json_compact(frame),
    )


# --- OKX private stream ---------------------------------------------------


def build_okx_private_login(
    credentials: LiveCredentials,
    *,
    timestamp: Optional[str] = None,
) -> WsOutboundMessage:
    """OKX private WS login: Base64(HMAC_SHA256(secret, ts+GET+/users/self/verify))."""
    if not credentials.passphrase:
        raise OrderPlanError("okx_live WS login requires passphrase")
    ts = timestamp or str(int(time.time()))
    prehash = f"{ts}GET/users/self/verify"
    digest = hmac.new(
        credentials.api_secret.encode("utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    sign = base64.b64encode(digest).decode("utf-8")
    body = {
        "op": "login",
        "args": [
            {
                "apiKey": credentials.api_key,
                "passphrase": credentials.passphrase,
                "timestamp": ts,
                "sign": sign,
            }
        ],
    }
    return WsOutboundMessage(
        venue="okx_live",
        channel="private_stream",
        op="login",
        text=_json_compact(body),
    )


def build_okx_private_subscribe(
    *,
    symbol: str,
    inst_type: str = "SWAP",
) -> WsOutboundMessage:
    """One-symbol orders + positions subscription on OKX private channel."""
    args = [
        {"channel": "orders", "instType": inst_type, "instId": symbol},
        {"channel": "positions", "instType": inst_type, "instId": symbol},
    ]
    body = {"op": "subscribe", "args": args}
    return WsOutboundMessage(
        venue="okx_live",
        channel="private_stream",
        op="subscribe",
        text=_json_compact(body),
    )


def build_okx_ping() -> WsOutboundMessage:
    return WsOutboundMessage(
        venue="okx_live",
        channel="private_stream",
        op="ping",
        text="ping",
    )


def _require_okx_inst_id_code(code: object) -> int:
    """Fail-closed: OKX WS place/cancel requires a positive JSON integer instIdCode."""
    if isinstance(code, bool) or not isinstance(code, int) or code <= 0:
        raise OrderPlanError("okx WS order requires positive int instIdCode")
    return code


def build_okx_trade_place(
    plan: OrderPlan,
    *,
    req_id: str,
    inst_id_code: Optional[int] = None,
) -> WsOutboundMessage:
    """OKX WS place (op=order) — separate from stream subscription frames."""
    if plan.venue != "okx_live":
        raise OrderPlanError("okx trade place requires okx_live plan")
    code = _require_okx_inst_id_code(
        inst_id_code if inst_id_code is not None else plan.inst_id_code
    )
    args_obj: dict[str, object] = {
        "instId": plan.symbol,
        "instIdCode": code,
        "tdMode": "cross",
        "side": plan.side,
        "sz": plan.qty,
        "clOrdId": plan.order_attempt_id.replace("_", "")[:32],
    }
    if plan.position_side in {"long", "short"}:
        args_obj["posSide"] = plan.position_side
    if plan.mode == "market":
        args_obj["ordType"] = "market"
    else:
        args_obj["ordType"] = "post_only"
        args_obj["px"] = plan.price
    if plan.reduce_only:
        args_obj["reduceOnly"] = True
    # Frame-boundary sanitize: journal ``prefix_hex`` ids stay unchanged elsewhere.
    frame = {"id": sanitize_okx_ws_id(req_id), "op": "order", "args": [args_obj]}
    return WsOutboundMessage(
        venue="okx_live",
        channel="trade",
        op="order",
        text=_json_compact(frame),
    )


def build_okx_trade_cancel(
    plan: OrderPlan,
    *,
    req_id: str,
    inst_id_code: Optional[int] = None,
) -> WsOutboundMessage:
    if plan.venue != "okx_live":
        raise OrderPlanError("okx trade cancel requires okx_live plan")
    code = _require_okx_inst_id_code(
        inst_id_code if inst_id_code is not None else plan.inst_id_code
    )
    args_obj: dict[str, object] = {
        "instId": plan.symbol,
        "instIdCode": code,
        "clOrdId": plan.order_attempt_id.replace("_", "")[:32],
    }
    frame = {
        "id": sanitize_okx_ws_id(req_id),
        "op": "cancel-order",
        "args": [args_obj],
    }
    return WsOutboundMessage(
        venue="okx_live",
        channel="trade",
        op="cancel-order",
        text=_json_compact(frame),
    )


def message_shape_without_secrets(msg: WsOutboundMessage) -> dict[str, object]:
    """Safe shape summary for tests/logs — never includes raw text."""
    view = msg.public_view()
    # Structural hints without payload content.
    if msg.venue.startswith("bybit"):
        view["expected_keys"] = ["op"]
    elif msg.venue.startswith("okx"):
        view["expected_keys"] = ["op"] if msg.text != "ping" else ["ping_literal"]
    return view
