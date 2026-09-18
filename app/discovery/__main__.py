"""CLI: python -m app.discovery --universe CSV --delta DELTA --max-new N"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .intersection import DEFAULT_MAX_NEW, DiscoveryError, run_discovery


def _max_new_default() -> int:
    raw = os.environ.get("SPREAD_DISCOVERY_MAX_NEW", str(DEFAULT_MAX_NEW)).strip()
    return int(raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "REST Bybit linear USDT ∩ OKX USDT SWAP, diff against the given "
            "universe CSV, atomically write a capped delta. Does not rewrite "
            "live take=yes rows."
        )
    )
    parser.add_argument(
        "--universe",
        default=os.environ.get("SPREAD_UNIVERSE", "bybit_okx_universe.csv"),
        help="Universe CSV to diff (the path you give; not a different checkout)",
    )
    parser.add_argument(
        "--delta",
        default=os.environ.get("SPREAD_HOT_ADD_DELTA", "hot_add_delta.csv"),
        help="Atomic delta output path (must not be the universe CSV)",
    )
    parser.add_argument(
        "--max-new",
        type=int,
        default=_max_new_default(),
        help="Hard cap on new coins in the delta (env SPREAD_DISCOVERY_MAX_NEW)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%y-%m-%d %H:%M:%S",
    )
    args = build_parser().parse_args(argv)
    try:
        summary = run_discovery(
            universe_path=Path(args.universe),
            delta_path=Path(args.delta),
            max_new=args.max_new,
        )
    except (DiscoveryError, ValueError, OSError) as exc:
        logging.getLogger("discovery").error("discovery_failed | error=%s", exc)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
