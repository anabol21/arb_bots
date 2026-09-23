"""Venue message delay (same convention as lean_ticks_io / gear22 viz).

``okx_latency_ms   = okx_local_recv_ts_ms − okx_ts_ms``
``bybit_latency_ms = bybit_local_recv_ts_ms − bybit_ts_ms``

This is the send-to-receive delay of that venue's last book, not a blended
pair delay and not ``event_local_ts_ms − exchange_ts``.
"""

from __future__ import annotations

from typing import Optional, Union

import numpy as np
import pandas as pd

Number = Union[int, float, np.integer, np.floating]


def message_delay_ms(
    local_recv_ts_ms: Optional[Number],
    exchange_ts_ms: Optional[Number],
) -> Optional[float]:
    """Delivery delay of one venue message: ``local_recv − exchange_ts``.

    Returns ``None`` when either stamp is missing or non-finite.
    """
    try:
        local = float(local_recv_ts_ms)  # type: ignore[arg-type]
        exch = float(exchange_ts_ms)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not np.isfinite(local) or not np.isfinite(exch):
        return None
    return local - exch


def attach_venue_latency(df: pd.DataFrame) -> pd.DataFrame:
    """Attach ``okx_latency_ms`` / ``bybit_latency_ms`` when source cols exist.

    Precomputed latency columns are kept (numeric coerce). Does not invent
    stamps. Missing or non-finite diffs become NaN.
    """
    out = df
    if "okx_latency_ms" not in out.columns and {
        "okx_local_recv_ts_ms",
        "okx_ts_ms",
    } <= set(out.columns):
        out["okx_latency_ms"] = (
            pd.to_numeric(out["okx_local_recv_ts_ms"], errors="coerce")
            - pd.to_numeric(out["okx_ts_ms"], errors="coerce")
        )
    if "bybit_latency_ms" not in out.columns and {
        "bybit_local_recv_ts_ms",
        "bybit_ts_ms",
    } <= set(out.columns):
        out["bybit_latency_ms"] = (
            pd.to_numeric(out["bybit_local_recv_ts_ms"], errors="coerce")
            - pd.to_numeric(out["bybit_ts_ms"], errors="coerce")
        )
    for col in ("okx_latency_ms", "bybit_latency_ms"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out
