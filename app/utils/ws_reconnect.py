"""Planned WebSocket reconnect policy for the production collector.

Lifecycle only: classify close, backoff, fleet wave gate, per-connection
budget, structured events. Does not parse exchange payloads or compute spreads.

Enabled by SPREAD_WS_RECONNECT_V2=1 (default off).
"""

from __future__ import annotations

import asyncio
import heapq
import os
import random
import time
from collections import defaultdict, deque
from typing import Any, Callable, Optional


CLEAN_CLOSE_CODES = {1000, 1001}
BOOK_QUOTE_KEYS = (
    "bid_price",
    "bid_size",
    "ask_price",
    "ask_size",
    "ts_exchange",
    "local_recv_ts_ms",
    "delivery_latency_ms",
    "cts_exchange",
)

DEFAULT_BACKOFF_BASE_SEC = 1.0
DEFAULT_BACKOFF_CAP_SEC = 60.0
DEFAULT_JITTER_FRAC = 0.20
DEFAULT_WAVE_WINDOW_SEC = 60.0
DEFAULT_WAVE_STORM_THRESHOLD = 8
DEFAULT_CONNECT_INTERVAL_SEC = 0.5
DEFAULT_BUDGET_WINDOW_SEC = 3600.0
DEFAULT_BUDGET_MAX = 2
DEFAULT_UNRECOVERED_AFTER_ATTEMPTS = 6
DEFAULT_CONNECT_PER_SEC = 3.0
BOOK_CONNECT_PRIORITY = 0
CANDLE_CONNECT_PRIORITY = 1


def connect_per_sec() -> float:
    raw = os.environ.get("SPREAD_WS_CONNECT_PER_SEC", "").strip()
    if not raw:
        return DEFAULT_CONNECT_PER_SEC
    return max(0.1, float(raw))


def subscribe_batch_size(*, v2: bool) -> int:
    """0 = always-on scheduler. v1 default 30; v2 default 0. Env overrides both."""
    raw = os.environ.get("SPREAD_SUBSCRIBE_BATCH_SIZE", "").strip()
    if raw:
        return max(0, int(raw))
    return 0 if v2 else 30


