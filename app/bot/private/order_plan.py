"""Immutable canonical OrderPlan for R3 (planning only).

No network, no transport, no strategy.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_UP
from typing import Any, Mapping, Optional
from uuid import uuid4

from app.bot.private.order_metadata import (
    InstrumentMetadata,
    MetadataError,
    MetadataProvider,
    parse_decimal,
)
from app.bot.private.order_symbols import (
    SymbolGateError,
    resolve_allowed_futures_symbol,
)

# Per-exchange live risk: strictly below 100 USD (not ≤).
MAX_NOTIONAL_USD_EXCLUSIVE = Decimal("100")
K_LIVE_REQUIRED = 1

ORDER_MODES = frozenset({"post_only_limit", "market"})
SIDES = frozenset({"buy", "sell"})

# Bounded TTL for post-only limits (seconds).
LIMIT_TTL_MIN_SEC = 1
LIMIT_TTL_MAX_SEC = 60


class OrderPlanError(ValueError):
    """Order plan construction / validation failure."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise OrderPlanError("timestamp must be timezone-aware UTC")
    dt = dt.astimezone(timezone.utc)
    ms = dt.microsecond // 1000
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms:03d}Z"


def parse_rfc3339_utc(ts: str) -> datetime:
    if not isinstance(ts, str) or not ts.endswith("Z"):
        raise OrderPlanError("expires_at_utc must be RFC3339 Z")
    body = ts[:-1]
    if "." in body:
        head, frac = body.split(".", 1)
        frac = (frac + "000000")[:6]
        dt = datetime.strptime(head, "%Y-%m-%dT%H:%M:%S")
        return dt.replace(microsecond=int(frac), tzinfo=timezone.utc)
    return datetime.strptime(body, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _quantize_qty(qty: Decimal, meta: InstrumentMetadata) -> Decimal:
    if qty <= 0:
        raise OrderPlanError("quantity must be positive")
    steps = (qty / meta.qty_step).to_integral_value(rounding=ROUND_UP)
    q = steps * meta.qty_step
    if q < meta.min_qty:
        q = meta.min_qty
    steps = (q / meta.qty_step).to_integral_value(rounding=ROUND_UP)
    q = steps * meta.qty_step
    if q < meta.min_qty:
        raise OrderPlanError("quantity below exchange min lot after step align")
    return q


def _quantize_price(price: Decimal, meta: InstrumentMetadata) -> Decimal:
    if price <= 0:
        raise OrderPlanError("price must be positive")
    steps = (price / meta.tick_size).to_integral_value(rounding=ROUND_UP)
    return steps * meta.tick_size


def _qty_on_step(qty: Decimal, meta: InstrumentMetadata) -> bool:
    if qty < meta.min_qty:
        return False
    rem = (qty / meta.qty_step) % 1
    return rem == 0


def _price_on_tick(price: Decimal, meta: InstrumentMetadata) -> bool:
    rem = (price / meta.tick_size) % 1
    return rem == 0


def _limit_notional_usdt(
    qty: Decimal, price: Decimal, meta: InstrumentMetadata
) -> Decimal:
    """Explicit limit notional — OKX contracts use qty * ctVal * price."""
    if meta.notional_unit == "usdt_per_contract":
        return qty * meta.contract_multiplier * price
    return qty * price


@dataclass(frozen=True)
class OrderPlan:
    """Immutable canonical plan — approval binds to this exact payload."""

    intent_id: str
    leg_id: str
    order_attempt_id: str
    venue: str
    symbol: str
    symbol_alias: str
    instrument_class: str
    side: str
    mode: str
    qty: str
    price: Optional[str]
    max_notional_usd: str
    time_in_force: str
    ttl_sec: int
    expires_at_utc: str
    expires_at_monotonic_ns: int
    k_live: int
    post_only: bool
    reduce_only: bool
    request_fingerprint: str
    dual_leg_id: str
    quantity_bucket: str
    notional_bucket: str
    # OKX hedge (long/short) mode only: "long" | "short". None for net/one-way.
    position_side: Optional[str] = None
    # OKX WS Place/Cancel requires integer instIdCode; not part of approval fingerprint.
    inst_id_code: Optional[int] = None

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "dual_leg_id": self.dual_leg_id,
            "expires_at_monotonic_ns": self.expires_at_monotonic_ns,
            "expires_at_utc": self.expires_at_utc,
            "instrument_class": self.instrument_class,
            "intent_id": self.intent_id,
            "k_live": self.k_live,
            "leg_id": self.leg_id,
            "max_notional_usd": self.max_notional_usd,
            "mode": self.mode,
            "notional_bucket": self.notional_bucket,
            "order_attempt_id": self.order_attempt_id,
            "position_side": self.position_side,
            "post_only": self.post_only,
            "price": self.price,
            "qty": self.qty,
            "quantity_bucket": self.quantity_bucket,
            "reduce_only": self.reduce_only,
            "request_fingerprint": self.request_fingerprint,
            "side": self.side,
            "symbol": self.symbol,
            "symbol_alias": self.symbol_alias,
            "time_in_force": self.time_in_force,
            "ttl_sec": self.ttl_sec,
            "venue": self.venue,
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.canonical_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def public_summary(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "leg_id": self.leg_id,
            "order_attempt_id": self.order_attempt_id,
            "venue": self.venue,
            "symbol_alias": self.symbol_alias,
            "instrument_class": self.instrument_class,
            "side": self.side,
            "mode": self.mode,
            "quantity_bucket": self.quantity_bucket,
            "notional_bucket": self.notional_bucket,
            "max_notional_usd": self.max_notional_usd,
            "time_in_force": self.time_in_force,
            "ttl_sec": self.ttl_sec,
            "expires_at_utc": self.expires_at_utc,
            "expires_monotonic_present": True,
            "k_live": self.k_live,
            "post_only": self.post_only,
            "reduce_only": self.reduce_only,
            "request_fingerprint": self.request_fingerprint,
            "price_present": self.price is not None,
            "qty_bucket_only": True,
            # GTC alone does not enforce TTL — cancellation required after TTL.
            "ttl_requires_cancel": self.mode == "post_only_limit",
            "ttl_bucket": ttl_bucket_for_sec(self.ttl_sec) if self.post_only else None,
        }

    def is_expired(
        self,
        *,
        now_utc: datetime | None = None,
        now_mono_ns: int | None = None,
    ) -> bool:
        now_u = now_utc or _utc_now()
        now_m = now_mono_ns if now_mono_ns is not None else time.monotonic_ns()
        if now_m >= int(self.expires_at_monotonic_ns):
            return True
        return now_u >= parse_rfc3339_utc(self.expires_at_utc)


def ttl_bucket_for_sec(ttl_sec: int) -> str:
    """Map post-only TTL seconds to journal bucket labels.

    Authorized R4 TTL=10s is ``short`` (never ``medium``).
    """
    if ttl_sec <= 10:
        return "short"
    if ttl_sec <= 30:
        return "medium"
    return "long"


def _fingerprint(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(
        dict(payload), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    return f"fp_{digest[:32]}"


def revalidate_order_plan(
    plan: OrderPlan,
    metadata_provider: MetadataProvider,
) -> None:
    """Independently re-check a plan against trusted metadata (anti-forgery)."""
    try:
        allowed = resolve_allowed_futures_symbol(plan.venue, plan.symbol)
    except SymbolGateError as exc:
        raise OrderPlanError(str(exc)) from exc
    if plan.symbol != allowed.symbol:
        raise OrderPlanError("plan symbol does not match allowlist native symbol")
    if plan.symbol_alias != allowed.symbol_alias:
        raise OrderPlanError("plan symbol_alias mismatch")
    if plan.instrument_class != "linear_perpetual":
        raise OrderPlanError("only linear_perpetual futures allowed")
    if plan.instrument_class != allowed.instrument_class:
        raise OrderPlanError("instrument_class mismatch")
    if plan.side not in SIDES:
        raise OrderPlanError("invalid side")
    if plan.mode not in ORDER_MODES:
        raise OrderPlanError("invalid mode")
    if plan.k_live != K_LIVE_REQUIRED:
        raise OrderPlanError("K_live must be 1")
    if plan.max_notional_usd != str(MAX_NOTIONAL_USD_EXCLUSIVE):
        raise OrderPlanError("max_notional_usd must be strict 100 exclusive cap label")
    if plan.quantity_bucket != "min_lot" or plan.notional_bucket != "under_100_usd":
        raise OrderPlanError("invalid size buckets")

    try:
        meta = metadata_provider.get(allowed.venue, allowed.symbol)
    except MetadataError as exc:
        raise OrderPlanError(str(exc)) from exc
    if meta.venue != allowed.venue or meta.symbol != allowed.symbol:
        raise OrderPlanError("metadata venue/symbol mismatch")
    try:
        meta.assert_mark_fresh()
    except MetadataError as exc:
        raise OrderPlanError(str(exc)) from exc

    q = parse_decimal(plan.qty, field="qty")
    if q <= 0 or not _qty_on_step(q, meta):
        raise OrderPlanError("qty fails min/step revalidation")

    if plan.mode == "post_only_limit":
        if not plan.post_only or plan.time_in_force != "post_only":
            raise OrderPlanError("post_only_limit requires post_only TIF")
        if plan.price is None:
            raise OrderPlanError("post_only_limit requires price")
        px = parse_decimal(plan.price, field="price")
        if not _price_on_tick(px, meta):
            raise OrderPlanError("price fails tick revalidation")
        if plan.ttl_sec < LIMIT_TTL_MIN_SEC or plan.ttl_sec > LIMIT_TTL_MAX_SEC:
            raise OrderPlanError("ttl_sec out of bounds")
        notional = _limit_notional_usdt(q, px, meta)
    else:
        if plan.post_only or plan.price is not None or plan.ttl_sec != 0:
            raise OrderPlanError("market mode field mismatch")
        if plan.time_in_force != "ioc":
            raise OrderPlanError("market mode requires ioc")
        try:
            notional = meta.market_notional_usdt(q)
        except MetadataError as exc:
            raise OrderPlanError(str(exc)) from exc

    if notional <= 0 or notional >= MAX_NOTIONAL_USD_EXCLUSIVE:
        raise OrderPlanError("notional fails strict <100 USD revalidation")

    if int(plan.expires_at_monotonic_ns) < 0:
        raise OrderPlanError("invalid expires_at_monotonic_ns")
    parse_rfc3339_utc(plan.expires_at_utc)

    # Fingerprint must match recomputed canonical body excluding fingerprint itself.
    body = {k: v for k, v in plan.canonical_dict().items() if k != "request_fingerprint"}
    expected_fp = _fingerprint(body)
    if expected_fp != plan.request_fingerprint:
        raise OrderPlanError("forged or inconsistent request_fingerprint")


def build_order_plan(
    *,
    venue: str,
    symbol: str,
    side: str,
    mode: str,
    metadata_provider: MetadataProvider,
    qty: str | Decimal | None = None,
    price: str | Decimal | None = None,
    ttl_sec: int | None = None,
    reduce_only: bool = False,
    position_side: str | None = None,
    intent_id: str | None = None,
    leg_id: str | None = None,
    order_attempt_id: str | None = None,
    dual_leg_id: str | None = None,
    expires_in_sec: int = 30,
    now: datetime | None = None,
    now_mono_ns: int | None = None,
) -> OrderPlan:
    """Build an immutable allowlisted live futures OrderPlan (K_live=1)."""
    try:
        allowed = resolve_allowed_futures_symbol(venue, symbol)
    except SymbolGateError as exc:
        raise OrderPlanError(str(exc)) from exc

    side_n = str(side).strip().lower()
    if side_n not in SIDES:
        raise OrderPlanError("side must be buy or sell")
    mode_n = str(mode).strip().lower()
    if mode_n not in ORDER_MODES:
        raise OrderPlanError("mode must be post_only_limit or market")
    if mode_n == "market" and mode.strip().lower() != "market":
        raise OrderPlanError("market mode must be explicit")

    pos_side: Optional[str]
    if position_side is None:
        pos_side = None
    else:
        pos_side = str(position_side).strip().lower()
        if pos_side not in {"long", "short"}:
            raise OrderPlanError("position_side must be long or short when set")
        if allowed.venue != "okx_live":
            raise OrderPlanError("position_side only valid for okx_live hedge body")
        # Open direction: buy→long, sell→short (no close/reduce inferred here).
        if side_n == "buy" and pos_side != "long":
            raise OrderPlanError("buy requires position_side=long")
        if side_n == "sell" and pos_side != "short":
            raise OrderPlanError("sell requires position_side=short")
        if bool(reduce_only):
            raise OrderPlanError("reduce_only not used with hedge open position_side")
    try:
        meta = metadata_provider.get(allowed.venue, allowed.symbol)
    except MetadataError as exc:
        raise OrderPlanError(str(exc)) from exc
    if meta.venue != allowed.venue or meta.symbol != allowed.symbol:
        raise OrderPlanError("metadata venue/symbol mismatch")
    try:
        meta.assert_mark_fresh()
    except MetadataError as exc:
        raise OrderPlanError(str(exc)) from exc

    if qty is None:
        q = meta.min_qty
    else:
        q = _quantize_qty(parse_decimal(qty, field="qty"), meta)

    px: Optional[Decimal] = None
    if mode_n == "post_only_limit":
        if price is None:
            raise OrderPlanError("post_only_limit requires price")
        px = _quantize_price(parse_decimal(price, field="price"), meta)
        if ttl_sec is None:
            raise OrderPlanError("post_only_limit requires ttl_sec")
        ttl = int(ttl_sec)
        if ttl < LIMIT_TTL_MIN_SEC or ttl > LIMIT_TTL_MAX_SEC:
            raise OrderPlanError(
                f"ttl_sec must be in [{LIMIT_TTL_MIN_SEC}, {LIMIT_TTL_MAX_SEC}]"
            )
        tif = "post_only"
        post_only = True
        notional = _limit_notional_usdt(q, px, meta)
    else:
        if price is not None:
            raise OrderPlanError("market mode must not include price")
        if ttl_sec is not None:
            raise OrderPlanError("market mode must not set limit TTL")
        ttl = 0
        tif = "ioc"
        post_only = False
        try:
            notional = meta.market_notional_usdt(q)
        except MetadataError as exc:
            raise OrderPlanError(str(exc)) from exc

    if notional <= 0:
        raise OrderPlanError("notional must be positive")
    if notional >= MAX_NOTIONAL_USD_EXCLUSIVE:
        raise OrderPlanError(
            "per-exchange notional must be strictly below 100 USD"
        )
    if expires_in_sec < 1 or expires_in_sec > 300:
        raise OrderPlanError("expires_in_sec out of bounds")

    now_dt = now or _utc_now()
    mono0 = int(now_mono_ns if now_mono_ns is not None else time.monotonic_ns())
    expires = now_dt + timedelta(seconds=int(expires_in_sec))
    expires_mono = mono0 + int(expires_in_sec) * 1_000_000_000
    expires_utc = _rfc3339(expires)

    intent = intent_id or _new_id("intent")
    leg = leg_id or _new_id("leg")
    attempt = order_attempt_id or _new_id("attempt")
    dual = dual_leg_id or _new_id("dual")

    pre = {
        "dual_leg_id": dual,
        "expires_at_monotonic_ns": expires_mono,
        "expires_at_utc": expires_utc,
        "instrument_class": allowed.instrument_class,
        "intent_id": intent,
        "k_live": K_LIVE_REQUIRED,
        "leg_id": leg,
        "max_notional_usd": str(MAX_NOTIONAL_USD_EXCLUSIVE),
        "mode": mode_n,
        "notional_bucket": "under_100_usd",
        "order_attempt_id": attempt,
        "position_side": pos_side,
        "post_only": post_only,
        "price": None if px is None else format(px, "f"),
        "qty": format(q, "f"),
        "quantity_bucket": "min_lot",
        "reduce_only": bool(reduce_only),
        "side": side_n,
        "symbol": allowed.symbol,
        "symbol_alias": allowed.symbol_alias,
        "time_in_force": tif,
        "ttl_sec": ttl,
        "venue": allowed.venue,
    }
    fp = _fingerprint(pre)
    plan = OrderPlan(
        intent_id=intent,
        leg_id=leg,
        order_attempt_id=attempt,
        venue=allowed.venue,
        symbol=allowed.symbol,
        symbol_alias=allowed.symbol_alias,
        instrument_class=allowed.instrument_class,
        side=side_n,
        mode=mode_n,
        qty=format(q, "f"),
        price=None if px is None else format(px, "f"),
        max_notional_usd=str(MAX_NOTIONAL_USD_EXCLUSIVE),
        time_in_force=tif,
        ttl_sec=ttl,
        expires_at_utc=expires_utc,
        expires_at_monotonic_ns=expires_mono,
        k_live=K_LIVE_REQUIRED,
        post_only=post_only,
        reduce_only=bool(reduce_only),
        request_fingerprint=fp,
        dual_leg_id=dual,
        quantity_bucket="min_lot",
        notional_bucket="under_100_usd",
        position_side=pos_side,
        inst_id_code=meta.inst_id_code,
    )
    revalidate_order_plan(plan, metadata_provider)
    return plan

