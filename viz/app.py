"""FastAPI app: meta / coins / coverage / ticks + static React dist."""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from research.gear2_coin_overview_html import WindowTooWideError
from research.is_crypto import is_crypto
from research.lean_ticks_io import parse_ts_ms
from viz import catalog as cat
from viz.config import (
    DEFAULT_CATALOG,
    DEFAULT_TICKS,
    DEFAULT_WEB_DIST,
    MAX_ALL_TICK_POINTS,
)
from viz.overview import SUMMARY_PATH, load_coin_overview, load_summary
from viz.overview_cache import (
    payload_from_store,
    payload_from_summary,
    read_coin_page,
    write_coin_page,
)
from viz.ticks import load_coin_ticks, ms_to_iso_z

COIN_RE = re.compile(r"^[A-Z0-9]{1,20}$")

_state: dict[str, Any] = {
    "ticks": DEFAULT_TICKS,
    "catalog": DEFAULT_CATALOG,
    "web_dist": DEFAULT_WEB_DIST,
    "max_points": MAX_ALL_TICK_POINTS,
    "workers": 8,
    "coins_cache": None,
    "coins_cache_ts": 0.0,
}


def create_app(
    *,
    ticks: Path = DEFAULT_TICKS,
    catalog_path: Path = DEFAULT_CATALOG,
    web_dist: Path = DEFAULT_WEB_DIST,
    max_points: int = MAX_ALL_TICK_POINTS,
    workers: int = 8,
) -> FastAPI:
    _state["ticks"] = Path(ticks).resolve()
    _state["catalog"] = Path(catalog_path).resolve()
    _state["web_dist"] = Path(web_dist).resolve()
    _state["max_points"] = int(max_points)
    _state["workers"] = int(workers)

    app = FastAPI(title="Spread viz", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.get("/api/meta")
    def api_meta() -> dict[str, Any]:
        ticks_dir: Path = _state["ticks"]
        catalog_path: Path = _state["catalog"]
        bounds = cat.catalog_bounds(catalog_path)
        n_files = 0
        try:
            con = cat.connect(catalog_path)
            n_files = int(con.execute("SELECT COUNT(*) FROM lean_files").fetchone()[0])
            con.close()
        except Exception:
            n_files = 0
        return {
            "ok": True,
            "host": "mac",
            "source_of_truth": "backup1tb:spread-compacted (via local lean_ticks cache)",
            "ticks_dir": str(ticks_dir),
            "catalog": str(catalog_path),
            "ticks_dir_exists": ticks_dir.is_dir(),
            "n_files": n_files,
            "span_start": ms_to_iso_z(bounds[0]) if bounds else None,
            "span_end": ms_to_iso_z(bounds[1]) if bounds else None,
            "max_points": _state["max_points"],
            "note": (
                "UI/API never read VPS /data/live. Sync from backup is a separate "
                "one-shot script (viz/sync_from_backup.py)."
            ),
        }

    @app.post("/api/catalog/rebuild")
    def api_catalog_rebuild() -> dict[str, Any]:
        try:
            out = cat.rebuild_catalog(_state["ticks"], _state["catalog"])
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        _state["coins_cache"] = None
        return out

    @app.get("/api/coins")
    def api_coins(
        refresh: bool = Query(False),
        sample_files: int = Query(24, ge=1, le=200),
    ) -> dict[str, Any]:
        now = time.time()
        if (
            not refresh
            and _state["coins_cache"] is not None
            and (now - float(_state["coins_cache_ts"])) < 300
        ):
            return _state["coins_cache"]

        # Ensure catalog exists
        bounds = cat.catalog_bounds(_state["catalog"])
        if bounds is None:
            try:
                cat.rebuild_catalog(_state["ticks"], _state["catalog"])
            except FileNotFoundError as exc:
                raise HTTPException(404, str(exc)) from exc

        coins = cat.distinct_coins_sample(
            _state["ticks"],
            _state["catalog"],
            sample_files=int(sample_files),
        )
        crypto = sorted(c for c in coins if is_crypto(c))
        other = sorted(c for c in coins if not is_crypto(c))
        payload = {
            "ok": True,
            "n": len(coins),
            "n_crypto": len(crypto),
            "n_other": len(other),
            "crypto": crypto,
            "other": other,
            "sample_files": int(sample_files),
        }
        _state["coins_cache"] = payload
        _state["coins_cache_ts"] = now
        return payload

    @app.get("/api/coverage")
    def api_coverage(
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> dict[str, Any]:
        bounds = cat.catalog_bounds(_state["catalog"])
        if bounds is None:
            try:
                cat.rebuild_catalog(_state["ticks"], _state["catalog"])
            except FileNotFoundError as exc:
                raise HTTPException(404, str(exc)) from exc
        start_ms = parse_ts_ms(start) if start else None
        end_ms = parse_ts_ms(end) if end else None
        try:
            return cat.coverage_holes(
                _state["catalog"],
                start_ms=start_ms,
                end_ms=end_ms,
            )
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/overview/summary")
    def api_overview_summary() -> dict[str, Any]:
        payload = load_summary(SUMMARY_PATH)
        if payload is None:
            return {
                "ok": False,
                "error": (
                    "нет кэша overview; соберите: "
                    "./venv/bin/python -m viz.build_overview"
                ),
                "path": str(SUMMARY_PATH),
            }
        return payload

    @app.get("/api/overview/coin")
    def api_overview_coin(
        coin: str = Query(...),
        start: Optional[str] = None,
        end: Optional[str] = None,
        refresh: bool = Query(False),
        compute: bool = Query(
            False,
            description="If true, allow slow full-span live scan to build line cache",
        ),
    ) -> dict[str, Any]:
        u = coin.strip().upper()
        if not COIN_RE.match(u):
            raise HTTPException(400, f"некорректная монета: {coin}")
        bounds = cat.catalog_bounds(_state["catalog"])
        if bounds is None:
            try:
                cat.rebuild_catalog(_state["ticks"], _state["catalog"])
                bounds = cat.catalog_bounds(_state["catalog"])
            except FileNotFoundError as exc:
                raise HTTPException(404, str(exc)) from exc
        if bounds is None:
            raise HTTPException(404, "пустой каталог lean_ticks")
        try:
            start_ms = parse_ts_ms(start) if start else bounds[0]
            end_ms = parse_ts_ms(end) if end else bounds[1]
        except Exception as exc:
            raise HTTPException(400, f"не разобрать start/end: {exc}") from exc

        full_span = start_ms == bounds[0] and end_ms == bounds[1]

        # Fast paths — never hang the UI on a multi-hour lean scan.
        if not refresh and full_span:
            cached = read_coin_page(u)
            if (
                cached
                and cached.get("start") == ms_to_iso_z(start_ms)
                and int(cached.get("n_line") or 0) > 0
            ):
                cached["cache_hit"] = True
                return cached
            stored = payload_from_store(u, start_ms, end_ms)
            if stored is not None and int(stored.get("n_line") or 0) > 0:
                write_coin_page(u, stored)
                return stored
            if not compute:
                instant = payload_from_summary(u, start_ms, end_ms)
                if instant is not None:
                    instant["cache_hit"] = True
                    return instant
                return {
                    "ok": False,
                    "error": (
                        "нет overview_summary и нет кэша линии; "
                        "соберите summary или вызовите с compute=1"
                    ),
                    "coin": u,
                }

        # Short custom windows always compute; full-span only with compute=1.
        if full_span and not compute and not refresh:
            raise HTTPException(
                400,
                "full-span live scan disabled; pass compute=1 to build line cache",
            )

        try:
            payload = load_coin_overview(
                _state["ticks"],
                u,
                start_ms,
                end_ms,
                workers=_state["workers"],
            )
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(400, str(exc)) from exc
        payload["cache_hit"] = False
        payload["source"] = "live_scan"
        payload["has_line"] = int(payload.get("n_line") or 0) > 0
        if full_span and payload.get("n_all", 0) > 0:
            write_coin_page(u, payload)
        return payload

    @app.get("/api/ticks")
    def api_ticks(
        coin: str = Query(...),
        start: str = Query(...),
        end: str = Query(...),
    ) -> dict[str, Any]:
        u = coin.strip().upper()
        if not COIN_RE.match(u):
            raise HTTPException(400, f"некорректная монета: {coin}")
        try:
            start_ms = parse_ts_ms(start)
            end_ms = parse_ts_ms(end)
        except Exception as exc:
            raise HTTPException(400, f"не разобрать start/end: {exc}") from exc
        try:
            return load_coin_ticks(
                _state["ticks"],
                u,
                start_ms,
                end_ms,
                workers=_state["workers"],
                max_points=_state["max_points"],
            )
        except WindowTooWideError as exc:
            raise HTTPException(
                400,
                {
                    "ok": False,
                    "error": str(exc),
                    "n": exc.n,
                    "max_points": exc.max_points,
                },
            ) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    dist: Path = _state["web_dist"]
    if dist.is_dir() and (dist / "index.html").is_file():
        assets = dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/")
        def spa_index() -> FileResponse:
            return FileResponse(dist / "index.html")

        @app.get("/{path:path}")
        def spa_fallback(path: str) -> FileResponse:
            # API already registered; this catches client-side routes
            candidate = dist / path
            if candidate.is_file() and dist.resolve() in candidate.resolve().parents:
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")

    return app


app = create_app()
