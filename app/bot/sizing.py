"""Dual-leg lot sizer for live-size work (pure function, no I/O, no send).

One coin quantity, two venue lot counts
--------------------------------------
Do not size each venue independently from ``target_usdt``. Convert USD → a
**single** coin quantity using the **worse ask** (``max`` of the two venue
prices; when L1 books are present, ``max(okx_ask, bybit_ask)``). Snap that
quantity **up** to the LCM of ``okx_lot_size`` and ``bybit_qty_step`` (both
in coin units in ``bybit_okx_universe.csv``). The same ``coin_qty`` is then
expressed as OKX and Bybit order qtys that represent the same number of coins.

OKX contract vs coin
--------------------
Universe lots are already in coins. For SOL/XRP linear USDT perps, live
``ctVal`` is 1 ⇒ 1 contract ≈ 1 coin, so ``okx_qty == bybit_qty == coin_qty``.
If ``okx_ct_val`` (or meta ``contract_multiplier``) is not 1, OKX qty is
``coin_qty / ctVal`` contracts. Do not assume 1:1 for every coin.

L1 depth gate
-------------
Before a market send, each venue's **execution** L1 size must be ≥ ``coin_qty``
(buy → ask size, sell → bid size). Missing size or a thinner book is
fail-closed (do not send). Only top-of-book is used (OKX books5 / Bybit ob1
as available); deeper levels are not summed.

Band $10–20 and cap <100 apply to **actual** notionals of both legs after the
shared snap. Contour for the ladder is SOL and XRP; BTC/ETH are not subscribed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_UP
from typing import Any, Mapping, Optional, Sequence, Union

# Live-size / dual-leg product contour. Not a signal-only leftover of BTC/ETH.
LIVE_SIZE_COINS: tuple[str, ...] = ("SOL", "XRP")

DEFAULT_TARGET_USDT = Decimal("10")
DEFAULT_BAND = (Decimal("10"), Decimal("20"))
DEFAULT_CAP_USDT = Decimal("100")

# Conservative USD→coin reference: higher of the two venue prices (asks).
REF_MODE_WORSE_ASK = "worse_ask"

Number = Union[int, float, Decimal, str]


class SizingError(ValueError):
    """Invalid sizer inputs (lots, prices, band, cap)."""


@dataclass(frozen=True)
class DualLegQtyPlan:
    """Result of ``plan_dual_leg_qty``. Always populated; check ``feasible``."""

    base_coin: str
    coin_qty: Decimal
    qty_okx: Decimal
    qty_bybit: Decimal
    okx_px: Decimal
    bybit_px: Decimal
    notional_okx: Decimal
    notional_bybit: Decimal
    target_usdt: Decimal
    band_low: Decimal
    band_high: Decimal
    cap_usdt: Decimal
    feasible: bool
    reason: Optional[str]
    ref_px: Decimal = Decimal("0")
    ref_mode: str = REF_MODE_WORSE_ASK
    lot_lcm: Decimal = Decimal("0")
    okx_ct_val: Decimal = Decimal("1")
    okx_bid_size: Optional[Decimal] = None
    okx_ask_size: Optional[Decimal] = None
    bybit_bid_size: Optional[Decimal] = None
    bybit_ask_size: Optional[Decimal] = None
    okx_exec_size: Optional[Decimal] = None
    bybit_exec_size: Optional[Decimal] = None
    depth_ok: Optional[bool] = None
    okx_side: Optional[str] = None
    bybit_side: Optional[str] = None

    @property
    def okx_qty(self) -> Decimal:
        return self.qty_okx

    @property
    def bybit_qty(self) -> Decimal:
        return self.qty_bybit

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "base_coin": self.base_coin,
            "coin_qty": _fmt(self.coin_qty),
            "okx_qty": _fmt(self.qty_okx),
            "bybit_qty": _fmt(self.qty_bybit),
            "qty_okx": _fmt(self.qty_okx),
            "qty_bybit": _fmt(self.qty_bybit),
            "okx_px": _fmt(self.okx_px),
            "bybit_px": _fmt(self.bybit_px),
            "ref_px": _fmt(self.ref_px),
            "ref_mode": self.ref_mode,
            "lot_lcm": _fmt(self.lot_lcm),
            "okx_ct_val": _fmt(self.okx_ct_val),
            "notional_okx": _fmt(self.notional_okx),
            "notional_bybit": _fmt(self.notional_bybit),
            "target_usdt": _fmt(self.target_usdt),
            "band": [_fmt(self.band_low), _fmt(self.band_high)],
            "cap_usdt": _fmt(self.cap_usdt),
            "okx_bid_size": _fmt_opt(self.okx_bid_size),
            "okx_ask_size": _fmt_opt(self.okx_ask_size),
            "bybit_bid_size": _fmt_opt(self.bybit_bid_size),
            "bybit_ask_size": _fmt_opt(self.bybit_ask_size),
            "okx_exec_size": _fmt_opt(self.okx_exec_size),
            "bybit_exec_size": _fmt_opt(self.bybit_exec_size),
            "depth_ok": self.depth_ok,
            "okx_side": self.okx_side,
            "bybit_side": self.bybit_side,
            "feasible": self.feasible,
            "reason": self.reason,
        }


def _fmt(value: Decimal) -> str:
    return format(value, "f")


def _fmt_opt(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    return format(value, "f")


def _dec(raw: Number, *, field: str) -> Decimal:
    try:
        val = raw if isinstance(raw, Decimal) else Decimal(str(raw))
    except Exception as exc:  # noqa: BLE001 — Decimal constructor is broad
        raise SizingError(f"invalid decimal for {field}") from exc
    if val.is_nan() or val.is_infinite():
        raise SizingError(f"invalid decimal for {field}")
    return val


def ceil_to_lot(raw_qty: Number, lot: Number) -> Decimal:
    """Ceil ``raw_qty`` up to a positive lot step (never floor)."""
    qty = _dec(raw_qty, field="qty")
    step = _dec(lot, field="lot")
    if step <= 0:
        raise SizingError("lot must be > 0")
    if qty <= 0:
        return Decimal("0")
    steps = (qty / step).to_integral_value(rounding=ROUND_UP)
    return steps * step


def gcd_lot(a: Number, b: Number) -> Decimal:
    """Greatest common divisor of two positive lot steps (coin units)."""
    x = _dec(a, field="lot_a")
    y = _dec(b, field="lot_b")
    if x <= 0 or y <= 0:
        raise SizingError("lot sizes must be > 0")
    x, y = abs(x), abs(y)
    while y > 0:
        x, y = y, x % y
    if x <= 0:
        raise SizingError("gcd of lots is not positive")
    return x


def lcm_lot(a: Number, b: Number) -> Decimal:
    """Least common multiple of two positive lot steps (coin units).

    ``lcm(okx_lot, bybit_step)`` is the smallest coin qty that is an integer
    multiple of both venue lots.
    """
    x = _dec(a, field="lot_a")
    y = _dec(b, field="lot_b")
    if x <= 0 or y <= 0:
        raise SizingError("lot sizes must be > 0")
    return (x / gcd_lot(x, y)) * y


def _attr(meta: Any, name: str) -> Any:
    if isinstance(meta, Mapping):
        if name not in meta:
            raise SizingError(f"meta missing {name}")
        return meta[name]
    if not hasattr(meta, name):
        raise SizingError(f"meta missing {name}")
    return getattr(meta, name)


def _optional_attr(meta: Any, name: str) -> Any:
    if isinstance(meta, Mapping):
        return meta.get(name)
    return getattr(meta, name, None)


def is_live_size_coin(coin: str) -> bool:
    return str(coin).strip().upper() in LIVE_SIZE_COINS


def _normalize_side(side: Optional[str], *, field: str) -> Optional[str]:
    if side is None:
        return None
    out = str(side).strip().lower()
    if out not in {"buy", "sell"}:
        raise SizingError(f"{field} must be buy or sell")
    return out


def _book_size(book: Optional[Mapping[str, Any]], key: str) -> Optional[Decimal]:
    if book is None:
        return None
    raw = book.get(key)
    if raw is None or raw == "":
        return None
    val = _dec(raw, field=key)
    if val < 0:
        raise SizingError(f"{key} must be >= 0")
    return val


def execution_l1_size(
    book: Optional[Mapping[str, Any]],
    side: Optional[str],
) -> Optional[Decimal]:
    """Top-of-book size on the market-execution side. None if unavailable.

    Limitation: only L1 (OKX books5 / Bybit ob1 top row). Deeper size is ignored.
    """
    if book is None or side is None:
        return None
    key = "ask_size" if side == "buy" else "bid_size"
    return _book_size(book, key)


def resolve_okx_ct_val(
    meta: Any,
    okx_ct_val: Optional[Number] = None,
) -> Decimal:
    """Coins per OKX contract. Default 1 (universe lots already in coins)."""
    if okx_ct_val is not None:
        val = _dec(okx_ct_val, field="okx_ct_val")
        if val <= 0:
            raise SizingError("okx_ct_val must be > 0")
        return val
    raw = _optional_attr(meta, "okx_ct_val")
    if raw is None:
        raw = _optional_attr(meta, "contract_multiplier")
    if raw is None:
        return Decimal("1")
    val = _dec(raw, field="okx_ct_val")
    if val <= 0:
        raise SizingError("okx_ct_val must be > 0")
    return val


def _venue_qtys_from_coin(
    coin_qty: Decimal,
    *,
    okx_lot: Decimal,
    bybit_lot: Decimal,
    okx_ct_val: Decimal,
) -> tuple[Decimal, Decimal]:
    """Same coins → OKX qty (contracts if ctVal≠1) and Bybit qty (coins)."""
    if coin_qty <= 0:
        return Decimal("0"), Decimal("0")
    bybit_qty = coin_qty
    if bybit_qty % bybit_lot != 0:
        raise SizingError("coin_qty is not an integer multiple of bybit lot")
    okx_coin_lot = okx_lot * okx_ct_val
    if coin_qty % okx_coin_lot != 0:
        raise SizingError("coin_qty is not an integer multiple of OKX coin-lot")
    okx_qty = coin_qty / okx_ct_val
    if okx_qty % okx_lot != 0:
        raise SizingError("okx_qty is not an integer multiple of okx lot")
    return okx_qty, bybit_qty


def plan_dual_leg_qty(
    meta: Any,
    okx_px: Number,
    bybit_px: Number,
    target_usdt: Number = DEFAULT_TARGET_USDT,
    band: Sequence[Number] = DEFAULT_BAND,
    cap: Number = DEFAULT_CAP_USDT,
    *,
    okx_book: Optional[Mapping[str, Any]] = None,
    bybit_book: Optional[Mapping[str, Any]] = None,
    okx_side: Optional[str] = None,
    bybit_side: Optional[str] = None,
    okx_ct_val: Optional[Number] = None,
    flatten_okx_side: Optional[str] = None,
    flatten_bybit_side: Optional[str] = None,
    require_l1: bool = False,
) -> DualLegQtyPlan:
    """Shared-coin LCM snap, then band/cap and optional L1 depth gate.

    ``meta`` is universe/stub lot metadata (``InstrumentMeta`` or a mapping
    with the same field names). Qty units match that table (coin-space lots)
    unless ``okx_ct_val`` ≠ 1, in which case ``okx_qty`` is contracts.

    When ``require_l1`` is true, missing or thin L1 is fail-closed. Callers
    that will send must pass books and set ``require_l1=True``.
    """
    coin = str(_attr(meta, "base_coin")).strip().upper()
    okx_lot = _dec(_attr(meta, "okx_lot_size"), field="okx_lot_size")
    okx_min = _dec(_attr(meta, "okx_min_size"), field="okx_min_size")
    bybit_lot = _dec(_attr(meta, "bybit_qty_step"), field="bybit_qty_step")
    bybit_min = _dec(_attr(meta, "bybit_min_order_qty"), field="bybit_min_order_qty")
    bybit_min_notional = _dec(
        _attr(meta, "bybit_min_notional_value")
        if (
            (isinstance(meta, Mapping) and "bybit_min_notional_value" in meta)
            or hasattr(meta, "bybit_min_notional_value")
        )
        else Decimal("0"),
        field="bybit_min_notional_value",
    )
    ct_val = resolve_okx_ct_val(meta, okx_ct_val)
    okx_side_n = _normalize_side(okx_side, field="okx_side")
    bybit_side_n = _normalize_side(bybit_side, field="bybit_side")
    flat_okx_n = _normalize_side(flatten_okx_side, field="flatten_okx_side")
    flat_bybit_n = _normalize_side(flatten_bybit_side, field="flatten_bybit_side")

    px_okx = _dec(okx_px, field="okx_px")
    px_bybit = _dec(bybit_px, field="bybit_px")
    target = _dec(target_usdt, field="target_usdt")
    cap_usdt = _dec(cap, field="cap")
    if len(band) != 2:
        raise SizingError("band must be (low, high)")
    band_low = _dec(band[0], field="band_low")
    band_high = _dec(band[1], field="band_high")

    if okx_lot <= 0 or bybit_lot <= 0:
        raise SizingError("lot sizes must be > 0")
    if okx_min <= 0 or bybit_min <= 0:
        raise SizingError("min qty must be > 0")
    if target <= 0:
        raise SizingError("target_usdt must be > 0")
    if band_low <= 0 or band_high < band_low:
        raise SizingError("band must be positive and low <= high")
    if cap_usdt <= 0:
        raise SizingError("cap must be > 0")
    if target >= cap_usdt:
        raise SizingError("target_usdt must be strictly below cap")

    okx_bid_sz = _book_size(okx_book, "bid_size")
    okx_ask_sz = _book_size(okx_book, "ask_size")
    bybit_bid_sz = _book_size(bybit_book, "bid_size")
    bybit_ask_sz = _book_size(bybit_book, "ask_size")
    okx_ask_px = _book_size(okx_book, "ask_price") if okx_book is not None else None
    bybit_ask_px = _book_size(bybit_book, "ask_price") if bybit_book is not None else None

    zero = Decimal("0")
    lot_step = lcm_lot(okx_lot * ct_val, bybit_lot)

    def _out(
        *,
        coin_qty: Decimal,
        qty_okx: Decimal,
        qty_bybit: Decimal,
        ref_px: Decimal,
        notional_okx: Decimal,
        notional_bybit: Decimal,
        feasible: bool,
        reason: Optional[str],
        okx_exec: Optional[Decimal],
        bybit_exec: Optional[Decimal],
        depth_ok: Optional[bool],
    ) -> DualLegQtyPlan:
        return DualLegQtyPlan(
            base_coin=coin,
            coin_qty=coin_qty,
            qty_okx=qty_okx,
            qty_bybit=qty_bybit,
            okx_px=px_okx,
            bybit_px=px_bybit,
            notional_okx=notional_okx,
            notional_bybit=notional_bybit,
            target_usdt=target,
            band_low=band_low,
            band_high=band_high,
            cap_usdt=cap_usdt,
            feasible=feasible,
            reason=reason,
            ref_px=ref_px,
            ref_mode=REF_MODE_WORSE_ASK,
            lot_lcm=lot_step,
            okx_ct_val=ct_val,
            okx_bid_size=okx_bid_sz,
            okx_ask_size=okx_ask_sz,
            bybit_bid_size=bybit_bid_sz,
            bybit_ask_size=bybit_ask_sz,
            okx_exec_size=okx_exec,
            bybit_exec_size=bybit_exec,
            depth_ok=depth_ok,
            okx_side=okx_side_n,
            bybit_side=bybit_side_n,
        )

    if px_okx <= 0 or px_bybit <= 0:
        return _out(
            coin_qty=zero,
            qty_okx=zero,
            qty_bybit=zero,
            ref_px=zero,
            notional_okx=zero,
            notional_bybit=zero,
            feasible=False,
            reason="non_positive_price",
            okx_exec=None,
            bybit_exec=None,
            depth_ok=None,
        )

    # Worse ask: max of L1 asks when present, else max of the supplied prints.
    ref_candidates = [px_okx, px_bybit]
    if okx_ask_px is not None and okx_ask_px > 0:
        ref_candidates.append(okx_ask_px)
    if bybit_ask_px is not None and bybit_ask_px > 0:
        ref_candidates.append(bybit_ask_px)
    ref_px = max(ref_candidates)

    raw_coin = target / ref_px
    coin_qty = ceil_to_lot(raw_coin, lot_step)
    qty_okx, qty_bybit = _venue_qtys_from_coin(
        coin_qty,
        okx_lot=okx_lot,
        bybit_lot=bybit_lot,
        okx_ct_val=ct_val,
    )
    # Actual notionals use the venue prints the caller supplied (marks or L1).
    notional_okx = coin_qty * px_okx
    notional_bybit = coin_qty * px_bybit

    okx_exec = execution_l1_size(okx_book, okx_side_n)
    bybit_exec = execution_l1_size(bybit_book, bybit_side_n)
    okx_flat_exec = execution_l1_size(okx_book, flat_okx_n)
    bybit_flat_exec = execution_l1_size(bybit_book, flat_bybit_n)
    # OKX books5 sz is contracts; compare coin depth via ctVal. Bybit linear is coins.
    okx_exec_coins = None if okx_exec is None else okx_exec * ct_val
    okx_flat_coins = None if okx_flat_exec is None else okx_flat_exec * ct_val
    books_given = okx_book is not None or bybit_book is not None
    depth_checked = books_given or require_l1
    exec_pairs = [(okx_exec_coins, okx_side_n), (bybit_exec, bybit_side_n)]
    if flat_okx_n is not None or flat_bybit_n is not None:
        exec_pairs.extend([(okx_flat_coins, flat_okx_n), (bybit_flat_exec, flat_bybit_n)])
    depth_ok: Optional[bool]
    if not depth_checked:
        depth_ok = None
    else:
        depth_ok = True
        for size, side in exec_pairs:
            if side is None:
                continue
            if size is None or size < coin_qty:
                depth_ok = False
                break
        if require_l1 and (okx_side_n is None or bybit_side_n is None):
            depth_ok = False

    reason: Optional[str] = None
    if qty_okx < okx_min or qty_okx <= 0:
        reason = "okx_qty_below_min"
    elif qty_bybit < bybit_min or qty_bybit <= 0:
        reason = "bybit_qty_below_min"
    elif bybit_min_notional > 0 and notional_bybit < bybit_min_notional:
        reason = "bybit_notional_below_min"
    elif notional_okx >= cap_usdt:
        reason = "okx_notional_at_or_above_cap"
    elif notional_bybit >= cap_usdt:
        reason = "bybit_notional_at_or_above_cap"
    elif notional_okx < band_low:
        reason = "okx_notional_below_band"
    elif notional_bybit < band_low:
        reason = "bybit_notional_below_band"
    elif notional_okx > band_high:
        reason = "okx_notional_above_band"
    elif notional_bybit > band_high:
        reason = "bybit_notional_above_band"
    elif depth_checked and not depth_ok:
        if okx_exec is None or bybit_exec is None:
            reason = "l1_depth_missing"
        else:
            reason = "l1_depth_thin"

    return _out(
        coin_qty=coin_qty,
        qty_okx=qty_okx,
        qty_bybit=qty_bybit,
        ref_px=ref_px,
        notional_okx=notional_okx,
        notional_bybit=notional_bybit,
        feasible=reason is None,
        reason=reason,
        okx_exec=okx_exec,
        bybit_exec=bybit_exec,
        depth_ok=depth_ok,
    )
