#!/usr/bin/env python3
"""Build the tiny synthetic fixture used by tests / CI smoke (no VPS)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from app.schema.lean_event import LEAN_TICK_BODY_COLS

# Window intentionally overlaps default --since 2026-09-03T08:21:00Z.
SINCE_MS = 1_788_423_660_000  # 2026-09-03 08:21:00 UTC


def _coin_ticks(coin: str, px0: float, edge0_bps: float, seed: int) -> pd.DataFrame:
    """Sparse noisy ticks with an intentional multi-minute hole."""
    rng = np.random.default_rng(seed)
    # Dense-ish for ~12 minutes, then a ~4 minute hole, then sparse resume.
    t0 = SINCE_MS + 30_000
    block_a = t0 + np.cumsum(rng.integers(800, 2500, size=40))
    hole_start = int(block_a[-1])
    hole_end = hole_start + 240_000  # 4 minutes > default 30s gap threshold
    block_b = hole_end + np.cumsum(rng.integers(1500, 4000, size=25))
    # Another quieter stretch into the next 5m buckets
    block_c = int(block_b[-1]) + 90_000 + np.cumsum(rng.integers(2000, 5000, size=20))
    ts = np.concatenate([block_a, block_b, block_c]).astype(np.int64)

    n = len(ts)
    # Slow edge drift + noise (bps → percent of mid).
    t_rel = (ts - ts[0]) / 60_000.0
    edge_pct = (edge0_bps / 100.0) + 0.02 * np.sin(t_rel / 3.0) + rng.normal(0, 0.008, n)
    bybit_mid = px0 * (1.0 + 0.0004 * np.sin(t_rel / 5.0) + rng.normal(0, 0.00005, n))
    okx_mid = bybit_mid * (1.0 + edge_pct / 100.0)
    half_spread = px0 * 0.00005
    # Varied venue delivery latency (local_recv − exchange_ts); occasional NaN.
    okx_lat = np.clip(rng.normal(4.0, 1.5, n), 0.5, 25.0)
    bybit_lat = np.clip(rng.normal(3.0, 1.2, n), 0.5, 20.0)
    okx_lat[rng.random(n) < 0.05] = np.nan
    bybit_lat[rng.random(n) < 0.05] = np.nan
    okx_recv = ts - 1
    bybit_recv = ts - 1
    # When latency is NaN, leave exchange_ts as NaN so derived latency stays missing.
    okx_ts = np.where(np.isfinite(okx_lat), okx_recv - okx_lat, np.nan)
    bybit_ts = np.where(np.isfinite(bybit_lat), bybit_recv - bybit_lat, np.nan)
    rows = {
        "event_local_ts_ms": ts,
        "base_coin": np.full(n, coin),
        "trigger": np.where(rng.random(n) > 0.5, "okx", "bybit"),
        "calc_local_ts_ms": ts + 2,
        "okx_local_recv_ts_ms": okx_recv,
        "okx_ts_ms": okx_ts,
        "bybit_local_recv_ts_ms": bybit_recv,
        "bybit_ts_ms": bybit_ts,
        "okx_bid_price": okx_mid - half_spread,
        "okx_bid_size": np.full(n, 1.0),
        "okx_ask_price": okx_mid + half_spread,
        "okx_ask_size": np.full(n, 1.0),
        "bybit_bid_price": bybit_mid - half_spread,
        "bybit_bid_size": np.full(n, 1.0),
        "bybit_ask_price": bybit_mid + half_spread,
        "bybit_ask_size": np.full(n, 1.0),
    }
    return pd.DataFrame(rows)


def build_fixture(out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sol = _coin_ticks("SOL", px0=140.0, edge0_bps=3.0, seed=7)
    xrp = _coin_ticks("XRP", px0=0.55, edge0_bps=-2.0, seed=11)
    df = pd.concat([sol, xrp], ignore_index=True)
    # Keep lean body column order.
    for c in LEAN_TICK_BODY_COLS:
        if c not in df.columns:
            raise RuntimeError(f"fixture missing {c}")
    df = df.loc[:, list(LEAN_TICK_BODY_COLS)]
    tmin = int(df["event_local_ts_ms"].min())
    tmax = int(df["event_local_ts_ms"].max()) + 1
    from datetime import datetime, timezone

    def fmt(ms: int) -> str:
        return (
            datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        )

    name = f"spread_{fmt(tmin)}_{fmt(tmax)}.parquet"
    path = out_dir / name
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table, path, compression="zstd")
    # Also write a CSV twin for loader coverage.
    csv_path = out_dir / "ticks_sample.csv"
    df.to_csv(csv_path, index=False)
    return path


def main() -> int:
    root = Path(__file__).resolve().parents[1] / "fixtures" / "gear22_quiet_regime_viz"
    path = build_fixture(root / "ticks")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