class ExchangeConnectScheduler:
    """Always-on per-exchange connect rate limit for first connect and reconnect."""

    def __init__(
        self,
        *,
        connects_per_sec: Optional[float] = None,
        jitter_frac: float = 0.15,
        rng: Optional[random.Random] = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        rate = DEFAULT_CONNECT_PER_SEC if connects_per_sec is None else float(connects_per_sec)
        self.interval = 1.0 / max(0.1, rate)
        self.jitter_frac = jitter_frac
        self._rng = rng if rng is not None else random
        self._monotonic = monotonic
        self._sleep = sleeper
        self._seq = 0
        self._last: dict[str, float] = {}
        self._waiters: dict[str, list[tuple[int, int, asyncio.Future]]] = defaultdict(list)
        self._pumping: set[str] = set()

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def acquire(self, exchange: str, *, priority: int = BOOK_CONNECT_PRIORITY, coin: str = "") -> None:
        del coin  # reserved for logs / pairing diagnostics
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        heapq.heappush(self._waiters[exchange], (int(priority), self._next_seq(), fut))
        if exchange not in self._pumping:
            self._pumping.add(exchange)
            loop.create_task(self._drain(exchange))
        try:
            await fut
        except asyncio.CancelledError:
            if not fut.done():
                fut.cancel()
            raise

    async def _drain(self, exchange: str) -> None:
        try:
            while self._waiters[exchange]:
                now = self._monotonic()
                last = self._last.get(exchange)
                if last is not None:
                    wait = self.interval - (now - last)
                    if wait > 0:
                        wait += self.interval * self.jitter_frac * self._rng.random()
                        await self._sleep(wait)
                    elif self.jitter_frac:
                        await self._sleep(
                            self.interval * self.jitter_frac * self._rng.random() * 0.25
                        )
                if not self._waiters[exchange]:
                    break
                _priority, _seq, fut = heapq.heappop(self._waiters[exchange])
                if fut.done():
                    continue
                self._last[exchange] = self._monotonic()
                fut.set_result(True)
        finally:
            self._pumping.discard(exchange)
            if self._waiters[exchange]:
                asyncio.get_running_loop().create_task(self._drain(exchange))


def reconnect_v2_enabled() -> bool:
    return os.environ.get("SPREAD_WS_RECONNECT_V2", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def planned_backoff_sec(
    attempt: int,
    *,
    base: float = DEFAULT_BACKOFF_BASE_SEC,
    cap: float = DEFAULT_BACKOFF_CAP_SEC,
    jitter_frac: float = DEFAULT_JITTER_FRAC,
    rng: Optional[random.Random] = None,
) -> float:
    """attempt is 1-based consecutive failures since last subscribe_ok."""
    safe_attempt = max(1, int(attempt))
    delay = min(cap, base * (2 ** (safe_attempt - 1)))
    jitter_src = rng if rng is not None else random
    return delay + (delay * jitter_frac * jitter_src.random())


def _close_code(exc: Optional[BaseException], ws: Any = None) -> Optional[int]:
    for obj in (exc, ws):
        if obj is None:
            continue
        for attr in ("code", "close_code"):
            value = getattr(obj, attr, None)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def classify_close(exc: Optional[BaseException], ws: Any = None) -> str:
    """Return clean | abrupt | keepalive | protocol_error | error."""
    code = _close_code(exc, ws)
    name = type(exc).__name__ if exc is not None else ""
    text = " ".join(
        part
        for part in (
            name,
            str(exc) if exc is not None else "",
            str(getattr(exc, "reason", "") or ""),
            str(getattr(ws, "close_reason", "") or "") if ws is not None else "",
        )
        if part
    ).lower()

    if "keepalive ping timeout" in text or (
        "1011" in text and "ping" in text
    ):
        return "keepalive"
    if name in {"InvalidMessage", "PayloadTooBig", "ProtocolError"} or (
        "protocol" in text and "error" in text
    ):
        return "protocol_error"
    if name == "ConnectionClosedOK" or code in CLEAN_CLOSE_CODES:
        return "clean"
    if code == 1006 or "no close frame" in text:
        return "abrupt"
    if name in {"ConnectionClosed", "ConnectionClosedError", "ConnectionClosedOK"}:
        return "abrupt"
    return "error"


def invalidate_book_quote(quotes: dict[str, Any], base_coin: str, exchange: str) -> None:
    """Drop cached book for one leg so calc_and_store_spread sees an explicit gap."""
    coin_state = quotes.get(base_coin)
    if not isinstance(coin_state, dict):
        return
    book = coin_state.get(exchange)
    if not isinstance(book, dict):
        return
    for key in BOOK_QUOTE_KEYS:
        if key in book:
            book[key] = None


def conn_key(exchange: str, channel: str, base_coin: str) -> str:
    return f"{exchange}:{channel}:{base_coin}"


class ReconnectController:
    """Process-wide reconnect accounting: wave, budget, counters."""

    def __init__(
        self,
        *,
        wave_window_sec: float = DEFAULT_WAVE_WINDOW_SEC,
        wave_storm_threshold: int = DEFAULT_WAVE_STORM_THRESHOLD,
        connect_interval_sec: float = DEFAULT_CONNECT_INTERVAL_SEC,
        budget_window_sec: float = DEFAULT_BUDGET_WINDOW_SEC,
        budget_max: int = DEFAULT_BUDGET_MAX,
        unrecovered_after_attempts: int = DEFAULT_UNRECOVERED_AFTER_ATTEMPTS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.wave_window_sec = wave_window_sec
        self.wave_storm_threshold = wave_storm_threshold
        self.connect_interval_sec = connect_interval_sec
        self.budget_window_sec = budget_window_sec
        self.budget_max = budget_max
        self.unrecovered_after_attempts = unrecovered_after_attempts
        self._monotonic = monotonic
        self._wave: dict[str, deque[float]] = defaultdict(deque)
        self._budget: dict[str, deque[float]] = defaultdict(deque)
        self._last_connect: dict[str, float] = {}
        self.counters = {
            "disconnects_total": 0,
            "planned_reconnects_total": 0,
            "unplanned_reconnects_total": 0,
            "subscribe_ok_total": 0,
            "unrecovered_total": 0,
            "budget_exceeded_total": 0,
            "protocol_errors_total": 0,
            "active_unrecovered": 0,
        }
        self._unrecovered_keys: set[str] = set()

    def _prune(self, events: deque[float], now: float, window_sec: float) -> None:
        while events and now - events[0] > window_sec:
            events.popleft()

    def wave_60s(self, exchange: str, now: Optional[float] = None) -> int:
        ts = self._monotonic() if now is None else now
        events = self._wave[exchange]
        self._prune(events, ts, self.wave_window_sec)
        return len(events)

    def in_storm(self, exchange: str, now: Optional[float] = None) -> bool:
        return self.wave_60s(exchange, now) > self.wave_storm_threshold

    def record_wave(self, exchange: str, now: Optional[float] = None) -> int:
        ts = self._monotonic() if now is None else now
        events = self._wave[exchange]
        events.append(ts)
        self._prune(events, ts, self.wave_window_sec)
        return len(events)

    def budget_count(self, key: str, now: Optional[float] = None) -> int:
        ts = self._monotonic() if now is None else now
        events = self._budget[key]
        self._prune(events, ts, self.budget_window_sec)
        return len(events)

    def budget_exceeded(self, key: str, now: Optional[float] = None) -> bool:
        return self.budget_count(key, now) >= self.budget_max

    def record_planned(self, key: str, now: Optional[float] = None) -> int:
        ts = self._monotonic() if now is None else now
        events = self._budget[key]
        events.append(ts)
        self._prune(events, ts, self.budget_window_sec)
        self.counters["planned_reconnects_total"] += 1
        return len(events)

    def time_until_budget_slot(self, key: str, now: Optional[float] = None) -> float:
        ts = self._monotonic() if now is None else now
        events = self._budget[key]
        self._prune(events, ts, self.budget_window_sec)
        if len(events) < self.budget_max:
            return 0.0
        return max(0.0, self.budget_window_sec - (ts - events[0]))

    def mark_connect_slot(self, exchange: str, now: Optional[float] = None) -> None:
        self._last_connect[exchange] = self._monotonic() if now is None else now

    def connect_slot_wait_sec(self, exchange: str, now: Optional[float] = None) -> float:
        ts = self._monotonic() if now is None else now
        if not self.in_storm(exchange, ts):
            return 0.0
        last = self._last_connect.get(exchange)
        if last is None:
            return 0.0
        return max(0.0, self.connect_interval_sec - (ts - last))

    def session(self, exchange: str, channel: str, base_coin: str) -> "ReconnectSession":
        return ReconnectSession(self, exchange, channel, base_coin)

    def heartbeat_fields(self) -> dict[str, Any]:
        return {
            "reconnect_mode": "v2",
            "ws_disconnects": self.counters["disconnects_total"],
            "ws_reconnect_planned": self.counters["planned_reconnects_total"],
            "ws_reconnect_unplanned": self.counters["unplanned_reconnects_total"],
            "ws_subscribe_ok": self.counters["subscribe_ok_total"],
            "ws_unrecovered": self.counters["unrecovered_total"],
            "ws_unrecovered_active": len(self._unrecovered_keys),
            "ws_budget_exceeded": self.counters["budget_exceeded_total"],
            "ws_protocol_errors": self.counters["protocol_errors_total"],
            "ws_wave_okx_60s": self.wave_60s("okx"),
            "ws_wave_bybit_60s": self.wave_60s("bybit"),
        }


class ReconnectSession:
    def __init__(
        self,
        controller: ReconnectController,
        exchange: str,
        channel: str,
        base_coin: str,
    ) -> None:
        self.controller = controller
        self.exchange = exchange
        self.channel = channel
        self.base_coin = base_coin
        self.key = conn_key(exchange, channel, base_coin)
        self.attempt = 0
        self.ever_subscribed = False
        self._logged_unrecovered = False

    def on_disconnect(
        self,
        exc: BaseException,
        ws: Any = None,
        *,
        quotes: Optional[dict[str, Any]] = None,
        book_exchange: Optional[str] = None,
    ) -> dict[str, Any]:
        reason_class = classify_close(exc, ws)
        close_code = _close_code(exc, ws)
        self.attempt += 1
        ctrl = self.controller
        ctrl.counters["disconnects_total"] += 1
        if reason_class == "protocol_error":
            ctrl.counters["protocol_errors_total"] += 1
            ctrl.counters["unplanned_reconnects_total"] += 1
        wave_60s = ctrl.record_wave(self.exchange)
        backoff_sec = planned_backoff_sec(self.attempt)
        unrecovered = self.attempt >= ctrl.unrecovered_after_attempts
        if unrecovered and self.key not in ctrl._unrecovered_keys:
            ctrl._unrecovered_keys.add(self.key)
            ctrl.counters["unrecovered_total"] += 1
            self._logged_unrecovered = True
        if quotes is not None and book_exchange is not None:
            invalidate_book_quote(quotes, self.base_coin, book_exchange)
        return {
            "event": "ws_disconnect",
            "exchange": self.exchange,
            "channel": self.channel,
            "coin": self.base_coin,
            "close_code": close_code,
            "reason_class": reason_class,
            "attempt": self.attempt,
            "backoff_ms": int(round(backoff_sec * 1000)),
            "wave_60s": wave_60s,
            "exception_class": type(exc).__name__,
            "exception": str(exc),
            "unrecovered": unrecovered,
            "planned": reason_class != "protocol_error",
            "backoff_sec": backoff_sec,
        }

    def plan_reconnect(self, disconnect: dict[str, Any]) -> dict[str, Any]:
        planned = bool(disconnect.get("planned", True))
        event = "ws_reconnect_planned" if planned else "ws_reconnect_unplanned"
        return {
            "event": event,
            "exchange": self.exchange,
            "channel": self.channel,
            "coin": self.base_coin,
            "close_code": disconnect.get("close_code"),
            "reason_class": disconnect.get("reason_class"),
            "attempt": self.attempt,
            "backoff_ms": disconnect.get("backoff_ms"),
            "wave_60s": self.controller.wave_60s(self.exchange),
        }

    def mark_subscribe_ok(self) -> dict[str, Any]:
        self.ever_subscribed = True
        self.attempt = 0
        self._logged_unrecovered = False
        if self.key in self.controller._unrecovered_keys:
            self.controller._unrecovered_keys.discard(self.key)
        self.controller.counters["subscribe_ok_total"] += 1
        return {
            "event": "ws_subscribe_ok",
            "exchange": self.exchange,
            "channel": self.channel,
            "coin": self.base_coin,
            "attempt": 0,
            "wave_60s": self.controller.wave_60s(self.exchange),
        }

    def unrecovered_event(self, disconnect: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not disconnect.get("unrecovered") or not self._logged_unrecovered:
            return None
        # Emit once when the session first crosses the threshold.
        self._logged_unrecovered = False
        return {
            "event": "ws_unrecovered",
            "exchange": self.exchange,
            "channel": self.channel,
            "coin": self.base_coin,
            "close_code": disconnect.get("close_code"),
            "reason_class": disconnect.get("reason_class"),
            "attempt": self.attempt,
            "backoff_ms": disconnect.get("backoff_ms"),
            "wave_60s": disconnect.get("wave_60s"),
        }
