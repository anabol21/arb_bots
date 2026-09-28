"""Per-symbol OKX ``ctVal`` cache for synthetic live place.

Prefetch runs at warm start, off the signal path, same idea as
``prefetch_okx_inst_id_codes``. Place only reads the cache. A missing
``ctVal`` aborts before ``ws.send``. The coin count is never sent as ``sz``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Callable, Mapping, Optional, Sequence
from urllib.request import Request

FetchFn = Callable[[str], object]


def _positive_decimal(raw: object) -> Optional[Decimal]:
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    try:
        val = raw if isinstance(raw, Decimal) else Decimal(str(raw))
    except Exception:  # noqa: BLE001 — metadata boundary
        return None
    if val <= 0:
        return None
    return val


def lookup_okx_ct_val(
    values: Mapping[str, Decimal], symbol: str
) -> Optional[Decimal]:
    """Positive cached ctVal for one OKX symbol, else None. No network."""
    return _positive_decimal(values.get(str(symbol)))


class CtValView:
    """Universe meta plus a prefetched ``okx_ct_val``. Does not mutate the row."""

    def __init__(self, inner: object, ct_val: Decimal) -> None:
        parsed = _positive_decimal(ct_val)
        if parsed is None:
            raise ValueError("ct_val")
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "okx_ct_val", parsed)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def bind_okx_ct_val(
    meta: object, values: Mapping[str, Decimal]
) -> tuple[object, Optional[str]]:
    """Attach a cached ctVal, or keep an explicit positive value already on meta.

    Returns ``(meta, "okx_ct_val_missing")`` when neither source has ctVal.
    """
    symbol = str(getattr(meta, "okx_symbol", "") or "")
    cached = lookup_okx_ct_val(values, symbol)
    if cached is not None:
        return CtValView(meta, cached), None
    raw = getattr(meta, "okx_ct_val", None)
    if raw is None:
        raw = getattr(meta, "ct_val", None)
    parsed = _positive_decimal(raw)
    if parsed is None:
        return meta, "okx_ct_val_missing"
    return meta, None


def prefetch_okx_ct_vals(
    symbols: Sequence[str],
    *,
    fetch_fn: Optional[FetchFn] = None,
    cached: Optional[Mapping[str, object]] = None,
) -> dict[str, Decimal]:
    """Fill symbol → ctVal before any place.

    ``fetch_fn`` is injected in tests. The production instruments GET is
    passed in by the warm-start caller and is not invoked when ``fetch_fn``
    is omitted. There is no env default and no fallback of 1.
    """
    out: dict[str, Decimal] = {}
    for key, raw in dict(cached or {}).items():
        parsed = _positive_decimal(raw)
        if parsed is not None:
            out[str(key)] = parsed
    names = [str(s).strip() for s in symbols if str(s).strip()]
    if fetch_fn is None:
        return out
    for symbol in names:
        if symbol in out:
            continue
        try:
            raw = fetch_fn(symbol)
        except (TypeError, ValueError, OSError, RuntimeError):
            continue
        parsed = _positive_decimal(raw)
        if parsed is not None:
            out[symbol] = parsed
    return out


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


def fetch_okx_ct_val(symbol: str) -> Decimal:
    """Warm-start lookup via ``LiveHttpMetadataProvider.okx_ct_val``.

    Public instruments only. Do not call this from ``place``.
    """
    from app.bot.private.order_preflight import LiveHttpMetadataProvider

    provider = LiveHttpMetadataProvider(http_get_json=_public_instruments_get)
    return provider.okx_ct_val(str(symbol))
