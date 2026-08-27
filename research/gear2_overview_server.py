"""Local HTTP server for gear-2 per-coin overview: static HTML + all-ticks API.

Serves ``output/gear2_coin_overview/`` and::

  GET /api/ticks?coin=0G&start=2026-08-19T11:55:00Z&end=2026-08-19T12:00:00Z
  GET /api/meta

``/api/ticks`` reads ``output/lean_ticks`` via ``read_lean_raw`` / ``iter_lean_tables``
with ``coins={coin}``, ``per_file_cap=None`` (no even-take). If the window has more
than ``MAX_ALL_TICK_POINTS`` rows, the handler refuses with an explicit error
(no silent downsample).

Not a gear-2 close. Do not open pages as ``file://`` for the all-ticks view.

Usage (repo root)::

  ./venv/bin/python research/gear2_overview_server.py
  # http://127.0.0.1:8765/0G.html
"""

from __future__ import annotations

import argparse
import errno
import json
import re
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from research.gear2_coin_overview_html import (
    DEFAULT_OUT,
    DEFAULT_TICKS,
    MAX_ALL_TICK_POINTS,
    OVERVIEW_END,
    OVERVIEW_GAP_BREAK_MS,
    OVERVIEW_START,
    PERIOD_JS_NAME,
    REGIME_TOP_N,
    WindowTooWideError,
    build_window_figure,
    copy_period_js,
    infer_default_period,
    load_window_overview,
    ms_to_iso_z,
    patch_overview_pages,
)
from research.lean_ticks_io import parse_ts_ms

COIN_RE = re.compile(r"^[A-Z0-9]{1,20}$")
DEFAULT_PORT = 8765
DEFAULT_THRESH = 0.5
_PERIOD_JS_SRC = Path(__file__).resolve().parent / "gear2_overview_period.js"


def _is_addr_in_use(exc: BaseException) -> bool:
    if not isinstance(exc, OSError):
        return False
    if exc.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", 10048)):
        return True
    return "address already in use" in str(exc).lower()


def _process_cmdline(pid: int) -> str:
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(int(pid)), "-o", "args="],
            text=True,
            timeout=2,
            stderr=subprocess.DEVNULL,
        )
        return " ".join(out.split())
    except (OSError, subprocess.SubprocessError):
        return ""


