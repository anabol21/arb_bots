"""Explicit production live transports (injected only; never default CLI).

Parses Bybit/OKX business outcomes accurately (incl. OKX per-order sCode).
Ambiguous timeout/connection after possible dispatch is surfaced explicitly.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping, Optional
from urllib.request import Request

from app.bot.private.order_sender import TransportAck, TransportFn
from app.bot.private.order_sign import SignedRequest

# re-export detection helper used by W4 fail-closed checks


def parse_bybit_place_response(
    data: Mapping[str, Any], *, http_status: int
) -> TransportAck:
    if http_status != 200:
        return TransportAck(
            kind="rejected",
            ack_state="received",
            error_code="venue_rejected",
        )
    ret = data.get("retCode")
    if ret == 0 or str(ret) == "0":
        return TransportAck(kind="accepted", ack_state="accepted")
    return TransportAck(
        kind="rejected",
        ack_state="received",
        error_code="order_rejected",
    )


def parse_okx_place_response(
    data: Mapping[str, Any], *, http_status: int
) -> TransportAck:
    """OKX may return top-level code=0 with per-order data[].sCode != 0."""
    if http_status != 200:
        return TransportAck(
            kind="rejected",
            ack_state="received",
            error_code="venue_rejected",
        )
    if str(data.get("code")) != "0":
        return TransportAck(
            kind="rejected",
            ack_state="received",
            error_code="order_rejected",
        )
    rows = data.get("data")
    if not isinstance(rows, list) or not rows:
        return TransportAck(
            kind="rejected",
            ack_state="received",
            error_code="invalid_request",
        )
    first = rows[0]
    if not isinstance(first, Mapping):
        return TransportAck(
            kind="rejected",
            ack_state="received",
            error_code="invalid_request",
        )
    scode = first.get("sCode")
    if scode is None:
        return TransportAck(
            kind="rejected",
            ack_state="received",
            error_code="invalid_request",
        )
    if str(scode) != "0":
        # Malformed / per-order failure despite top-level success.
        return TransportAck(
            kind="rejected",
            ack_state="received",
            error_code="order_rejected",
        )
    return TransportAck(kind="accepted", ack_state="accepted")


def _http_post(signed: SignedRequest, *, timeout_sec: float = 15.0) -> TransportAck:
    """Real HTTP POST — only reachable if this transport was explicitly injected."""
    req = Request(
        signed.base_url + signed.path,
        data=signed.body.encode("utf-8"),
        headers=dict(signed._headers),  # noqa: SLF001 — internal send path only
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = int(getattr(resp, "status", None) or resp.getcode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = int(exc.code)
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return TransportAck(
                kind="rejected", ack_state="received", error_code="venue_rejected"
            )
        if not isinstance(data, dict):
            return TransportAck(
                kind="rejected", ack_state="received", error_code="venue_rejected"
            )
        if signed.venue.startswith("bybit"):
            return parse_bybit_place_response(data, http_status=status)
        return parse_okx_place_response(data, http_status=status)
    except TimeoutError:
        # May have reached venue — ambiguous.
        return TransportAck(
            kind="ambiguous",
            ack_state="received",
            error_code="timeout",
            ambiguous=True,
        )
    except (urllib.error.URLError, OSError):
        return TransportAck(
            kind="ambiguous",
            ack_state="received",
            error_code="network_error",
            ambiguous=True,
        )
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return TransportAck(
            kind="rejected", ack_state="received", error_code="invalid_request"
        )
    if not isinstance(data, dict):
        return TransportAck(
            kind="rejected", ack_state="received", error_code="invalid_request"
        )
    if signed.venue.startswith("bybit"):
        return parse_bybit_place_response(data, http_status=status)
    return parse_okx_place_response(data, http_status=status)


def is_live_http_order_transport(transport: Any) -> bool:
    """True for production REST place transports from this module (never W4)."""
    if transport is None:
        return False
    mod = getattr(transport, "__module__", "") or ""
    return mod.endswith("order_transport") or mod.endswith("app.bot.private.order_transport")


def build_bybit_live_http_transport() -> TransportFn:
    """Explicit Bybit live futures place transport. Do not wire from CLI."""

    def _send(signed: SignedRequest) -> TransportAck:
        if not isinstance(signed, SignedRequest):
            return TransportAck(
                kind="rejected", ack_state="received", error_code="invalid_request"
            )
        if signed.venue != "bybit_live" or signed.path != "/v5/order/create":
            return TransportAck(
                kind="rejected", ack_state="received", error_code="invalid_request"
            )
        return _http_post(signed)

    return _send


def build_okx_live_http_transport() -> TransportFn:
    """Explicit OKX live futures place transport. Do not wire from CLI."""

    def _send(signed: SignedRequest) -> TransportAck:
        if not isinstance(signed, SignedRequest):
            return TransportAck(
                kind="rejected", ack_state="received", error_code="invalid_request"
            )
        if signed.venue != "okx_live" or signed.path != "/api/v5/trade/order":
            return TransportAck(
                kind="rejected", ack_state="received", error_code="invalid_request"
            )
        return _http_post(signed)

    return _send


def assert_production_transports_unbound_from_runtime_slot(
    get_runtime_transport,
) -> None:
    """Guard used by tests/CLI: runtime slot must not hold production transports."""
    fn: Optional[TransportFn] = get_runtime_transport()
    if fn is None:
        return
    name = getattr(fn, "__name__", "")
    if name in {"_send"} and getattr(fn, "__module__", "").endswith("order_transport"):
        raise RuntimeError("production transport unexpectedly bound in runtime slot")
