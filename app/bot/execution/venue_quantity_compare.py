"""Pure two-venue native-quantity comparison for an EV2 lifecycle candidate.

Inputs must later be obtained by signed, fresh, complete REST reads under the
owned account. A match here is not permission to publish K=1 or send orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from app.bot.execution.contracts import Venue
from app.bot.execution.durable_projection import DurableManagerCandidate


class QuantityCompareError(ValueError):
    """Malformed, incomplete, or unsupported venue quantity snapshot."""


@dataclass(frozen=True)
class QuantityComparison:
    matched: bool
    reason: str
    bybit_quantity: Decimal
    okx_quantity: Decimal
    requires_signed_fresh_source: bool = True


def _quantity(raw: object, *, signed: bool) -> Decimal:
    if raw is None or isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise QuantityCompareError("invalid_quantity")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise QuantityCompareError("invalid_quantity") from exc
    if not value.is_finite() or (not signed and value < 0):
        raise QuantityCompareError("invalid_quantity")
    return value


def _rows(data: Mapping[str, Any], *, venue: Venue) -> list[Mapping[str, Any]]:
    if not isinstance(data, Mapping):
        raise QuantityCompareError("invalid_response")
    if venue is Venue.BYBIT:
        code = data.get("retCode")
        if isinstance(code, bool) or code not in (0, "0"):
            raise QuantityCompareError("venue_response_rejected")
        result = data.get("result")
        if not isinstance(result, Mapping):
            raise QuantityCompareError("invalid_response")
        cursor = result.get("nextPageCursor")
        if not isinstance(cursor, str):
            raise QuantityCompareError("bybit_pagination_unknown")
        if cursor:
            raise QuantityCompareError("bybit_pagination_incomplete")
        items = result.get("list")
    else:
        if str(data.get("code")) != "0":
            raise QuantityCompareError("venue_response_rejected")
        items = data.get("data")
    if not isinstance(items, list) or any(not isinstance(item, Mapping) for item in items):
        raise QuantityCompareError("invalid_response")
    return items


def _bybit_position(
    rows: Sequence[Mapping[str, Any]], *, pool: set[str], expected: str,
) -> tuple[str | None, Decimal]:
    found: tuple[str | None, Decimal] | None = None
    for row in rows:
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise QuantityCompareError("position_symbol_missing")
        if symbol not in pool:
            continue
        qty = _quantity(row.get("size"), signed=False)
        position_idx = row.get("positionIdx")
        if isinstance(position_idx, bool) or position_idx != 0:
            raise QuantityCompareError("bybit_position_mode_unsupported")
        if qty == 0:
            continue
        if symbol != expected:
            raise QuantityCompareError("extra_pool_position")
        side = str(row.get("side") or "").lower()
        if side not in {"buy", "sell"} or found is not None:
            raise QuantityCompareError("bybit_position_ambiguous")
        found = (side, qty)
    return found or (None, Decimal("0"))


def _okx_position(
    rows: Sequence[Mapping[str, Any]], *, pool: set[str], expected: str,
) -> tuple[str | None, Decimal]:
    found: tuple[str | None, Decimal] | None = None
    for row in rows:
        symbol = row.get("instId")
        if not isinstance(symbol, str) or not symbol:
            raise QuantityCompareError("position_symbol_missing")
        if symbol not in pool:
            continue
        qty = _quantity(row.get("pos"), signed=True)
        if str(row.get("posSide") or "").lower() != "net":
            raise QuantityCompareError("okx_position_mode_unsupported")
        if qty == 0:
            continue
        if symbol != expected:
            raise QuantityCompareError("extra_pool_position")
        if found is not None:
            raise QuantityCompareError("okx_position_ambiguous")
        found = ("buy" if qty > 0 else "sell", abs(qty))
    return found or (None, Decimal("0"))


def _no_pool_orders(
    rows: Sequence[Mapping[str, Any]], *, venue: Venue, pool: set[str],
) -> bool:
    field = "symbol" if venue is Venue.BYBIT else "instId"
    for row in rows:
        symbol = row.get(field)
        if not isinstance(symbol, str) or not symbol:
            raise QuantityCompareError("order_symbol_missing")
        if symbol in pool:
            return False
    return True


def compare_candidate_quantities(
    *,
    candidate: DurableManagerCandidate,
    instruments: Mapping[Venue, str],
    pool_symbols: Mapping[Venue, Sequence[str]],
    bybit_positions: Mapping[str, Any],
    okx_positions: Mapping[str, Any],
    bybit_open_orders: Mapping[str, Any],
    okx_open_orders: Mapping[str, Any],
) -> QuantityComparison:
    """Compare exact native venue quantities; no cross-venue unit conversion.

    ``instruments`` must later be bound to the durable order plan, and both
    account snapshots must be signed, fresh and complete before use in a
    lifecycle commit. This pure function intentionally cannot grant that.
    """

    if not isinstance(candidate, DurableManagerCandidate):
        raise QuantityCompareError("invalid_candidate")
    if candidate.venue_reconciliation_required is not True:
        raise QuantityCompareError("reconciliation_requirement_missing")
    if candidate.projection.publication not in {"open", "close"}:
        raise QuantityCompareError("invalid_publication")
    if set(instruments) != {Venue.BYBIT, Venue.OKX} or set(pool_symbols) != {
        Venue.BYBIT, Venue.OKX
    }:
        raise QuantityCompareError("two_venue_symbols_required")
    if any(
        not isinstance(instruments[venue], str) or not instruments[venue]
        or not isinstance(pool_symbols[venue], (tuple, list))
        or any(not isinstance(item, str) or not item for item in pool_symbols[venue])
        for venue in (Venue.BYBIT, Venue.OKX)
    ):
        raise QuantityCompareError("invalid_instrument_pool")
    pools = {venue: set(pool_symbols[venue]) for venue in (Venue.BYBIT, Venue.OKX)}
    if any(not pools[venue] or instruments[venue] not in pools[venue] for venue in pools):
        raise QuantityCompareError("expected_instrument_outside_pool")
    legs = {leg.venue: leg for leg in candidate.projection.legs}
    if set(legs) != {Venue.BYBIT, Venue.OKX}:
        raise QuantityCompareError("two_venue_legs_required")
    if candidate.projection.publication == "open" and any(
        leg.effective_open_quantity <= 0 for leg in legs.values()
    ):
        raise QuantityCompareError("open_quantity_not_proven")
    if candidate.projection.publication == "close" and any(
        leg.effective_open_quantity != 0 for leg in legs.values()
    ):
        raise QuantityCompareError("close_quantity_not_flat")

    bybit_side, bybit_qty = _bybit_position(
        _rows(bybit_positions, venue=Venue.BYBIT),
        pool=pools[Venue.BYBIT], expected=instruments[Venue.BYBIT],
    )
    okx_side, okx_qty = _okx_position(
        _rows(okx_positions, venue=Venue.OKX),
        pool=pools[Venue.OKX], expected=instruments[Venue.OKX],
    )
    bybit_orders_flat = _no_pool_orders(
        _rows(bybit_open_orders, venue=Venue.BYBIT),
        venue=Venue.BYBIT, pool=pools[Venue.BYBIT],
    )
    okx_orders_flat = _no_pool_orders(
        _rows(okx_open_orders, venue=Venue.OKX),
        venue=Venue.OKX, pool=pools[Venue.OKX],
    )
    if not bybit_orders_flat or not okx_orders_flat:
        return QuantityComparison(False, "pool_open_orders_present", bybit_qty, okx_qty)

    if candidate.projection.publication == "close":
        matched = bybit_qty == 0 and okx_qty == 0
        return QuantityComparison(
            matched, "quantity_matched" if matched else "not_flat",
            bybit_qty, okx_qty,
        )

    side = candidate.projection.side
    if side not in {"long", "short"}:
        raise QuantityCompareError("invalid_spread_side")
    expected_bybit_side = "sell" if side == "long" else "buy"
    expected_okx_side = "buy" if side == "long" else "sell"
    matched = (
        bybit_side == expected_bybit_side
        and okx_side == expected_okx_side
        and bybit_qty == legs[Venue.BYBIT].effective_open_quantity
        and okx_qty == legs[Venue.OKX].effective_open_quantity
    )
    return QuantityComparison(
        matched, "quantity_matched" if matched else "quantity_or_side_mismatch",
        bybit_qty, okx_qty,
    )
