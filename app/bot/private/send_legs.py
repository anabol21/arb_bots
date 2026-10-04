"""Dual-leg send that reads ``private_leg_up`` and does not open sockets itself.

``send_long`` / ``send_short`` call ``build_signed_place_text`` and
``send_signed_dual``. Tests inject ``sender``. A down leg returns
``private_channel_down`` and does not call ``ws.send``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.bot.private.order_metadata import parse_inst_id_code
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.private_leg_up import leg_up
from app.bot.private.ws_trivial_dual_leg import (
    TrivialSendResult,
    build_signed_place_text,
    send_signed_dual,
)

LegUpFn = Callable[[str, str], bool]


@dataclass(frozen=True)
class SendLegsResult:
    abort: Optional[str]
    sent: int
    send_result: Optional[TrivialSendResult] = None


_OKX_CL_ORD_ID = re.compile(r"^[A-Za-z0-9]{1,32}$")


def _both_up(coin: str, leg_up_fn: LegUpFn) -> bool:
    return bool(leg_up_fn("okx", coin) and leg_up_fn("bybit", coin))


def _attempt_ids(intent_id: str) -> tuple[str, str, str]:
    """Same shape as ``LiveBroker``: hyphen-free hex, not a raw uuid.

    OKX ``clOrdId`` is ``o`` + hex, at most 32. Bybit ``orderLinkId`` is
    ``b`` + hex, at most 36.
    """
    dual = str(intent_id).replace("-", "")[:32]
    return f"b{dual}"[:36], f"o{dual}"[:32], dual


def _frame_cl_ord_id(okx_text: str) -> str:
    try:
        data = json.loads(okx_text)
        args = data.get("args") or []
        if not args or not isinstance(args[0], dict):
            return ""
        return str(args[0].get("clOrdId") or "")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return ""


def _send_pair(
    *,
    coin: str,
    okx_side: str,
    bybit_side: str,
    okx_symbol: str,
    bybit_symbol: str,
    okx_sz: str,
    bybit_qty: str,
    reduce_only: bool,
    sender: Any,
    credentials: Optional[LiveCredentials],
    inst_id_code: Optional[int],
    intent_id: str,
    signal_ts_ms: int,
    phase: str,
    leg_up_fn: LegUpFn,
) -> SendLegsResult:
    # Map only. Do not call is_ready() and do not read the journal.
    if not _both_up(coin, leg_up_fn):
        return SendLegsResult(abort="private_channel_down", sent=0)
    if sender is None:
        return SendLegsResult(abort="private_channel_down", sent=0)
    code = parse_inst_id_code(inst_id_code)
    if code is None:
        return SendLegsResult(abort="okx_inst_id_code_missing", sent=0)
    inst_id_code = code
    bybit_attempt, okx_attempt, dual_id = _attempt_ids(intent_id)
    try:
        bybit_text, bybit_req, _ = build_signed_place_text(
            venue="bybit",
            symbol=bybit_symbol,
            side=bybit_side,
            qty=str(bybit_qty),
            credentials=credentials,
            reduce_only=reduce_only,
            order_attempt_id=bybit_attempt,
            dual_leg_id=dual_id,
        )
        okx_text, okx_req, _ = build_signed_place_text(
            venue="okx",
            symbol=okx_symbol,
            side=okx_side,
            qty=str(okx_sz),
            credentials=credentials,
            reduce_only=reduce_only,
            inst_id_code=inst_id_code,
            order_attempt_id=okx_attempt,
            dual_leg_id=dual_id,
        )
    except Exception as exc:  # noqa: BLE001 — fail closed, no send
        return SendLegsResult(abort=f"place_build_failed:{type(exc).__name__}", sent=0)
    cl_ord_id = _frame_cl_ord_id(okx_text)
    if _OKX_CL_ORD_ID.fullmatch(cl_ord_id) is None:
        return SendLegsResult(abort="okx_cl_ord_id_illegal", sent=0)
    send_result = send_signed_dual(
        sender=sender,
        bybit_text=bybit_text,
        okx_text=okx_text,
        bybit_req_id=bybit_req,
        okx_req_id=okx_req,
        phase=phase,
        intent_id=intent_id,
        dual_leg_id=dual_id,
        signal_ts_ms=int(signal_ts_ms),
    )
    return SendLegsResult(abort=None, sent=2, send_result=send_result)


def send_long(
    *,
    coin: str,
    okx_symbol: str,
    bybit_symbol: str,
    okx_sz: str,
    bybit_qty: str,
    reduce_only: bool = False,
    sender: Any,
    credentials: Optional[LiveCredentials],
    inst_id_code: Optional[int],
    intent_id: str,
    signal_ts_ms: int,
    phase: str = "open",
    leg_up_fn: LegUpFn = leg_up,
) -> SendLegsResult:
    """Buy OKX, sell Bybit. ``reduce_only`` is True only on close."""
    return _send_pair(
        coin=coin,
        okx_side="buy",
        bybit_side="sell",
        okx_symbol=okx_symbol,
        bybit_symbol=bybit_symbol,
        okx_sz=okx_sz,
        bybit_qty=bybit_qty,
        reduce_only=reduce_only,
        sender=sender,
        credentials=credentials,
        inst_id_code=inst_id_code,
        intent_id=intent_id,
        signal_ts_ms=signal_ts_ms,
        phase=phase,
        leg_up_fn=leg_up_fn,
    )


def send_short(
    *,
    coin: str,
    okx_symbol: str,
    bybit_symbol: str,
    okx_sz: str,
    bybit_qty: str,
    reduce_only: bool = False,
    sender: Any,
    credentials: Optional[LiveCredentials],
    inst_id_code: Optional[int],
    intent_id: str,
    signal_ts_ms: int,
    phase: str = "open",
    leg_up_fn: LegUpFn = leg_up,
) -> SendLegsResult:
    """Sell OKX, buy Bybit. ``reduce_only`` is True only on close."""
    return _send_pair(
        coin=coin,
        okx_side="sell",
        bybit_side="buy",
        okx_symbol=okx_symbol,
        bybit_symbol=bybit_symbol,
        okx_sz=okx_sz,
        bybit_qty=bybit_qty,
        reduce_only=reduce_only,
        sender=sender,
        credentials=credentials,
        inst_id_code=inst_id_code,
        intent_id=intent_id,
        signal_ts_ms=signal_ts_ms,
        phase=phase,
        leg_up_fn=leg_up_fn,
    )
