"""Live-manager default send: bybit_ws-style dual queue → ws.send.

Contour B / historic ``a1ba2b1:bybit_ws.py`` shape:

    queue.put(bybit_frame); queue.put(okx_frame)
    # long-lived sender: item = await queue.get(); await ws.send(...)

This is the DEFAULT live-manager path. Full W6
(recover → operator_approval → lease → prepare_approved + journal fsync
→ preflight) must not sit on signal → first ws.send.

Frames are built with the existing W6 trade builders (Bybit reqId+HMAC+
orderLinkId, OKX instIdCode). Primitive unsigned dicts are rejected —
Contour B initially broke on venue without those fields.

Warm private+trade sockets must already be up (``PrivateWarmSession``).
This module does not recover inflight leases, consume approvals, or
acquire leases. Journal / fill observation is after both sends. Trade ACK
wait (Contour B local position) is owned by ``live_broker`` /
``dual_leg_ack`` after ``enqueue_dual`` returns.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from app.bot.private.journal_v1 import new_opaque_id
from app.bot.private.order_plan import OrderPlan
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.wire_transcript import bind_place_on_process_transcript
from app.bot.private.ws_messages import (
    assert_okx_ws_message_id,
    build_bybit_trade_place,
    build_okx_trade_place,
    new_okx_ws_id,
    sanitize_okx_ws_id,
)

SEND_PATH_TRIVIAL = "trivial"
SEND_PATH_W6 = "w6"

# Explicit aliases so operators can name the proven contour.
_TRIVIAL_ALIASES = frozenset(
    {"", "trivial", "b", "contour_b", "queue", "bybit_ws", "default"}
)
_W6_ALIASES = frozenset({"w6", "a", "contour_a", "manager"})


class TrivialSendError(RuntimeError):
    """Fail-closed trivial dual-leg send (no silent drop)."""


SendFn = Callable[["TrivialSendItem"], None]


@dataclass(frozen=True)
class TrivialSendItem:
    """One already-signed venue frame for the long-lived sender."""

    venue: str  # bybit | okx
    text: str
    req_id: str
    phase: str  # open | close
    enqueued_ns: int
    intent_id: Optional[str] = None
    dual_leg_id: Optional[str] = None
    signal_ts_ms: Optional[int] = None


@dataclass
class TrivialSendResult:
    """Outcome of one dual enqueue + drain (both ws.send attempted)."""

    first_enqueued_ns: int
    second_enqueued_ns: int
    first_sent_ns: Optional[int] = None
    second_sent_ns: Optional[int] = None
    first_venue: str = "bybit"
    second_venue: str = "okx"
    items: list[TrivialSendItem] = field(default_factory=list)
    error: Optional[str] = None


def parse_inst_id_code_env(raw: Optional[str]) -> dict[str, int]:
    """Parse ``BBOT_OKX_INST_ID_CODES=SOL-USDT-SWAP:193761,BTC-USDT-SWAP:1``."""
    out: dict[str, int] = {}
    if not raw:
        return out
    for part in str(raw).split(","):
        item = part.strip()
        if not item or ":" not in item:
            continue
        symbol, code_raw = item.split(":", 1)
        try:
            code = int(str(code_raw).strip())
        except ValueError:
            continue
        if code > 0:
            out[symbol.strip()] = code
    return out


def resolve_live_send_path(env: Optional[Mapping[str, str]] = None) -> str:
    """Default is trivial. W6 only when ``BBOT_PRIVATE_SEND_PATH=w6``."""
    e = env if env is not None else {}
    raw = str(e.get("BBOT_PRIVATE_SEND_PATH") or "trivial").strip().lower()
    if raw in _TRIVIAL_ALIASES:
        return SEND_PATH_TRIVIAL
    if raw in _W6_ALIASES:
        return SEND_PATH_W6
    raise TrivialSendError(
        f"BBOT_PRIVATE_SEND_PATH must be trivial|w6, got {raw!r}"
    )


def w6_manager_opt_in(env: Optional[Mapping[str, str]] = None) -> bool:
    """True only when the operator explicitly selected the old W6 manager."""
    e = env if env is not None else {}
    if resolve_live_send_path(e) != SEND_PATH_W6:
        return False
    flag = str(e.get("BBOT_PRIVATE_W6") or e.get("W6_DUAL_LEG") or "").strip().lower()
    return flag in {"1", "true", "yes", "on"}


def make_frame_plan(
    *,
    venue: str,
    symbol: str,
    side: str,
    qty: str,
    reduce_only: bool = False,
    inst_id_code: Optional[int] = None,
    order_attempt_id: Optional[str] = None,
    dual_leg_id: Optional[str] = None,
) -> OrderPlan:
    """Local OrderPlan for W6 frame builders — no allowlist HTTP, no approval.

    Does **not** call ``build_order_plan`` (metadata + mark freshness +
    symbol allowlist). Gear2 coins are not all on the R3 BTC/TRUMP list.
    """
    oid = order_attempt_id or new_opaque_id("op")
    dual = dual_leg_id or new_opaque_id("dual")
    now = datetime.now(timezone.utc)
    ms = now.microsecond // 1000
    expires = now.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms:03d}Z"
    return OrderPlan(
        intent_id=new_opaque_id("intent"),
        leg_id=new_opaque_id("leg"),
        order_attempt_id=oid,
        venue=venue,
        symbol=symbol,
        symbol_alias=symbol,
        instrument_class="linear_perpetual",
        side=str(side).strip().lower(),
        mode="market",
        qty=str(qty),
        price=None,
        max_notional_usd="100",
        time_in_force="ioc",
        ttl_sec=0,
        expires_at_utc=expires,
        expires_at_monotonic_ns=time.monotonic_ns() + 60_000_000_000,
        k_live=1,
        post_only=False,
        reduce_only=bool(reduce_only),
        request_fingerprint="fp_trivial",
        dual_leg_id=dual,
        quantity_bucket="min_lot",
        notional_bucket="under_100_usd",
        position_side=None,
        inst_id_code=inst_id_code,
    )


def build_signed_place_text(
    *,
    venue: str,
    symbol: str,
    side: str,
    qty: str,
    credentials: Optional[LiveCredentials],
    reduce_only: bool = False,
    inst_id_code: Optional[int] = None,
    req_id: Optional[str] = None,
    order_attempt_id: Optional[str] = None,
    dual_leg_id: Optional[str] = None,
) -> tuple[str, str, OrderPlan]:
    """Return (compact frame text, req_id, plan) via W6 builders.

    Bybit requires credentials for HMAC. OKX requires a positive instIdCode.
    """
    key = str(venue).strip().lower()
    if key in {"bybit", "bybit_live"}:
        plan_venue = "bybit_live"
    elif key in {"okx", "okx_live"}:
        plan_venue = "okx_live"
    else:
        raise TrivialSendError(f"venue must be bybit|okx, got {venue!r}")
    # Bybit reqId may keep journal-style ``prefix_``. OKX trade WS ``id``
    # must be alphanumeric ≤32 — ``new_opaque_id("req")`` is illegal there.
    if plan_venue == "okx_live":
        rid = sanitize_okx_ws_id(req_id) if req_id else new_okx_ws_id()
    else:
        rid = req_id or new_opaque_id("req")[:32]
    plan = make_frame_plan(
        venue=plan_venue,
        symbol=symbol,
        side=side,
        qty=qty,
        reduce_only=reduce_only,
        inst_id_code=inst_id_code,
        order_attempt_id=order_attempt_id,
        dual_leg_id=dual_leg_id,
    )
    if plan_venue == "bybit_live":
        if credentials is None:
            raise TrivialSendError("bybit place requires credentials for HMAC")
        msg = build_bybit_trade_place(plan, credentials, req_id=rid)
    else:
        if inst_id_code is None and plan.inst_id_code is None:
            raise TrivialSendError("okx place requires positive instIdCode")
        msg = build_okx_trade_place(plan, req_id=rid, inst_id_code=inst_id_code)
    assert_signed_place_frame(plan_venue, msg.text)
    if plan_venue == "okx_live":
        try:
            rid = str(json.loads(msg.text)["id"])
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise TrivialSendError("okx place missing id after build") from exc
    return msg.text, rid, plan


def assert_signed_place_frame(venue: str, text: str) -> None:
    """Fail-closed: reject the unsigned primitive dicts that broke Contour B."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TrivialSendError("place frame is not JSON") from exc
    if not isinstance(data, dict):
        raise TrivialSendError("place frame must be an object")
    if venue == "bybit_live":
        if not data.get("reqId"):
            raise TrivialSendError("bybit place missing reqId")
        header = data.get("header") or {}
        if not isinstance(header, dict) or not header.get("X-BAPI-SIGN"):
            raise TrivialSendError("bybit place missing X-BAPI-SIGN")
        args = data.get("args") or []
        if not args or not isinstance(args[0], dict) or not args[0].get("orderLinkId"):
            raise TrivialSendError("bybit place missing orderLinkId")
        return
    if venue == "okx_live":
        if not data.get("id"):
            raise TrivialSendError("okx place missing id")
        try:
            assert_okx_ws_message_id(data.get("id"))
        except ValueError as exc:
            raise TrivialSendError("okx place id is not alphanumeric ≤32") from exc
        args = data.get("args") or []
        if not args or not isinstance(args[0], dict):
            raise TrivialSendError("okx place missing args")
        code = args[0].get("instIdCode")
        if not isinstance(code, int) or isinstance(code, bool) or code <= 0:
            raise TrivialSendError("okx place missing positive instIdCode")
        return
    raise TrivialSendError(f"unknown frame venue {venue!r}")


