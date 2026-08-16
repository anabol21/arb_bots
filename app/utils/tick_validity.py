"""Fail-closed tick gates: prefer an explicit gap over a stale-cross print.

Does not change spread formulas. Lean parquet schema is unchanged.
"""

from __future__ import annotations

import os
from typing import Any, Optional


DEFAULT_SKEW_MAX_MS = 2000
DEFAULT_AGE_MAX_MS = 2000
BOOK_CHANNELS = frozenset({"books5", "orderbook.1"})


def skew_age_thresholds() -> tuple[int, int]:
    return (
        int(os.environ.get("SPREAD_TICK_SKEW_MAX_MS", str(DEFAULT_SKEW_MAX_MS))),
        int(os.environ.get("SPREAD_TICK_AGE_MAX_MS", str(DEFAULT_AGE_MAX_MS))),
    )


def book_l1_complete(book: dict[str, Any]) -> bool:
    return (
        book.get("bid_price") is not None
        and book.get("ask_price") is not None
        and book.get("ts_exchange") is not None
        and book.get("local_recv_ts_ms") is not None
    )


class TickValidityGate:
    """Per-coin generation + skew/age suppressors."""

    def __init__(
        self,
        *,
        skew_max_ms: Optional[int] = None,
        age_max_ms: Optional[int] = None,
    ) -> None:
        env_skew, env_age = skew_age_thresholds()
        self.skew_max_ms = env_skew if skew_max_ms is None else int(skew_max_ms)
        self.age_max_ms = env_age if age_max_ms is None else int(age_max_ms)
        self.coin_generation: dict[str, int] = {}
        self.leg_generation: dict[tuple[str, str], int] = {}
        self.counters = {
            "ticks_suppressed_stale": 0,
            "ticks_suppressed_generation": 0,
            "ticks_accepted": 0,
        }

    def note_subscribe_ok(self, base_coin: str, channel: str) -> int:
        if channel not in BOOK_CHANNELS:
            return self.coin_generation.get(base_coin, 0)
        nxt = self.coin_generation.get(base_coin, 0) + 1
        self.coin_generation[base_coin] = nxt
        return nxt

    def note_book_update(self, base_coin: str, exchange: str, *, complete_l1: bool) -> None:
        if not complete_l1:
            return
        self.leg_generation[(base_coin, exchange)] = self.coin_generation.get(base_coin, 0)

    def note_disconnect(self, base_coin: str, exchange: str) -> None:
        self.leg_generation.pop((base_coin, exchange), None)

    def evaluate(
        self,
        base_coin: str,
        okx: dict[str, Any],
        bybit: dict[str, Any],
        calc_local_ts_ms: float,
    ) -> Optional[str]:
        """Return suppress reason, or None if the tick may be written."""
        gen = self.coin_generation.get(base_coin, 0)
        if (
            self.leg_generation.get((base_coin, "okx")) != gen
            or self.leg_generation.get((base_coin, "bybit")) != gen
        ):
            self.counters["ticks_suppressed_generation"] += 1
            return "generation"
        okx_ts = float(okx["ts_exchange"])
        bybit_ts = float(bybit["ts_exchange"])
        if abs(okx_ts - bybit_ts) > self.skew_max_ms:
            self.counters["ticks_suppressed_stale"] += 1
            return "skew"
        for book in (okx, bybit):
            age = float(calc_local_ts_ms) - float(book["local_recv_ts_ms"])
            if age > self.age_max_ms:
                self.counters["ticks_suppressed_stale"] += 1
                return "age"
        self.counters["ticks_accepted"] += 1
        return None

    def heartbeat_fields(self) -> dict[str, int]:
        return {
            "ticks_suppressed_stale": self.counters["ticks_suppressed_stale"],
            "ticks_suppressed_generation": self.counters["ticks_suppressed_generation"],
            "ticks_accepted": self.counters["ticks_accepted"],
        }
