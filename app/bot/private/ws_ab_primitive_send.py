"""Contour B — primitive bybit_ws-style dual-leg send.

Historic shape (git ``a1ba2b1:bybit_ws.py``, not on this branch):

    await bybit_cmd_queue.put(bybit_order)
    await okx_cmd_queue.put(okx_order)

    async def sender():
        order_msg = await cmd_queue.get()
        await ws.send(json.dumps(order_msg))

No W6 recover / operator_approval / lease / journal prepare on this path.
Shares warm session + trade sockets when a live caller passes ``send_fn``.

Dry default: ``send_fn`` records the payload and returns. Never opens a socket.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.bot.private.ws_w6_dual_leg import W6_LEGS

SendFn = Callable[["PrimitiveSendItem"], None]


@dataclass(frozen=True)
class PrimitiveSendItem:
    """One venue payload for the long-lived sender."""

    venue: str  # bybit | okx
    payload: dict[str, Any]
    req_id: str
    phase: str  # open | close
    enqueued_ns: int


@dataclass
class PrimitiveSendResult:
    """Outcome of one dual enqueue + drain."""

    first_enqueued_ns: int
    second_enqueued_ns: int
    first_sent_ns: Optional[int] = None
    second_sent_ns: Optional[int] = None
    first_venue: str = "bybit"
    second_venue: str = "okx"
    items: list[PrimitiveSendItem] = field(default_factory=list)
    error: Optional[str] = None


def build_primitive_bybit_payload(
    *,
    side: str,
    qty: str,
    symbol: str = "TRUMPUSDT",
    reduce_only: bool = False,
    ts_ms: Optional[int] = None,
) -> dict[str, Any]:
    """Historic Bybit trade-WS ``order.create`` dict (W6 TRUMP qty/symbol)."""
    stamp = int(ts_ms if ts_ms is not None else (time.time() * 1000))
    side_norm = "Buy" if str(side).lower() == "buy" else "Sell"
    args: dict[str, Any] = {
        "symbol": symbol,
        "side": side_norm,
        "orderType": "Market",
        "qty": str(qty),
        "category": "linear",
        "positionIdx": 0,
    }
    if reduce_only:
        args["reduceOnly"] = True
    return {
        "op": "order.create",
        "header": {"X-BAPI-TIMESTAMP": str(stamp)},
        "args": [args],
    }


def build_primitive_okx_payload(
    *,
    side: str,
    qty: str,
    symbol: str = "TRUMP-USDT-SWAP",
    reduce_only: bool = False,
    cl_ord_id: Optional[str] = None,
    req_id: Optional[str] = None,
) -> dict[str, Any]:
    """Historic OKX private-WS ``order`` dict (W6 TRUMP qty/symbol)."""
    cid = cl_ord_id or f"okx{int(time.time_ns())}"[:32]
    rid = req_id or str(int(time.time() * 1000))
    args: dict[str, Any] = {
        "instId": symbol,
        "tdMode": "cross",
        "side": str(side).lower(),
        "ordType": "market",
        "sz": str(qty),
        "clOrdId": cid,
    }
    if reduce_only:
        args["reduceOnly"] = True
    return {"id": rid, "op": "order", "args": [args]}


def build_w6_dual_payloads(*, phase: str) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    """Same venues/symbols/qty as W6. ``phase`` is open or close."""
    bybit = W6_LEGS["bybit"]
    okx = W6_LEGS["okx"]
    reduce_only = phase == "close"
    b_side = bybit["flatten_side"] if reduce_only else bybit["open_side"]
    o_side = okx["flatten_side"] if reduce_only else okx["open_side"]
    b_req = f"abB{int(time.time_ns())}"[:32]
    o_req = f"abO{int(time.time_ns())}"[:32]
    bybit_payload = build_primitive_bybit_payload(
        side=str(b_side),
        qty=str(bybit["qty"]),
        symbol=str(bybit["symbol"]),
        reduce_only=reduce_only,
    )
    okx_payload = build_primitive_okx_payload(
        side=str(o_side),
        qty=str(okx["qty"]),
        symbol=str(okx["symbol"]),
        reduce_only=reduce_only,
        req_id=o_req,
        cl_ord_id=o_req,
    )
    return bybit_payload, okx_payload, b_req, o_req


class PrimitiveDualSender:
    """Long-lived asyncio senders + dual ``queue.put`` on the signal path.

    Analogous to ``bybit_private_listener`` / ``okx_private_listener`` sender
    tasks in historic ``bybit_ws.py``. The critical path after signal is
    ``enqueue_dual`` only.
    """

    def __init__(self, *, send_fn: Optional[SendFn] = None) -> None:
        self._send_fn = send_fn
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._sent_ns: dict[str, int] = {}
        self._sent_lock = threading.Lock()
        self._errors: list[str] = []
        self._bybit_q: Optional[asyncio.Queue[Optional[PrimitiveSendItem]]] = None
        self._okx_q: Optional[asyncio.Queue[Optional[PrimitiveSendItem]]] = None
        self._thread = threading.Thread(
            target=self._run_loop, name="ab-primitive-sender", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("primitive sender loop failed to start")

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
        queue: asyncio.Queue[Optional[PrimitiveSendItem]],
    ) -> None:
        while True:
            item = await queue.get()
            if item is None:
                return
            try:
                if self._send_fn is not None:
                    self._send_fn(item)
                else:
                    json.dumps(item.payload, separators=(",", ":"))
                with self._sent_lock:
                    self._sent_ns[f"{item.phase}:{item.venue}"] = time.monotonic_ns()
            except Exception as exc:  # noqa: BLE001 — never log payload/secrets
                self._errors.append(f"{venue}:{type(exc).__name__}")

    def enqueue_dual(
        self,
        *,
        bybit_payload: dict[str, Any],
        okx_payload: dict[str, Any],
        bybit_req_id: str,
        okx_req_id: str,
        phase: str,
    ) -> PrimitiveSendResult:
        """Critical path: put both legs, then wait until both senders drained."""
        if self._bybit_q is None or self._okx_q is None:
            raise RuntimeError("primitive sender queues not ready")
        t0 = time.monotonic_ns()
        b_item = PrimitiveSendItem(
            venue="bybit",
            payload=bybit_payload,
            req_id=bybit_req_id,
            phase=phase,
            enqueued_ns=t0,
        )
        o_item = PrimitiveSendItem(
            venue="okx",
            payload=okx_payload,
            req_id=okx_req_id,
            phase=phase,
            enqueued_ns=time.monotonic_ns(),
        )
        t1 = o_item.enqueued_ns
        fut_b = asyncio.run_coroutine_threadsafe(self._bybit_q.put(b_item), self._loop)
        fut_o = asyncio.run_coroutine_threadsafe(self._okx_q.put(o_item), self._loop)
        fut_b.result(timeout=5.0)
        fut_o.result(timeout=5.0)
        first_sent = self._wait_sent(phase, "bybit", timeout_sec=5.0)
        second_sent = self._wait_sent(phase, "okx", timeout_sec=5.0)
        return PrimitiveSendResult(
            first_enqueued_ns=t0,
            second_enqueued_ns=t1,
            first_sent_ns=first_sent,
            second_sent_ns=second_sent,
            items=[b_item, o_item],
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
