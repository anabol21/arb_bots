"""Run: python -m viz --host 0.0.0.0 --port 8787"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from viz import catalog as cat
from viz.app import create_app
from viz.config import (
    DEFAULT_CATALOG,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_TICKS,
    DEFAULT_WEB_DIST,
    MAX_ALL_TICK_POINTS,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Spread viz (Mac + lean_ticks cache)")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--ticks", type=Path, default=DEFAULT_TICKS)
    p.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    p.add_argument("--web-dist", type=Path, default=DEFAULT_WEB_DIST)
    p.add_argument("--max-points", type=int, default=MAX_ALL_TICK_POINTS)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument(
        "--rebuild-catalog",
        action="store_true",
        help="rebuild DuckDB file index before serving",
    )
    args = p.parse_args(argv)

    ticks = args.ticks.resolve()
    if not ticks.is_dir():
        print(f"lean ticks missing: {ticks}", file=sys.stderr)
        return 1

    if args.rebuild_catalog or not args.catalog.exists():
        print("rebuilding catalog…", flush=True)
        info = cat.rebuild_catalog(ticks, args.catalog.resolve())
        print(
            f"catalog: n_files={info['n_files']} "
            f"span={info['start']} → {info['end']}",
            flush=True,
        )

    app = create_app(
        ticks=ticks,
        catalog_path=args.catalog.resolve(),
        web_dist=args.web_dist.resolve(),
        max_points=args.max_points,
        workers=args.workers,
    )
    url = f"http://{args.host}:{args.port}"
    print(
        f"Spread viz\n"
        f"  UI   {url}/\n"
        f"  API  {url}/api/meta\n"
        f"  ticks cache: {ticks}\n"
        f"  SoT: backup1tb (sync separately; no VPS hot-path queries)\n",
        flush=True,
    )
    uvicorn.run(app, host=args.host, port=int(args.port), log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