class TrivialDualSender:
    """Long-lived asyncio senders + dual ``queue.put`` on the signal path.

    Analogous to ``bybit_private_listener`` / ``okx_private_listener`` sender
    tasks in historic ``bybit_ws.py``. After signal the critical path is
    ``enqueue_dual`` only — both legs are put, then each sender ``ws.send``s
    without waiting for the other leg's fill.
    """

    def __init__(self, *, send_fn: Optional[SendFn] = None) -> None:
        self._send_fn = send_fn
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._sent_ns: dict[str, int] = {}
        self._sent_lock = threading.Lock()
        self._errors: list[str] = []
        self._bybit_q: Optional[asyncio.Queue[Optional[TrivialSendItem]]] = None
        self._okx_q: Optional[asyncio.Queue[Optional[TrivialSendItem]]] = None
        self._thread = threading.Thread(
            target=self._run_loop, name="trivial-dual-sender", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("trivial sender loop failed to start")

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._bybit_q = asyncio.Queue()
        self._okx_q = asyncio.Queue()
        self._loop.create_task(self._sender("bybit", self._bybit_q))
        self._loop.create_task(self._sender("okx", self._okx_q))
        self._ready.set()
        self._loop.run_forever()

    async def _sender(
        self,
        venue: str,
        queue: asyncio.Queue[Optional[TrivialSendItem]],
    ) -> None:
        while True:
            item = await queue.get()
            if item is None:
                return
            try:
                if self._send_fn is not None:
                    self._send_fn(item)
                with self._sent_lock:
                    self._sent_ns[f"{item.phase}:{item.venue}"] = time.monotonic_ns()
            except Exception as exc:  # noqa: BLE001 — never log payload/secrets
                self._errors.append(f"{venue}:{type(exc).__name__}")

    def enqueue_dual(
        self,
        *,
        bybit_text: str,
        okx_text: str,
        bybit_req_id: str,
        okx_req_id: str,
        phase: str,
        intent_id: Optional[str] = None,
        dual_leg_id: Optional[str] = None,
        signal_ts_ms: Optional[int] = None,
    ) -> TrivialSendResult:
        """Critical path: put both legs, then wait until both senders drained.

        Does not wait for venue ack/fill. Second put does not wait on first
        send completing. Correlation ids are bound before the puts (no I/O).
        """
        if self._bybit_q is None or self._okx_q is None:
            raise RuntimeError("trivial sender queues not ready")
        bind_place_on_process_transcript(
            req_ids=(("bybit", bybit_req_id), ("okx", okx_req_id)),
            intent_id=intent_id,
            dual_leg_id=dual_leg_id,
            signal_ts_ms=signal_ts_ms,
            phase=phase,
        )
        with self._sent_lock:
            self._sent_ns.pop(f"{phase}:bybit", None)
            self._sent_ns.pop(f"{phase}:okx", None)
        t0 = time.monotonic_ns()
        b_item = TrivialSendItem(
            venue="bybit",
            text=bybit_text,
            req_id=bybit_req_id,
            phase=phase,
            enqueued_ns=t0,
            intent_id=intent_id,
            dual_leg_id=dual_leg_id,
            signal_ts_ms=signal_ts_ms,
        )
        o_item = TrivialSendItem(
            venue="okx",
            text=okx_text,
            req_id=okx_req_id,
            phase=phase,
            enqueued_ns=time.monotonic_ns(),
            intent_id=intent_id,
            dual_leg_id=dual_leg_id,
            signal_ts_ms=signal_ts_ms,
        )
        t1 = o_item.enqueued_ns
        fut_b = asyncio.run_coroutine_threadsafe(self._bybit_q.put(b_item), self._loop)
        fut_o = asyncio.run_coroutine_threadsafe(self._okx_q.put(o_item), self._loop)
        fut_b.result(timeout=5.0)
        fut_o.result(timeout=5.0)
        first_sent = self._wait_sent(phase, "bybit", timeout_sec=5.0)
        second_sent = self._wait_sent(phase, "okx", timeout_sec=5.0)
        return TrivialSendResult(
            first_enqueued_ns=t0,
            second_enqueued_ns=t1,
            first_sent_ns=first_sent,
            second_sent_ns=second_sent,
            items=[b_item, o_item],
            error=self._errors[-1] if self._errors else None,
        )

    def enqueue_one(
        self,
        *,
        venue: str,
        text: str,
        req_id: str,
        phase: str,
        intent_id: Optional[str] = None,
        dual_leg_id: Optional[str] = None,
        signal_ts_ms: Optional[int] = None,
    ) -> TrivialSendResult:
        """Put one already-signed frame (reduce-only flatten of one accepted leg)."""
        key = str(venue).strip().lower()
        if key not in {"bybit", "okx"}:
            raise TrivialSendError(f"enqueue_one venue must be bybit|okx, got {venue!r}")
        queue = self._bybit_q if key == "bybit" else self._okx_q
        if queue is None:
            raise RuntimeError("trivial sender queues not ready")
        with self._sent_lock:
            self._sent_ns.pop(f"{phase}:{key}", None)
        t0 = time.monotonic_ns()
        item = TrivialSendItem(
            venue=key,
            text=text,
            req_id=req_id,
            phase=phase,
            enqueued_ns=t0,
            intent_id=intent_id,
            dual_leg_id=dual_leg_id,
            signal_ts_ms=signal_ts_ms,
        )
        fut = asyncio.run_coroutine_threadsafe(queue.put(item), self._loop)
        fut.result(timeout=5.0)
        sent = self._wait_sent(phase, key, timeout_sec=5.0)
        return TrivialSendResult(
            first_enqueued_ns=t0,
            second_enqueued_ns=t0,
            first_sent_ns=sent,
            second_sent_ns=sent if sent is not None else None,
            first_venue=key,
            second_venue=key,
            items=[item],
            error=self._errors[-1] if self._errors else None,
        )

    def _wait_sent(self, phase: str, venue: str, *, timeout_sec: float) -> Optional[int]:
        deadline = time.monotonic() + float(timeout_sec)
        key = f"{phase}:{venue}"
        while time.monotonic() < deadline:
            with self._sent_lock:
                ns = self._sent_ns.get(key)
            if ns is not None:
                return ns
            time.sleep(0.001)
        return None

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        if self._bybit_q is not None and self._okx_q is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._bybit_q.put(None), self._loop).result(
                    timeout=2.0
                )
                asyncio.run_coroutine_threadsafe(self._okx_q.put(None), self._loop).result(
                    timeout=2.0
                )
            except Exception:  # noqa: BLE001
                pass
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:  # noqa: BLE001
            pass
        self._thread.join(timeout=2.0)
        if not self._loop.is_closed():
            self._loop.close()


