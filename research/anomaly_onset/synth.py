"""Synthetic quiet+anomaly day generator for smoke tests.

Writes flat compacted ``spread_*.parquet`` files (one 5-minute window per file,
all coins concatenated, lean-16 body) so the real reader path is exercised. The
default quiet regime is an irregular-interval random walk of two venue mids with
a small dislocation noise; anomalies are injected as sustained widenings of the
directional spread (plus a short spike that should NOT qualify).

This is a test fixture only — never a substitute for real collected data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from research.anomaly_onset.io_lean import LEAN_TICK_BODY_COLS


@dataclass
class Anomaly:
    start_s: float          # seconds from window start
    dur_s: float            # sustained duration
    bump_pct: float         # added to spread_long over the episode (percent)
    direction: str = "long"


@dataclass
class CoinSpec:
    base_coin: str
    price0: float = 100.0
    tick_hz: float = 0.7            # mean ticks/sec (Poisson interarrival)
    quiet_spread_pct: float = 0.02  # mean quiet directional spread
    quiet_noise_pct: float = 0.01   # sd of quiet spread noise
    walk_sigma: float = 0.0005      # per-tick relative mid random-walk sd
    anomalies: list = field(default_factory=list)


def _one_coin(spec: CoinSpec, start_ms: int, end_ms: int, day_start_ms: int,
              rng: np.random.Generator) -> pd.DataFrame:
    dur_s = (end_ms - start_ms) / 1000.0
    # irregular tick timestamps via exponential interarrivals (window-local)
    n_est = int(dur_s * spec.tick_hz * 1.3) + 16
    gaps = rng.exponential(1.0 / spec.tick_hz, size=n_est)
    t_local = np.cumsum(gaps)
    t_local = t_local[t_local < dur_s]
    n = len(t_local)
    ts = start_ms + (t_local * 1000).astype(np.int64)
    # absolute seconds from day start — anomalies are keyed to this, not window time
    t_s = (ts - day_start_ms) / 1000.0

    # venue mid random walk
    steps = rng.normal(0.0, spec.walk_sigma, size=n)
    mid = spec.price0 * np.exp(np.cumsum(steps))

    # quiet directional spread (percent), noisy, non-negative-ish
    base = spec.quiet_spread_pct + rng.normal(0.0, spec.quiet_noise_pct, size=n)
    bump_long = np.zeros(n)
    bump_short = np.zeros(n)
    for a in spec.anomalies:
        m = (t_s >= a.start_s) & (t_s < a.start_s + a.dur_s)
        # ramp shape so onset is gradual then sustained
        ramp = np.clip((t_s[m] - a.start_s) / max(a.dur_s * 0.15, 1e-9), 0, 1)
        if a.direction == "long":
            bump_long[m] += a.bump_pct * ramp
        else:
            bump_short[m] += a.bump_pct * ramp
    spread_long = base + bump_long
    spread_short = base + bump_short

    # invert directional spread definitions into venue L1 prices
    #   spread_long  = (bybit_bid - okx_ask)/bybit_bid*100
    #   spread_short = (okx_bid   - bybit_ask)/okx_bid*100
    half = (spec.quiet_spread_pct * 0.5 + 0.01) / 100.0  # per-venue half spread (frac)
    okx_bid = mid * (1 - half)
    okx_ask = mid * (1 + half)
    bybit_bid = okx_ask / (1 - spread_long / 100.0)      # from spread_long
    bybit_ask = okx_bid * (1 - spread_short / 100.0)      # from spread_short
    size = rng.uniform(5, 50, size=n)

    return pd.DataFrame(
        {
            "event_local_ts_ms": ts,
            "base_coin": spec.base_coin,
            "trigger": "okx",
            "calc_local_ts_ms": ts,
            "okx_local_recv_ts_ms": ts,
            "okx_ts_ms": ts - 5,
            "bybit_local_recv_ts_ms": ts,
            "bybit_ts_ms": ts - 5,
            "okx_bid_price": okx_bid,
            "okx_bid_size": size,
            "okx_ask_price": okx_ask,
            "okx_ask_size": size,
            "bybit_bid_price": bybit_bid,
            "bybit_bid_size": size,
            "bybit_ask_price": bybit_ask,
            "bybit_ask_size": size,
        }
    )


def generate_day(
    out_dir,
    coins: list[CoinSpec],
    start: str = "2026-08-21T00:00:00Z",
    hours: float = 24.0,
    window_min: int = 5,
    seed: int = 7,
) -> Path:
    """Write flat ``spread_*.parquet`` window files for ``coins`` over ``hours``."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    start_ms = int(datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp() * 1000)
    win_ms = window_min * 60_000
    n_win = int(round(hours * 60 / window_min))
    rng = np.random.default_rng(seed)

    for k in range(n_win):
        w0 = start_ms + k * win_ms
        w1 = w0 + win_ms
        parts = [_one_coin(c, w0, w1, start_ms, rng) for c in coins]
        df = pd.concat(parts, axis=0, ignore_index=True)
        df = df[list(LEAN_TICK_BODY_COLS)]
        a = datetime.fromtimestamp(w0 / 1000, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        b = datetime.fromtimestamp(w1 / 1000, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        pq.write_table(pa.Table.from_pandas(df, preserve_index=False), out_dir / f"spread_{a}_{b}.parquet")
    return out_dir


def default_two_coins() -> list[CoinSpec]:
    """Two coins with anomalies placed in the warm region (past the H_floor warm-up).

    BTCX: two sustained anomalies + one short spike (spike should NOT qualify as an
    onset because it lacks occupancy/area). ETHX: one sustained anomaly.
    """
    return [
        CoinSpec(
            base_coin="BTCX", price0=100.0, tick_hz=0.9,
            quiet_spread_pct=0.02, quiet_noise_pct=0.012, walk_sigma=0.0004,
            anomalies=[
                Anomaly(start_s=8.0 * 3600, dur_s=40 * 60, bump_pct=0.25),   # sustained -> fire
                Anomaly(start_s=15.0 * 3600, dur_s=25 * 60, bump_pct=0.18),  # sustained -> fire
                Anomaly(start_s=20.0 * 3600, dur_s=45, bump_pct=0.5),        # short spike -> should NOT fire
            ],
        ),
        CoinSpec(
            base_coin="ETHX", price0=50.0, tick_hz=0.7,
            quiet_spread_pct=0.03, quiet_noise_pct=0.015, walk_sigma=0.0005,
            anomalies=[
                Anomaly(start_s=12.0 * 3600, dur_s=35 * 60, bump_pct=0.22),
            ],
        ),
    ]
