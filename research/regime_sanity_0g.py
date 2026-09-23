"""Sanity-check regime metrics v0 on vacation dump bars for base_coin=0G.

Usage (from repo root):
  python3 research/regime_sanity_0g.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from research.regime_metrics import RegimeParams, build_regime_frame, sanity_summary

DUMP = REPO / "output" / "vacation_return_20260810"
BARS_ROOT = DUMP / "bars" / "bar_5m" / "base_coin=0G"
OUT_DIR = DUMP / "_inspect"
OUT_JSON = OUT_DIR / "regime_sanity_0g.json"


def load_0g_bars() -> pd.DataFrame:
    files = sorted(BARS_ROOT.glob("event_date=*/*.parquet"))
    if not files:
        raise FileNotFoundError(f"no bar parquet under {BARS_ROOT}")
    parts = [pd.read_parquet(p) for p in files]
    df = pd.concat(parts, ignore_index=True)
    df = df.sort_values("bar_start_ts_ms", kind="mergesort").drop_duplicates(
        subset=["bar_start_ts_ms"], keep="last"
    )
    return df.reset_index(drop=True)


def main() -> None:
    bars = load_0g_bars()
    params = RegimeParams()
    frame = build_regime_frame(bars, params=params, ticks=None)
    summary = sanity_summary(frame)
    summary["base_coin"] = "0G"
    summary["params"] = params.__dict__
    summary["bar_start_min_ms"] = int(frame["bar_start_ts_ms"].min())
    summary["bar_start_max_ms"] = int(frame["bar_start_ts_ms"].max())
    summary["volume_min"] = float(frame["volume"].min())
    summary["volume_max"] = float(frame["volume"].max())
    summary["pass"] = (
        summary["n_bars"] > 0
        and summary["z_vol_smooth_finite_share"] > 0.5
        and not summary["warn_high_regime_share"]
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    frame_path = OUT_DIR / "regime_frame_0g.parquet"
    frame.to_parquet(frame_path, index=False)

    print(json.dumps(summary, indent=2))
    print(f"wrote {OUT_JSON}")
    print(f"wrote {frame_path}")


if __name__ == "__main__":
    main()
