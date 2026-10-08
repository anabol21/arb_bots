"""Thin synthetic_roll place path. Not ``LiveBroker.place``.

Order: preprocess (sides + shared coin qty) → send_long/send_short → journal
``pending`` → read the place ack → keep reading the already-subscribed private
orders/execution push for that order → journal each raw body. The ack does not
finish the trade. ``open`` / ``closed`` is written only when the later push
has the fill price. No REST query and no second order request.

An accept whose fill push misses the bound stays pending and is not
``partial_fill``. A reject stops immediately. No order answer before the
deadline stays ``partial_fill``.

Local mode uses the same journal and chrono steps. Fill prices come from the
signal books. No sockets.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from app.bot.paths import theta_trades_jsonl_path
from app.bot.private.coin_qty import (
    CoinQtyError,
    dec_str,
    exact_close_from_meta,
    reference_px,
    shared_from_meta,
)
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.send_legs import _attempt_ids, send_long, send_short
from app.bot.private.step_chrono import StepChrono
from app.bot.stub_broker import legs_for_spread_side, reverse_sides

RecvFn = Callable[[str], Optional[str]]
WaitFn = Callable[[], None]
ReadFrameFn = Callable[[float], str]

# Shared bound for both venues. One ack/ping must not end the wait.
SYNTHETIC_FILL_WAIT_SEC = 5.0
_WIRE_LOG = logging.getLogger("bbot.private.wire")


@dataclass
class PlaceSendResult:
    abort: Optional[str] = None
    completed: bool = False
    keep_pending: bool = False
    status: Optional[str] = None
    okx_fill_px: Optional[str] = None
    bybit_fill_px: Optional[str] = None
    latency_ms: Optional[int] = None
    fill_ts_ms: Optional[int] = None
    coin_qty: Optional[str] = None
    base_coin: Optional[str] = None
    side: Optional[str] = None
    intent_id: Optional[str] = None
    okx_filled_qty: Optional[str] = None
    bybit_filled_qty: Optional[str] = None
    send_attempted: bool = False


def _sides(spread_side: str, close_of: Optional[str]) -> tuple[str, str, str, bool]:
    """Return okx_side, bybit_side, position side (long|short), reduce_only."""
    label = str(spread_side).strip().lower()
    if label == "close":
        open_side = str(close_of or "").strip().lower()
        okx_side, bybit_side = reverse_sides(*legs_for_spread_side(open_side))
        pos = "long" if open_side == "open_long" else "short"
        return okx_side, bybit_side, pos, True
    okx_side, bybit_side = legs_for_spread_side(label)
    pos = "long" if label == "open_long" else "short"
    return okx_side, bybit_side, pos, False


def _append_trade_rows(data_root: Path, rows: list[Mapping[str, Any]]) -> None:
    if not rows:
        return
    by_date: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        ts_ms = int(row.get("signal_ts_ms") or row.get("fill_ts_ms") or 0)
        event_date = datetime.fromtimestamp(
            ts_ms / 1000.0, tz=timezone.utc
        ).date().isoformat()
        by_date.setdefault(event_date, []).append(row)
    for event_date, batch in by_date.items():
        path = theta_trades_jsonl_path(data_root, event_date)
        with path.open("a", encoding="utf-8") as fh:
            for rec in batch:
                fh.write(json.dumps(dict(rec), separators=(",", ":"), ensure_ascii=False))
                fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())


_PARSED_KEYS = (
    "fillPx",
    "avgPx",
    "accFillSz",
    "state",
    "orderStatus",
    "cumExecQty",
    "execQty",
    "fillSz",
    "execPrice",
    "avgPrice",
    "execTime",
    "fillTime",
    "updatedTime",
    "uTime",
    "orderId",
    "ordId",
    "orderLinkId",
    "clOrdId",
    "execId",
    "code",
    "sCode",
    "retCode",
    "sMsg",
    "retMsg",
)
_OKX_PX_KEYS = ("fillPx", "avgPx")
_BYBIT_PX_KEYS = ("execPrice", "avgPrice", "fillPx", "avgPx")
_ORDER_ID_KEYS = ("clOrdId", "ordId", "orderId", "orderLinkId")


def _is_zero_code(val: object) -> bool:
    return val == 0 or str(val) == "0"


def _parse_json_obj(body: Optional[str]) -> Optional[dict[str, Any]]:
    if not body:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _present_fields(data: Mapping[str, Any]) -> dict[str, Any]:
    """First value of each order field that the message actually contains."""
    found: dict[str, Any] = {}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key in _PARSED_KEYS and val not in (None, "") and key not in found:
                    found[str(key)] = val
                else:
                    walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


def _fill_px(body: Optional[str], exchange: str = "okx") -> Optional[str]:
    data = _parse_json_obj(body)
    if data is None:
        return None
    fields = _present_fields(data)
    keys = _BYBIT_PX_KEYS if str(exchange).strip().lower() == "bybit" else _OKX_PX_KEYS
    for key in keys:
        val = fields.get(key)
        if val not in (None, ""):
            try:
                px = Decimal(str(val))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if px.is_finite() and px > 0:
                return str(val)
    return None


def _matching_order_row(
    exchange: str, body: Optional[str], known_ids: set[str]
) -> Optional[dict[str, Any]]:
    """Return the one order row matching this attempt, never fields from siblings."""
    data = _parse_json_obj(body)
    if data is None:
        return None
    venue = str(exchange).strip().lower()
    if venue == "bybit":
        if not str(data.get("topic") or "").startswith("order"):
            return None
    else:
        arg = data.get("arg")
        if not isinstance(arg, dict) or str(arg.get("channel") or "") != "orders":
            return None
    rows = data.get("data")
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return None
    targets = {str(item) for item in known_ids if str(item)}
    if not targets:
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        ids = {
            str(row[key])
            for key in _ORDER_ID_KEYS
            if row.get(key) not in (None, "")
        }
        if ids & targets:
            return row
    return None


def _order_status(exchange: str, row: Mapping[str, Any]) -> str:
    key = "orderStatus" if str(exchange).strip().lower() == "bybit" else "state"
    return str(row.get(key) or "").strip().lower().replace("_", "").replace(" ", "")


_TERMINAL_ORDER_STATES = {
    "filled",
    "cancelled",
    "canceled",
    "partiallyfilledcanceled",
    "rejected",
    "deactivated",
    "expired",
}


def _row_fill_px(row: Mapping[str, Any], exchange: str) -> Optional[str]:
    # Terminal order rows must carry cumulative average price, not the last
    # execution fragment's price.
    key = "avgPrice" if str(exchange).strip().lower() == "bybit" else "avgPx"
    value = row.get(key)
    if value in (None, ""):
        return None
    try:
        px = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return str(value) if px.is_finite() and px > 0 else None


def _frame_order_ids(body: Optional[str]) -> set[str]:
    data = _parse_json_obj(body)
    if data is None:
        return set()
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key in _ORDER_ID_KEYS and val not in (None, ""):
                    found.add(str(val))
                else:
                    walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


def _frame_ack_ids(body: Optional[str]) -> set[str]:
    """Return current-request and client/order identifiers from one ACK."""
    data = _parse_json_obj(body)
    if data is None:
        return set()
    found = _frame_order_ids(body)
    for key in ("reqId", "id"):
        value = data.get(key)
        if value not in (None, ""):
            found.add(str(value))
    return found


def classify_order_answer(exchange: str, body: Optional[str]) -> Optional[str]:
    """``accept`` or ``reject`` for a place ack. ``None`` is not one.

    The orders/execution push is not an ack. OKX accepted: top-level ``code``
    is ``0`` and ``data[0].sCode`` is ``0`` or absent. Bybit accepted:
    ``retCode`` is ``0`` on ``order.create``. Any other place ack is a reject.
    """
    data = _parse_json_obj(body)
    if data is None:
        return None
    venue = str(exchange).strip().lower()
    event = str(data.get("event") or "")
    if event in {"subscribe", "login", "channel-conn-count", "notice"}:
        return None
    if venue == "bybit":
        op = str(data.get("op") or "")
        if op in {"subscribe", "auth", "ping", "pong"}:
            return None
        topic = str(data.get("topic") or "")
        if topic.startswith("order") or topic.startswith("execution"):
            return None
        if "retCode" not in data:
            if event == "error":
                return "reject"
            return None
        return "accept" if _is_zero_code(data.get("retCode")) else "reject"
    arg = data.get("arg")
    if isinstance(arg, dict) and str(arg.get("channel") or "") == "orders":
        return None
    if "code" not in data:
        if event == "error":
            return "reject"
        return None
    if not _is_zero_code(data.get("code")):
        return "reject"
    rows = data.get("data")
    if isinstance(rows, list) and rows and isinstance(rows[0], dict) and "sCode" in rows[0]:
        if not _is_zero_code(rows[0].get("sCode")):
            return "reject"
    return "accept"


def _is_matching_fill(exchange: str, body: str, known_ids: set[str]) -> bool:
    """A matching terminal Filled order row with a finite positive price."""
    if classify_order_answer(exchange, body) is not None:
        return False
    row = _matching_order_row(exchange, body, known_ids)
    return bool(row and _order_status(exchange, row) == "filled" and _row_fill_px(row, exchange))


@dataclass
class VenueWaitResult:
    """Place ack plus a matching terminal order update, if observed in time."""

    ack_body: Optional[str] = None
    # Kept under the established field name; this is now a terminal order row,
    # not any message that happens to contain a price.
    fill_body: Optional[str] = None
    terminal_state: Optional[str] = None
    verdict: Optional[str] = None
    ack_wall_ms: Optional[int] = None
    fill_wall_ms: Optional[int] = None


def drain_trade_fill(
    read_frame: ReadFrameFn,
    *,
    exchange: str,
    timeout_sec: float = SYNTHETIC_FILL_WAIT_SEC,
    order_ids: Optional[set[str]] = None,
) -> VenueWaitResult:
    """Read the place ack and matching terminal private order update.

    ``read_frame(timeout_sec)`` raises ``TimeoutError`` when no frame is ready.
    Ping/pong is skipped. An accept is recorded and does not finish the wait.
    Only an OKX ``state=filled`` or Bybit ``orderStatus=Filled`` update for this
    attempt ends the successful wait. Execution fragments and working/partial
    order states continue until terminal state or timeout. A private terminal
    order update may arrive before the trade ACK and is authoritative by itself.
    """
    from app.bot.private.ws_private import is_ws_noise_frame

    deadline = time.monotonic() + float(timeout_sec)
    venue = str(exchange).strip().lower()
    known = {str(item) for item in (order_ids or set()) if str(item)}
    result = VenueWaitResult()
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            raw = read_frame(min(1.0, remaining))
        except TimeoutError:
            continue
        if not raw:
            continue
        if is_ws_noise_frame(venue, raw):
            continue
        now_ms = int(time.time() * 1000)
        verdict = classify_order_answer(venue, raw)
        if verdict in {"accept", "reject"}:
            ack_ids = _frame_ack_ids(raw)
            if known and ack_ids and not (ack_ids & known):
                # A late response from an earlier action must not contribute
                # order IDs or acceptance to this attempt.
                continue
        if verdict == "reject":
            result.ack_body = raw
            result.verdict = "reject"
            result.ack_wall_ms = now_ms
            return result
        if verdict == "accept" and result.ack_body is None:
            result.ack_body = raw
            result.verdict = "accept"
            result.ack_wall_ms = now_ms
            known |= ack_ids
            continue
        row = _matching_order_row(venue, raw, known)
        if row is None:
            continue
        state = _order_status(venue, row)
        if state in _TERMINAL_ORDER_STATES:
            result.fill_body = raw
            result.terminal_state = state
            result.fill_wall_ms = now_ms
            return result
    return result


def read_warm_trade_frame(runtime: Any, timeout_sec: float) -> str:
    """One trade frame from the warm session. No new socket and no ``ws.recv``.

    Order matches ``PrivateStreamRuntime.recv_trade_ack``: the in-memory stash
    first (thread keepalive parks frames there while place is in flight), then
    ``trade_socket.recv_text``. On the production warm loop that pop is the
    listen-task inbound queue, not a second read of the websocket.
    """
    pop = getattr(runtime, "_pop_trade_inbound", None)
    if callable(pop):
        stashed = pop()
        if stashed:
            raw = str(stashed)
            private_order = False
            try:
                from app.bot.private.ws_warm_loop import _is_private_order_push

                private_order = _is_private_order_push(
                    str(getattr(runtime, "exchange", "")), raw
                )
            except Exception:  # noqa: BLE001 — capture classification is best-effort
                pass
            socket = (
                getattr(runtime, "private_socket", None)
                if private_order
                else getattr(runtime, "trade_socket", None)
            )
            transcript = getattr(socket, "_wire_transcript", None)
            if transcript is not None:
                try:
                    transcript.record_io(
                        direction="in",
                        venue=str(getattr(runtime, "exchange", "")),
                        socket="private" if private_order else "trade",
                        text=raw,
                        wall_ms=int(time.time() * 1000),
                        mono_ns=time.monotonic_ns(),
                        reconnect_generation=int(
                            getattr(runtime, "reconnect_generation", 0)
                        ),
                        run_id=str(transcript.run_id),
                        capture_stage="manager_consume",
                    )
                except Exception as exc:  # noqa: BLE001 — transcript marks capture failed
                    transcript.mark_unhealthy(type(exc).__name__)
                    _WIRE_LOG.error(
                        "wire_manager_consume_record_failed err=%s",
                        type(exc).__name__,
                    )
            return raw
    sock = getattr(runtime, "trade_socket", None)
    if sock is None:
        raise TimeoutError("trade socket missing")
    return str(sock.recv_text(timeout_sec=timeout_sec))


def _frames_from_recv(
    venue: str, raw: object
) -> list[tuple[str, str, Optional[int], str, Optional[str]]]:
    """Normalize one recv result into ``(venue, body, wall_ms, kind, verdict)``."""
    if isinstance(raw, VenueWaitResult):
        out: list[tuple[str, str, Optional[int], str, Optional[str]]] = []
        if raw.ack_body:
            out.append((venue, raw.ack_body, raw.ack_wall_ms, "ack", raw.verdict))
        if raw.fill_body:
            out.append((venue, raw.fill_body, raw.fill_wall_ms, "order", None))
        return out
    if not isinstance(raw, str) or not raw:
        return []
    verdict = classify_order_answer(venue, raw)
    if verdict is not None:
        return [(venue, raw, int(time.time() * 1000), "ack", verdict)]
    parsed = _parse_json_obj(raw)
    if parsed is not None:
        is_order = (
            str(parsed.get("topic") or "").startswith("order")
            if str(venue).strip().lower() == "bybit"
            else isinstance(parsed.get("arg"), dict)
            and str(parsed["arg"].get("channel") or "") == "orders"
        )
        if is_order:
            return [(venue, raw, int(time.time() * 1000), "order", None)]
    return [(venue, raw, None, "other", None)]


def _qty_matches(value: object, expected: Decimal) -> bool:
    try:
        actual = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return actual.is_finite() and expected.is_finite() and actual == expected


def _row_fill_qty(row: Mapping[str, Any], exchange: str) -> object:
    key = "cumExecQty" if str(exchange).strip().lower() == "bybit" else "accFillSz"
    return row.get(key)


def _book_body(venue: str, px: object) -> str:
    text = dec_str(px) if not isinstance(px, str) else px
    if venue == "okx":
        return json.dumps({"fillPx": text})
    return json.dumps({"avgPx": text})


def _place(
    *,
    data_root: Path,
    spread_side: str,
    base_coin: str,
    signal_ts_ms: int,
    okx_book: Mapping[str, Any],
    bybit_book: Mapping[str, Any],
    meta: object,
    transport: str,
    close_of: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
    intent_id: Optional[str] = None,
    sender: Any = None,
    credentials: Optional[LiveCredentials] = None,
    inst_id_code: Optional[int] = None,
    recv_fn: Optional[RecvFn] = None,
    wait_fn: Optional[WaitFn] = None,
    leverage_one: bool = False,
    close_qty: Optional[Mapping[str, object]] = None,
) -> PlaceSendResult:
    extra = extra if isinstance(extra, dict) else {}
    iid = str(intent_id or uuid.uuid4())
    coin = str(base_coin).strip().upper()
    chrono = StepChrono(
        data_root,
        intent_id=iid,
        signal_ts_ms=int(signal_ts_ms),
        signal_monotonic_ns=extra.get("signal_mono_ns"),
    )
    pre_send_stamps = extra.get("pre_send_stamps")
    if isinstance(pre_send_stamps, dict):
        pre_send_stamps["chrono_created"] = time.monotonic_ns()
        chrono.pre_send_timing(pre_send_stamps)
    send_attempted = False
    okx_side, bybit_side, pos_side, reduce_only = _sides(spread_side, close_of)
    event = "close" if reduce_only else "open"
    phase = event

    def _abort(code: str, *, keep_pending: bool = False) -> PlaceSendResult:
        chrono.abort(code)
        chrono.flush()
        return PlaceSendResult(
            abort=code,
            completed=False,
            keep_pending=keep_pending,
            base_coin=coin,
            side=pos_side,
            intent_id=iid,
            send_attempted=send_attempted,
        )

    chrono.enter("preprocess")
    try:
        okx_px = reference_px(okx_book, okx_side)
        bybit_px = reference_px(bybit_book, bybit_side)
        if reduce_only:
            if not isinstance(close_qty, Mapping) or not {
                "okx_filled_qty",
                "bybit_filled_qty",
            }.issubset(close_qty):
                raise CoinQtyError("close_qty_unavailable")
            sized = exact_close_from_meta(
                meta=meta,
                okx_px=okx_px,
                bybit_px=bybit_px,
                okx_sz=close_qty["okx_filled_qty"],
                bybit_qty=close_qty["bybit_filled_qty"],
            )
        else:
            sized = shared_from_meta(meta=meta, okx_px=okx_px, bybit_px=bybit_px)
    except CoinQtyError as exc:
        chrono.exit("preprocess")
        return _abort(exc.code)
    chrono.exit("preprocess")

    chrono.enter("channel_check")
    if transport == "live":
        from app.bot.private.private_leg_up import leg_up

        if not (leg_up("okx", coin) and leg_up("bybit", coin)):
            chrono.exit("channel_check")
            return _abort("private_channel_down")
    chrono.exit("channel_check")

    # Account leverage is set once at warmup. This read stays outside ws_send.
    if transport == "live" and leverage_one is not True:
        return _abort("leverage_not_one")

    chrono.enter("ws_send")
    send_abort: Optional[str] = None
    send_result = None
    try:
        if transport == "live":
            common = dict(
                coin=coin,
                okx_symbol=str(getattr(meta, "okx_symbol", "") or f"{coin}-USDT-SWAP"),
                bybit_symbol=str(getattr(meta, "bybit_symbol", "") or f"{coin}USDT"),
                okx_sz=dec_str(sized.okx_sz),
                bybit_qty=dec_str(sized.bybit_qty),
                reduce_only=reduce_only,
                sender=sender,
                credentials=credentials,
                inst_id_code=inst_id_code,
                intent_id=iid,
                signal_ts_ms=int(signal_ts_ms),
                phase=phase,
            )
            if okx_side == "buy":
                send_attempted = True
                sent = send_long(**common)
            else:
                send_attempted = True
                sent = send_short(**common)
            send_abort = sent.abort
            send_result = sent.send_result
    finally:
        chrono.exit("ws_send")
    if send_result is not None:
        chrono.send_timing(send_result.timings, phase=phase)
    chrono.flush()
    if send_abort:
        return _abort(send_abort)

    coin_qty = dec_str(sized.coin_qty)
    pending_row = {
        "schema_version": "bbot.synthetic_roll.v1",
        "intent_id": iid,
        "status": "pending",
        "event": event,
        "base_coin": coin,
        "side": pos_side,
        "coin_qty": coin_qty,
        "okx_sz": dec_str(sized.okx_sz),
        "bybit_qty": dec_str(sized.bybit_qty),
        "okx_side": okx_side,
        "bybit_side": bybit_side,
        "signal_ts_ms": int(signal_ts_ms),
        "reduce_only": bool(reduce_only),
    }
    chrono.enter("journal_pending")
    _append_trade_rows(data_root, [pending_row])
    chrono.exit("journal_pending")
    chrono.flush()

    chrono.enter("wait_fill")
    if wait_fn is not None:
        wait_fn()
    seen: list[tuple[str, str, Optional[int], str, Optional[str]]] = []
    if transport == "local":
        now_ms = int(time.time() * 1000)
        seen = [
            ("okx", _book_body("okx", okx_px), now_ms, "fill", None),
            ("bybit", _book_body("bybit", bybit_px), now_ms, "fill", None),
        ]
    elif recv_fn is not None:
        for venue in ("okx", "bybit"):
            seen.extend(_frames_from_recv(venue, recv_fn(venue)))
    chrono.exit("wait_fill")
    chrono.flush()

    venue_rows: list[dict[str, Any]] = []
    verdicts: dict[str, Optional[str]] = {}
    fill_px: dict[str, Optional[str]] = {}
    fill_wall: dict[str, Optional[int]] = {}
    fill_qty: dict[str, Optional[str]] = {"okx": None, "bybit": None}
    leg_errors: dict[str, str] = {}
    bybit_order_id, okx_order_id, _dual_id = _attempt_ids(iid)
    known_ids = {"okx": {okx_order_id}, "bybit": {bybit_order_id}}
    for venue, body, _wall_ms, kind, _verdict in seen:
        if kind == "ack":
            ack_ids = _frame_order_ids(body)
            if ack_ids & known_ids[venue]:
                known_ids[venue].update(ack_ids)
    for venue, body, wall_ms, kind, verdict in seen:
        chrono.venue_message(venue, wall_ms=wall_ms)
        row: dict[str, Any] = {
            "schema_version": "bbot.synthetic_roll.v1",
            "intent_id": iid,
            "status": "venue_message",
            "venue": venue,
            "body": body,
            "signal_ts_ms": int(signal_ts_ms),
            "base_coin": coin,
        }
        if wall_ms is not None:
            row["wall_ms"] = int(wall_ms)
        parsed = _parse_json_obj(body)
        matched_order = (
            _matching_order_row(venue, body, known_ids[venue])
            if kind == "order"
            else None
        )
        if parsed is not None:
            field_source = matched_order if kind == "order" else parsed
            fields = _present_fields(field_source) if field_source is not None else {}
            if fields:
                row["fields"] = fields
        if verdict is None and kind == "ack":
            verdict = classify_order_answer(venue, body)
        if verdict is not None:
            row["venue_verdict"] = verdict
            verdicts[venue] = verdict
        if kind == "order" and matched_order is not None:
            state = _order_status(venue, matched_order)
            row["venue_status"] = state or "unknown"
            if state in _TERMINAL_ORDER_STATES and state != "filled":
                reason = f"order_{state}" if state else "order_state_unknown"
                row["venue_verdict"] = "reject" if state == "rejected" else "incomplete"
                row["venue_reason"] = reason
                verdicts[venue] = row["venue_verdict"]
                leg_errors[venue] = "partial_fill" if "partial" in state else reason
            elif state == "filled":
                expected_qty = sized.okx_sz if venue == "okx" else sized.bybit_qty
                actual_qty = _row_fill_qty(matched_order, venue)
                if not _qty_matches(actual_qty, expected_qty):
                    row["local_validation"] = "fill_qty_mismatch"
                    leg_errors[venue] = "fill_qty_mismatch"
                else:
                    px = _row_fill_px(matched_order, venue)
                    if px is None:
                        row["local_validation"] = "invalid_fill_price"
                        leg_errors[venue] = "invalid_fill_price"
                    else:
                        fill_px[venue] = px
                        fill_wall[venue] = wall_ms
                        fill_qty[venue] = str(actual_qty)
        elif kind == "fill" and transport == "local":
            # Local simulation uses signal-book prices and has no venue qty row.
            px = _fill_px(body, venue)
            if px:
                fill_px[venue] = px
                fill_wall[venue] = wall_ms
                fill_qty[venue] = dec_str(
                    sized.okx_sz if venue == "okx" else sized.bybit_qty
                )
        venue_rows.append(row)
    if venue_rows:
        _append_trade_rows(data_root, venue_rows)
    chrono.flush()

    okx_fill = fill_px.get("okx")
    bybit_fill = fill_px.get("bybit")
    okx_verdict = verdicts.get("okx")
    bybit_verdict = verdicts.get("bybit")
    if okx_verdict == "reject" or bybit_verdict == "reject":
        both_reject = okx_verdict == "reject" and bybit_verdict == "reject"
        reason = "asymmetric_fill" if okx_fill or bybit_fill else "venue_reject"
        return _abort(reason, keep_pending=not (both_reject and not (okx_fill or bybit_fill)))
    if leg_errors:
        reason = "asymmetric_fill" if okx_fill or bybit_fill else next(iter(leg_errors.values()))
        return _abort(reason, keep_pending=True)
    if not (okx_fill and bybit_fill):
        reason = "asymmetric_fill" if okx_fill or bybit_fill else "fill_timeout"
        return _abort(reason, keep_pending=True)

    walls = [w for w in (fill_wall.get("okx"), fill_wall.get("bybit")) if w is not None]
    fill_ts = max(walls) if walls else int(time.time() * 1000)
    if fill_ts < int(signal_ts_ms):
        fill_ts = int(signal_ts_ms)
    latency = int(fill_ts) - int(signal_ts_ms)
    status = "closed" if reduce_only else "open"
    done = {
        "schema_version": "bbot.synthetic_roll.v1",
        "intent_id": iid,
        "status": status,
        "event": event,
        "base_coin": coin,
        "side": pos_side,
        "coin_qty": coin_qty,
        "okx_filled_qty": fill_qty["okx"],
        "bybit_filled_qty": fill_qty["bybit"],
        "okx_fill_px": okx_fill,
        "bybit_fill_px": bybit_fill,
        "latency_ms": latency,
        "fill_ts_ms": fill_ts,
        "signal_ts_ms": int(signal_ts_ms),
        "reduce_only": bool(reduce_only),
    }
    chrono.enter("fill_done")
    _append_trade_rows(data_root, [done])
    chrono.exit("fill_done")
    chrono.flush()
    return PlaceSendResult(
        abort=None,
        completed=True,
        keep_pending=False,
        status=status,
        okx_fill_px=okx_fill,
        bybit_fill_px=bybit_fill,
        latency_ms=latency,
        fill_ts_ms=fill_ts,
        coin_qty=coin_qty,
        base_coin=coin,
        side=pos_side,
        intent_id=iid,
        okx_filled_qty=fill_qty["okx"],
        bybit_filled_qty=fill_qty["bybit"],
        send_attempted=True,
    )


def place_local(
    *,
    data_root: Path,
    spread_side: str,
    base_coin: str,
    signal_ts_ms: int,
    okx_book: Mapping[str, Any],
    bybit_book: Mapping[str, Any],
    meta: object,
    close_of: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
    intent_id: Optional[str] = None,
    close_qty: Optional[Mapping[str, object]] = None,
    **_ignored: Any,
) -> PlaceSendResult:
    """No-network sender. Fills are the signal-book ask (buy) or bid (sell)."""
    return _place(
        data_root=Path(data_root),
        spread_side=spread_side,
        base_coin=base_coin,
        signal_ts_ms=signal_ts_ms,
        okx_book=okx_book,
        bybit_book=bybit_book,
        meta=meta,
        transport="local",
        close_of=close_of,
        extra=extra,
        intent_id=intent_id,
        close_qty=close_qty,
    )


def place_live(
    *,
    data_root: Path,
    spread_side: str,
    base_coin: str,
    signal_ts_ms: int,
    okx_book: Mapping[str, Any],
    bybit_book: Mapping[str, Any],
    meta: object,
    sender: Any,
    credentials: Optional[LiveCredentials],
    inst_id_code: Optional[int] = None,
    recv_fn: Optional[RecvFn] = None,
    wait_fn: Optional[WaitFn] = None,
    close_of: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
    intent_id: Optional[str] = None,
    leverage_one: bool = False,
    close_qty: Optional[Mapping[str, object]] = None,
    **_ignored: Any,
) -> PlaceSendResult:
    """Production live place. ``sender`` is injected in tests (no exchange sockets)."""
    return _place(
        data_root=Path(data_root),
        spread_side=spread_side,
        base_coin=base_coin,
        signal_ts_ms=signal_ts_ms,
        okx_book=okx_book,
        bybit_book=bybit_book,
        meta=meta,
        transport="live",
        close_of=close_of,
        extra=extra,
        intent_id=intent_id,
        sender=sender,
        credentials=credentials,
        inst_id_code=inst_id_code,
        recv_fn=recv_fn,
        wait_fn=wait_fn,
        leverage_one=leverage_one,
        close_qty=close_qty,
    )
