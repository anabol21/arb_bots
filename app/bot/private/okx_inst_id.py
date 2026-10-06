"""Per-symbol OKX ``instIdCode`` cache for synthetic live place.

Prefetch runs at warm start, off the signal path, same idea as
``LiveBroker.warmup_inst_id_codes``. Place only reads the cache. A missing
code aborts before ``ws.send``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable, Mapping, Optional, Sequence
from urllib.request import Request

from app.bot.private.order_metadata import parse_inst_id_code
from app.bot.private.ws_trivial_dual_leg import parse_inst_id_code_env

FetchFn = Callable[[str], object]


def lookup_okx_inst_id_code(codes: Mapping[str, int], symbol: str) -> Optional[int]:
    """Positive cached code for one OKX symbol, else None. No network."""
    return parse_inst_id_code(codes.get(str(symbol)))


def prefetch_okx_inst_id_codes(
    symbols: Sequence[str],
    *,
    env: Optional[Mapping[str, str]] = None,
    fetch_fn: Optional[FetchFn] = None,
    cached: Optional[Mapping[str, int]] = None,
) -> dict[str, int]:
    """Fill symbol → instIdCode before any place.

    Order: caller cache, then ``BBOT_OKX_INST_ID_CODES``, then ``fetch_fn``
    for symbols still missing. ``fetch_fn`` is injected in tests. The
    production instruments GET is passed in by the warm-start caller and is
    not invoked when ``fetch_fn`` is omitted.
    """
    codes: dict[str, int] = {}
    for key, raw in dict(cached or {}).items():
        parsed = parse_inst_id_code(raw)
        if parsed is not None:
            codes[str(key)] = parsed
    if env is not None:
        for key, value in parse_inst_id_code_env(env.get("BBOT_OKX_INST_ID_CODES")).items():
            codes[str(key)] = value
    names = [str(s).strip() for s in symbols if str(s).strip()]
    if fetch_fn is None:
        return codes
    for symbol in names:
        if symbol in codes:
            continue
        try:
            raw = fetch_fn(symbol)
        except (TypeError, ValueError, OSError, RuntimeError):
            continue
        parsed = parse_inst_id_code(raw)
        if parsed is not None:
            codes[symbol] = parsed
    return codes


def _public_instruments_get(url: str, headers: Mapping[str, str]) -> dict:
    """Stdlib GET for the public instruments URL. Not an order endpoint."""
    req = Request(url, headers={str(k): str(v) for k, v in headers.items()}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"okx instruments HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("okx instruments lookup failed") from exc
    if not isinstance(data, dict):
        raise RuntimeError("okx instruments payload")
    return data


def fetch_okx_inst_id_code(symbol: str) -> int:
    """Warm-start lookup via ``LiveHttpMetadataProvider.okx_inst_id_code``.

    Public instruments only. Do not call this from ``place``.
    """
    from app.bot.private.order_preflight import LiveHttpMetadataProvider

    provider = LiveHttpMetadataProvider(http_get_json=_public_instruments_get)
    code = provider.okx_inst_id_code(str(symbol))
    if code is None:
        raise RuntimeError("okx instIdCode missing")
    return code
