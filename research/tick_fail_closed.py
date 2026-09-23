"""Fail-closed tick mask matching collector ``app.utils.tick_validity``.

Prefer an explicit gap over a stale-cross print (one leg still on an old book).
Generation suppress is not stored in lean parquet; skew + age are the readable proxy.

Used by the Gear 2 reader and by ``research/filter_stale_ticks.py``.
"""

from __future__ import annotations

import pandas as pd

from app.utils.tick_validity import DEFAULT_AGE_MAX_MS, DEFAULT_SKEW_MAX_MS

SKEW_MAX_MS = DEFAULT_SKEW_MAX_MS
AGE_MAX_MS = DEFAULT_AGE_MAX_MS

_TS = (
    "okx_ts_ms",
    "bybit_ts_ms",
    "okx_local_recv_ts_ms",
    "bybit_local_recv_ts_ms",
    "calc_local_ts_ms",
)


def fail_closed_ok(df: pd.DataFrame) -> pd.Series:
    """True = tick may be used. Missing timestamp columns → all False."""
    if not set(_TS) <= set(df.columns):
        return pd.Series(False, index=df.index)
    okx_ts = pd.to_numeric(df["okx_ts_ms"], errors="coerce")
    bybit_ts = pd.to_numeric(df["bybit_ts_ms"], errors="coerce")
    calc = pd.to_numeric(df["calc_local_ts_ms"], errors="coerce")
    okx_recv = pd.to_numeric(df["okx_local_recv_ts_ms"], errors="coerce")
    bybit_recv = pd.to_numeric(df["bybit_local_recv_ts_ms"], errors="coerce")
    skew = (okx_ts - bybit_ts).abs()
    age_okx = calc - okx_recv
    age_bybit = calc - bybit_recv
    return (
        okx_ts.notna()
        & bybit_ts.notna()
        & calc.notna()
        & okx_recv.notna()
        & bybit_recv.notna()
        & (skew <= SKEW_MAX_MS)
        & (age_okx <= AGE_MAX_MS)
        & (age_bybit <= AGE_MAX_MS)
        & (age_okx >= -1_000)
        & (age_bybit >= -1_000)
    )


def drop_stale_cross(df: pd.DataFrame) -> pd.DataFrame:
    """Drop ticks the current collector would not have written."""
    return df.loc[fail_closed_ok(df)].copy()
