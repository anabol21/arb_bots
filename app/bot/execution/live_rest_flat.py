"""Complete signed read-only flat proof for an isolated EV2 live account.

The probe is deliberately outside the signal-to-send hot path: run it before
arming an OPEN and after a CLOSE. It never sends or cancels an order. It fails
closed on incomplete pagination, changing private generations, malformed rows,
or any existing USDT-linear/SWAP exposure on either venue.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote

from app.bot.execution.engine import ReadinessSnapshot
from app.bot.execution.signed_quantity_probe import (
    SignedQuantityProbeError,
    _okx_complete_pending_orders,
)
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.venue import VenueEndpoints, endpoints_for_venue
from app.bot.private.ws_w4_baseline import (
    BaselineError,
    _BYBIT_OPEN,
    _BYBIT_POS,
    _OKX_POS,
    _bybit_signed_get,
    _okx_signed_get,
)


class LiveRestFlatError(RuntimeError):
    """The account cannot be proven flat from complete signed responses."""


_MAX_BYBIT_PAGES = 10


def _ready_generations(snapshot: ReadinessSnapshot) -> tuple[int, int]:
    if not isinstance(snapshot, ReadinessSnapshot) or any((
        not snapshot.bybit_private_ready,
        not snapshot.okx_private_ready,
        not snapshot.bybit_trade_ready,
        not snapshot.okx_trade_ready,
        snapshot.pause,
        snapshot.kill_switch,
    )):
        raise LiveRestFlatError("private_or_trade_not_ready")
    return snapshot.bybit_generation, snapshot.okx_generation


def _bybit_all_pages(
    *, credentials: LiveCredentials, base: str, path: str,
    query: str, http_get_json: Optional[Callable[..., Mapping[str, Any]]],
) -> Mapping[str, Any]:
    rows: list[Mapping[str, Any]] = []
    seen_cursors: set[str] = set()
    seen_rows: set[str] = set()
    cursor: Optional[str] = None
    for _ in range(_MAX_BYBIT_PAGES):
        page_query = query if cursor is None else f"{query}&cursor={quote(cursor, safe='')}"
        page = _bybit_signed_get(
            credentials=credentials, base=base, path=path,
            query=page_query, http_get_json=http_get_json,
        )
        result = page.get("result") if isinstance(page, Mapping) else None
        if not isinstance(page, Mapping) or page.get("retCode") not in (0, "0") or not isinstance(result, Mapping):
            raise LiveRestFlatError("bybit_page_rejected")
        items = result.get("list")
        if not isinstance(items, list):
            raise LiveRestFlatError("bybit_page_malformed")
        for row in items:
            if not isinstance(row, Mapping):
                raise LiveRestFlatError("bybit_row_malformed")
            if path == _BYBIT_OPEN:
                identity = row.get("orderId")
            else:
                identity = f"{row.get('symbol')}:{row.get('positionIdx')}"
            if not isinstance(identity, str) or not identity or identity in seen_rows:
                raise LiveRestFlatError("bybit_page_overlap")
            seen_rows.add(identity)
            rows.append(row)
        if "nextPageCursor" not in result:
            raise LiveRestFlatError("bybit_cursor_missing")
        nxt = result.get("nextPageCursor")
        if nxt in (None, ""):
            return {"retCode": 0, "result": {"list": rows, "nextPageCursor": ""}}
        if not isinstance(nxt, str) or nxt in seen_cursors:
            raise LiveRestFlatError("bybit_cursor_invalid")
        seen_cursors.add(nxt)
        cursor = nxt
    raise LiveRestFlatError("bybit_page_limit")


def _rows(payload: Mapping[str, Any], *, venue: str, kind: str) -> list[Mapping[str, Any]]:
    if venue == "bybit":
        result = payload.get("result")
        if payload.get("retCode") not in (0, "0") or not isinstance(result, Mapping):
            raise LiveRestFlatError("bybit_snapshot_invalid")
        raw = result.get("list")
    else:
        if str(payload.get("code")) != "0":
            raise LiveRestFlatError("okx_snapshot_invalid")
        raw = payload.get("data")
    if not isinstance(raw, list) or not all(isinstance(row, Mapping) for row in raw):
        raise LiveRestFlatError(f"{venue}_{kind}_rows_invalid")
    return raw


def _nonzero(raw: object) -> bool:
    if not isinstance(raw, (str, int, Decimal)) or isinstance(raw, bool):
        raise LiveRestFlatError("position_quantity_invalid")
    try:
        number = Decimal(str(raw))
    except InvalidOperation as exc:
        raise LiveRestFlatError("position_quantity_invalid") from exc
    if not number.is_finite():
        raise LiveRestFlatError("position_quantity_invalid")
    return number != 0


@dataclass(frozen=True)
class CompleteLiveRestSnapshot:
    bybit_positions: Mapping[str, Any] = field(repr=False)
    bybit_open_orders: Mapping[str, Any] = field(repr=False)
    okx_positions: Mapping[str, Any] = field(repr=False)
    okx_open_orders: Mapping[str, Any] = field(repr=False)
    generations: tuple[int, int]
    elapsed_ms: int

    def assert_account_flat(self) -> None:
        bybit_pos = _rows(self.bybit_positions, venue="bybit", kind="positions")
        okx_pos = _rows(self.okx_positions, venue="okx", kind="positions")
        bybit_orders = _rows(self.bybit_open_orders, venue="bybit", kind="orders")
        okx_orders = _rows(self.okx_open_orders, venue="okx", kind="orders")
        if any(
            not isinstance(row.get("symbol"), str)
            or row.get("positionIdx") not in (0, "0")
            for row in bybit_pos
        ):
            raise LiveRestFlatError("bybit_position_mode_or_symbol_invalid")
        if any(
            not isinstance(row.get("instId"), str)
            or row.get("posSide") not in ("net", None)
            for row in okx_pos
        ):
            raise LiveRestFlatError("okx_position_mode_or_symbol_invalid")
        if any(_nonzero(row.get("size")) for row in bybit_pos):
            raise LiveRestFlatError("bybit_position_not_flat")
        if any(_nonzero(row.get("pos")) for row in okx_pos):
            raise LiveRestFlatError("okx_position_not_flat")
        if bybit_orders or okx_orders:
            raise LiveRestFlatError("open_orders_remain")


@dataclass(frozen=True)
class CompleteLiveRestReader:
    bybit_credentials: LiveCredentials = field(repr=False)
    okx_credentials: LiveCredentials = field(repr=False)
    readiness: Callable[[], ReadinessSnapshot] = field(repr=False)
    endpoints: Optional[VenueEndpoints] = field(default=None, repr=False)
    http_get_json: Optional[Callable[..., Mapping[str, Any]]] = field(default=None, repr=False)
    max_elapsed_ms: int = 10_000

    def capture(self) -> CompleteLiveRestSnapshot:
        if self.max_elapsed_ms <= 0:
            raise LiveRestFlatError("invalid_snapshot_window")
        endpoints = self.endpoints or endpoints_for_venue("live")
        live = endpoints_for_venue("live")
        if (
            endpoints.bybit_rest != live.bybit_rest
            or endpoints.okx_rest != live.okx_rest
        ):
            raise LiveRestFlatError("live_endpoints_required")
        before = _ready_generations(self.readiness())
        start = time.monotonic_ns()
        try:
            bybit_positions = _bybit_all_pages(
                credentials=self.bybit_credentials, base=endpoints.bybit_rest,
                path=_BYBIT_POS, query="category=linear&settleCoin=USDT&limit=200",
                http_get_json=self.http_get_json,
            )
            bybit_orders = _bybit_all_pages(
                credentials=self.bybit_credentials, base=endpoints.bybit_rest,
                path=_BYBIT_OPEN,
                query="category=linear&settleCoin=USDT&openOnly=0&limit=50",
                http_get_json=self.http_get_json,
            )
            okx_positions = _okx_signed_get(
                credentials=self.okx_credentials, base=endpoints.okx_rest,
                path_with_query=f"{_OKX_POS}?instType=SWAP",
                http_get_json=self.http_get_json,
            )
            okx_orders = _okx_complete_pending_orders(
                credentials=self.okx_credentials, base=endpoints.okx_rest,
                http_get_json=self.http_get_json,
            )
        except (SignedQuantityProbeError, BaselineError, OSError, TimeoutError, TypeError, ValueError) as exc:
            raise LiveRestFlatError("signed_snapshot_failed") from exc
        elapsed_ms = (time.monotonic_ns() - start) // 1_000_000
        if elapsed_ms > self.max_elapsed_ms or _ready_generations(self.readiness()) != before:
            raise LiveRestFlatError("snapshot_window_or_generation_changed")
        return CompleteLiveRestSnapshot(
            bybit_positions=bybit_positions,
            bybit_open_orders=bybit_orders,
            okx_positions=okx_positions,
            okx_open_orders=okx_orders,
            generations=before,
            elapsed_ms=elapsed_ms,
        )
