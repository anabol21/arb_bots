"""Build market floor snapshot rows from lean ticks (observation only).

Reuses gear22 viz candle builders and ``compute_chosen_floor`` — does not
redefine the locked floor formula.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence

import numpy as np
import pandas as pd

from research.gear22_quiet_regime_viz.candles import (
    BAR_MS,
    SPREAD_LONG_COL,
    SPREAD_SHORT_COL,
    build_5m_bucket_stats,
    causal_sma,
    floor_bar_start_ms,
)
from research.gear22_quiet_regime_viz.floors import (
    TF_SELECT_25_NAME,
    W1_BARS,
    compute_chosen_floor,
)
from research.gear22_quiet_regime_viz.load import load_ticks

# Locked formula id for journal metadata (must match floors.compute_chosen_floor).
FORMULA_ID = "tf-select-a25-of-sma12"

# Default warm-up lookback: 12h trim window + SMA-12 bars (+ small pad).
DEFAULT_LOOKBACK_HOURS = 13.0

SIDES: tuple[tuple[str, str], ...] = (
    ("long", SPREAD_LONG_COL),
    ("short", SPREAD_SHORT_COL),
)

SNAPSHOT_COLUMNS: tuple[str, ...] = (
    "bar_end_ms",
    "bar_start_ms",
    "base_coin",
    "side",
    "close",
    "sma12",
    "floor_tf_select_a25",
    "edge",
    "tick_count",
    "formula_id",
    "computed_at_ms",
    "data_root",
    "since_ms",
    "until_ms",
    "lookback_ms",
    "run_mode",
)


def _edge(close: np.ndarray, floor: np.ndarray) -> np.ndarray:
    """``close - floor`` when both finite; else NaN."""
    c = np.asarray(close, dtype="float64")
    f = np.asarray(floor, dtype="float64")
    out = np.full(c.shape, np.nan, dtype="float64")
    ok = np.isfinite(c) & np.isfinite(f)
    out[ok] = c[ok] - f[ok]
    return out


def build_side_floor_rows(
    ticks: pd.DataFrame,
    *,
    base_coin: str,
    side: str,
    value_col: str,
    emit_start_ms: int,
    emit_end_ms: int,
    bucket_start_ms: int,
    meta: dict,
) -> pd.DataFrame:
    """5m closes → SMA-12 → chosen floor for one coin/side; emit window only."""
    if ticks.empty:
        return pd.DataFrame(columns=list(SNAPSHOT_COLUMNS))

    buckets = build_5m_bucket_stats(
        ticks,
        value_col=value_col,
        start_ms=bucket_start_ms,
        end_ms=emit_end_ms,
        fill_empty_buckets=True,
        ma_bars=(W1_BARS,),
    )
    if buckets.empty:
        return pd.DataFrame(columns=list(SNAPSHOT_COLUMNS))

    closes = buckets["close"].to_numpy(dtype="float64")
    if f"ma_{W1_BARS}" in buckets.columns:
        sma12 = buckets[f"ma_{W1_BARS}"].to_numpy(dtype="float64")
    else:
        sma12 = causal_sma(closes, W1_BARS)
    chosen = compute_chosen_floor(sma12)
    floor = chosen[TF_SELECT_25_NAME]
    edge = _edge(closes, floor)

    bar_end = buckets["bar_end_ms"].to_numpy(dtype="int64")
    bar_start = buckets["bar_start_ms"].to_numpy(dtype="int64")
    # Emit completed bars whose end is inside (emit_start, emit_end].
    mask = (bar_end > int(emit_start_ms)) & (bar_end <= int(emit_end_ms))
    if not np.any(mask):
        return pd.DataFrame(columns=list(SNAPSHOT_COLUMNS))

    n = int(np.sum(mask))
    tick_count = buckets["tick_count"].to_numpy(dtype="int64")[mask]
    out = pd.DataFrame(
        {
            "bar_end_ms": bar_end[mask],
            "bar_start_ms": bar_start[mask],
            "base_coin": [str(base_coin).upper()] * n,
            "side": [str(side)] * n,
            "close": closes[mask],
            "sma12": sma12[mask],
            "floor_tf_select_a25": floor[mask],
            "edge": edge[mask],
            "tick_count": tick_count,
            "formula_id": [FORMULA_ID] * n,
            "computed_at_ms": [int(meta["computed_at_ms"])] * n,
            "data_root": [str(meta["data_root"])] * n,
            "since_ms": [int(meta["since_ms"])] * n,
            "until_ms": [int(meta["until_ms"])] * n,
            "lookback_ms": [int(meta["lookback_ms"])] * n,
            "run_mode": [str(meta["run_mode"])] * n,
        }
    )
    return out[list(SNAPSHOT_COLUMNS)]


def build_market_floor_snapshot(
    data_root,
    *,
    coins: Sequence[str],
    since_ms: int,
    until_ms: int,
    lookback_hours: float = DEFAULT_LOOKBACK_HOURS,
    run_mode: str = "oneshot",
    computed_at_ms: Optional[int] = None,
) -> pd.DataFrame:
    """Load ticks (with lookback), compute floors for all coins × sides.

    Bars are built from ``since_ms - lookback`` so warm-up can fill; only
    rows with ``bar_end_ms`` in ``(since_ms, until_ms]`` are emitted.
    Coins with no ticks in the load window are skipped (no empty stubs).
    """
    if until_ms <= since_ms:
        raise ValueError("until_ms must be after since_ms")
    lookback_ms = int(float(lookback_hours) * 3600.0 * 1000.0)
    load_since = int(since_ms) - lookback_ms
    if load_since < 0:
        load_since = 0
    coins_u = [str(c).strip().upper() for c in coins if str(c).strip()]
    if not coins_u:
        raise ValueError("coins list is empty")

    if computed_at_ms is None:
        computed_at_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

    meta = {
        "computed_at_ms": int(computed_at_ms),
        "data_root": str(data_root),
        "since_ms": int(since_ms),
        "until_ms": int(until_ms),
        "lookback_ms": lookback_ms,
        "run_mode": str(run_mode),
    }

    try:
        ticks = load_ticks(
            data_root,
            coins=coins_u,
            since_ms=load_since,
            until_ms=until_ms,
        )
    except ValueError:
        # No rows in window — empty snapshot (caller may still write metadata).
        return pd.DataFrame(columns=list(SNAPSHOT_COLUMNS))

    bucket_start = floor_bar_start_ms(load_since, BAR_MS)
    frames: list[pd.DataFrame] = []
    for coin in coins_u:
        sub = ticks.loc[ticks["base_coin"] == coin]
        if sub.empty:
            continue
        for side, value_col in SIDES:
            part = build_side_floor_rows(
                sub,
                base_coin=coin,
                side=side,
                value_col=value_col,
                emit_start_ms=since_ms,
                emit_end_ms=until_ms,
                bucket_start_ms=bucket_start,
                meta=meta,
            )
            if not part.empty:
                frames.append(part)

    if not frames:
        return pd.DataFrame(columns=list(SNAPSHOT_COLUMNS))
    out = pd.concat(frames, ignore_index=True)
    out = out.sort_values(
        ["bar_end_ms", "base_coin", "side"], kind="mergesort"
    ).reset_index(drop=True)
    return out
