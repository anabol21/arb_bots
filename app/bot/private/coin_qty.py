"""Shared base-coin qty near 10 USD for both legs.

OKX ``sz = coin_qty / ctVal`` snapped with the same ROUND_UP step helper as
``order_plan._quantize_qty``. Bybit ``qty`` is that coin amount snapped to
``qtyStep``. Unequal post-snap coin amounts abort ``qty_mismatch``. A minimum
legal notional already above 15 USD aborts ``min_notional_above_band``.
A missing ``ctVal`` aborts ``qty_mismatch``. It is never defaulted to 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_UP
from math import gcd
from typing import Optional

from app.bot.private.order_metadata import InstrumentMetadata, parse_decimal
from app.bot.private.order_plan import OrderPlanError, _quantize_qty

TARGET_NOTIONAL_USD = Decimal("10")
MIN_NOTIONAL_USD = Decimal("7")
NOTIONAL_BAND_USD = Decimal("15")
_MISSING = object()


class CoinQtyError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SharedCoinQty:
    coin_qty: Decimal
    okx_sz: Decimal
    bybit_qty: Decimal
    okx_notional: Decimal
    bybit_notional: Decimal
    okx_px: Decimal
    bybit_px: Decimal


def dec_str(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _snap_meta(*, symbol: str, step: Decimal, min_qty: Decimal) -> InstrumentMetadata:
    venue = "okx_live" if symbol.startswith("OKX") else "bybit_live"
    return InstrumentMetadata(
        venue=venue,
        symbol=symbol,
        min_qty=min_qty,
        qty_step=step,
        tick_size=Decimal("0.00000001"),
        contract_multiplier=Decimal("1"),
        contract_value_ccy="USDT",
        notional_unit="usdt_per_coin",
        mark_price_usdt=Decimal("1"),
        mark_asof_monotonic_ns=0,
        mark_max_age_ns=10**18,
    )


def _snap(qty: Decimal, *, symbol: str, step: Decimal, min_qty: Decimal) -> Decimal:
    if step <= 0 or min_qty <= 0 or qty <= 0:
        raise CoinQtyError("qty_mismatch")
    try:
        return _quantize_qty(qty, _snap_meta(symbol=symbol, step=step, min_qty=min_qty))
    except OrderPlanError as exc:
        raise CoinQtyError("qty_mismatch") from exc


def _lcm_decimal(a: Decimal, b: Decimal) -> Decimal:
    if a <= 0 or b <= 0:
        raise CoinQtyError("qty_mismatch")
    exp = max(-a.as_tuple().exponent, -b.as_tuple().exponent, 0)
    scale = Decimal(10) ** exp
    ai = int((a * scale).to_integral_value(rounding=ROUND_UP))
    bi = int((b * scale).to_integral_value(rounding=ROUND_UP))
    if ai <= 0 or bi <= 0:
        raise CoinQtyError("qty_mismatch")
    return Decimal(ai // gcd(ai, bi) * bi) / scale


def _ceil_to_step(qty: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise CoinQtyError("qty_mismatch")
    if qty <= 0:
        return step
    steps = (qty / step).to_integral_value(rounding=ROUND_UP)
    out = steps * step
    if out < qty:
        out += step
    return out


def _as_decimal(raw: object, *, field: str) -> Decimal:
    try:
        return parse_decimal(raw if isinstance(raw, Decimal) else str(raw), field=field)
    except Exception as exc:  # noqa: BLE001 — metadata boundary
        raise CoinQtyError("qty_mismatch") from exc


def reference_px(book: object, leg_side: str) -> Decimal:
    """Ask for a buy leg, bid for a sell leg."""
    if not isinstance(book, dict):
        raise CoinQtyError("qty_mismatch")
    key = "ask_price" if str(leg_side).strip().lower() == "buy" else "bid_price"
    return _as_decimal(book.get(key), field=key)


def shared_coin_qty(
    *,
    okx_px: Decimal,
    bybit_px: Decimal,
    ct_val: Optional[Decimal],
    okx_lot_sz: Decimal,
    okx_min_sz: Decimal,
    bybit_qty_step: Decimal,
    bybit_min_qty: Decimal,
    okx_min_notional: Optional[Decimal] = None,
    bybit_min_notional: Optional[Decimal] = None,
) -> SharedCoinQty:
    """Legal shared coin amount whose leg notionals sit closest to 10 USD."""
    if ct_val is None or ct_val <= 0:
        raise CoinQtyError("qty_mismatch")
    if okx_px <= 0 or bybit_px <= 0:
        raise CoinQtyError("qty_mismatch")
    if (
        okx_min_notional is not None
        and okx_min_notional > NOTIONAL_BAND_USD
    ) or (
        bybit_min_notional is not None
        and bybit_min_notional > NOTIONAL_BAND_USD
    ):
        raise CoinQtyError("min_notional_above_band")

    okx_coin_step = okx_lot_sz * ct_val
    common = _lcm_decimal(okx_coin_step, bybit_qty_step)
    okx_min_coin = _snap(
        okx_min_sz, symbol="OKX", step=okx_lot_sz, min_qty=okx_min_sz
    ) * ct_val
    bybit_min_coin = _snap(
        bybit_min_qty,
        symbol="BYBIT",
        step=bybit_qty_step,
        min_qty=bybit_min_qty,
    )
    start = _ceil_to_step(max(okx_min_coin, bybit_min_coin), common)

    best: Optional[tuple[Decimal, SharedCoinQty]] = None
    smallest: Optional[SharedCoinQty] = None
    coin = start
    for _ in range(200000):
        okx_sz = _snap(coin / ct_val, symbol="OKX", step=okx_lot_sz, min_qty=okx_min_sz)
        bybit_qty = _snap(
            coin, symbol="BYBIT", step=bybit_qty_step, min_qty=bybit_min_qty
        )
        okx_coin = okx_sz * ct_val
        if okx_coin != bybit_qty:
            coin = coin + common
            if coin * min(okx_px, bybit_px) > Decimal("10000"):
                break
            continue
        n_okx = okx_coin * okx_px
        n_by = bybit_qty * bybit_px
        if okx_min_notional is not None and n_okx < okx_min_notional:
            coin = coin + common
            continue
        if bybit_min_notional is not None and n_by < bybit_min_notional:
            coin = coin + common
            continue
        rec = SharedCoinQty(
            coin_qty=okx_coin,
            okx_sz=okx_sz,
            bybit_qty=bybit_qty,
            okx_notional=n_okx,
            bybit_notional=n_by,
            okx_px=okx_px,
            bybit_px=bybit_px,
        )
        if smallest is None:
            smallest = rec
        dev = abs(n_okx - TARGET_NOTIONAL_USD) + abs(n_by - TARGET_NOTIONAL_USD)
        if best is None or dev < best[0] or (
            dev == best[0] and rec.coin_qty < best[1].coin_qty
        ):
            best = (dev, rec)
        if n_okx > TARGET_NOTIONAL_USD and n_by > TARGET_NOTIONAL_USD:
            break
        coin = coin + common
        if coin * min(okx_px, bybit_px) > Decimal("10000"):
            break

    if smallest is None or best is None:
        raise CoinQtyError("qty_mismatch")
    if (
        max(smallest.okx_notional, smallest.bybit_notional) > NOTIONAL_BAND_USD
    ):
        raise CoinQtyError("min_notional_above_band")
    chosen = best[1]
    if (
        min(chosen.okx_notional, chosen.bybit_notional) < MIN_NOTIONAL_USD
        or max(chosen.okx_notional, chosen.bybit_notional) > NOTIONAL_BAND_USD
    ):
        raise CoinQtyError("notional_outside_band")
    # Safety: the legs we would send must still be the same coin amount.
    okx_sz2 = _snap(
        chosen.coin_qty / ct_val, symbol="OKX", step=okx_lot_sz, min_qty=okx_min_sz
    )
    bybit2 = _snap(
        chosen.coin_qty,
        symbol="BYBIT",
        step=bybit_qty_step,
        min_qty=bybit_min_qty,
    )
    if okx_sz2 * ct_val != bybit2:
        raise CoinQtyError("qty_mismatch")
    return chosen


def shared_from_meta(
    *,
    meta: object,
    okx_px: Decimal,
    bybit_px: Decimal,
) -> SharedCoinQty:
    """Read lot/step/ctVal off the meta object the manager passes in."""
    ct_raw = getattr(meta, "okx_ct_val", _MISSING)
    if ct_raw is _MISSING:
        ct_raw = getattr(meta, "ct_val", _MISSING)
    # Never assume ctVal=1. A missing value is the XRP bug: coin count sent as sz.
    if ct_raw is _MISSING or ct_raw is None or ct_raw == "":
        raise CoinQtyError("qty_mismatch")
    ct_val = _as_decimal(ct_raw, field="ct_val")

    def _opt(name: str) -> Optional[Decimal]:
        raw = getattr(meta, name, None)
        if raw is None or raw == "":
            return None
        return _as_decimal(raw, field=name)

    lot = _as_decimal(getattr(meta, "okx_lot_size", None), field="okx_lot_size")
    min_sz = _as_decimal(
        getattr(meta, "okx_min_size", None) or lot, field="okx_min_size"
    )
    step = _as_decimal(getattr(meta, "bybit_qty_step", None), field="bybit_qty_step")
    min_qty = _as_decimal(
        getattr(meta, "bybit_min_order_qty", None) or step,
        field="bybit_min_order_qty",
    )
    return shared_coin_qty(
        okx_px=okx_px,
        bybit_px=bybit_px,
        ct_val=ct_val,
        okx_lot_sz=lot,
        okx_min_sz=min_sz,
        bybit_qty_step=step,
        bybit_min_qty=min_qty,
        okx_min_notional=_opt("okx_min_notional"),
        bybit_min_notional=_opt("bybit_min_notional_value"),
    )


def exact_close_from_meta(
    *,
    meta: object,
    okx_px: Decimal,
    bybit_px: Decimal,
    okx_sz: object,
    bybit_qty: object,
) -> SharedCoinQty:
    """Validate the original filled leg quantities for a reduce-only close.

    Close quantity is carried from terminal open fills. It is never rounded or
    resized to a current-price notional target; current lot, step, and ctVal
    metadata must still match the saved quantities exactly.
    """
    ct_raw = getattr(meta, "okx_ct_val", _MISSING)
    if ct_raw is _MISSING:
        ct_raw = getattr(meta, "ct_val", _MISSING)
    if ct_raw is _MISSING or ct_raw is None or ct_raw == "":
        raise CoinQtyError("qty_mismatch")
    ct_val = _as_decimal(ct_raw, field="ct_val")
    lot = _as_decimal(getattr(meta, "okx_lot_size", None), field="okx_lot_size")
    min_sz = _as_decimal(
        getattr(meta, "okx_min_size", None) or lot, field="okx_min_size"
    )
    step = _as_decimal(getattr(meta, "bybit_qty_step", None), field="bybit_qty_step")
    min_qty = _as_decimal(
        getattr(meta, "bybit_min_order_qty", None) or step,
        field="bybit_min_order_qty",
    )
    saved_okx_sz = _as_decimal(okx_sz, field="okx_filled_qty")
    saved_bybit_qty = _as_decimal(bybit_qty, field="bybit_filled_qty")
    if (
        _snap(saved_okx_sz, symbol="OKX", step=lot, min_qty=min_sz) != saved_okx_sz
        or _snap(saved_bybit_qty, symbol="BYBIT", step=step, min_qty=min_qty)
        != saved_bybit_qty
        or saved_okx_sz * ct_val != saved_bybit_qty
        or okx_px <= 0
        or bybit_px <= 0
    ):
        raise CoinQtyError("qty_mismatch")
    coin_qty = saved_okx_sz * ct_val
    return SharedCoinQty(
        coin_qty=coin_qty,
        okx_sz=saved_okx_sz,
        bybit_qty=saved_bybit_qty,
        okx_notional=coin_qty * okx_px,
        bybit_notional=coin_qty * bybit_px,
        okx_px=okx_px,
        bybit_px=bybit_px,
    )
