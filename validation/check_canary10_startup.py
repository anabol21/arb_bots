#!/usr/bin/env python3
"""Pre-start guard: discovery against canary universe must show delta_rows=0."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.discovery.intersection import run_discovery  # noqa: E402
from app.utils.canary10_guards import assert_dry_run_discovery_summary  # noqa: E402
from app.utils.universe_delta import assert_delta_path_safe  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--universe", required=True)
    p.add_argument("--delta", required=True)
    p.add_argument("--min-csv-coins", type=int, default=200)
    p.add_argument("--max-new", type=int, default=8)
    args = p.parse_args()
    universe = Path(args.universe)
    delta = Path(args.delta)
    assert_delta_path_safe(delta, universe)
    summary = run_discovery(universe_path=universe, delta_path=delta, max_new=args.max_new)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    assert_dry_run_discovery_summary(summary, min_csv_coins=args.min_csv_coins)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
