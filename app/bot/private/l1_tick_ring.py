"""Rolling public L1 ring for canary chronometry (WAL/EDEN).

Cheap append on the public book path. Not a send-path gate and not a
fill wait. Default retention is ~60s / 16384 ticks per coin.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional

DEFAULT_RING_SEC = 60.0
DEFAULT_MAX_TICKS = 16384
ENV_RING_SEC = "BBOT_L1_RING_SEC"
ENV_MAX_TICKS = "BBOT_L1_RING_MAX_TICKS"
CANARY_COINS = frozenset({"WAL", "EDEN"})
CANARY_PROFILES = frozenset({"canary_wal_eden", "canary"})


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def spread_long_pct(bybit_bid: Any, okx_ask: Any) -> Optional[float]:
    """Policy: ``(bybit_bid − okx_ask) / bybit_bid * 100``."""
    bid = _finite(bybit_bid)
    ask = _finite(okx_ask)
    if bid is None or ask is None or bid == 0.0:
        return None
    return (bid - ask) / bid * 100.0


def spread_short_pct(okx_bid: Any, bybit_ask: Any) -> Optional[float]:
    """Policy: ``(okx_bid − bybit_ask) / okx_bid * 100``."""
    bid = _finite(okx_bid)
    ask = _finite(bybit_ask)
    if bid is None or ask is None or bid == 0.0:
        return None
    return (bid - ask) / bid * 100.0


def fill_spread_pct(
    *,
    spread_kind: str,
    bybit_exec: Any,
    okx_exec: Any,
) -> Optional[float]:
    """Same formula as signal, using venue exec / avg fill prices.

    open_long (and close-of-short): sell Bybit, buy OKX → long formula.
    open_short (and close-of-long): sell OKX, buy Bybit → short formula.
    """
    kind = str(spread_kind).strip().lower()
    if kind == "long":
        return spread_long_pct(bybit_exec, okx_exec)
    if kind == "short":
        return spread_short_pct(okx_exec, bybit_exec)
    raise ValueError(f"spread_kind must be long|short, got {spread_kind!r}")


@dataclass(frozen=True)
class SignalBookSnapshot:
    """Book at place/signal. Survives ring wrap."""

    bybit_bid: Optional[float]
    bybit_ask: Optional[float]
    okx_bid: Optional[float]
    okx_ask: Optional[float]
    bybit_bid_size: Optional[float] = None
    bybit_ask_size: Optional[float] = None
    okx_bid_size: Optional[float] = None
    okx_ask_size: Optional[float] = None
    spread_long_pct: Optional[float] = None
    spread_short_pct: Optional[float] = None
    captured_wall_ms: Optional[int] = None
    event_local_ts_ms: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def signal_spread_pct(self, spread_kind: str) -> Optional[float]:
        kind = str(spread_kind).strip().lower()
        if kind == "long":
            return self.spread_long_pct
        if kind == "short":
            return self.spread_short_pct
        raise ValueError(f"spread_kind must be long|short, got {spread_kind!r}")


def capture_signal_book(
    okx_book: Mapping[str, Any],
    bybit_book: Mapping[str, Any],
    *,
    event_local_ts_ms: Optional[int] = None,
    wall_ms: Optional[int] = None,
) -> SignalBookSnapshot:
    bybit_bid = _finite(bybit_book.get("bid_price"))
    bybit_ask = _finite(bybit_book.get("ask_price"))
    okx_bid = _finite(okx_book.get("bid_price"))
    okx_ask = _finite(okx_book.get("ask_price"))
    return SignalBookSnapshot(
        bybit_bid=bybit_bid,
        bybit_ask=bybit_ask,
        okx_bid=okx_bid,
        okx_ask=okx_ask,
        bybit_bid_size=_finite(bybit_book.get("bid_size") or bybit_book.get("bid_qty")),
        bybit_ask_size=_finite(bybit_book.get("ask_size") or bybit_book.get("ask_qty")),
        okx_bid_size=_finite(okx_book.get("bid_size") or okx_book.get("bid_qty")),
        okx_ask_size=_finite(okx_book.get("ask_size") or okx_book.get("ask_qty")),
        spread_long_pct=spread_long_pct(bybit_bid, okx_ask),
        spread_short_pct=spread_short_pct(okx_bid, bybit_ask),
        captured_wall_ms=int(wall_ms if wall_ms is not None else time.time() * 1000),
        event_local_ts_ms=(
            int(event_local_ts_ms) if event_local_ts_ms is not None else None
        ),
    )


@dataclass
class L1Tick:
    """One accepted public L1 update (one venue)."""

    wall_ms: int
    mono_ns: int
    venue: str
    bid: Optional[float]
    ask: Optional[float]
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    event_local_ts_ms: Optional[int] = None
    ts_exchange: Optional[float] = None
    local_recv_ts_ms: Optional[float] = None
    spread_long_pct: Optional[float] = None
    spread_short_pct: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class L1TickRing:
    """Per-coin deque. Evicts by age and by count."""

    def __init__(
        self,
        *,
        max_age_ms: int = int(DEFAULT_RING_SEC * 1000),
        max_ticks: int = DEFAULT_MAX_TICKS,
    ) -> None:
        if max_age_ms <= 0:
            raise ValueError("max_age_ms must be > 0")
        if max_ticks <= 0:
            raise ValueError("max_ticks must be > 0")
        self.max_age_ms = int(max_age_ms)
        self.max_ticks = int(max_ticks)
        self._ticks: deque[L1Tick] = deque()
        self._last: dict[str, L1Tick] = {}
        self._lock = threading.Lock()

    def append(self, tick: L1Tick) -> None:
        """Copy-in. O(1) amortized. Never raises on missing prices."""
        venue = str(tick.venue).strip().lower()
        if venue not in {"bybit", "okx"}:
            return
        with self._lock:
            last_other = self._last.get("okx" if venue == "bybit" else "bybit")
            long_pct = tick.spread_long_pct
            short_pct = tick.spread_short_pct
            if long_pct is None or short_pct is None:
                bybit_bid = tick.bid if venue == "bybit" else (
                    last_other.bid if last_other is not None else None
                )
                bybit_ask = tick.ask if venue == "bybit" else (
                    last_other.ask if last_other is not None else None
                )
                okx_bid = tick.bid if venue == "okx" else (
                    last_other.bid if last_other is not None else None
                )
                okx_ask = tick.ask if venue == "okx" else (
                    last_other.ask if last_other is not None else None
                )
                if long_pct is None:
                    long_pct = spread_long_pct(bybit_bid, okx_ask)
                if short_pct is None:
                    short_pct = spread_short_pct(okx_bid, bybit_ask)
            stored = L1Tick(
                wall_ms=int(tick.wall_ms),
                mono_ns=int(tick.mono_ns),
                venue=venue,
                bid=tick.bid,
                ask=tick.ask,
                bid_size=tick.bid_size,
                ask_size=tick.ask_size,
                event_local_ts_ms=tick.event_local_ts_ms,
                ts_exchange=tick.ts_exchange,
                local_recv_ts_ms=tick.local_recv_ts_ms,
                spread_long_pct=long_pct,
                spread_short_pct=short_pct,
            )
            self._ticks.append(stored)
            self._last[venue] = stored
            self._evict_unlocked(stored.wall_ms)

    def _evict_unlocked(self, now_wall_ms: int) -> None:
        cutoff = int(now_wall_ms) - self.max_age_ms
        while self._ticks and (
            self._ticks[0].wall_ms < cutoff or len(self._ticks) > self.max_ticks
        ):
            self._ticks.popleft()

    def snapshot(
        self,
        *,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> list[L1Tick]:
        """Frozen copy of ticks in ``[start_ms, end_ms]`` (inclusive)."""
        with self._lock:
            ticks = list(self._ticks)
        if start_ms is not None:
            ticks = [t for t in ticks if t.wall_ms >= int(start_ms)]
        if end_ms is not None:
            ticks = [t for t in ticks if t.wall_ms <= int(end_ms)]
        return ticks

    def __len__(self) -> int:
        with self._lock:
            return len(self._ticks)


def resolve_ring_sec(env: Optional[Mapping[str, str]] = None) -> float:
    e = env if env is not None else os.environ
    raw = e.get(ENV_RING_SEC)
    if raw is None or str(raw).strip() == "":
        return DEFAULT_RING_SEC
    val = float(raw)
    if val <= 0 or val > 600:
        raise ValueError(f"{ENV_RING_SEC} must be in (0, 600], got {val}")
    return val


def resolve_max_ticks(env: Optional[Mapping[str, str]] = None) -> int:
    e = env if env is not None else os.environ
    raw = e.get(ENV_MAX_TICKS)
    if raw is None or str(raw).strip() == "":
        return DEFAULT_MAX_TICKS
    val = int(raw)
    if val <= 0 or val > 200_000:
        raise ValueError(f"{ENV_MAX_TICKS} must be in (0, 200000], got {val}")
    return val


def should_record_canary_l1(
    profile: Optional[str] = None,
    coin: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """Record for canary profile, or WAL/EDEN when an explicit flag is on."""
    e = env if env is not None else os.environ
    raw = str(e.get("BBOT_L1_RING") or "").strip().lower()
    if raw in {"0", "false", "off", "no"}:
        return False
    name = str(profile or e.get("BBOT_PROFILE") or "").strip().lower()
    if name in CANARY_PROFILES:
        return True
    if raw in {"1", "true", "on", "yes"}:
        return str(coin or "").strip().upper() in CANARY_COINS or not coin
    return False


_PROCESS_LOCK = threading.Lock()
_PROCESS_RINGS: dict[str, L1TickRing] = {}
_SIGNAL_BOOKS: dict[str, SignalBookSnapshot] = {}


def _ring_for(coin: str, env: Optional[Mapping[str, str]] = None) -> L1TickRing:
    key = str(coin).strip().upper()
    with _PROCESS_LOCK:
        ring = _PROCESS_RINGS.get(key)
        if ring is None:
            ring = L1TickRing(
                max_age_ms=int(resolve_ring_sec(env) * 1000),
                max_ticks=resolve_max_ticks(env),
            )
            _PROCESS_RINGS[key] = ring
        return ring


def record_public_l1(
    *,
    coin: str,
    venue: str,
    book: Mapping[str, Any],
    event_local_ts_ms: Optional[int] = None,
    wall_ms: Optional[int] = None,
    mono_ns: Optional[int] = None,
    env: Optional[Mapping[str, str]] = None,
    profile: Optional[str] = None,
) -> Optional[L1Tick]:
    """Append one accepted public book update. Cheap; copies scalars only."""
    if not should_record_canary_l1(profile, coin, env):
        return None
    bid = _finite(book.get("bid_price"))
    ask = _finite(book.get("ask_price"))
    if bid is None and ask is None:
        return None
    tick = L1Tick(
        wall_ms=int(wall_ms if wall_ms is not None else time.time() * 1000),
        mono_ns=int(mono_ns if mono_ns is not None else time.monotonic_ns()),
        venue=str(venue).strip().lower(),
        bid=bid,
        ask=ask,
        bid_size=_finite(book.get("bid_size") or book.get("bid_qty")),
        ask_size=_finite(book.get("ask_size") or book.get("ask_qty")),
        event_local_ts_ms=(
            int(event_local_ts_ms)
            if event_local_ts_ms is not None
            else (
                int(book["local_recv_ts_ms"])
                if _finite(book.get("local_recv_ts_ms")) is not None
                else None
            )
        ),
        ts_exchange=_finite(book.get("ts_exchange")),
        local_recv_ts_ms=_finite(book.get("local_recv_ts_ms")),
    )
    _ring_for(coin, env).append(tick)
    return tick


def freeze_window(
    coin: str,
    *,
    start_ms: int,
    end_ms: int,
    env: Optional[Mapping[str, str]] = None,
) -> list[L1Tick]:
    return _ring_for(coin, env).snapshot(start_ms=start_ms, end_ms=end_ms)


def store_signal_book(intent_id: str, snapshot: SignalBookSnapshot) -> None:
    with _PROCESS_LOCK:
        _SIGNAL_BOOKS[str(intent_id)] = snapshot


def get_signal_book(intent_id: str) -> Optional[SignalBookSnapshot]:
    with _PROCESS_LOCK:
        return _SIGNAL_BOOKS.get(str(intent_id))


def clear_process_rings() -> None:
    """Test helper. Does not touch production files."""
    with _PROCESS_LOCK:
        _PROCESS_RINGS.clear()
        _SIGNAL_BOOKS.clear()
