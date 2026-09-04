"""CLI for Gear 2.2 quiet-regime HTML visualizer."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from research.gear22_quiet_regime_viz.candles import (
    DEFAULT_MA_BARS,
    DEFAULT_CANDLE_BINS,
    DEFAULT_CANDLE_TEMPORAL_BINS,
    DEFAULT_LATENCY_BINS,
    DEFAULT_LATENCY_TEMPORAL_BINS,
    BAR_MS,
    SPREAD_LONG_COL,
    SPREAD_SHORT_COL,
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
from research.gear22_quiet_regime_viz.plot import (
    coin_html_filename,
    write_coin_html,
    write_coins_json,
)

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
            "Gear 2.2 quiet-regime research visualizer: gappy L1 → 5m "
            "spread_long/short candles + intra-stats + TW quantiles → Plotly HTML."
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
        help="Directory for per-coin HTML pages (+ plotly.min.js, coins.json).",
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
    p.add_argument(
        "--candle-bins",
        type=int,
        default=DEFAULT_CANDLE_BINS,
        help=(
            "TW-mass in-bar histogram bins for click-to-inspect "
            f"(default {DEFAULT_CANDLE_BINS}; 0 disables inspect payloads)."
        ),
    )
    p.add_argument(
        "--candle-temporal-bins",
        type=int,
        default=DEFAULT_CANDLE_TEMPORAL_BINS,
        help=(
            "Equal-time mean slots per 5m bar for click-to-inspect temporal view "
            f"(default {DEFAULT_CANDLE_TEMPORAL_BINS})."
        ),
    )
    p.add_argument(
        "--latency-bins",
        type=int,
        default=DEFAULT_LATENCY_BINS,
        help=(
            "In-bar venue latency TW-mass histogram bins for click-to-inspect "
            f"(default {DEFAULT_LATENCY_BINS}; 0 disables latency payloads)."
        ),
    )
    p.add_argument(
        "--latency-temporal-bins",
        type=int,
        default=DEFAULT_LATENCY_TEMPORAL_BINS,
        help=(
            "Equal-time mean slots for in-bar latency temporal view "
            f"(default {DEFAULT_LATENCY_TEMPORAL_BINS})."
        ),
    )
    p.add_argument(
        "--inline-plotly",
        action="store_true",
        help=(
            "Embed plotly.js inside each HTML (large single-file pages). "
            "Default: copy sibling plotly.min.js next to the HTML (file://-friendly)."
        ),
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
    inline_plotly: bool = False,
    candle_bins: int = DEFAULT_CANDLE_BINS,
    candle_temporal_bins: int = DEFAULT_CANDLE_TEMPORAL_BINS,
    latency_bins: int = DEFAULT_LATENCY_BINS,
    latency_temporal_bins: int = DEFAULT_LATENCY_TEMPORAL_BINS,
) -> list[Path]:
    since_ms = parse_since_ms(since)
    until_ms = parse_since_ms(until) if until else int(
        datetime.now(tz=timezone.utc).timestamp() * 1000
    )
    coins_u = [c.upper() for c in coins]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ticks = load_ticks(
        data_root,
        coins=coins_u,
        since_ms=since_ms,
        until_ms=until_ms,
    )

    written: list[Path] = []
    bucket_start = floor_bar_start_ms(since_ms, BAR_MS)
    present = [
        c for c in coins_u if not ticks.loc[ticks["base_coin"] == c].empty
    ]
    for c in coins_u:
        if c not in present:
            print(f"skip {c}: no ticks in window")
    if not present:
        raise SystemExit("no HTML written — check coins / window / data-root")
    write_coins_json(out_dir, present)

    until_label = (
        until
        if until
        else datetime.fromtimestamp(until_ms / 1000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    for coin_u in present:
        sub = ticks.loc[ticks["base_coin"] == coin_u].copy()
        buckets_long = build_5m_bucket_stats(
            sub,
            value_col=SPREAD_LONG_COL,
            start_ms=bucket_start,
            end_ms=until_ms,
            fill_empty_buckets=True,
            ma_bars=ma_bars,
        )
        buckets_short = build_5m_bucket_stats(
            sub,
            value_col=SPREAD_SHORT_COL,
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
            "coins_nav": ",".join(present),
            "since_utc": since,
            "until_utc": until_label,
            "n_ticks": len(sub),
            "n_buckets": len(buckets_long),
            "n_gaps": len(gaps),
            "gap_threshold_ms": gap_threshold_ms,
            "ma_bars": ",".join(str(x) for x in ma_bars),
            "candle_bins": candle_bins,
            "candle_temporal_bins": candle_temporal_bins,
            "latency_bins": latency_bins,
            "latency_temporal_bins": latency_temporal_bins,
            "latency_def": "okx/bybit_latency_ms = local_recv − exchange_ts",
            "spread_long": "(bybit_bid-okx_ask)/bybit_bid*100 → open_long",
            "spread_short": "(okx_bid-bybit_ask)/okx_bid*100 → open_short",
            "tw_weights": "hold until next tick; last tick → 5m bar end",
            "bar_ms": BAR_MS,
            "data_root": str(data_root),
            "plotly": "inline" if inline_plotly else "sibling plotly.min.js",
        }
        path = write_coin_html(
            out_dir / coin_html_filename(coin_u),
            coin=coin_u,
            coins=present,
            ticks=sub,
            buckets_long=buckets_long,
            buckets_short=buckets_short,
            gaps=gaps,
            meta=meta,
            ma_bars=ma_bars,
            max_tick_points=max_tick_points,
            inline_plotly=inline_plotly,
            candle_bins=candle_bins,
            candle_temporal_bins=candle_temporal_bins,
            latency_bins=latency_bins,
            latency_temporal_bins=latency_temporal_bins,
        )
        print(f"wrote {path} (ticks={len(sub)} gaps={len(gaps)})")
        written.append(path)
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
        inline_plotly=args.inline_plotly,
        candle_bins=args.candle_bins,
        candle_temporal_bins=args.candle_temporal_bins,
        latency_bins=args.latency_bins,
        latency_temporal_bins=args.latency_temporal_bins,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
