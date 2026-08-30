"""Causal spread / Gate-B MA helpers for gear 1.0 (no collector imports)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional


def spread_long_pct(bybit_bid: float, okx_ask: float) -> float:
    """(bybit_bid - okx_ask) / bybit_bid * 100."""
    return (bybit_bid - okx_ask) / bybit_bid * 100.0


def spread_short_pct(okx_bid: float, bybit_ask: float) -> float:
    """(okx_bid - bybit_ask) / okx_bid * 100."""
    return (okx_bid - bybit_ask) / okx_bid * 100.0


def latency_ok_for_avg(
    okx_latency_ms: Optional[float],
    bybit_latency_ms: Optional[float],
    *,
    max_latency_okx_ms: Optional[float],
    max_latency_bybit_ms: Optional[float],
) -> bool:
    """Same filter as notebook ``compute_gate_b_ma``: NaN/None fails when a cap is set."""
    if max_latency_okx_ms is not None:
        if okx_latency_ms is None or okx_latency_ms != okx_latency_ms:
            return False
        if okx_latency_ms > max_latency_okx_ms:
            return False
    if max_latency_bybit_ms is not None:
        if bybit_latency_ms is None or bybit_latency_ms != bybit_latency_ms:
            return False
        if bybit_latency_ms > max_latency_bybit_ms:
            return False
    return True


@dataclass
class MaSample:
    """One causal tick contribution for Gate B."""

    ts_ms: float
    spread_long: float
    spread_short: float
    avg_valid: bool


@dataclass
class CausalMaWindow:
    """Caller-owned rolling window; pure update, no wall-clock."""

    avg_window_sec: float
    _buf: Deque[MaSample]

    def __init__(self, avg_window_sec: float) -> None:
        if avg_window_sec <= 0:
            raise ValueError("avg_window_sec must be > 0")
        self.avg_window_sec = float(avg_window_sec)
        self._buf = deque()

    def update(
        self,
        *,
        ts_ms: float,
        spread_long: float,
        spread_short: float,
        avg_valid: bool,
    ) -> tuple[Optional[float], Optional[float]]:
        """Append sample, drop points older than ``avg_window_sec``, return (ma_long, ma_short).

        Only ``avg_valid`` samples enter the mean (notebook Gate B). Returns
        ``(None, None)`` when the window has no valid points.
        """
        self._buf.append(
            MaSample(
                ts_ms=float(ts_ms),
                spread_long=float(spread_long),
                spread_short=float(spread_short),
                avg_valid=bool(avg_valid),
            )
        )
        window_ms = self.avg_window_sec * 1000.0
        t_lo = float(ts_ms) - window_ms
        while self._buf and self._buf[0].ts_ms < t_lo:
            self._buf.popleft()

        sum_long = 0.0
        sum_short = 0.0
        n = 0
        for s in self._buf:
            if not s.avg_valid:
                continue
            sum_long += s.spread_long
            sum_short += s.spread_short
            n += 1
        if n == 0:
            return None, None
        return sum_long / n, sum_short / n
