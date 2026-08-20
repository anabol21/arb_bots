"""Public one-symbol L1 adapter for W4 post-only pricing (ask × 0.99).

Separate from private/trade sockets. Never logs raw frames or account data.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any, Mapping, Optional, Protocol

from app.bot.private.order_metadata import InstrumentMetadata, parse_decimal
from app.bot.private.ws_socket import PrivateWsSocket, open_private_socket

LOG = logging.getLogger("bbot.private.ws_l1")

L1_MAX_AGE_SEC = 2.0
ASK_AWAY_FACTOR = Decimal("0.99")


class L1Error(RuntimeError):
    """Public L1 quote missing, stale, or unusable for W4 pricing."""


@dataclass(frozen=True)
class PublicL1Quote:
    exchange: str
    symbol: str
    best_ask: Decimal
    asof_mono_ns: int

    def age_sec(self, *, now_mono_ns: Optional[int] = None) -> float:
        now = now_mono_ns if now_mono_ns is not None else time.monotonic_ns()
        return max(0.0, (now - self.asof_mono_ns) / 1_000_000_000)

    def assert_fresh(
        self,
        *,
        max_age_sec: float = L1_MAX_AGE_SEC,
        now_mono_ns: Optional[int] = None,
    ) -> None:
        if self.age_sec(now_mono_ns=now_mono_ns) > float(max_age_sec):
            raise L1Error("public L1 quote stale")


def limit_price_from_opposing_ask(
    ask: Decimal,
    meta: InstrumentMetadata,
    *,
    factor: Decimal = ASK_AWAY_FACTOR,
) -> Decimal:
    """BUY post-only: opposing ask × factor, tick-aligned (ROUND_DOWN)."""
    if ask <= 0:
        raise L1Error("best_ask must be positive")
    raw = ask * factor
    if raw <= 0:
        raise L1Error("derived limit price non-positive")
    steps = (raw / meta.tick_size).to_integral_value(rounding=ROUND_DOWN)
    px = steps * meta.tick_size
    if px <= 0:
        raise L1Error("tick-aligned limit price non-positive")
    return px


def assert_plan_price_matches_l1(
    *,
    plan_price: str,
    quote: PublicL1Quote,
    meta: InstrumentMetadata,
    max_age_sec: float = L1_MAX_AGE_SEC,
    now_mono_ns: Optional[int] = None,
) -> None:
    """Fail-closed pre-send revalidation of L1 freshness and 1%-away rule."""
    quote.assert_fresh(max_age_sec=max_age_sec, now_mono_ns=now_mono_ns)
    expected = limit_price_from_opposing_ask(quote.best_ask, meta)
    got = parse_decimal(plan_price, field="price")
    if got != expected:
        raise L1Error("plan price no longer equals fresh ask×0.99 rule")


class PublicL1Port(Protocol):
    def snapshot(self, *, exchange: str, symbol: str) -> PublicL1Quote:
        ...


@dataclass
class FakePublicL1Adapter:
    """Test-only injectable L1 quotes (no network)."""

    quotes: dict[tuple[str, str], PublicL1Quote]
    # When True, each snapshot stamps a fresh asof (avoids recovery-wait stale flakes).
    refresh_asof_on_snapshot: bool = False

    def snapshot(self, *, exchange: str, symbol: str) -> PublicL1Quote:
        key = (exchange.strip().lower(), symbol)
        q = self.quotes.get(key)
        if q is None:
            raise L1Error(f"fake L1 missing for {exchange}/{symbol}")
        if not self.refresh_asof_on_snapshot:
            return q
        fresh = PublicL1Quote(
            exchange=q.exchange,
            symbol=q.symbol,
            best_ask=q.best_ask,
            asof_mono_ns=time.monotonic_ns(),
        )
        self.quotes[key] = fresh
        return fresh

    def set_quote(self, quote: PublicL1Quote) -> None:
        self.quotes[(quote.exchange, quote.symbol)] = quote


@dataclass
class PublicL1WsAdapter:
    """One-symbol public WS L1 reader (injectable socket; default unbound)."""

    exchange: str
    symbol: str
    socket: Optional[PrivateWsSocket] = None
    max_age_sec: float = L1_MAX_AGE_SEC
    _last: Optional[PublicL1Quote] = None

    def bind(self, sock: PrivateWsSocket) -> None:
        sock.connect()
        self.socket = sock
        self._subscribe()

    def connect_url(self, url: str) -> None:
        sock = open_private_socket(url)
        self.bind(sock)

    def _subscribe(self) -> None:
        assert self.socket is not None
        if self.exchange == "bybit":
            body = {
                "op": "subscribe",
                "args": [f"orderbook.1.{self.symbol}"],
            }
        else:
            body = {
                "op": "subscribe",
                "args": [{"channel": "bbo-tbt", "instId": self.symbol}],
            }
        self.socket.send_text(json.dumps(body, separators=(",", ":")))

    def ingest_text(self, text: str, *, now_mono_ns: Optional[int] = None) -> Optional[PublicL1Quote]:
        """Parse one public frame categorically; never log raw text."""
        now = now_mono_ns if now_mono_ns is not None else time.monotonic_ns()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, Mapping):
            return None
        ask: Optional[Decimal] = None
        if self.exchange == "bybit":
            topic = str(data.get("topic") or "")
            if not topic.startswith("orderbook.1."):
                return None
            payload = data.get("data")
            row = payload if isinstance(payload, Mapping) else {}
            asks = row.get("a") if isinstance(row, Mapping) else None
            if isinstance(asks, list) and asks:
                first = asks[0]
                if isinstance(first, (list, tuple)) and first:
                    ask = parse_decimal(str(first[0]), field="ask")
        else:
            arg = data.get("arg") if isinstance(data.get("arg"), Mapping) else {}
            if str(arg.get("channel") or "") not in {"bbo-tbt", "books5"}:
                return None
            rows = data.get("data")
            if not isinstance(rows, list) or not rows:
                return None
            row = rows[0] if isinstance(rows[0], Mapping) else {}
            asks = row.get("asks")
            if isinstance(asks, list) and asks:
                first = asks[0]
                if isinstance(first, (list, tuple)) and first:
                    ask = parse_decimal(str(first[0]), field="ask")
            elif row.get("askPx") is not None:
                ask = parse_decimal(str(row.get("askPx")), field="ask")
        if ask is None or ask <= 0:
            return None
        quote = PublicL1Quote(
            exchange=self.exchange,
            symbol=self.symbol,
            best_ask=ask,
            asof_mono_ns=now,
        )
        self._last = quote
        return quote

    def poll(self, *, timeout_sec: float = 2.0) -> PublicL1Quote:
        assert self.socket is not None
        raw = self.socket.recv_text(timeout_sec=timeout_sec)
        q = self.ingest_text(raw)
        if q is None:
            raise L1Error("public L1 frame missing best ask")
        return q

    def await_fresh_quote(
        self,
        *,
        timeout_sec: float = 10.0,
        recv_timeout_sec: float = 2.0,
    ) -> PublicL1Quote:
        """Subscribe is not enough — keep reading until a usable ask arrives."""
        assert self.socket is not None
        deadline = time.monotonic() + float(timeout_sec)
        last_err: Optional[BaseException] = None
        while time.monotonic() < deadline:
            try:
                raw = self.socket.recv_text(timeout_sec=recv_timeout_sec)
            except TimeoutError as exc:
                last_err = exc
                continue
            q = self.ingest_text(raw)
            if q is not None:
                q.assert_fresh(max_age_sec=self.max_age_sec)
                return q
        if last_err is not None:
            raise L1Error("public L1 quote timeout") from last_err
        raise L1Error("public L1 quote timeout")

    def snapshot(self, *, exchange: str, symbol: str) -> PublicL1Quote:
        if exchange != self.exchange or symbol != self.symbol:
            raise L1Error("L1 adapter symbol/exchange mismatch")
        if self._last is None:
            return self.await_fresh_quote()
        try:
            self._last.assert_fresh(max_age_sec=self.max_age_sec)
            return self._last
        except L1Error:
            return self.await_fresh_quote()

    def close(self) -> None:
        if self.socket is not None:
            try:
                self.socket.close()
            except Exception:  # noqa: BLE001
                pass
            self.socket = None
            self._last = None
