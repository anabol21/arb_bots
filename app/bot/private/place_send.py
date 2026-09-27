"""Thin synthetic_roll place path. Not ``LiveBroker.place``.

Order: preprocess (sides + shared coin qty) → send_long/send_short → journal
``pending`` → wait for fills → journal each raw venue body → completed row
only when both ``fillPx``/``avgPx`` exist. One price leaves the slot pending.

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


def _fill_px(body: Optional[str]) -> Optional[str]:
    if not body:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    found: list[tuple[str, str]] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key in ("fillPx", "avgPx") and val not in (None, ""):
                    found.append((str(key), str(val)))
                else:
                    walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    for prefer in ("fillPx", "avgPx"):
        for key, val in found:
            if key == prefer:
                return val
    return None


def drain_trade_fill(
    read_frame: ReadFrameFn,
    *,
    exchange: str,
    timeout_sec: float = SYNTHETIC_FILL_WAIT_SEC,
) -> Optional[str]:
    """Read until a frame carries ``fillPx`` or ``avgPx``, or the deadline.

    ``read_frame(timeout_sec)`` raises ``TimeoutError`` when no frame is ready.
    Ping/pong and other frames without a fill price are skipped. Returns
    ``None`` when the bound expires so the caller keeps ``partial_fill``.
    """
    from app.bot.private.ws_private import is_ws_noise_frame

    deadline = time.monotonic() + float(timeout_sec)
    venue = str(exchange).strip().lower()
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
        if _fill_px(raw):
            return raw
    return None


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
    bodies: list[tuple[str, str]] = []
    if transport == "local":
        bodies = [
            ("okx", _book_body("okx", okx_px)),
            ("bybit", _book_body("bybit", bybit_px)),
        ]
    elif recv_fn is not None:
        for venue in ("okx", "bybit"):
            body = recv_fn(venue)
            if body:
                bodies.append((venue, body))
    chrono.exit("wait_fill")
    chrono.flush()

    venue_rows: list[dict[str, Any]] = []
    for venue, body in bodies:
        chrono.venue_message(venue)
        venue_rows.append(
            {
                "schema_version": "bbot.synthetic_roll.v1",
                "intent_id": iid,
                "status": "venue_message",
                "venue": venue,
                "body": body,
                "signal_ts_ms": int(signal_ts_ms),
                "base_coin": coin,
            }
        )
    if venue_rows:
        _append_trade_rows(data_root, venue_rows)
    chrono.flush()

    px = {venue: _fill_px(body) for venue, body in bodies}
    okx_fill = px.get("okx")
    bybit_fill = px.get("bybit")
    if not okx_fill or not bybit_fill:
        return _abort("partial_fill", keep_pending=True)

    fill_ts = int(time.time() * 1000)
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
    )
