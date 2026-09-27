"""In-memory quote staff and lean-style row emission (no spreads)."""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

OfferFn = Callable[[dict[str, Any]], None]


def empty_leg() -> dict[str, Any]:
    return {
        "bid_price": None,
        "bid_size": None,
        "ask_price": None,
        "ask_size": None,
        "ts_exchange": None,
        "local_recv_ts_ms": None,
    }


def quote_state_for_pair(okx_symbol: str, bybit_symbol: str) -> dict[str, Any]:
    return {
        "okx_symbol": okx_symbol,
        "bybit_symbol": bybit_symbol,
        "okx": empty_leg(),
        "bybit": empty_leg(),
        "hl": empty_leg(),
    }


def _ms_int(value: Any) -> int:
    return int(round(float(value)))


def _leg_ready(leg: dict[str, Any]) -> bool:
    return (
        leg.get("bid_price") is not None
        and leg.get("ask_price") is not None
        and leg.get("bid_size") is not None
        and leg.get("ask_size") is not None
        and leg.get("ts_exchange") is not None
        and leg.get("local_recv_ts_ms") is not None
    )


def build_staff_record(
    *,
    base_coin: str,
    trigger: str,
    state: dict[str, Any],
    calc_local_ts_ms: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """Build one hl_v2 row when Bybit + OKX + HL L1 are all present.

    No spread columns. Returns None until all three books are ready.
    """
    okx = state["okx"]
    bybit = state["bybit"]
    hl = state["hl"]
    if not (_leg_ready(okx) and _leg_ready(bybit) and _leg_ready(hl)):
        return None
    calc_ms = time.time() * 1000 if calc_local_ts_ms is None else calc_local_ts_ms
    if trigger == "bybit":
        event_local = bybit["local_recv_ts_ms"]
    elif trigger == "okx":
        event_local = okx["local_recv_ts_ms"]
    else:
        event_local = hl["local_recv_ts_ms"]
    return {
        "event_local_ts_ms": _ms_int(event_local),
        "base_coin": base_coin,
        "trigger": trigger,
        "calc_local_ts_ms": _ms_int(calc_ms),
        "okx_local_recv_ts_ms": _ms_int(okx["local_recv_ts_ms"]),
        "okx_ts_ms": _ms_int(okx["ts_exchange"]),
        "bybit_local_recv_ts_ms": _ms_int(bybit["local_recv_ts_ms"]),
        "bybit_ts_ms": _ms_int(bybit["ts_exchange"]),
        "okx_bid_price": float(okx["bid_price"]),
        "okx_bid_size": float(okx["bid_size"]),
        "okx_ask_price": float(okx["ask_price"]),
        "okx_ask_size": float(okx["ask_size"]),
        "bybit_bid_price": float(bybit["bid_price"]),
        "bybit_bid_size": float(bybit["bid_size"]),
        "bybit_ask_price": float(bybit["ask_price"]),
        "bybit_ask_size": float(bybit["ask_size"]),
        "hl_local_recv_ts_ms": _ms_int(hl["local_recv_ts_ms"]),
        "hl_ts_ms": _ms_int(hl["ts_exchange"]),
        "hl_bid_price": float(hl["bid_price"]),
        "hl_bid_size": float(hl["bid_size"]),
        "hl_ask_price": float(hl["ask_price"]),
        "hl_ask_size": float(hl["ask_size"]),
    }


class QuoteBook:
    """Shared quote staff for one contour process."""

    def __init__(self, offer: OfferFn) -> None:
        self.offer = offer
        self.quotes: dict[str, dict[str, Any]] = {}

    def register(self, base_coin: str, okx_symbol: str, bybit_symbol: str) -> None:
        self.quotes[base_coin] = quote_state_for_pair(okx_symbol, bybit_symbol)

    def update_cex(
        self,
        *,
        base_coin: str,
        exchange: str,
        bid_price: float,
        bid_size: float,
        ask_price: float,
        ask_size: float,
        ts_exchange: float,
        local_recv_ts_ms: float,
    ) -> None:
        state = self.quotes[base_coin]
        leg = state[exchange]
        leg["bid_price"] = bid_price
        leg["bid_size"] = bid_size
        leg["ask_price"] = ask_price
        leg["ask_size"] = ask_size
        leg["ts_exchange"] = ts_exchange
        leg["local_recv_ts_ms"] = local_recv_ts_ms
        record = build_staff_record(
            base_coin=base_coin,
            trigger=exchange,
            state=state,
        )
        if record is not None:
            self.offer(record)

    def update_hl(
        self,
        *,
        base_coin: str,
        bid_price: float,
        bid_size: float,
        ask_price: float,
        ask_size: float,
        ts_exchange: float,
        local_recv_ts_ms: float,
    ) -> None:
        state = self.quotes[base_coin]
        leg = state["hl"]
        leg["bid_price"] = bid_price
        leg["bid_size"] = bid_size
        leg["ask_price"] = ask_price
        leg["ask_size"] = ask_size
        leg["ts_exchange"] = ts_exchange
        leg["local_recv_ts_ms"] = local_recv_ts_ms
        record = build_staff_record(
            base_coin=base_coin,
            trigger="hl",
            state=state,
        )
        if record is not None:
            self.offer(record)
