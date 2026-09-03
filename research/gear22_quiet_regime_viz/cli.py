"""CLI for Gear 2.2 quiet-regime HTML visualizer."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from research.gear22_quiet_regime_viz.candles import (
    DEFAULT_MA_BARS,
    BAR_MS,
    build_5m_bucket_stats,
    floor_bar_start_ms,
)
from research.gear22_quiet_regime_viz.gaps import (
    DEFAULT_GAP_THRESHOLD_MS,
    detect_gap_intervals,
)
from research.gear22_quiet_regime_viz.load import (
    DEFAULT_SINCE_UTC,
    load_ticks,
    parse_since_ms,
)
from research.gear22_quiet_regime_viz.plot import write_coin_html

DEFAULT_COINS = ("SOL", "XRP")


def _parse_coins(raw: str) -> list[str]:
    parts = [p.strip().upper() for p in str(raw).split(",")]
    coins = [p for p in parts if p]
    if not coins:
        raise argparse.ArgumentTypeError("coins list is empty")
    return coins


def _parse_ma_bars(raw: str) -> tuple[int, ...]:
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    out = tuple(int(p) for p in parts)
    if not out or any(x <= 0 for x in out):
        raise argparse.ArgumentTypeError("ma-bars must be positive integers")
    return out


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m research.gear22_quiet_regime_viz",
        description=(
            "Gear 2.2 quiet-regime research visualizer: gappy L1 → 5m edge "
            "candles + intra-stats → Plotly HTML (one page per coin)."
        ),
    )
    p.add_argument(
        "--data-root",
        type=Path,
        required=True,
        help="Local dump root (compacted spread_*.parquet, hive, or CSV).",
    )
    p.add_argument(
        "--coins",
        type=_parse_coins,
        default=list(DEFAULT_COINS),
        help=f"Comma-separated coins (default: {','.join(DEFAULT_COINS)}).",
    )
    p.add_argument(
        "--since",
        default=DEFAULT_SINCE_UTC,
        help=(
            "Window start (UTC). Default is the documented live gear2 restart "
            f"{DEFAULT_SINCE_UTC} (== 11:21 MSK). Example: 2026-09-03T08:21:00Z"
        ),
    )
    p.add_argument(
        "--until",
        default=None,
        help="Optional window end (UTC ISO). Default: now.",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="Directory for per-coin HTML pages.",
    )
    p.add_argument(
        "--gap-threshold-ms",
        type=int,
        default=DEFAULT_GAP_THRESHOLD_MS,
        help=f"Inter-tick gap mark threshold (default {DEFAULT_GAP_THRESHOLD_MS}).",
    )
    p.add_argument(
        "--ma-bars",
        type=_parse_ma_bars,
        default=tuple(DEFAULT_MA_BARS),
        help="Causal SMA windows in units of 5m bars (default 3,12 → 15m/60m).",
    )
    p.add_argument(
        "--max-tick-points",
        type=int,
        default=4_000,
        help="Even downsample cap for sparse tick overlay.",
    )
    return p


def run_viz(
    *,
    data_root: Path,
    coins: Sequence[str],
    since: str,
    out_dir: Path,
    until: str | None = None,
    gap_threshold_ms: int = DEFAULT_GAP_THRESHOLD_MS,
    ma_bars: Sequence[int] = DEFAULT_MA_BARS,
    max_tick_points: int = 4_000,
) -> list[Path]:
    since_ms = parse_since_ms(since)
    until_ms = parse_since_ms(until) if until else int(
        datetime.now(tz=timezone.utc).timestamp() * 1000
    )
    ticks = load_ticks(
        data_root,
        coins=coins,
        since_ms=since_ms,
        until_ms=until_ms,
    )
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    bucket_start = floor_bar_start_ms(since_ms, BAR_MS)
    for coin in coins:
        coin_u = coin.upper()
        sub = ticks.loc[ticks["base_coin"] == coin_u].copy()
        if sub.empty:
            print(f"skip {coin_u}: no ticks in window")
            continue
        buckets = build_5m_bucket_stats(
            sub,
            value_col="edge_pct",
            start_ms=bucket_start,
            end_ms=until_ms,
            fill_empty_buckets=True,
            ma_bars=ma_bars,
        )
        gaps = detect_gap_intervals(
            sub["event_local_ts_ms"],
            gap_threshold_ms=gap_threshold_ms,
        )
        meta = {
            "coin": coin_u,
            "since_utc": since,
            "until_utc": (
                until
                if until
                else datetime.fromtimestamp(until_ms / 1000, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            ),
            "n_ticks": len(sub),
            "n_buckets": len(buckets),
            "n_gaps": len(gaps),
            "gap_threshold_ms": gap_threshold_ms,
            "ma_bars": ",".join(str(x) for x in ma_bars),
            "primary_series": "edge_pct = (okx_mid-bybit_mid)/bybit_mid*100",
            "bar_ms": BAR_MS,
            "data_root": str(data_root),
        }
        path = write_coin_html(
            out_dir / f"gear22_quiet_regime_{coin_u}.html",
            coin=coin_u,
            ticks=sub,
            buckets=buckets,
            gaps=gaps,
            meta=meta,
            ma_bars=ma_bars,
            max_tick_points=max_tick_points,
        )
        print(f"wrote {path} (ticks={len(sub)} gaps={len(gaps)})")
        written.append(path)
    if not written:
        raise SystemExit("no HTML written — check coins / window / data-root")
    return written


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    run_viz(
        data_root=args.data_root,
        coins=args.coins,
        since=args.since,
        until=args.until,
        out_dir=args.out_dir,
        gap_threshold_ms=args.gap_threshold_ms,
        ma_bars=args.ma_bars,
        max_tick_points=args.max_tick_points,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
