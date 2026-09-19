"""Bybit linear USDT ∩ OKX USDT SWAP (notebook logic, not notebook cells)."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.request import Request

from app.utils.universe_delta import (
    apply_hard_cap,
    assert_delta_path_safe,
    csv_base_coins,
    diff_intersection_against_csv,
    write_delta_atomic,
)
from research.is_crypto import is_crypto

logger = logging.getLogger("discovery")

BYBIT_BASE = "https://api.bybit.com"
BYBIT_INSTRUMENTS = "/v5/market/instruments-info"
OKX_BASE = "https://www.okx.com"
OKX_INSTRUMENTS = "/api/v5/public/instruments"
USER_AGENT = "spread-collector-discovery/0"
HTTP_TIMEOUT_SEC = 20.0
BYBIT_PAGE_PAUSE_SEC = 0.05
DEFAULT_MAX_NEW = 8


class DiscoveryError(RuntimeError):
    """REST or intersection failure. Fail loud; do not write a partial universe."""


def _http_get_json(url: str, params: Mapping[str, str], *, timeout: float = HTTP_TIMEOUT_SEC) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    full = f"{url}?{query}" if query else url
    req = Request(
        full,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise DiscoveryError(f"HTTP {exc.code} for {full}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DiscoveryError(f"network error for {full}: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise DiscoveryError(f"non-JSON response from {full}") from exc
    if not isinstance(data, dict):
        raise DiscoveryError(f"unexpected JSON type from {full}: {type(data)!r}")
    return data


def fetch_bybit_linear_instruments(
    *,
    http_get_json: Callable[[str, Mapping[str, str]], dict[str, Any]] = _http_get_json,
    page_pause_sec: float = BYBIT_PAGE_PAUSE_SEC,
) -> list[dict[str, Any]]:
    """Paginated Bybit linear instruments-info. Same endpoint as screaner.ipynb."""
    cursor: Optional[str] = None
    all_items: list[dict[str, Any]] = []
    page_num = 0
    while True:
        params: dict[str, str] = {"category": "linear", "limit": "1000"}
        if cursor:
            params["cursor"] = cursor
        data = http_get_json(BYBIT_BASE + BYBIT_INSTRUMENTS, params)
        if data.get("retCode") != 0:
            raise DiscoveryError(f"Bybit API error: {data}")
        result = data.get("result") or {}
        items = result.get("list") or []
        if not isinstance(items, list):
            raise DiscoveryError("Bybit instruments list is not a list")
        all_items.extend(items)
        cursor = result.get("nextPageCursor") or ""
        page_num += 1
        logger.info(
            "discovery_bybit_page | page=%s | items=%s | total=%s | cursor=%s",
            page_num,
            len(items),
            len(all_items),
            "yes" if cursor else "no",
        )
        if not cursor:
            break
        if page_pause_sec > 0:
            time.sleep(page_pause_sec)
    return all_items


def fetch_okx_swap_instruments(
    *,
    http_get_json: Callable[[str, Mapping[str, str]], dict[str, Any]] = _http_get_json,
) -> list[dict[str, Any]]:
    """OKX SWAP instruments. Same endpoint as screaner.ipynb."""
    data = http_get_json(OKX_BASE + OKX_INSTRUMENTS, {"instType": "SWAP"})
    if str(data.get("code")) != "0":
        raise DiscoveryError(f"OKX API error: {data}")
    items = data.get("data") or []
    if not isinstance(items, list):
        raise DiscoveryError("OKX instruments data is not a list")
    return items


def normalize_bybit_item(item: Mapping[str, Any]) -> dict[str, str]:
    price_filter = item.get("priceFilter") or {}
    lot_filter = item.get("lotSizeFilter") or {}
    base = str(item.get("baseCoin") or "").strip()
    return {
        "exchange": "bybit",
        "symbol_raw": str(item.get("symbol") or "").strip(),
        "symbol_norm": base,
        "category": str(item.get("category") or "linear").strip(),
        "contract_type": str(item.get("contractType") or "").strip(),
        "status": str(item.get("status") or "").strip(),
        "base_coin": base,
        "quote_coin": str(item.get("quoteCoin") or "").strip(),
        "settle_coin": str(item.get("settleCoin") or "").strip(),
        "tick_size": str(price_filter.get("tickSize") or "").strip(),
        "qty_step": str(lot_filter.get("qtyStep") or "").strip(),
        "min_order_qty": str(lot_filter.get("minOrderQty") or "").strip(),
        "min_notional_value": str(lot_filter.get("minNotionalValue") or "").strip(),
    }


def normalize_okx_item(item: Mapping[str, Any]) -> dict[str, str]:
    inst_id = str(item.get("instId") or "").strip()
    base = str(item.get("baseCcy") or "").strip()
    quote = str(item.get("quoteCcy") or "").strip()
    if not base and inst_id.endswith("-SWAP"):
        parts = inst_id.split("-")
        if len(parts) >= 3:
            base = parts[0]
            quote = parts[1]
    return {
        "exchange": "okx",
        "symbol_raw": inst_id,
        "symbol_norm": base,
        "inst_type": str(item.get("instType") or "").strip(),
        "state": str(item.get("state") or "").strip(),
        "base_coin": base,
        "quote_coin": quote,
        "settle_ccy": str(item.get("settleCcy") or "").strip(),
        "tick_size": str(item.get("tickSz") or "").strip(),
        "lot_size": str(item.get("lotSz") or "").strip(),
        "min_size": str(item.get("minSz") or "").strip(),
    }


def filter_okx_live_usdt_swap(rows: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    kept: list[dict[str, str]] = []
    for row in rows:
        if str(row.get("inst_type", "")) != "SWAP":
            continue
        if str(row.get("state", "")).lower() != "live":
            continue
        if str(row.get("settle_ccy", "")).upper() != "USDT":
            continue
        if not str(row.get("symbol_norm", "")).strip():
            continue
        kept.append(dict(row))
    return _dedupe_symbol_norm(kept)


def filter_bybit_live_usdt_linear(rows: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    kept: list[dict[str, str]] = []
    for row in rows:
        if str(row.get("category", "")).lower() != "linear":
            continue
        if str(row.get("status", "")).lower() != "trading":
            continue
        if str(row.get("quote_coin", "")).upper() != "USDT":
            continue
        if str(row.get("settle_coin", "")).upper() != "USDT":
            continue
        if not str(row.get("symbol_norm", "")).strip():
            continue
        kept.append(dict(row))
    return _dedupe_symbol_norm(kept)


def _dedupe_symbol_norm(rows: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    ordered = sorted(
        (dict(row) for row in rows),
        key=lambda r: (r.get("symbol_norm", ""), r.get("symbol_raw", "")),
    )
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for row in ordered:
        key = row.get("symbol_norm", "")
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def build_intersection_rows(
    okx_rows: Sequence[Mapping[str, str]],
    bybit_rows: Sequence[Mapping[str, str]],
    *,
    discovered_at_utc: Optional[str] = None,
) -> list[dict[str, str]]:
    """Inner join on symbol_norm. Notebook compact universe columns + discovered_at."""
    stamp = discovered_at_utc or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    okx_by_norm = {row["symbol_norm"]: row for row in okx_rows}
    bybit_by_norm = {row["symbol_norm"]: row for row in bybit_rows}
    shared = sorted(set(okx_by_norm) & set(bybit_by_norm))
    out: list[dict[str, str]] = []
    for norm in shared:
        okx = okx_by_norm[norm]
        bybit = bybit_by_norm[norm]
        out.append(
            {
                "base_coin": norm,
                "okx_symbol": str(okx.get("symbol_raw", "")).strip(),
                "bybit_symbol": str(bybit.get("symbol_raw", "")).strip(),
                "okx_tick_size": str(okx.get("tick_size", "")).strip(),
                "okx_lot_size": str(okx.get("lot_size", "")).strip(),
                "okx_min_size": str(okx.get("min_size", "")).strip(),
                "bybit_tick_size": str(bybit.get("tick_size", "")).strip(),
                "bybit_qty_step": str(bybit.get("qty_step", "")).strip(),
                "bybit_min_order_qty": str(bybit.get("min_order_qty", "")).strip(),
                "bybit_min_notional_value": str(bybit.get("min_notional_value", "")).strip(),
                "discovered_at_utc": stamp,
            }
        )
    return out


def run_discovery(
    *,
    universe_path: Path,
    delta_path: Path,
    max_new: int = DEFAULT_MAX_NEW,
    http_get_json: Callable[[str, Mapping[str, str]], dict[str, Any]] = _http_get_json,
) -> dict[str, Any]:
    """Fetch REST intersection, diff the given CSV, atomically replace delta.

    Never writes the universe CSV. Hard-caps the delta. Fail loud on REST errors.
    """
    if max_new < 0:
        raise DiscoveryError(f"max_new must be >= 0, got {max_new}")
    assert_delta_path_safe(delta_path, universe_path)
    if not universe_path.exists():
        raise DiscoveryError(f"universe CSV not found: {universe_path}")

    logger.info(
        "discovery_start | universe=%s | delta=%s | max_new=%s",
        universe_path,
        delta_path,
        max_new,
    )
    bybit_raw = fetch_bybit_linear_instruments(http_get_json=http_get_json)
    okx_raw = fetch_okx_swap_instruments(http_get_json=http_get_json)
    bybit_norm = [normalize_bybit_item(item) for item in bybit_raw]
    okx_norm = [normalize_okx_item(item) for item in okx_raw]
    bybit_f = filter_bybit_live_usdt_linear(bybit_norm)
    okx_f = filter_okx_live_usdt_swap(okx_norm)
    intersection = build_intersection_rows(okx_f, bybit_f)
    crypto_intersection: list[dict[str, str]] = []
    skipped_non_crypto: list[str] = []
    for row in intersection:
        coin = str(row.get("base_coin", "")).strip()
        if not coin:
            continue
        if not is_crypto(coin):
            skipped_non_crypto.append(coin)
            continue
        crypto_intersection.append(row)
    csv_coins = csv_base_coins(universe_path)
    fresh = diff_intersection_against_csv(crypto_intersection, csv_coins)
    kept, dropped = apply_hard_cap(fresh, max_new)
    write_delta_atomic(delta_path, kept, universe_path=universe_path)
    summary = {
        "universe_path": str(universe_path),
        "delta_path": str(delta_path),
        "bybit_raw": len(bybit_raw),
        "okx_raw": len(okx_raw),
        "bybit_filtered": len(bybit_f),
        "okx_filtered": len(okx_f),
        "intersection": len(intersection),
        "intersection_crypto": len(crypto_intersection),
        "skipped_non_crypto": len(skipped_non_crypto),
        "csv_coins": len(csv_coins),
        "new_before_cap": len(fresh),
        "delta_rows": len(kept),
        "dropped_by_cap": dropped,
        "max_new": max_new,
        "coins": [row["base_coin"] for row in kept],
    }
    logger.info(
        "discovery_done | bybit_raw=%s | okx_raw=%s | bybit_filtered=%s | "
        "okx_filtered=%s | intersection=%s | intersection_crypto=%s | "
        "skipped_non_crypto=%s | csv_coins=%s | new_before_cap=%s | "
        "delta_rows=%s | dropped_by_cap=%s | max_new=%s | coins=%s",
        summary["bybit_raw"],
        summary["okx_raw"],
        summary["bybit_filtered"],
        summary["okx_filtered"],
        summary["intersection"],
        summary["intersection_crypto"],
        summary["skipped_non_crypto"],
        summary["csv_coins"],
        summary["new_before_cap"],
        summary["delta_rows"],
        summary["dropped_by_cap"],
        summary["max_new"],
        ",".join(summary["coins"]) or "-",
    )
    return summary
