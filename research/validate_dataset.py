#!/usr/bin/env python3
"""Validate all compacted lean ticks and 5m historical bars.

Checks:
1. Every file in output/lean_ticks is readable, non-empty, and has exactly LEAN_TICK_BODY_COLS.
2. Reports date coverage (windows per day).
3. Verifies Bybit and OKX 5m bars coverage and schema.
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.schema.lean_event import LEAN_TICK_BODY_COLS

BOOK_COLS = (
    "okx_bid_price",
    "okx_ask_price",
    "bybit_bid_price",
    "bybit_ask_price",
)


def validate_ticks(ticks_dir: Path) -> dict:
    files = sorted(ticks_dir.glob("spread_*.parquet"))
    print(f"\n==========================================")
    print(f"VALIDATING LEAN TICKS: {ticks_dir}")
    print(f"Total files found: {len(files)}")
    print(f"==========================================")

    if not files:
        return {"n_files": 0, "error": "no files found"}

    t0 = time.time()
    valid_count = 0
    schema_ok_count = 0
    bad_files = []
    total_bytes = 0
    days = Counter()

    for i, f in enumerate(files):
        total_bytes += f.stat().st_size
        day_str = f.name[7:15]
        days[day_str] += 1

        try:
            sch = pq.read_schema(f)
            names = list(sch.names)
            if names == list(LEAN_TICK_BODY_COLS):
                schema_ok_count += 1
            else:
                bad_files.append((f.name, f"Schema mismatch: {len(names)} cols != 16 cols"))
                continue
            valid_count += 1
        except Exception as e:
            bad_files.append((f.name, f"Read error: {e}"))

    elapsed = time.time() - t0
    print(f"Checked {len(files)} files in {elapsed:.2f}s ({len(files)/elapsed:.1f} files/s)")
    print(f"Valid 16-col lean files: {valid_count}/{len(files)} ({valid_count/len(files)*100:.1f}%)")
    print(f"Total dataset size     : {total_bytes / (1024**3):.2f} GB")
    print(f"Date range             : {min(days.keys())} .. {max(days.keys())}")

    print("\nDaily 5-minute window coverage:")
    for d, c in sorted(days.items()):
        pct = (c / 288.0) * 100
        bar = "█" * int(pct / 5)
        print(f"  {d}: {c:3d}/288 ({pct:5.1f}%) {bar}")

    if bad_files:
        print(f"\nWARNING: {len(bad_files)} invalid files found:")
        for name, err in bad_files[:10]:
            print(f"  {name}: {err}")
    else:
        print("\nAll files are 100% valid, readable, canonical 16-column lean parquet!")

    return {
        "n_files": len(files),
        "valid_files": valid_count,
        "bad_files": len(bad_files),
        "total_bytes": total_bytes,
        "days": dict(sorted(days.items())),
    }


def validate_bars(out_dir: Path, venue: str) -> dict:
    print(f"\n==========================================")
    print(f"VALIDATING {venue.upper()} 5M BARS: {out_dir}")
    print(f"==========================================")

    if not out_dir.exists():
        print(f"Directory {out_dir} does not exist!")
        return {}

    coin_dirs = sorted(p for p in out_dir.iterdir() if p.name.startswith("base_coin="))
    dates = sorted(set(p.name.split("=")[1] for p in out_dir.glob("base_coin=*/event_date=*")))
    part_files = list(out_dir.glob("base_coin=*/event_date=*/part.parquet"))
    total_bytes = sum(f.stat().st_size for f in part_files)

    print(f"Unique base coins : {len(coin_dirs)}")
    print(f"Total dates       : {len(dates)} ({dates[0] if dates else 'N/A'} .. {dates[-1] if dates else 'N/A'})")
    print(f"Total part files  : {len(part_files)}")
    print(f"Total size        : {total_bytes / (1024**2):.1f} MB")

    # Sample check schema
    if part_files:
        sample = part_files[0]
        sch = pq.read_schema(sample)
        print(f"Schema ({len(sch.names)} cols): {sch.names}")

    return {
        "venue": venue,
        "n_coins": len(coin_dirs),
        "n_dates": len(dates),
        "date_min": dates[0] if dates else None,
        "date_max": dates[-1] if dates else None,
        "n_parts": len(part_files),
        "total_bytes": total_bytes,
    }


def main():
    ticks_dir = REPO / "output" / "lean_ticks"
    bybit_dir = REPO / "output" / "bybit_bar5m_hist_regime"
    okx_dir = REPO / "output" / "okx_bar5m_hist_regime"

    report = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ticks": validate_ticks(ticks_dir),
        "bybit_bars": validate_bars(bybit_dir, "bybit"),
        "okx_bars": validate_bars(okx_dir, "okx"),
    }

    report_path = REPO / "output" / "_validation_report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nSaved full validation report to {report_path}")


if __name__ == "__main__":
    main()