def list_tcp_listeners(port: int) -> list[tuple[str, int, str]]:
    """Return (command, pid, cmdline) for processes listening on TCP ``port``."""
    try:
        out = subprocess.check_output(
            ["lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN"],
            text=True,
            timeout=3,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    rows: list[tuple[str, int, str]] = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        cmd = parts[0]
        pid = int(parts[1])
        rows.append((cmd, pid, _process_cmdline(pid)))
    return rows


def _is_overview_server_cmd(cmdline: str) -> bool:
    return "gear2_overview_server.py" in (cmdline or "")


def format_busy_port_message(
    host: str,
    port: int,
    listeners: list[tuple[str, int, str]],
) -> str:
    url = f"http://{host}:{int(port)}"
    lines = [
        f"Порт {host}:{int(port)} уже занят (Address already in use).",
        "",
    ]
    overview_pids: list[int] = []
    if listeners:
        lines.append("Кто слушает:")
        for cmd, pid, cmdline in listeners:
            detail = cmdline or cmd
            lines.append(f"  {cmd} PID {pid}")
            lines.append(f"    {detail}")
            if _is_overview_server_cmd(cmdline) or _is_overview_server_cmd(cmd):
                overview_pids.append(pid)
        lines.append("")
    else:
        lines.append("Не удалось определить процесс (lsof).")
        lines.append("")

    if overview_pids or any(_is_overview_server_cmd(c) for _, _, c in listeners):
        lines.append(
            f"Это уже запущенный gear2 overview server — откройте {url}/index.html"
        )
        lines.append(f"или страницу монеты, например {url}/0G.html")
        lines.append("Второй экземпляр не нужен.")
        pid_txt = ", ".join(str(p) for p in overview_pids)
        if pid_txt:
            lines.append(
                f"Если это зависший экземпляр того же скрипта, можно остановить "
                f"только его: kill {pid_txt}"
            )
            lines.append("(не убивайте чужие процессы).")
    else:
        lines.append(
            "Порт занят другим процессом. Не убивайте его, если это не "
            "gear2_overview_server.py."
        )
        lines.append(f"Если тот сервер всё же ваш — откройте {url}/index.html")

    lines.append("")
    lines.append(
        "Другой порт: ./venv/bin/python research/gear2_overview_server.py "
        f"--port {int(port) + 1}"
    )
    return "\n".join(lines)


class TopnCache:
    def __init__(self, start_ms: int, end_ms: int, workers: int) -> None:
        self.start_ms = int(start_ms)
        self.end_ms = int(end_ms)
        self.workers = int(workers)
        self._lock = threading.Lock()
        self._frames = None
        self._topn = None
        self._ready = False

    def ensure(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            from research.gear2_regime_topn import (
                OKX_BAR_ROOT,
                build_topn_by_bar,
                canon_ma_params,
                load_crypto_feature_frames,
            )

            print("overview server: building Gear 1.5 Top-10 map (once)…", flush=True)
            frames, _root, _missing = load_crypto_feature_frames(
                start_ms=self.start_ms,
                end_ms=self.end_ms,
                root=OKX_BAR_ROOT,
                params=canon_ma_params(),
                workers=self.workers,
            )
            self._frames = {str(k).upper(): v for k, v in frames.items()}
            self._topn = (
                build_topn_by_bar(
                    self._frames,
                    start_ms=self.start_ms,
                    end_ms=self.end_ms,
                    top_n=REGIME_TOP_N,
                    params=canon_ma_params(),
                )
                if self._frames
                else {}
            )
            self._ready = True
            print(
                f"overview server: Top-10 bars={len(self._topn)} "
                f"feat_coins={len(self._frames)}",
                flush=True,
            )

    def frames(self) -> dict:
        self.ensure()
        return self._frames or {}

    def topn(self) -> dict:
        self.ensure()
        return self._topn or {}


def _json_bytes(payload: dict, status: int = 200):
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return status, body


def _figure_json(fig) -> dict:
    return json.loads(fig.to_json())


def handle_meta(ctx: dict) -> tuple:
    ps, pe = infer_default_period(
        ctx["ticks"], ctx["cal_start_ms"], ctx["cal_end_ms"]
    )
    return _json_bytes(
        {
            "ok": True,
            "calendar_start": OVERVIEW_START,
            "calendar_end": OVERVIEW_END,
            "default_start": ms_to_iso_z(ps),
            "default_end": ms_to_iso_z(pe),
            "max_points": ctx["max_points"],
            "gap_ms": OVERVIEW_GAP_BREAK_MS,
        }
    )


def handle_ticks(qs: dict, ctx: dict) -> tuple:
    from research.gear2_regime_topn import (
        load_coin_ma_features,
        topn_intervals_ms,
        topn_span_note,
    )

    coins = qs.get("coin") or qs.get("base_coin") or []
    starts = qs.get("start") or []
    ends = qs.get("end") or []
    if not coins or not starts or not ends:
        return _json_bytes(
            {"ok": False, "error": "нужны query coin, start, end (UTC ISO)"},
            400,
        )
    coin = str(coins[0]).strip().upper()
    if not COIN_RE.match(coin):
        return _json_bytes({"ok": False, "error": f"некорректная монета: {coin}"}, 400)
    try:
        start_ms = parse_ts_ms(starts[0])
        end_ms = parse_ts_ms(ends[0])
    except Exception as exc:
        return _json_bytes(
            {"ok": False, "error": f"не разобрать start/end: {exc}"},
            400,
        )
    if end_ms <= start_ms:
        return _json_bytes({"ok": False, "error": "END должен быть позже START"}, 400)

    try:
        ov, used, n_raw = load_window_overview(
            ctx["ticks"],
            coin,
            start_ms,
            end_ms,
            workers=ctx["workers"],
            max_points=ctx["max_points"],
        )
    except WindowTooWideError as exc:
        return _json_bytes(
            {
                "ok": False,
                "error": str(exc),
                "n": exc.n,
                "max_points": exc.max_points,
            },
            400,
        )
    except ValueError as exc:
        return _json_bytes({"ok": False, "error": str(exc)}, 400)

    if ov is None or ov.empty:
        return _json_bytes(
            {
                "ok": False,
                "error": f"нет тиков {coin} в [{ms_to_iso_z(start_ms)}, {ms_to_iso_z(end_ms)})",
                "n_parquet": int(n_raw),
                "n_plot": 0,
            },
            404,
        )

    feats = load_coin_ma_features(coin, start_ms=start_ms, end_ms=end_ms)
    has_hist = feats is not None and len(feats) > 0
    cache: Optional[TopnCache] = ctx.get("topn_cache")
    spans = []
    if has_hist and cache is not None:
        spans = topn_intervals_ms(
            coin, cache.topn(), start_ms=start_ms, end_ms=end_ms
        )
    tnote = topn_span_note(coin, spans, has_hist=has_hist)
    fig, ov_plot, pct_l, pct_s = build_window_figure(
        ov,
        coin,
        start_ms=start_ms,
        end_ms=end_ms,
        thresh=ctx["thresh"],
        gap_ms=OVERVIEW_GAP_BREAK_MS,
        features=feats,
        topn_intervals=spans,
        topn_note=tnote,
    )
    n_plot = int(len(ov_plot))
    payload = {
        "ok": True,
        "coin": coin,
        "start": ms_to_iso_z(start_ms),
        "end": ms_to_iso_z(end_ms),
        "n_parquet": int(n_raw),
        "n_plot": n_plot,
        "n_files": len(used),
        "max_points": ctx["max_points"],
        "pct_long": {str(k): float(v) for k, v in (pct_l or {}).items()},
        "pct_short": {str(k): float(v) for k, v in (pct_s or {}).items()},
        "n_topn": len(spans),
        "figure": _figure_json(fig),
    }
    return _json_bytes(payload)


def make_handler(ctx: dict):
    html_dir = str(ctx["html"])

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=html_dir, **kwargs)

        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _send_json(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/api/meta":
                status, body = handle_meta(ctx)
                self._send_json(status, body)
                return
            if parsed.path == "/api/ticks":
                qs = parse_qs(parsed.query)
                status, body = handle_ticks(qs, ctx)
                self._send_json(status, body)
                return
            # Period JS lives as overview_period.js in the HTML dir; fall back
            # to research/ so --no-patch-html still serves the form script.
            if parsed.path.rstrip("/") == f"/{PERIOD_JS_NAME}":
                disk = Path(html_dir) / PERIOD_JS_NAME
                src = disk if disk.is_file() else _PERIOD_JS_SRC
                if src.is_file():
                    self._send_bytes(
                        200,
                        src.read_bytes(),
                        "application/javascript; charset=utf-8",
                    )
                    return
            return super().do_GET()

    return Handler


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticks", type=Path, default=DEFAULT_TICKS)
    parser.add_argument("--html", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"TCP port (default {DEFAULT_PORT}). If busy, print who listens and exit.",
    )
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-points", type=int, default=MAX_ALL_TICK_POINTS)
    parser.add_argument("--thresh", type=float, default=DEFAULT_THRESH)
    parser.add_argument(
        "--no-patch-html",
        action="store_true",
        help="do not inject period UI into existing coin HTML on startup",
    )
    args = parser.parse_args(argv)

    html_dir = args.html.resolve()
    ticks = args.ticks.resolve()
    if not html_dir.is_dir():
        print(f"HTML dir missing: {html_dir}", file=sys.stderr)
        return 1
    if not ticks.is_dir():
        print(f"lean ticks dir missing: {ticks}", file=sys.stderr)
        return 1

    cal_start = parse_ts_ms(OVERVIEW_START)
    cal_end = parse_ts_ms(OVERVIEW_END)
    js_dest = copy_period_js(html_dir)
    print(f"period JS → {js_dest}", flush=True)
    if not args.no_patch_html:
        n = patch_overview_pages(
            html_dir,
            calendar_start=OVERVIEW_START,
            calendar_end=OVERVIEW_END,
            max_points=int(args.max_points),
            ticks=ticks,
        )
        if n:
            print(f"period UI добавлен в {n} HTML под {html_dir}", flush=True)
        else:
            print(
                "period UI уже был в страницах монет (0 файлов изменено) — "
                f"форма на диске, откройте через http://{args.host}:{int(args.port)}/index.html",
                flush=True,
            )

    ctx = {
        "ticks": ticks,
        "html": html_dir,
        "workers": int(args.workers),
        "max_points": int(args.max_points),
        "thresh": float(args.thresh),
        "cal_start_ms": cal_start,
        "cal_end_ms": cal_end,
        "topn_cache": TopnCache(cal_start, cal_end, int(args.workers)),
    }
    handler = make_handler(ctx)
    try:
        server = ThreadingHTTPServer((args.host, int(args.port)), handler)
    except OSError as exc:
        if _is_addr_in_use(exc):
            listeners = list_tcp_listeners(int(args.port))
            print(
                format_busy_port_message(args.host, int(args.port), listeners),
                file=sys.stderr,
            )
            return 1
        raise
    threading.Thread(target=ctx["topn_cache"].ensure, daemon=True).start()
    url = f"http://{args.host}:{int(args.port)}"
    print(
        f"gear2 overview server {url}/\n"
        f"  open {url}/0G.html\n"
        f"  API  GET {url}/api/ticks?coin=0G&start=…&end=…\n"
        f"  ticks={ticks}\n"
        f"  html={html_dir}\n"
        f"  max_points={args.max_points} (refuse, no downsample)\n"
        f"  bind {args.host} only",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
