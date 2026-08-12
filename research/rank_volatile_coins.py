"""Rank coins by composite volatility score on hist 5m dumps.

Score modes:
  z_rank   (default) — mean of cross-sectional ranks of z_vol and z_amp
  ma_ratio — raw short/MA-ratio composites (canonical: blend α·EMA+(1−α)·MA;
             variants geom/mean/min/log_mean/vol_only/atr_only)

Default run: OKX month dump, last common bar, top-20.
Optional: Bybit merge (mean or min of per-exchange composites).

Usage (from repo root, venv):
  ./venv/bin/python research/rank_volatile_coins.py
  ./venv/bin/python research/rank_volatile_coins.py --top 10 --csv
  ./venv/bin/python research/rank_volatile_coins.py --ts 2026-08-07T12:00:00Z
  ./venv/bin/python research/rank_volatile_coins.py --exchanges okx,bybit --combine mean
  ./venv/bin/python research/rank_volatile_coins.py --washout --limit-coins 40
  ./venv/bin/python research/rank_volatile_coins.py --score-mode ma_ratio --variant geom
  ./venv/bin/python research/rank_volatile_coins.py --score-mode ma_ratio --variant geom --long 288
  ./venv/bin/python research/rank_volatile_coins.py --score-mode ma_ratio --numerator blend --blend-alpha 0.75
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from research.regime_composite import (  # noqa: E402
    CompositeParams,
    build_composite_features,
    cross_sectional_composite,
    last_common_bar_ts,
    merge_exchange_composites,
    snapshot_at_ts,
    washout_summary,
)
from research.regime_ma_ratio import (  # noqa: E402
    BLEND_ALPHA_DEFAULT,
    NUMERATOR_BLEND,
    NUMERATORS,
    SCORE_MODE_MA_RATIO,
    SCORE_MODE_Z_RANK,
    VARIANTS,
    MaRatioParams,
    build_ma_ratio_features,
    formula_doc,
    numerator_formula,
    snapshot_ma_ratio_at_ts,
)
from research.regime_metrics import RegimeParams  # noqa: E402

OKX_ROOT = REPO / "output" / "okx_bar5m_hist_regime"
BYBIT_ROOT = REPO / "output" / "bybit_bar5m_hist_regime"
OUT_DIR = REPO / "output"

# Warmup: z_vol/z_amp need W; optional pct diagnostics need lookback — pad recent days.
WARMUP_BARS_PAD = 64


def list_coins(root: Path) -> list[str]:
    coins = []
    for p in sorted(root.iterdir()):
        if p.is_dir() and p.name.startswith("base_coin="):
            coins.append(p.name.split("=", 1)[1])
    return coins


def parse_ts_utc(s: str) -> int:
    """Parse ISO UTC (…Z or +00:00) or bare epoch ms to bar_start_ts_ms."""
    s = s.strip()
    if s.isdigit():
        return int(s)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def format_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def floor_bar_ms(ts_ms: int, bar_ms: int = 300_000) -> int:
    return (ts_ms // bar_ms) * bar_ms


def load_hist_bars_recent(
    root: Path,
    base_coin: str,
    *,
    min_ts_ms: Optional[int] = None,
    max_ts_ms: Optional[int] = None,
) -> pd.DataFrame:
    from datetime import timedelta

    coin_dir = root / f"base_coin={base_coin}"
    if not coin_dir.is_dir():
        return pd.DataFrame()
    day_dirs = sorted(
        p for p in coin_dir.glob("event_date=*") if p.is_dir() and (p / "part.parquet").exists()
    )
    if not day_dirs:
        return pd.DataFrame()

    if min_ts_ms is not None:
        # Calendar pad so day partitions covering warmup are included.
        start_day = datetime.fromtimestamp(min_ts_ms / 1000.0, tz=timezone.utc).date() - timedelta(
            days=1
        )
        day_dirs = [p for p in day_dirs if p.name.split("=", 1)[1] >= start_day.isoformat()]
    if max_ts_ms is not None:
        end_day = datetime.fromtimestamp(max_ts_ms / 1000.0, tz=timezone.utc).date().isoformat()
        day_dirs = [p for p in day_dirs if p.name.split("=", 1)[1] <= end_day]

    parts = [pd.read_parquet(p / "part.parquet") for p in day_dirs]
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    df = (
        df.sort_values("bar_start_ts_ms", kind="mergesort")
        .drop_duplicates(subset=["bar_start_ts_ms"], keep="last")
        .reset_index(drop=True)
    )
    if min_ts_ms is not None:
        df = df.loc[df["bar_start_ts_ms"] >= min_ts_ms].reset_index(drop=True)
    if max_ts_ms is not None:
        df = df.loc[df["bar_start_ts_ms"] <= max_ts_ms].reset_index(drop=True)
    return df


def _warmup_need_bars(params: Union[CompositeParams, MaRatioParams], score_mode: str) -> int:
    """Bars of lookback before asof. Duck-typed so importlib.reload cannot break isinstance."""
    if score_mode == SCORE_MODE_MA_RATIO:
        return int(params.long) + int(params.atr_n) + int(params.short) + WARMUP_BARS_PAD
    return int(params.regime.W) + int(params.delta_k) + int(params.pct_lookback) + WARMUP_BARS_PAD


def warmup_min_ts(
    asof_ts_ms: int,
    params: Union[CompositeParams, MaRatioParams],
    *,
    score_mode: str = SCORE_MODE_Z_RANK,
) -> int:
    return asof_ts_ms - _warmup_need_bars(params, score_mode) * 300_000


def load_universe_frames(
    root: Path,
    coins: list[str],
    *,
    params: Union[CompositeParams, MaRatioParams],
    asof_ts_ms: int,
    score_mode: str = SCORE_MODE_Z_RANK,
    ma_extra_longs: tuple[int, ...] = (),
) -> dict[str, pd.DataFrame]:
    """Load a warmup window ending at asof_ts_ms and build per-coin features."""
    min_ts = warmup_min_ts(asof_ts_ms, params, score_mode=score_mode)
    frames: dict[str, pd.DataFrame] = {}
    for coin in coins:
        bars = load_hist_bars_recent(root, coin, min_ts_ms=min_ts, max_ts_ms=asof_ts_ms)
        if bars.empty:
            continue
        try:
            if score_mode == SCORE_MODE_MA_RATIO:
                frames[coin] = build_ma_ratio_features(
                    bars, params=params, extra_longs=ma_extra_longs
                )
            else:
                frames[coin] = build_composite_features(bars, params=params)
        except Exception as exc:  # noqa: BLE001 — keep screener going
            print(f"skip {coin}: {exc}", file=sys.stderr)
    return frames


def load_full_frames(
    root: Path,
    coins: list[str],
    *,
    params: CompositeParams,
) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for coin in coins:
        bars = load_hist_bars_recent(root, coin)
        if bars.empty:
            continue
        try:
            frames[coin] = build_composite_features(bars, params=params)
        except Exception as exc:  # noqa: BLE001
            print(f"skip washout {coin}: {exc}", file=sys.stderr)
    return frames


def discover_last_ts(root: Path, coins: list[str], sample: int = 24) -> Optional[int]:
    """Fast last-bar probe from a sample of coins; refined later by intersection."""
    maxima = []
    for coin in coins[:sample]:
        coin_dir = root / f"base_coin={coin}"
        days = sorted(coin_dir.glob("event_date=*"))
        if not days:
            continue
        last = days[-1] / "part.parquet"
        if not last.exists():
            continue
        ts = pd.read_parquet(last, columns=["bar_start_ts_ms"])["bar_start_ts_ms"].max()
        maxima.append(int(ts))
    if not maxima:
        return None
    # Prefer mode-ish: most frequent max among sample
    return int(pd.Series(maxima).mode().iloc[0])


def build_rank_table(
    frames: dict[str, pd.DataFrame],
    ts_ms: int,
    *,
    params: CompositeParams,
    include_delta_vol: bool = False,
) -> pd.DataFrame:
    snap = snapshot_at_ts(frames, ts_ms)
    return cross_sectional_composite(
        snap,
        params=params,
        winsorize=True,
        include_delta_vol=include_delta_vol,
    )


def build_ma_ratio_rank_table(
    frames: dict[str, pd.DataFrame],
    ts_ms: int,
    *,
    variant: str,
    long: int,
    primary_long: int,
) -> pd.DataFrame:
    return snapshot_ma_ratio_at_ts(
        frames,
        ts_ms,
        variant=variant,
        long=long,
        primary_long=primary_long,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Rank coins by composite volatility score")
    ap.add_argument("--exchanges", default="okx", help="okx | bybit | okx,bybit")
    ap.add_argument(
        "--combine",
        default="mean",
        choices=("mean", "min"),
        help="when both exchanges: mean or min of per-exchange composites",
    )
    ap.add_argument("--top", type=int, default=20, help="print top-K")
    ap.add_argument(
        "--ts",
        default=None,
        help="UTC timestamp (ISO or epoch ms); default = last common bar",
    )
    ap.add_argument(
        "--score-mode",
        default=SCORE_MODE_Z_RANK,
        choices=(SCORE_MODE_Z_RANK, SCORE_MODE_MA_RATIO),
        help="z_rank (default, cross-sectional z ranks) | ma_ratio (blend/EMA/MA ratios)",
    )
    ap.add_argument(
        "--variant",
        default="geom",
        choices=list(VARIANTS),
        help="ma_ratio variant (ignored for z_rank)",
    )
    ap.add_argument("--short", type=int, default=6, help="short span (ma_ratio EMA/MA/blend)")
    ap.add_argument("--long", type=int, default=48, help="MA long window / SMA floor (ma_ratio)")
    ap.add_argument(
        "--numerator",
        default=NUMERATOR_BLEND,
        choices=list(NUMERATORS),
        help="ma_ratio short-leg: blend (canonical α·EMA+(1−α)·MA) | ema | ma",
    )
    ap.add_argument(
        "--blend-alpha",
        type=float,
        default=BLEND_ALPHA_DEFAULT,
        help="EMA weight α in blend numerator (default 0.75); ignored for ema/ma",
    )
    ap.add_argument(
        "--atr-n",
        type=int,
        default=1,
        help="SMA length of true range before EMA/MA ratios (1 = raw TR)",
    )
    ap.add_argument(
        "--delta-k",
        type=int,
        default=6,
        help="lag for optional delta_vol diagnostic / --include-delta-vol",
    )
    ap.add_argument(
        "--pct-lookback",
        type=int,
        default=288,
        help="own-history pct lookback (diagnostic columns only; z_rank)",
    )
    ap.add_argument("--amp-mean-bars", type=int, default=1)
    ap.add_argument(
        "--include-delta-vol",
        action="store_true",
        help="legacy 3-way z_rank: also rank winsorized delta_vol into composite",
    )
    ap.add_argument("--limit-coins", type=int, default=0, help="0 = all")
    ap.add_argument("--csv", action="store_true", help="write CSV under output/")
    ap.add_argument(
        "--washout",
        action="store_true",
        help="light analysis: long regime duration vs local_score decay (z_rank features)",
    )
    ap.add_argument("--okx-root", type=Path, default=OKX_ROOT)
    ap.add_argument("--bybit-root", type=Path, default=BYBIT_ROOT)
    args = ap.parse_args()

    exchanges = [e.strip().lower() for e in args.exchanges.split(",") if e.strip()]
    for e in exchanges:
        if e not in {"okx", "bybit"}:
            ap.error(f"unknown exchange {e}")

    score_mode = args.score_mode
    if score_mode == SCORE_MODE_MA_RATIO:
        params: Union[CompositeParams, MaRatioParams] = MaRatioParams(
            short=args.short,
            long=args.long,
            atr_n=args.atr_n,
            numerator=args.numerator,
            blend_alpha=args.blend_alpha,
            regime=RegimeParams(),
        )
        z_params = CompositeParams(
            delta_k=args.delta_k,
            pct_lookback=args.pct_lookback,
            amp_mean_bars=args.amp_mean_bars,
            regime=RegimeParams(),
        )
    else:
        params = CompositeParams(
            delta_k=args.delta_k,
            pct_lookback=args.pct_lookback,
            amp_mean_bars=args.amp_mean_bars,
            regime=RegimeParams(),
        )
        z_params = params

    primary = "okx" if "okx" in exchanges else exchanges[0]
    primary_root = args.okx_root if primary == "okx" else args.bybit_root
    coins = list_coins(primary_root)
    if args.limit_coins and args.limit_coins > 0:
        coins = coins[: args.limit_coins]
    if not coins:
        raise SystemExit(f"no coins under {primary_root}")

    if args.ts:
        target_ts = floor_bar_ms(parse_ts_utc(args.ts))
    else:
        probed = discover_last_ts(primary_root, coins)
        if probed is None:
            raise SystemExit("could not discover last bar timestamp")
        target_ts = probed

    if score_mode == SCORE_MODE_MA_RATIO:
        assert isinstance(params, MaRatioParams)
        combine_rule = (
            f"{numerator_formula(params.numerator, blend_alpha=params.blend_alpha)}; "
            f"composite={formula_doc(args.variant)}  "
            f"[{params.numerator_label}; ATR=SMA(TR,{params.atr_n})]"
        )
        params_meta = {
            "score_mode": score_mode,
            "variant": args.variant,
            "numerator": params.numerator,
            "blend_alpha": params.blend_alpha,
            "short": params.short,
            "long": params.long,
            "atr_n": params.atr_n,
            "eps": params.eps,
            "formula": formula_doc(args.variant),
            "r_formula": numerator_formula(
                params.numerator, blend_alpha=params.blend_alpha
            ),
        }
    else:
        combine_rule = (
            (
                "composite = mean(cross_sectional_pct_rank(winsorized delta_vol), "
                "cross_sectional_pct_rank(winsorized z_vol), "
                "cross_sectional_pct_rank(winsorized z_amp))"
            )
            if args.include_delta_vol
            else (
                "composite = mean(cross_sectional_pct_rank(winsorized z_vol), "
                "cross_sectional_pct_rank(winsorized z_amp))"
            )
        )
        params_meta = {
            "score_mode": score_mode,
            "delta_k": params.delta_k,
            "pct_lookback": params.pct_lookback,
            "amp_mean_bars": params.amp_mean_bars,
            "winsor_lo": params.winsor_lo,
            "winsor_hi": params.winsor_hi,
            "W_z": params.regime.W,
            "include_delta_vol": args.include_delta_vol,
        }

    print(
        json.dumps(
            {
                "exchanges": exchanges,
                "combine": args.combine if len(exchanges) > 1 else None,
                "target_ts_ms": target_ts,
                "target_ts_utc": format_ts(target_ts),
                "n_coins_requested": len(coins),
                "params": params_meta,
                "combine_rule": combine_rule,
            },
            indent=2,
        )
    )

    tables: dict[str, pd.DataFrame] = {}

    for ex in exchanges:
        root = args.okx_root if ex == "okx" else args.bybit_root
        ex_coins = [c for c in coins if (root / f"base_coin={c}").is_dir()]
        frames = load_universe_frames(
            root,
            ex_coins,
            params=params,
            asof_ts_ms=target_ts,
            score_mode=score_mode,
        )
        if not args.ts and ex == primary:
            common = last_common_bar_ts(frames)
            if common is not None and common != target_ts:
                target_ts = common
                print(f"last_common_bar_ts={target_ts} ({format_ts(target_ts)})")
        if score_mode == SCORE_MODE_MA_RATIO:
            table = build_ma_ratio_rank_table(
                frames,
                target_ts,
                variant=args.variant,
                long=params.long,
                primary_long=params.long,
            )
        else:
            table = build_rank_table(
                frames,
                target_ts,
                params=params,
                include_delta_vol=args.include_delta_vol,
            )
        tables[ex] = table
        print(
            f"{ex}: snapshot_n={len(table)} "
            f"finite_composite={int(table['composite'].notna().sum()) if not table.empty else 0}"
        )

    if len(exchanges) == 1:
        ranked = tables[exchanges[0]]
    else:
        if "okx" not in tables or "bybit" not in tables:
            raise SystemExit("need both okx and bybit tables for combine")
        ranked = merge_exchange_composites(
            tables["okx"], tables["bybit"], how=args.combine
        )
        if score_mode == SCORE_MODE_MA_RATIO:
            okx_extra = ["base_coin", "r_vol", "r_atr", "regime_on", "rank_xs"]
        else:
            okx_extra = [
                "base_coin",
                "z_vol",
                "z_amp",
                "rank_z_vol",
                "rank_z_amp",
                "regime_on",
            ]
            if args.include_delta_vol:
                okx_extra.extend(["delta_vol", "rank_delta"])
        ranked = ranked.merge(
            tables["okx"][[c for c in okx_extra if c in tables["okx"].columns]],
            on="base_coin",
            how="left",
        )

    if score_mode == SCORE_MODE_MA_RATIO:
        show_cols = [
            c
            for c in [
                "rank",
                "base_coin",
                "composite",
                "r_vol",
                "r_atr",
                "rank_xs",
                "regime_on",
                "composite_okx",
                "composite_bybit",
            ]
            if c in ranked.columns
        ]
    else:
        show_cols = [
            c
            for c in [
                "rank",
                "base_coin",
                "composite",
                "rank_z_vol",
                "rank_z_amp",
                "z_vol",
                "z_amp",
                *(["rank_delta", "delta_vol"] if args.include_delta_vol else []),
                "regime_on",
                "composite_okx",
                "composite_bybit",
            ]
            if c in ranked.columns
        ]
    top = ranked.head(args.top)
    print()
    print(f"TOP {args.top} @ {format_ts(target_ts)}")
    print(top[show_cols].to_string(index=False, float_format=lambda x: f"{x:0.4f}"))

    if args.csv:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = format_ts(target_ts).replace(":", "").replace("-", "")
        mode_tag = score_mode if score_mode == SCORE_MODE_Z_RANK else f"{score_mode}_{args.variant}"
        path = OUT_DIR / f"volatile_rank_{mode_tag}_{'+'.join(exchanges)}_{stamp}.csv"
        ranked.to_csv(path, index=False)
        print(f"\nwrote {path}")

    if args.washout:
        if score_mode != SCORE_MODE_Z_RANK:
            print(
                "\nWASHOUT skipped: --washout uses z_rank local_score / delta_vol diagnostics",
                file=sys.stderr,
            )
        else:
            # Full-month features only for a small coin set (top + alphabetical sample).
            wash_coins = list(dict.fromkeys(top["base_coin"].tolist()[:10] + coins[:15]))
            primary_root = args.okx_root if primary == "okx" else args.bybit_root
            wash_frames = load_full_frames(primary_root, wash_coins, params=z_params)
            summary = washout_summary(wash_frames, coins=wash_coins, min_episode_bars=12)
            print("\nWASHOUT (light)")
            print(json.dumps(summary, indent=2))
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            wash_path = OUT_DIR / "volatile_rank_washout.json"
            wash_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            print(f"wrote {wash_path}")


if __name__ == "__main__":
    main()
