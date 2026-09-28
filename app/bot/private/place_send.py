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
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from app.bot.paths import theta_trades_jsonl_path
from app.bot.private.coin_qty import (
    CoinQtyError,
    dec_str,
    reference_px,
    shared_from_meta,
)
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.send_legs import send_long, send_short
from app.bot.private.step_chrono import StepChrono
from app.bot.stub_broker import legs_for_spread_side, reverse_sides

RecvFn = Callable[[str], Optional[str]]
WaitFn = Callable[[], None]
ReadFrameFn = Callable[[float], str]

# Shared bound for both venues. One ack/ping must not end the wait.
SYNTHETIC_FILL_WAIT_SEC = 5.0


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
    "execPrice",
    "avgPrice",
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
            return str(val)
    return None


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
    """Later private push for this order, with a fill price. Not the place ack."""
    if classify_order_answer(exchange, body) is not None:
        return False
    if not _fill_px(body, exchange):
        return False
    if not known_ids:
        return False
    return bool(_frame_order_ids(body) & known_ids)


@dataclass
class VenueWaitResult:
    """Place ack plus the later fill push, if it arrived before the bound."""

    ack_body: Optional[str] = None
    fill_body: Optional[str] = None
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
    """Read the place ack, then the matching orders/execution push.

    ``read_frame(timeout_sec)`` raises ``TimeoutError`` when no frame is ready.
    Ping/pong is skipped. An accept is recorded and does not finish the wait.
    The fill is a later frame with ``fillPx``/``avgPx`` (Bybit ``execPrice`` or
    ``avgPrice``) whose ``clOrdId`` or ``ordId`` matches the ack or ``order_ids``.
    A reject stops immediately. No ack before the deadline leaves both bodies
    empty so the caller keeps ``partial_fill``. An accept with no fill push
    leaves ``fill_body`` empty.
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
        if verdict == "reject":
            result.ack_body = raw
            result.verdict = "reject"
            result.ack_wall_ms = now_ms
            return result
        if verdict == "accept" and result.ack_body is None:
            result.ack_body = raw
            result.verdict = "accept"
            result.ack_wall_ms = now_ms
            known |= _frame_order_ids(raw)
            if result.fill_body and _is_matching_fill(venue, result.fill_body, known):
                return result
            continue
        if _is_matching_fill(venue, raw, known):
            result.fill_body = raw
            result.fill_wall_ms = now_ms
            if result.verdict == "accept":
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
            return str(stashed)
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
            out.append((venue, raw.fill_body, raw.fill_wall_ms, "fill", None))
        return out
    if not isinstance(raw, str) or not raw:
        return []
    verdict = classify_order_answer(venue, raw)
    if verdict is not None:
        return [(venue, raw, int(time.time() * 1000), "ack", verdict)]
    if _fill_px(raw, venue):
        return [(venue, raw, int(time.time() * 1000), "fill", None)]
    return [(venue, raw, None, "other", None)]


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
) -> PlaceSendResult:
    del extra  # manager passes the live-canary extra dict; this path does not use it
    iid = str(intent_id or uuid.uuid4())
    coin = str(base_coin).strip().upper()
    chrono = StepChrono(data_root, intent_id=iid, signal_ts_ms=int(signal_ts_ms))
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
        )

    chrono.enter("preprocess")
    try:
        okx_px = reference_px(okx_book, okx_side)
        bybit_px = reference_px(bybit_book, bybit_side)
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
                sent = send_long(**common)
            else:
                sent = send_short(**common)
            send_abort = sent.abort
    finally:
        chrono.exit("ws_send")
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
        if parsed is not None:
            fields = _present_fields(parsed)
            if fields:
                row["fields"] = fields
        if verdict is None and kind == "ack":
            verdict = classify_order_answer(venue, body)
        if verdict is not None:
            row["venue_verdict"] = verdict
            verdicts[venue] = verdict
        if kind == "fill":
            px = _fill_px(body, venue)
            if px:
                fill_px[venue] = px
                fill_wall[venue] = wall_ms
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
        return _abort("venue_reject", keep_pending=not both_reject)
    if not (okx_fill and bybit_fill):
        if okx_verdict == "accept" or bybit_verdict == "accept":
            chrono.abort("accepted_no_fill")
            chrono.flush()
            return PlaceSendResult(
                abort=None,
                completed=False,
                keep_pending=True,
                status="accepted",
                okx_fill_px=okx_fill,
                bybit_fill_px=bybit_fill,
                coin_qty=coin_qty,
                base_coin=coin,
                side=pos_side,
                intent_id=iid,
            )
        return _abort("partial_fill", keep_pending=True)

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
    )
