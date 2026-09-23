"""SOL/XRP sized dual-leg: dry plan (S2) and S3/S4 live cycle.

Dry path never sends. Live send requires VENUE=live, LIVE_ORDERS=1,
BBOT_PRIVATE_SIZED_CYCLE=1, and ``--s3-approve-one-shot``. Default CLI never
binds this. Shared-coin LCM sizer + L1 depth gate; thin/missing books abort
fail-closed (do not send).
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from app.bot.private.order_metadata import (
    InstrumentMetadata,
    StaticMetadataProvider,
)
from app.bot.private.order_plan import OrderPlan, build_order_plan
from app.bot.private.order_sender import assert_default_entrypoint_cannot_transport
from app.bot.private.order_symbols import (
    LIVE_SIZE_PAIR_ALIASES,
    live_size_native_symbol,
)
from app.bot.private.paths import resolve_data_root
from app.bot.private.venue import live_orders_enabled, resolve_venue, send_allowed
from app.bot.runtime import load_universe
from app.bot.sizing import (
    LIVE_SIZE_COINS,
    DualLegQtyPlan,
    is_live_size_coin,
    plan_dual_leg_qty,
)
from app.bot.stub_broker import InstrumentMeta

# W7-style cycle, qty from sizer — not the TRUMP profile.
HOLD_SEC = 10
OPEN_MODE = "parallel"
FLATTEN_MODE = "parallel"

# Open: Bybit buy / OKX sell. Flatten reverses. Depth uses execution L1.
DEFAULT_OPEN_BYBIT_SIDE = "buy"
DEFAULT_OPEN_OKX_SIDE = "sell"
DEFAULT_FLAT_BYBIT_SIDE = "sell"
DEFAULT_FLAT_OKX_SIDE = "buy"

# Fixture marks for dry / unit (no network). Realistic enough for SOL/XRP lots.
DRY_MARK_USDT: Mapping[str, tuple[Decimal, Decimal]] = {
    "SOL": (Decimal("150"), Decimal("150")),
    "XRP": (Decimal("0.60"), Decimal("0.60")),
}

# Dry L1 is a fixture, not a live book. Size is ample so depth_ok=true in dry.
# Live planning fetches OKX books / Bybit ob1 top-of-book only.
DRY_L1_SIZE = Decimal("1000")


class SizedCycleError(ValueError):
    """Sized dual-leg contour or plan failure."""


@dataclass(frozen=True)
class SizedCycleSpec:
    """Reusable S3 spec: sizer qtys + W7-style barrier / hold / flatten."""

    coin: str
    coin_qty: str
    qty_okx: str
    qty_bybit: str
    notional_okx: str
    notional_bybit: str
    okx_symbol: str
    bybit_symbol: str
    symbol_alias: str
    hold_sec: int = HOLD_SEC
    open_mode: str = OPEN_MODE
    flatten_mode: str = FLATTEN_MODE
    open_bybit_side: str = DEFAULT_OPEN_BYBIT_SIDE
    open_okx_side: str = DEFAULT_OPEN_OKX_SIDE
    flatten_bybit_side: str = DEFAULT_FLAT_BYBIT_SIDE
    flatten_okx_side: str = DEFAULT_FLAT_OKX_SIDE
    depth_ok: Optional[bool] = None
    okx_bid_size: Optional[str] = None
    okx_ask_size: Optional[str] = None
    bybit_bid_size: Optional[str] = None
    bybit_ask_size: Optional[str] = None


@dataclass
class DrySizeReport:
    status: str
    coin: str
    size_plan: dict[str, Any] = field(default_factory=dict)
    spec: dict[str, Any] = field(default_factory=dict)
    fingerprints: dict[str, str] = field(default_factory=dict)
    send_allowed: bool = False
    live_orders: str = "0"
    orders_sent: int = 0
    sends_blocked: bool = True
    data_root: str = ""
    error_code: Optional[str] = None

    def as_public_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status,
            "coin": self.coin,
            "contour": list(LIVE_SIZE_COINS),
            "size_plan": dict(self.size_plan),
            "spec": dict(self.spec),
            "fingerprints": dict(self.fingerprints),
            "send_allowed": self.send_allowed,
            "LIVE_ORDERS": self.live_orders,
            "orders_sent": self.orders_sent,
            "sends_blocked": self.sends_blocked,
            "data_root": self.data_root,
            "hold_sec": HOLD_SEC,
            "open_mode": OPEN_MODE,
            "flatten_mode": FLATTEN_MODE,
        }
        if self.error_code:
            out["error_code"] = self.error_code
        return out


def parse_dry_size_cli_args(argv: Sequence[str]) -> Optional[str]:
    coin: Optional[str] = None
    for arg in argv:
        if arg.startswith("--coin="):
            coin = arg.split("=", 1)[1].strip().upper() or None
    return coin


def parse_sized_cycle_cli_args(argv: Sequence[str]) -> tuple[Optional[str], int, bool]:
    coin = parse_dry_size_cli_args(argv)
    n = 1
    approve = False
    for arg in argv:
        if arg.startswith("--s3-n="):
            raw = arg.split("=", 1)[1].strip()
            try:
                n = int(raw)
            except ValueError:
                n = -1
        elif arg == "--s3-approve-one-shot":
            approve = True
    return coin, n, approve


def pick_contour_coin(
    *,
    coin: Optional[str] = None,
    rng: Optional[random.Random] = None,
) -> str:
    if coin:
        picked = str(coin).strip().upper()
        if not is_live_size_coin(picked):
            raise SizedCycleError(
                f"live-size contour is SOL/XRP only; got {coin!r}"
            )
        return picked
    source = rng if rng is not None else random.Random()
    return source.choice(list(LIVE_SIZE_COINS))


def universe_meta_for(coin: str, universe: Optional[Mapping[str, InstrumentMeta]] = None) -> InstrumentMeta:
    key = str(coin).strip().upper()
    if not is_live_size_coin(key):
        raise SizedCycleError(f"live-size contour is SOL/XRP only; got {coin!r}")
    table = universe if universe is not None else load_universe()
    if key not in table:
        raise SizedCycleError(f"{key} missing from bybit_okx_universe.csv")
    return table[key]


def spec_from_size_plan(
    size: DualLegQtyPlan,
    meta: InstrumentMeta,
) -> SizedCycleSpec:
    if not is_live_size_coin(size.base_coin):
        raise SizedCycleError(
            f"live-size contour is SOL/XRP only; got {size.base_coin!r}"
        )
    bybit = live_size_native_symbol("bybit_live", size.base_coin)
    okx = live_size_native_symbol("okx_live", size.base_coin)
    if bybit.symbol_alias not in LIVE_SIZE_PAIR_ALIASES:
        raise SizedCycleError("alias is not on the SOL/XRP live-size contour")
    return SizedCycleSpec(
        coin=size.base_coin,
        coin_qty=format(size.coin_qty, "f"),
        qty_okx=format(size.okx_qty, "f"),
        qty_bybit=format(size.bybit_qty, "f"),
        notional_okx=format(size.notional_okx, "f"),
        notional_bybit=format(size.notional_bybit, "f"),
        okx_symbol=okx.symbol,
        bybit_symbol=bybit.symbol,
        symbol_alias=bybit.symbol_alias,
        hold_sec=HOLD_SEC,
        open_mode=OPEN_MODE,
        flatten_mode=FLATTEN_MODE,
        depth_ok=size.depth_ok,
        okx_bid_size=_fmt_opt_dec(size.okx_bid_size),
        okx_ask_size=_fmt_opt_dec(size.okx_ask_size),
        bybit_bid_size=_fmt_opt_dec(size.bybit_bid_size),
        bybit_ask_size=_fmt_opt_dec(size.bybit_ask_size),
    )


def _fmt_opt_dec(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    return format(value, "f")


def _fixture_l1_book(px: Decimal, *, size: Decimal = DRY_L1_SIZE) -> dict[str, str]:
    """Dry-only L1. Not a live book; size is ample so dry plans are depth-ok."""
    p = format(px, "f")
    s = format(size, "f")
    return {"bid_price": p, "ask_price": p, "bid_size": s, "ask_size": s}


def _spec_public_dict(spec: SizedCycleSpec) -> dict[str, Any]:
    return {
        "coin": spec.coin,
        "coin_qty": spec.coin_qty,
        "okx_qty": spec.qty_okx,
        "bybit_qty": spec.qty_bybit,
        "qty_okx": spec.qty_okx,
        "qty_bybit": spec.qty_bybit,
        "notional_okx": spec.notional_okx,
        "notional_bybit": spec.notional_bybit,
        "okx_symbol": spec.okx_symbol,
        "bybit_symbol": spec.bybit_symbol,
        "symbol_alias": spec.symbol_alias,
        "hold_sec": spec.hold_sec,
        "open_mode": spec.open_mode,
        "flatten_mode": spec.flatten_mode,
        "depth_ok": spec.depth_ok,
        "okx_bid_size": spec.okx_bid_size,
        "okx_ask_size": spec.okx_ask_size,
        "bybit_bid_size": spec.bybit_bid_size,
        "bybit_ask_size": spec.bybit_ask_size,
    }


def _plan_qty(
    meta: InstrumentMeta,
    okx_px: Decimal,
    bybit_px: Decimal,
    *,
    okx_book: Optional[Mapping[str, Any]] = None,
    bybit_book: Optional[Mapping[str, Any]] = None,
    okx_ct_val: Optional[Decimal] = None,
    require_l1: bool = False,
) -> DualLegQtyPlan:
    return plan_dual_leg_qty(
        meta,
        okx_px,
        bybit_px,
        okx_book=okx_book,
        bybit_book=bybit_book,
        okx_side=DEFAULT_OPEN_OKX_SIDE,
        bybit_side=DEFAULT_OPEN_BYBIT_SIDE,
        flatten_okx_side=DEFAULT_FLAT_OKX_SIDE,
        flatten_bybit_side=DEFAULT_FLAT_BYBIT_SIDE,
        okx_ct_val=okx_ct_val,
        require_l1=require_l1,
    )


def _metadata_provider(
    spec: SizedCycleSpec,
    size: DualLegQtyPlan,
    meta: InstrumentMeta,
    *,
    now_mono_ns: Optional[int] = None,
) -> StaticMetadataProvider:
    """Universe-lot qty units: both venues usdt_per_coin so plan notional matches sizer."""
    asof = int(now_mono_ns if now_mono_ns is not None else time.monotonic_ns())
    bybit = InstrumentMetadata(
        venue="bybit_live",
        symbol=spec.bybit_symbol,
        min_qty=Decimal(str(meta.bybit_min_order_qty)),
        qty_step=Decimal(str(meta.bybit_qty_step)),
        tick_size=Decimal("0.0001"),
        contract_multiplier=Decimal("1"),
        contract_value_ccy="USDT",
        notional_unit="usdt_per_coin",
        mark_price_usdt=size.bybit_px,
        mark_asof_monotonic_ns=asof,
        mark_max_age_ns=60_000_000_000,
    )
    okx = InstrumentMetadata(
        venue="okx_live",
        symbol=spec.okx_symbol,
        min_qty=Decimal(str(meta.okx_min_size)),
        qty_step=Decimal(str(meta.okx_lot_size)),
        tick_size=Decimal("0.0001"),
        contract_multiplier=Decimal("1"),
        contract_value_ccy="USDT",
        notional_unit="usdt_per_coin",
        mark_price_usdt=size.okx_px,
        mark_asof_monotonic_ns=asof,
        mark_max_age_ns=60_000_000_000,
    )
    return StaticMetadataProvider(
        {
            ("bybit_live", spec.bybit_symbol): bybit,
            ("okx_live", spec.okx_symbol): okx,
        }
    )


def build_sized_order_plans(
    spec: SizedCycleSpec,
    size: DualLegQtyPlan,
    meta: InstrumentMeta,
    *,
    dual_leg_id: str = "dry_dual",
    now_mono_ns: Optional[int] = None,
) -> dict[str, OrderPlan]:
    """Open + flatten OrderPlans for fingerprinting. Does not send."""
    provider = _metadata_provider(spec, size, meta, now_mono_ns=now_mono_ns)
    bybit_open = build_order_plan(
        venue="bybit_live",
        symbol=spec.bybit_symbol,
        side=spec.open_bybit_side,
        mode="market",
        metadata_provider=provider,
        qty=spec.qty_bybit,
        dual_leg_id=dual_leg_id,
        intent_id="dry_intent_bybit_open",
        leg_id="dry_leg_bybit_open",
        order_attempt_id="dry_attempt_bybit_open",
        expires_in_sec=30,
        now_mono_ns=now_mono_ns,
    )
    okx_open = build_order_plan(
        venue="okx_live",
        symbol=spec.okx_symbol,
        side=spec.open_okx_side,
        mode="market",
        metadata_provider=provider,
        qty=spec.qty_okx,
        dual_leg_id=dual_leg_id,
        intent_id="dry_intent_okx_open",
        leg_id="dry_leg_okx_open",
        order_attempt_id="dry_attempt_okx_open",
        expires_in_sec=30,
        now_mono_ns=now_mono_ns,
    )
    bybit_flat = build_order_plan(
        venue="bybit_live",
        symbol=spec.bybit_symbol,
        side=spec.flatten_bybit_side,
        mode="market",
        metadata_provider=provider,
        qty=spec.qty_bybit,
        reduce_only=True,
        dual_leg_id=dual_leg_id,
        intent_id="dry_intent_bybit_flat",
        leg_id="dry_leg_bybit_flat",
        order_attempt_id="dry_attempt_bybit_flat",
        expires_in_sec=30,
        now_mono_ns=now_mono_ns,
    )
    okx_flat = build_order_plan(
        venue="okx_live",
        symbol=spec.okx_symbol,
        side=spec.flatten_okx_side,
        mode="market",
        metadata_provider=provider,
        qty=spec.qty_okx,
        reduce_only=True,
        dual_leg_id=dual_leg_id,
        intent_id="dry_intent_okx_flat",
        leg_id="dry_leg_okx_flat",
        order_attempt_id="dry_attempt_okx_flat",
        expires_in_sec=30,
        now_mono_ns=now_mono_ns,
    )
    return {
        "bybit_open": bybit_open,
        "okx_open": okx_open,
        "bybit_flatten": bybit_flat,
        "okx_flatten": okx_flat,
    }


def dry_random_size_plan(
    *,
    env: Optional[Mapping[str, str]] = None,
    coin: Optional[str] = None,
    rng: Optional[random.Random] = None,
    universe: Optional[Mapping[str, InstrumentMeta]] = None,
    okx_px: Optional[Decimal] = None,
    bybit_px: Optional[Decimal] = None,
    write: bool = True,
) -> DrySizeReport:
    """S2: pick SOL or XRP, print sizer + build_order_plan fingerprints. No send."""
    assert_default_entrypoint_cannot_transport()
    e = dict(env if env is not None else os.environ)
    e.setdefault("LIVE_ORDERS", "0")
    live_orders = str(e.get("LIVE_ORDERS") or "0")
    allowed = False
    try:
        allowed = send_allowed(e)
    except ValueError:
        allowed = False

    root = resolve_data_root(e)
    if "bbot-gear2" in str(root):
        raise SizedCycleError("refusing to write under /data/bbot-gear2")

    try:
        picked = pick_contour_coin(coin=coin, rng=rng)
        meta = universe_meta_for(picked, universe)
        marks = DRY_MARK_USDT[picked]
        px_okx = okx_px if okx_px is not None else marks[0]
        px_bybit = bybit_px if bybit_px is not None else marks[1]
        # Dry uses fixture L1 (ample size), not a live book. Documented limitation.
        size = _plan_qty(
            meta,
            px_okx,
            px_bybit,
            okx_book=_fixture_l1_book(px_okx),
            bybit_book=_fixture_l1_book(px_bybit),
            require_l1=True,
        )
    except SizedCycleError as exc:
        return DrySizeReport(
            status="rejected",
            coin=str(coin or ""),
            send_allowed=allowed,
            live_orders=live_orders,
            data_root=str(root),
            error_code="invalid_request",
            size_plan={"error": str(exc)},
        )

    if not size.feasible:
        report = DrySizeReport(
            status="infeasible",
            coin=picked,
            size_plan=size.as_public_dict(),
            send_allowed=allowed,
            live_orders=live_orders,
            data_root=str(root),
            error_code=size.reason,
        )
        if write:
            _write_dry_report(root, report)
        return report

    spec = spec_from_size_plan(size, meta)
    plans = build_sized_order_plans(spec, size, meta)
    fingerprints = {name: plan.request_fingerprint for name, plan in plans.items()}
    report = DrySizeReport(
        status="ok",
        coin=picked,
        size_plan=size.as_public_dict(),
        spec=_spec_public_dict(spec),
        fingerprints=fingerprints,
        send_allowed=allowed,
        live_orders=live_orders,
        data_root=str(root),
    )
    if write:
        _write_dry_report(root, report)
    return report


def run_sized_parallel_cycle(
    *,
    env: Optional[Mapping[str, str]] = None,
    coin: Optional[str] = None,
    n: int = 1,
    approve_one_shot: bool = False,
    bindings: Optional[Any] = None,
) -> dict[str, Any]:
    """S3/S4: W7-style parallel open / 10s hold / parallel flatten.

    Send requires gates plus ``--s3-approve-one-shot``. Default CLI never
    reaches here. Infeasible sizer plans abort without transport.
    """
    assert_default_entrypoint_cannot_transport()
    e = dict(env if env is not None else os.environ)
    from app.bot.private.ws_gates import WsProfileGateError, assert_sized_cycle_send_gates

    try:
        assert_sized_cycle_send_gates(e)
        gated = True
        gate_error = None
    except (WsProfileGateError, RuntimeError) as exc:
        gated = False
        gate_error = type(exc).__name__

    dry = dry_random_size_plan(env=e, coin=coin, write=False)
    payload = dry.as_public_dict()
    payload["cycle"] = "parallel_open_hold_parallel_flatten"
    payload["hold_sec"] = HOLD_SEC
    payload["n_requested"] = n
    payload["send_unlocked"] = False
    payload["orders_sent"] = 0
    payload["sends_blocked"] = True
    if not gated:
        payload["status"] = "rejected_before_socket"
        payload["error_code"] = "invalid_request"
        payload["gate"] = gate_error
        return payload
    if not approve_one_shot:
        payload["status"] = "approval_required"
        payload["error_code"] = "approval_required"
        return payload
    if n < 1 or n > 5:
        payload["status"] = "rejected_before_socket"
        payload["error_code"] = "invalid_request"
        payload["gate"] = "invalid_n"
        return payload
    return _run_sized_send_rounds(
        env=e,
        coin=coin,
        n=n,
        bindings=bindings,
        dry_payload=payload,
    )


SIZED_SAMPLE_MAX = Decimal("20")


def _okx_native_qty(coin_qty: Decimal, meta: Any) -> str:
    """Convert shared coin qty → OKX native qty.

    Universe / sizer ``coin_qty`` is coins. Live OKX SWAP uses contracts:
    ``contracts = coin_qty / ctVal``. For SOL/XRP linear USDT, ctVal is 1
    so 1 contract ≈ 1 coin — still apply the divide, do not hard-code 1:1.
    """
    from app.bot.sizing import ceil_to_lot

    unit = getattr(meta, "notional_unit", "usdt_per_coin")
    if unit == "usdt_per_contract":
        ct_val = Decimal(str(meta.contract_multiplier))
        if ct_val <= 0:
            raise SizedCycleError("okx ctVal invalid")
        contracts = coin_qty / ct_val
        return format(ceil_to_lot(contracts, meta.qty_step), "f")
    return format(coin_qty, "f")


def _fetch_live_marks(coin: str) -> tuple[Decimal, Decimal, Any, Any]:
    from app.bot.private.order_preflight import LiveHttpMetadataProvider
    from app.bot.private.ws_w6_dual_leg import _public_http_get_json

    provider = LiveHttpMetadataProvider(http_get_json=_public_http_get_json)
    bybit = live_size_native_symbol("bybit_live", coin)
    okx = live_size_native_symbol("okx_live", coin)
    b_meta = provider.get("bybit_live", bybit.symbol)
    o_meta = provider.get("okx_live", okx.symbol)
    return b_meta.mark_price_usdt, o_meta.mark_price_usdt, b_meta, o_meta


def _parse_okx_l1_book(payload: Mapping[str, Any]) -> dict[str, str]:
    """OKX books (sz=5) → L1 only. Deeper levels are not summed."""
    if str(payload.get("code") or "") != "0":
        raise SizedCycleError("okx l1 books rejected")
    rows = payload.get("data") or []
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], Mapping):
        raise SizedCycleError("okx l1 books empty")
    row = rows[0]
    bids = row.get("bids") or []
    asks = row.get("asks") or []
    if not bids or not asks:
        raise SizedCycleError("okx l1 top-of-book missing")
    bid = bids[0]
    ask = asks[0]
    if not isinstance(bid, (list, tuple)) or len(bid) < 2:
        raise SizedCycleError("okx l1 bid row invalid")
    if not isinstance(ask, (list, tuple)) or len(ask) < 2:
        raise SizedCycleError("okx l1 ask row invalid")
    return {
        "bid_price": str(bid[0]),
        "bid_size": str(bid[1]),
        "ask_price": str(ask[0]),
        "ask_size": str(ask[1]),
    }


def _parse_bybit_l1_book(payload: Mapping[str, Any]) -> dict[str, str]:
    """Bybit orderbook.1 → L1 only."""
    if payload.get("retCode") != 0 and str(payload.get("retCode")) != "0":
        raise SizedCycleError("bybit l1 orderbook rejected")
    row = payload.get("result") or {}
    if not isinstance(row, Mapping):
        raise SizedCycleError("bybit l1 orderbook empty")
    bids = row.get("b") or []
    asks = row.get("a") or []
    if not bids or not asks:
        raise SizedCycleError("bybit l1 top-of-book missing")
    bid = bids[0]
    ask = asks[0]
    if not isinstance(bid, (list, tuple)) or len(bid) < 2:
        raise SizedCycleError("bybit l1 bid row invalid")
    if not isinstance(ask, (list, tuple)) or len(ask) < 2:
        raise SizedCycleError("bybit l1 ask row invalid")
    return {
        "bid_price": str(bid[0]),
        "bid_size": str(bid[1]),
        "ask_price": str(ask[0]),
        "ask_size": str(ask[1]),
    }


def _fetch_live_l1_books(coin: str) -> tuple[dict[str, str], dict[str, str]]:
    """Public REST L1: OKX books sz=5, Bybit orderbook limit=1. Top row only."""
    from app.bot.private.rest_readonly import okx_public_rest_headers
    from app.bot.private.ws_w6_dual_leg import _public_http_get_json

    bybit = live_size_native_symbol("bybit_live", coin)
    okx = live_size_native_symbol("okx_live", coin)
    okx_url = f"https://www.okx.com/api/v5/market/books?instId={okx.symbol}&sz=5"
    bybit_url = (
        f"https://api.bybit.com/v5/market/orderbook"
        f"?category=linear&symbol={bybit.symbol}&limit=1"
    )
    okx_book = _parse_okx_l1_book(_public_http_get_json(okx_url, okx_public_rest_headers()))
    bybit_book = _parse_bybit_l1_book(_public_http_get_json(bybit_url, {"Accept": "application/json"}))
    return okx_book, bybit_book


def _legs_from_size(
    spec: SizedCycleSpec,
    okx_live_meta: Any,
    bybit_live_meta: Optional[Any] = None,
) -> dict[str, dict[str, Any]]:
    from app.bot.sizing import ceil_to_lot

    qty_okx = _okx_native_qty(Decimal(spec.coin_qty), okx_live_meta)
    raw_bybit = Decimal(spec.qty_bybit)
    if bybit_live_meta is not None:
        # Same coins; format must match order_plan.format(step-aligned, "f")
        # or W6 exact-qty gate rejects ("8.00" vs "8.0") before send.
        qty_bybit = format(ceil_to_lot(raw_bybit, bybit_live_meta.qty_step), "f")
        if Decimal(qty_bybit) != raw_bybit:
            raise SizedCycleError("bybit qty changed under live step; refuse send")
    else:
        qty_bybit = spec.qty_bybit
    return {
        "bybit": {
            "exchange": "bybit",
            "venue": "bybit_live",
            "symbol": spec.bybit_symbol,
            "symbol_alias": spec.symbol_alias,
            "qty": qty_bybit,
            "open_side": spec.open_bybit_side,
            "flatten_side": spec.flatten_bybit_side,
            "mode": "market",
            "leg_role": "first",
        },
        "okx": {
            "exchange": "okx",
            "venue": "okx_live",
            "symbol": spec.okx_symbol,
            "symbol_alias": spec.symbol_alias,
            "qty": qty_okx,
            "open_side": spec.open_okx_side,
            "flatten_side": spec.flatten_okx_side,
            "mode": "market",
            "leg_role": "second",
        },
    }


def _plan_one_live_round(
    *,
    env: Mapping[str, str],
    coin: Optional[str],
) -> dict[str, Any]:
    """Pick a feasible SOL/XRP plan from live marks. No sockets."""
    preferred = [coin] if coin else list(LIVE_SIZE_COINS)
    if coin is None:
        random.shuffle(preferred)
    last_infeasible: Optional[dict[str, Any]] = None
    for candidate in preferred:
        picked = pick_contour_coin(coin=candidate)
        meta = universe_meta_for(picked)
        bybit_px, okx_px, b_meta, o_meta = _fetch_live_marks(picked)
        try:
            okx_book, bybit_book = _fetch_live_l1_books(picked)
        except (SizedCycleError, OSError, ValueError, TypeError, KeyError) as exc:
            last_infeasible = {
                "base_coin": picked,
                "reason": "l1_depth_missing",
                "l1_error": type(exc).__name__,
            }
            continue
        # Universe lots are coins. Live OKX ctVal: SOL/XRP linear USDT is 1
        # contract ≈ 1 coin; pass live multiplier so other coins do not assume 1:1.
        size = _plan_qty(
            meta,
            okx_px,
            bybit_px,
            okx_book=okx_book,
            bybit_book=bybit_book,
            okx_ct_val=o_meta.contract_multiplier,
            require_l1=True,
        )
        if not size.feasible:
            last_infeasible = size.as_public_dict()
            continue
        spec = spec_from_size_plan(size, meta)
        legs = _legs_from_size(spec, o_meta, b_meta)
        spec_pub = _spec_public_dict(spec)
        spec_pub["qty_okx_native"] = legs["okx"]["qty"]
        spec_pub["qty_bybit_native"] = legs["bybit"]["qty"]
        spec_pub["okx_ct_val"] = format(o_meta.contract_multiplier, "f")
        return {
            "status": "ok",
            "coin": picked,
            "size_plan": size.as_public_dict(),
            "spec": spec_pub,
            "legs": legs,
            "marks": {"okx": format(okx_px, "f"), "bybit": format(bybit_px, "f")},
            "l1": {
                "okx": okx_book,
                "bybit": bybit_book,
                "limitation": "top_of_book_only",
            },
        }
    return {
        "status": "infeasible",
        "coin": str(coin or ""),
        "size_plan": last_infeasible or {},
        "error_code": (last_infeasible or {}).get("reason") or "infeasible",
    }


def _run_one_sized_send(
    *,
    env: Mapping[str, str],
    plan: Mapping[str, Any],
    bindings: Optional[Any],
) -> dict[str, Any]:
    from app.bot.private.order_preflight import PreflightError
    from app.bot.private.ws_gates import assert_sized_cycle_send_gates
    from app.bot.private.ws_socket import (
        assert_no_default_ws_socket,
        unbind_socket_factory,
    )
    from app.bot.private.ws_w4_baseline import BaselineError
    from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id
    from app.bot.private.ws_w6_dual_leg import (
        W6ProfileError,
        W6RuntimeBindings,
        open_w6_production_bindings,
        run_w6_dual_leg,
    )

    legs = plan["legs"]
    owned = bindings is None
    active: Optional[W6RuntimeBindings] = bindings
    root = resolve_data_root(env)
    journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"), env=env)
    try:
        if active is None:
            assert_no_default_ws_socket()
            active = open_w6_production_bindings(
                env=env,
                legs=legs,
                sample_max_notional=SIZED_SAMPLE_MAX,
            )
        report = run_w6_dual_leg(
            n=1,
            env=env,
            metadata_provider=active.metadata_provider,
            position_mode_provider=active.position_mode_provider,
            baseline=active.baseline,
            bybit_private_socket=active.bybit_private_socket,
            bybit_trade_socket=active.bybit_trade_socket,
            okx_private_socket=active.okx_private_socket,
            okx_trade_socket=active.okx_trade_socket,
            bybit_credentials=active.bybit_credentials,
            okx_credentials=active.okx_credentials,
            load_secrets=False,
            issue_approval=True,
            rest_order_recon=active.rest_order_recon,
            journal=journal,
            data_root=root,
            parallel_open=True,
            parallel_flatten=True,
            hold_sec=HOLD_SEC,
            send_gate=assert_sized_cycle_send_gates,
            legs=legs,
            sample_max_notional=SIZED_SAMPLE_MAX,
        )
        out = report.as_public_dict()
        out["coin"] = plan["coin"]
        out["size_plan"] = plan["size_plan"]
        out["spec"] = plan["spec"]
        out["marks"] = plan["marks"]
        out["run_id"] = journal.run_id
        out["signal_ts_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        out["timing"] = _timing_from_journal(root, journal.run_id)
        return out
    except (
        BaselineError,
        W6ProfileError,
        PreflightError,
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        SizedCycleError,
    ) as exc:
        return {
            "status": "bind_failed",
            "error_code": "transport_error",
            "coin": plan.get("coin"),
            "orders_sent": 0,
            "flat_after": False,
            "bind_error": type(exc).__name__,
        }
    finally:
        if owned:
            unbind_socket_factory()
            assert_default_entrypoint_cannot_transport()


def _timing_from_journal(data_root: Path, run_id: Optional[str] = None) -> list[dict[str, Any]]:
    from app.bot.private.journal_v1 import scan_all_journal_events

    keep = {"order_prepared", "request_sent", "ack_received", "terminal_update"}
    rows: list[dict[str, Any]] = []
    for ev in scan_all_journal_events(data_root):
        if run_id is not None and str(ev.get("run_id") or "") != run_id:
            continue
        et = str(ev.get("event_type") or "")
        if et not in keep:
            continue
        rows.append(
            {
                "event_type": et,
                "venue": ev.get("venue"),
                "event_ts_utc": ev.get("event_ts_utc"),
                "reduce_only": ev.get("reduce_only"),
                "send_monotonic_ns": ev.get("send_monotonic_ns"),
                "receive_monotonic_ns": ev.get("receive_monotonic_ns"),
            }
        )
    return rows


def _run_sized_send_rounds(
    *,
    env: Mapping[str, str],
    coin: Optional[str],
    n: int,
    bindings: Optional[Any],
    dry_payload: dict[str, Any],
) -> dict[str, Any]:
    rounds: list[dict[str, Any]] = []
    n_completed = 0
    n_aborted = 0
    orders_sent = 0
    coins: list[str] = []
    abort_reason: Optional[str] = None
    last_flat = False

    for _i in range(n):
        planned = _plan_one_live_round(env=env, coin=coin)
        if planned.get("status") != "ok":
            abort_reason = str(planned.get("error_code") or "infeasible")
            n_aborted += 1
            rounds.append(planned)
            break
        sent = _run_one_sized_send(env=env, plan=planned, bindings=bindings)
        rounds.append(sent)
        coins.append(str(planned["coin"]))
        orders_sent += int(sent.get("orders_sent") or 0)
        last_flat = bool(sent.get("flat_after"))
        if sent.get("status") == "ok" and last_flat and int(sent.get("n_aborted") or 0) == 0:
            n_completed += 1
            continue
        abort_reason = str(sent.get("status") or "one_legged")
        n_aborted += 1
        break

    payload = dict(dry_payload)
    last = rounds[-1] if rounds else {}
    payload.update(
        {
            "status": "ok" if n_completed == n and last_flat else (abort_reason or "aborted"),
            "send_unlocked": True,
            "sends_blocked": False,
            "n_requested": n,
            "n_completed": n_completed,
            "n_aborted": n_aborted,
            "orders_sent": orders_sent,
            "coins": coins,
            "flat_after": last_flat,
            "rounds": rounds,
            "error_code": None if n_completed == n and last_flat else (abort_reason or "aborted"),
        }
    )
    if last.get("size_plan"):
        payload["size_plan"] = last["size_plan"]
        payload["coin"] = last.get("coin") or payload.get("coin")
        payload["spec"] = last.get("spec") or payload.get("spec")
    if last.get("timing"):
        payload["timing"] = last["timing"]
    if last.get("latency_ms"):
        payload["latency_ms"] = last["latency_ms"]
    _write_sized_report(env, payload)
    return payload


def _write_sized_report(env: Mapping[str, str], payload: Mapping[str, Any]) -> None:
    root = resolve_data_root(env)
    if "bbot-gear2" in str(root):
        raise SizedCycleError("refusing to write under /data/bbot-gear2")
    path = root / "probes" / "sized_cycle.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = {k: v for k, v in payload.items() if k not in {"legs"}}
    line = json.dumps(safe, ensure_ascii=False, default=str, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _write_dry_report(root: Path, report: DrySizeReport) -> None:
    path = root / "probes" / "dry_size_plan.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(report.as_public_dict(), ensure_ascii=False, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def main_dry_size_plan(
    argv: Optional[Sequence[str]] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> int:
    argv = list(argv or [])
    e = dict(env if env is not None else os.environ)
    e.setdefault("LIVE_ORDERS", "0")
    coin = parse_dry_size_cli_args(argv)
    report = dry_random_size_plan(env=e, coin=coin)
    print(json.dumps(report.as_public_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    if report.orders_sent != 0 or report.send_allowed:
        return 2
    if report.status == "ok":
        return 0
    return 1


def main_sized_parallel_cycle(
    argv: Optional[Sequence[str]] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> int:
    """CLI for S3/S4. Send only with gates plus ``--s3-approve-one-shot``."""
    argv = list(argv or [])
    e = dict(env if env is not None else os.environ)
    coin, n, approve = parse_sized_cycle_cli_args(argv)
    payload = run_sized_parallel_cycle(
        env=e, coin=coin, n=n, approve_one_shot=approve
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    status = str(payload.get("status") or "")
    if status == "ok" and int(payload.get("orders_sent") or 0) > 0:
        return 0
    if status in {"approval_required", "rejected_before_socket", "infeasible"}:
        return 1
    if status == "s3_not_unlocked":
        return 1
    return 2