def send_signed_dual(
    *,
    sender: TrivialDualSender,
    bybit_text: str,
    okx_text: str,
    bybit_req_id: str,
    okx_req_id: str,
    phase: str,
    place_io=None,
    intent_id: Optional[str] = None,
    dual_leg_id: Optional[str] = None,
    signal_ts_ms: Optional[int] = None,
) -> TrivialSendResult:
    """Put both signed frames. ``place_io`` is an optional context manager
    (warm ``place_io_section``) — a lock, not a multi-second pre-send check.
    """
    kwargs = {
        "bybit_text": bybit_text,
        "okx_text": okx_text,
        "bybit_req_id": bybit_req_id,
        "okx_req_id": okx_req_id,
        "phase": phase,
        "intent_id": intent_id,
        "dual_leg_id": dual_leg_id,
        "signal_ts_ms": signal_ts_ms,
    }
    if place_io is None:
        return sender.enqueue_dual(**kwargs)
    with place_io:
        return sender.enqueue_dual(**kwargs)


def warm_trade_send_fn(session: Any) -> SendFn:
    """``send_fn`` that writes to an already-warm trade socket. No recover."""

    def _send(item: TrivialSendItem) -> None:
        runtime = (
            session.bybit_runtime if item.venue == "bybit" else session.okx_runtime
        )
        sock = getattr(runtime, "trade_socket", None)
        if sock is None:
            raise TrivialSendError("trade socket missing")
        sock.send_text(item.text)
        note = getattr(runtime, "note_trade_activity", None)
        if callable(note):
            note()

    return _send
