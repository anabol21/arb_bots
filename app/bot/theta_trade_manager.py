"""Gear 2.2 θ K=1 would_send contour, plus optional live canary send.

Driven off the live theta emit (~1 Hz) inside one BotRuntime. Journals
``theta_trades/``. The default path does **not** call StubBroker.place or
private APIs (existing ``gear22_would_send`` / stub unit).

Live canary (fail-closed): ``BBOT_PROFILE=gear22_live_canary`` or
``BBOT_THETA_LIVE_SEND=1`` requires ``BBOT_BROKER=private_live``,
``VENUE=live``, and ``LIVE_ORDERS=1``. Then open/close decisions that pass
size_check call an injected ``place_fn`` (Contour B ``LiveBroker.place``)
immediately at signal — no 70 ms synthetic fill sleep. ACK flatten stays
inside Contour B. This module does not import ``app.bot.private``.

**Policy:** Gear 2.2 research policy (`research.gear22_backtest.policy.decide`)
with frozen observation knobs from `research.gear22_backtest.params_frozen`.
Optional ``decide_fn`` (profile ``synthetic_roll``) replaces that call once
per tick; the size gate and K=1 slot stay here. This module does not import
``app.bot.private``.
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from app.bot.paths import theta_trades_jsonl_path
from app.bot.sentry_setup import capture_trade_event
from app.bot.stub_broker import legs_for_spread_side, signal_price_for_leg
from app.bot.theta_screener import ThetaSnapshot
from research.gear22_backtest.policy import (
    Decision as PolicyDecision,
    FeatureSnapshot,
    PolicyParams,
    PolicyState,
    decide as policy_decide,
    potential_profit_pp,
)
from research.gear22_backtest.params_frozen import DEFAULT_OBSERVE_PARAMS


def compute_spreads_pct(okx: Mapping[str, Any], bybit: Mapping[str, Any]) -> tuple[float, float]:
    """Same formula as ``app.bot.ws_books.compute_spreads`` (no websockets import)."""
    spread_long = (bybit["bid_price"] - okx["ask_price"]) * 100.0 / bybit["bid_price"]
    spread_short = (okx["bid_price"] - bybit["ask_price"]) * 100.0 / okx["bid_price"]
    return float(spread_long), float(spread_short)

SCHEMA_VERSION = "bbot.theta_trade.v1"
DEFAULT_THETA_THR = 0.2
DEFAULT_FILL_DELAY_MS = 70
DEFAULT_SLOT_K = 1
DEFAULT_NOTIONAL_USDT = 100.0
DEFAULT_LIVE_CANARY_NOTIONAL_USDT = 20.0
DEFAULT_BOOK_DEPTH = 1
POLICY_ID = "gear22_frozen_v1"

# August-std HTML top30 — same universe as VPS unit spread-bbot-theta-k1-canary.
GEAR22_HTML_TOP30: tuple[str, ...] = (
    "KAITO",
    "HOME",
    "WAL",
    "RVN",
    "ONT",
    "2Z",
    "BICO",
    "HMSTR",
    "CAP",
    "BLEND",
    "EDEN",
    "KMNO",
    "GPS",
    "ME",
    "ZBT",
    "MOVE",
    "COAI",
    "AZTEC",
    "APR",
    "YB",
    "ICX",
    "AT",
    "H",
    "MUBARAK",
    "ACU",
    "LA",
    "BEAT",
    "PARTI",
    "SIGN",
    "GIGGLE",
)

GEAR22_LIVE_CANARY_PROFILES = frozenset({"gear22_live_canary", "gear22_live"})
GEAR22_WOULD_SEND_PROFILES = frozenset({"gear22_would_send", "gear22"})
_GEAR22_TRADE_PROFILES = frozenset(
    {"gear22_would_send", "gear22", "gear2_would_send", "gear2"}
    | GEAR22_LIVE_CANARY_PROFILES
)


class ThetaLiveSendError(RuntimeError):
    """Fail-closed live canary: missing LIVE_ORDERS / private_live / VENUE=live."""


PlaceFn = Callable[..., Any]
MetaFn = Callable[[str], Any]


def normalize_gear22_profile(profile: str) -> str:
    name = str(profile).strip().lower()
    if name in GEAR22_WOULD_SEND_PROFILES:
        return "gear22_would_send"
    if name in GEAR22_LIVE_CANARY_PROFILES:
        return "gear22_live_canary"
    return name


def theta_live_send_requested(
    profile: str,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """True when the operator armed gear22 live send (profile or env flag)."""
    e = env if env is not None else os.environ
    raw = str(e.get("BBOT_THETA_LIVE_SEND") or "").strip().lower()
    if raw in ("0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    return normalize_gear22_profile(profile) == "gear22_live_canary"


def assert_theta_live_send_gates(
    profile: str,
    env: Optional[Mapping[str, str]] = None,
) -> None:
    """If live send is requested, require private_live + VENUE=live + LIVE_ORDERS=1.

    No-op when the gate is off so the stub would_send unit stays unchanged.
    Does not import ``app.bot.private`` (stub isolation).
    """
    e = env if env is not None else os.environ
    if not theta_live_send_requested(profile, e):
        return
    broker = str(e.get("BBOT_BROKER") or "").strip().lower()
    venue = str(e.get("VENUE") or "").strip().lower()
    live_orders = str(e.get("LIVE_ORDERS") or "0").strip().lower()
    if broker not in {"private_live", "live"}:
        raise ThetaLiveSendError(
            "theta live send requires BBOT_BROKER=private_live, "
            f"got {broker or 'stub'!r}"
        )
    if venue != "live":
        raise ThetaLiveSendError(
            f"theta live send requires VENUE=live, got {venue or 'unset'!r}"
        )
    if live_orders not in {"1", "true", "on", "yes"}:
        raise ThetaLiveSendError(
            "theta live send requires LIVE_ORDERS=1 (fail closed)"
        )

LogFn = Callable[[str], None]


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def build_feature_snapshot(
    *,
    coin: str,
    ts_s: int,
    snapshots: Sequence[ThetaSnapshot],
    quotes: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> Optional[FeatureSnapshot]:
    """Build FeatureSnapshot for policy.decide from live ThetaSnapshot + books.
    
    Returns None if data is incomplete (both sides need p50, theta, floor, spread).
    """
    by_key: dict[tuple[str, str], ThetaSnapshot] = {
        (s.base_coin, s.side): s for s in snapshots
    }
    long_snap = by_key.get((coin, "long"))
    short_snap = by_key.get((coin, "short"))
    if long_snap is None or short_snap is None:
        return None
    
    books = quotes.get(coin) or {}
    okx = books.get("okx") or {}
    bybit = books.get("bybit") or {}
    
    try:
        spread_long, spread_short = compute_spreads_pct(okx, bybit)
    except (TypeError, ValueError, ZeroDivisionError, KeyError):
        spread_long, spread_short = math.nan, math.nan
    
    def _usable(snap: ThetaSnapshot, spread: float) -> bool:
        return all(
            _finite(x) is not None
            for x in [
                snap.floor_tf_select_a25,
                snap.p50_1m,
                snap.theta_1m,
                spread,
            ]
        )
    
    return FeatureSnapshot(
        ts_s=ts_s,
        coin=coin,
        p50_1m_long=float(long_snap.p50_1m) if _finite(long_snap.p50_1m) is not None else math.nan,
        p50_1m_short=float(short_snap.p50_1m) if _finite(short_snap.p50_1m) is not None else math.nan,
        floor_long=float(long_snap.floor_tf_select_a25) if _finite(long_snap.floor_tf_select_a25) is not None else math.nan,
        floor_short=float(short_snap.floor_tf_select_a25) if _finite(short_snap.floor_tf_select_a25) is not None else math.nan,
        theta_1m_long=float(long_snap.theta_1m) if _finite(long_snap.theta_1m) is not None else math.nan,
        theta_1m_short=float(short_snap.theta_1m) if _finite(short_snap.theta_1m) is not None else math.nan,
        spread_last_long=spread_long,
        spread_last_short=spread_short,
        usable_long=_usable(long_snap, spread_long),
        usable_short=_usable(short_snap, spread_short),
    )


def theta_trade_enabled(
    profile: str,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """``BBOT_THETA_TRADE``: 1/0 override; default on for gear22 would_send and live canary."""
    e = env if env is not None else os.environ
    raw = str(e.get("BBOT_THETA_TRADE") or "").strip().lower()
    if raw in ("0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    name = str(profile).strip().lower()
    return (
        name in GEAR22_WOULD_SEND_PROFILES
        or name in GEAR22_LIVE_CANARY_PROFILES
        or name == "synthetic_roll"
    )


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = str(env.get(key) or "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except ValueError:
        return float(default)


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = str(env.get(key) or "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except ValueError:
        return int(default)


def opposite_side(side: str) -> str:
    s = str(side).strip().lower()
    if s == "long":
        return "short"
    if s == "short":
        return "long"
    raise ValueError(f"side must be long|short, got {side!r}")


def spread_side_for(side: str, *, event: str) -> str:
    """Map position side + open/close → stub spread_side label."""
    s = str(side).strip().lower()
    ev = str(event).strip().lower()
    if ev == "open":
        return "open_long" if s == "long" else "open_short"
    # close reverses the open
    return "open_short" if s == "long" else "open_long"


def snapshot_book(book: Mapping[str, Any]) -> dict[str, Any]:
    """Structured L1 (and optional depth) copy — never an opaque blob only."""
    out: dict[str, Any] = {
        "bid_price": _finite(book.get("bid_price")),
        "bid_size": _finite(book.get("bid_size")),
        "ask_price": _finite(book.get("ask_price")),
        "ask_size": _finite(book.get("ask_size")),
        "ts_exchange": book.get("ts_exchange"),
        "local_recv_ts_ms": book.get("local_recv_ts_ms"),
        "delivery_latency_ms": book.get("delivery_latency_ms"),
    }
    # Optional top-N if caller attached levels (v1 WS cache is L1-only).
    for key in ("bids", "asks", "depth"):
        if key in book:
            out[key] = book[key]
    return out


def available_leg_size(book: Mapping[str, Any], leg_side: str, *, depth: int = 1) -> Optional[float]:
    """Size available on the traded side; L1 by default.

    ``depth > 1`` sums ``bids``/``asks`` lists when present; otherwise falls
    back to L1 size (live WS cache is L1-only today).
    """
    d = max(1, int(depth))
    if d > 1:
        levels = book.get("bids" if leg_side == "sell" else "asks")
        if isinstance(levels, (list, tuple)) and levels:
            total = 0.0
            for lvl in levels[:d]:
                if isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                    sz = _finite(lvl[1])
                    if sz is not None:
                        total += float(sz)
                elif isinstance(lvl, Mapping):
                    sz = _finite(lvl.get("size") or lvl.get("sz"))
                    if sz is not None:
                        total += float(sz)
            if total > 0:
                return float(total)
    if leg_side == "buy":
        return _finite(book.get("ask_size"))
    return _finite(book.get("bid_size"))


def size_check(
    *,
    okx: Mapping[str, Any],
    bybit: Mapping[str, Any],
    side: str,
    event: str,
    notional_usdt: float,
    book_depth: int = 1,
    okx_ct_val: Any = None,
    required_okx_contracts: Any = None,
    required_bybit_qty: Any = None,
) -> dict[str, Any]:
    """Notional must fit available size on both chosen legs."""
    spread_side = spread_side_for(side, event=event)
    okx_leg, bybit_leg = legs_for_spread_side(spread_side)
    okx_px = signal_price_for_leg(dict(okx), okx_leg)
    bybit_px = signal_price_for_leg(dict(bybit), bybit_leg)
    okx_contracts = available_leg_size(okx, okx_leg, depth=book_depth)
    private_size_gate = bool(okx.get("_private_size_gate")) or okx_ct_val is not None
    try:
        ct_val = float(okx_ct_val if okx_ct_val is not None else okx.get("ct_val"))
        if not math.isfinite(ct_val) or ct_val <= 0:
            ct_val = None
    except (TypeError, ValueError):
        ct_val = None
    okx_sz = (
        float(okx_contracts) * ct_val
        if okx_contracts is not None and ct_val is not None
        else (None if private_size_gate else okx_contracts)
    )
    bybit_sz = available_leg_size(bybit, bybit_leg, depth=book_depth)
    planned_okx = (
        float(notional_usdt) / float(okx_px)
        if _finite(okx_px) and float(okx_px) > 0
        else None
    )
    planned_bybit = (
        float(notional_usdt) / float(bybit_px)
        if _finite(bybit_px) and float(bybit_px) > 0
        else None
    )
    if required_okx_contracts is not None:
        required_contracts = _finite(required_okx_contracts)
        planned_okx = (
            required_contracts * ct_val
            if required_contracts is not None and ct_val is not None
            else None
        )
    if required_bybit_qty is not None:
        planned_bybit = _finite(required_bybit_qty)
    okx_ok = (
        planned_okx is not None
        and okx_sz is not None
        and float(okx_sz) >= float(planned_okx)
    )
    bybit_ok = (
        planned_bybit is not None
        and bybit_sz is not None
        and float(bybit_sz) >= float(planned_bybit)
    )
    size_ok = bool(okx_ok and bybit_ok)
    return {
        "size_ok": size_ok,
        "notional_usdt": float(notional_usdt),
        "book_depth": int(book_depth),
        "okx_leg_side": okx_leg,
        "bybit_leg_side": bybit_leg,
        "okx_price": _finite(okx_px),
        "bybit_price": _finite(bybit_px),
        "okx_available_size": okx_sz,
        "okx_available_contracts": okx_contracts,
        "okx_ct_val": ct_val if private_size_gate else None,
        "bybit_available_size": bybit_sz,
        "okx_planned_qty": planned_okx,
        "bybit_planned_qty": planned_bybit,
        "leg_buy_ex": "okx" if okx_leg == "buy" else "bybit",
        "leg_sell_ex": "okx" if okx_leg == "sell" else "bybit",
    }


def journal_close_pnl_spread(
    open_spread: Optional[float],
    close_spread: Optional[float],
    fee_round_trip_pp: float,
) -> Optional[float]:
    """Gear 2.2 recorded close profit in percentage points.

    ``close_spread`` is already the opposite-side (unwind) fill: short when
    the open was long. Policy ``potential_pp`` is

        fill_spread_pp + spread_last_opposite − fee_round_trip_pp

    with frozen ``fee_round_trip_pp`` of 0.30. Subtracting the close spread
    (``open − close``) flips a negative close into a phantom gain, so the
    journal uses the same sum:

        pnl_spread = open_fill_spread + close_fill_spread − fee_round_trip_pp
    """
    opened = _finite(open_spread)
    closed = _finite(close_spread)
    fee = _finite(fee_round_trip_pp)
    if opened is None or closed is None or fee is None:
        return None
    return float(opened) + float(closed) - float(fee)


def spread_for_side(okx: Mapping[str, Any], bybit: Mapping[str, Any], side: str) -> Optional[float]:
    """Edge % for long/short using the same formula as live WS spreads."""
    try:
        long_s, short_s = compute_spreads_pct(dict(okx), dict(bybit))
    except (TypeError, ValueError, ZeroDivisionError, KeyError):
        return None
    s = str(side).strip().lower()
    if s == "long":
        return _finite(long_s)
    if s == "short":
        return _finite(short_s)
    return None


def slip_spread(*, signal_spread: Optional[float], fill_spread: Optional[float]) -> Optional[float]:
    """First-class slip on the arb edge.

    Definition (documented): ``slip_spread = signal_spread - fill_spread``.
    Positive ⇒ edge compressed between signal and fill ⇒ **worse for us**.
    Equivalent to ``-(fill − signal)`` on the edge metric (so the product
    phrase “fill−signal, positive = worse” is the negated edge delta).
    """
    sig = _finite(signal_spread)
    fil = _finite(fill_spread)
    if sig is None or fil is None:
        return None
    return float(sig) - float(fil)


def slip_leg_bps(
    *,
    signal_book: Mapping[str, Any],
    fill_book: Mapping[str, Any],
    leg_side: str,
) -> Optional[float]:
    """Per-leg slippage in bps; positive = worse for that leg."""
    sig_px = _finite(signal_price_for_leg(dict(signal_book), leg_side))
    fil_px = _finite(signal_price_for_leg(dict(fill_book), leg_side))
    if sig_px is None or fil_px is None or float(sig_px) <= 0:
        return None
    if leg_side == "buy":
        # Paid more at fill → worse.
        return (float(fil_px) - float(sig_px)) / float(sig_px) * 10_000.0
    # Sold lower at fill → worse.
    return (float(sig_px) - float(fil_px)) / float(sig_px) * 10_000.0


@dataclass
class ThetaTradeConfig:
    theta_thr: float = DEFAULT_THETA_THR
    fill_delay_ms: int = DEFAULT_FILL_DELAY_MS
    slot_k: int = DEFAULT_SLOT_K
    notional_usdt: float = DEFAULT_NOTIONAL_USDT
    book_depth: int = DEFAULT_BOOK_DEPTH
    policy_params: Optional[PolicyParams] = None

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "ThetaTradeConfig":
        e = env if env is not None else os.environ
        
        frozen = DEFAULT_OBSERVE_PARAMS
        policy_params = PolicyParams(
            theta_open=_env_float(e, "BBOT_THETA_OPEN", frozen.theta_open or 0.0),
            p50_open=_env_float(e, "BBOT_P50_OPEN", frozen.p50_open or 0.0),
            min_profit_pp=_env_float(e, "BBOT_MIN_PROFIT_PP", frozen.min_profit_pp or 0.0),
            fee_round_trip_pp=_env_float(e, "BBOT_FEE_RT_PP", frozen.fee_round_trip_pp),
            min_spread_open=None,
            min_theta_close=_env_float(e, "BBOT_MIN_THETA_CLOSE", frozen.min_theta_close or 0.0)
            if "BBOT_MIN_THETA_CLOSE" in e or frozen.min_theta_close is not None
            else None,
        )
        
        return cls(
            theta_thr=_env_float(e, "BBOT_THETA_THR", DEFAULT_THETA_THR),
            fill_delay_ms=_env_int(e, "BBOT_FILL_DELAY_MS", DEFAULT_FILL_DELAY_MS),
            slot_k=max(1, _env_int(e, "BBOT_SLOT_K", DEFAULT_SLOT_K)),
            notional_usdt=_env_float(e, "BBOT_NOTIONAL_USDT", DEFAULT_NOTIONAL_USDT),
            book_depth=max(1, _env_int(e, "BBOT_BOOK_DEPTH", DEFAULT_BOOK_DEPTH)),
            policy_params=policy_params,
        )


@dataclass
class OpenPosition:
    trade_id: str
    base_coin: str
    side: str
    open_signal_ts_ms: int
    open_fill_ts_ms: int
    open_fill_spread: Optional[float]
    open_notional: float
    open_theta_1m: Optional[float]
    fill_spread_pp: Optional[float] = None
    okx_filled_qty: Optional[str] = None
    bybit_filled_qty: Optional[str] = None
    coin_filled_qty: Optional[str] = None


@dataclass
class SlotState:
    """Global K=1 slot across all coins."""

    k: int = 1
    position: Optional[OpenPosition] = None
    pending: bool = False
    skip_counts: dict[str, int] = field(default_factory=dict)

    def slot_busy(self) -> bool:
        return self.position is not None or self.pending


@dataclass(frozen=True)
class ThetaDecision:
    action: str  # open | close | skip
    base_coin: str
    side: str
    reason: str
    theta_1m: Optional[float] = None
    opposite_theta_1m: Optional[float] = None
    reject_reason: Optional[str] = None
    size_info: Optional[dict[str, Any]] = None
    potential_pp: Optional[float] = None
    policy_decision: Optional[PolicyDecision] = None
    # ``open`` or ``close``: which size_check produced an insufficient_size skip.
    size_event: Optional[str] = None


def coerce_external_decision(raw: Any) -> ThetaDecision:
    """Adapt a synthetic (or other) decide result into ``ThetaDecision``.

    ``hold`` becomes ``skip`` so the manager does not send. Size info is left
    empty so ``execute_decision`` still runs ``size_check`` for open and close.
    """
    if isinstance(raw, ThetaDecision):
        return raw
    action = str(getattr(raw, "action", "") or "").strip().lower()
    if action == "hold":
        action = "skip"
    if action not in ("open", "close", "skip"):
        action = "skip"
    coin = getattr(raw, "coin", None)
    if coin is None:
        coin = getattr(raw, "base_coin", "") or ""
    side = str(getattr(raw, "side", "") or "").strip().lower()
    reason = str(getattr(raw, "reason", "") or "").strip()
    if not reason:
        reason = "hold" if action == "skip" else "synthetic_roll"
    return ThetaDecision(
        action=action,
        base_coin=str(coin).upper(),
        side=side,
        reason=reason,
    )


def _read_place_result(
    result: Any,
) -> tuple[Optional[str], bool, bool, Optional[int], Optional[str], Optional[str]]:
    """Normalize place result, including quantities from terminal fills."""
    if result is None:
        return None, True, False, None, None, None
    if isinstance(result, str):
        return result, False, False, None, None, None
    abort = getattr(result, "abort", None)
    if abort is not None:
        abort = str(abort)
    completed = bool(getattr(result, "completed", False)) and not abort
    keep_pending = bool(getattr(result, "keep_pending", False))
    fill_ts = getattr(result, "fill_ts_ms", None)
    fill_i = int(fill_ts) if fill_ts is not None else None
    okx_qty = getattr(result, "okx_filled_qty", None)
    bybit_qty = getattr(result, "bybit_filled_qty", None)
    return (
        abort,
        completed,
        keep_pending,
        fill_i,
        str(okx_qty) if okx_qty is not None else None,
        str(bybit_qty) if bybit_qty is not None else None,
    )


def insufficient_size_event(
    decision: ThetaDecision,
    *,
    held: Optional[OpenPosition] = None,
) -> str:
    """Size-check event for an ``insufficient_size`` skip fallback.

    Close rejects use the flatten legs (``event="close"``). Open rejects use
    the open legs. A held slot for the same coin and side is a close reject
    when the decision does not already name the event.
    """
    explicit = str(decision.size_event or "").strip().lower()
    if explicit in ("open", "close"):
        return explicit
    pd = decision.policy_decision
    if pd is not None and str(getattr(pd, "action", "")).strip().lower() == "close":
        return "close"
    if held is not None:
        same_coin = str(held.base_coin).upper() == str(decision.base_coin).upper()
        same_side = str(held.side).strip().lower() == str(decision.side).strip().lower()
        if same_coin and same_side:
            return "close"
    return "open"


def decide_theta_k1(
    snapshots: Sequence[ThetaSnapshot],
    *,
    slot: SlotState,
    thr: float,
    quotes: Mapping[str, Mapping[str, Mapping[str, Any]]],
    notional_usdt: float,
    book_depth: int = 1,
    coin_order: Optional[Sequence[str]] = None,
    policy_params: Optional[PolicyParams] = None,
    ts_s: Optional[int] = None,
) -> ThetaDecision:
    """Pure K=1 entry/exit decide using gear 2.2 policy (no I/O, no sleep).
    
    Uses policy.decide() from research.gear22_backtest.policy with frozen
    observation parameters. Replaces old theta_thr entry/exit logic.
    """
    params = policy_params or PolicyParams()
    by_key: dict[tuple[str, str], ThetaSnapshot] = {
        (s.base_coin, s.side): s for s in snapshots
    }
    
    now_s = int(ts_s if ts_s is not None else time.time())
    
    if slot.position is not None and not slot.pending:
        pos = slot.position
        feat = build_feature_snapshot(
            coin=pos.base_coin,
            ts_s=now_s,
            snapshots=snapshots,
            quotes=quotes,
        )
        if feat is None:
            own = by_key.get((pos.base_coin, pos.side))
            opp = by_key.get((pos.base_coin, opposite_side(pos.side)))
            return ThetaDecision(
                action="skip",
                base_coin=pos.base_coin,
                side=pos.side,
                reason="hold_incomplete_data",
                theta_1m=own.theta_1m if own else None,
                opposite_theta_1m=opp.theta_1m if opp else None,
                reject_reason="incomplete_data",
            )
        
        state = PolicyState(
            position_side=pos.side,
            held_coin=pos.base_coin,
            opened_ts_s=int(pos.open_fill_ts_ms / 1000),
            fill_spread_pp=pos.fill_spread_pp,
        )
        pd = policy_decide(feat, state, params)
        
        if pd.action == "close":
            books = quotes.get(pos.base_coin) or {}
            okx = books.get("okx") or {}
            bybit = books.get("bybit") or {}
            size_info = size_check(
                okx=okx,
                bybit=bybit,
                side=pos.side,
                event="close",
                notional_usdt=notional_usdt,
                book_depth=book_depth,
                required_okx_contracts=pos.okx_filled_qty,
                required_bybit_qty=pos.bybit_filled_qty,
            )
            pot_pp = potential_profit_pp(feat, state, params.fee_round_trip_pp)
            own = by_key.get((pos.base_coin, pos.side))
            opp = by_key.get((pos.base_coin, opposite_side(pos.side)))
            if not size_info["size_ok"]:
                return ThetaDecision(
                    action="skip",
                    base_coin=pos.base_coin,
                    side=pos.side,
                    reason="reject",
                    theta_1m=own.theta_1m if own else None,
                    opposite_theta_1m=opp.theta_1m if opp else None,
                    reject_reason="insufficient_size",
                    size_info=size_info,
                    potential_pp=pot_pp,
                    policy_decision=pd,
                    size_event="close",
                )
            return ThetaDecision(
                action="close",
                base_coin=pos.base_coin,
                side=pos.side,
                reason=pd.reason,
                theta_1m=own.theta_1m if own else None,
                opposite_theta_1m=opp.theta_1m if opp else None,
                size_info=size_info,
                potential_pp=pot_pp,
                policy_decision=pd,
                size_event="close",
            )
        
        own = by_key.get((pos.base_coin, pos.side))
        opp = by_key.get((pos.base_coin, opposite_side(pos.side)))
        return ThetaDecision(
            action="skip",
            base_coin=pos.base_coin,
            side=pos.side,
            reason="slot_busy" if pd.reason == "hold_open_overlap" else pd.reason,
            theta_1m=own.theta_1m if own else None,
            opposite_theta_1m=opp.theta_1m if opp else None,
            reject_reason="slot_busy" if pd.reason == "hold_open_overlap" else pd.reason,
            policy_decision=pd,
        )
    
    if slot.slot_busy():
        held = slot.position.base_coin if slot.position is not None else ""
        return ThetaDecision(
            action="skip",
            base_coin=held,
            side=slot.position.side if slot.position else "",
            reason="slot_busy",
            reject_reason="slot_busy",
        )
    
    order: list[str]
    if coin_order:
        order = [str(c).upper() for c in coin_order]
    else:
        seen: list[str] = []
        for s in snapshots:
            if s.base_coin not in seen:
                seen.append(s.base_coin)
        order = seen
    
    first_size_reject: Optional[ThetaDecision] = None
    for coin in order:
        feat = build_feature_snapshot(
            coin=coin,
            ts_s=now_s,
            snapshots=snapshots,
            quotes=quotes,
        )
        if feat is None:
            continue
        
        state = PolicyState()
        pd = policy_decide(feat, state, params)
        
        if pd.action in ("open_long", "open_short"):
            side = "long" if pd.action == "open_long" else "short"
            books = quotes.get(coin) or {}
            okx = books.get("okx") or {}
            bybit = books.get("bybit") or {}
            size_info = size_check(
                okx=okx,
                bybit=bybit,
                side=side,
                event="open",
                notional_usdt=notional_usdt,
                book_depth=book_depth,
            )
            if not size_info["size_ok"]:
                if first_size_reject is None:
                    first_size_reject = ThetaDecision(
                        action="skip",
                        base_coin=coin,
                        side=side,
                        reason="reject",
                        theta_1m=pd.theta,
                        opposite_theta_1m=None,
                        reject_reason="insufficient_size",
                        size_info=size_info,
                        policy_decision=pd,
                        size_event="open",
                    )
                continue
            return ThetaDecision(
                action="open",
                base_coin=coin,
                side=side,
                reason=pd.reason,
                theta_1m=pd.theta,
                opposite_theta_1m=None,
                size_info=size_info,
                policy_decision=pd,
            )
    
    if first_size_reject is not None:
        return first_size_reject
    return ThetaDecision(action="skip", base_coin="", side="", reason="no_signal")


_SYNTHETIC_ROLL_SCHEMA = "bbot.synthetic_roll.v1"
_SYNTHETIC_SLOT_STATUSES = ("pending", "open", "closed")


def restore_synthetic_slot(
    data_root: Path, *, notional_usdt: float
) -> tuple[Optional[OpenPosition], bool, str, str]:
    """Latest ``bbot.synthetic_roll.v1`` intent in the theta trade journal.

    Reads existing ``trades.jsonl`` files and does not create directories.
    A ``pending`` row with no later ``open`` or ``closed`` for that intent
    restores as pending. An ``open`` row restores the position. ``closed``
    leaves the slot flat. Gear 2.2 rows are ignored.
    """
    root = Path(data_root) / "theta_trades"
    if not root.is_dir():
        return None, False, "", ""
    paths = sorted(p for p in root.glob("event_date=*/trades.jsonl") if p.is_file())
    latest_id: Optional[str] = None
    rows_for: dict[str, list[dict[str, Any]]] = {}
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("schema_version") != _SYNTHETIC_ROLL_SCHEMA:
                continue
            iid = rec.get("intent_id")
            if not iid:
                continue
            iid_s = str(iid)
            latest_id = iid_s
            rows_for.setdefault(iid_s, []).append(rec)
    if latest_id is None:
        return None, False, "", ""
    last_status: Optional[str] = None
    last_row: Optional[dict[str, Any]] = None
    for rec in rows_for[latest_id]:
        status = rec.get("status")
        if status in _SYNTHETIC_SLOT_STATUSES:
            last_status = str(status)
            last_row = rec
    if last_row is None or last_status == "closed":
        return None, False, "", ""
    coin = str(last_row.get("base_coin") or "")
    side = str(last_row.get("side") or "")
    if last_status == "pending":
        return None, True, coin, side
    signal_ts = int(last_row.get("signal_ts_ms") or 0)
    fill_ts = int(last_row.get("fill_ts_ms") or signal_ts)
    position = OpenPosition(
        trade_id=latest_id,
        base_coin=coin,
        side=side,
        open_signal_ts_ms=signal_ts,
        open_fill_ts_ms=fill_ts,
        open_fill_spread=None,
        open_notional=float(notional_usdt),
        open_theta_1m=None,
        okx_filled_qty=(
            str(last_row["okx_filled_qty"])
            if last_row.get("okx_filled_qty") is not None
            else None
        ),
        bybit_filled_qty=(
            str(last_row["bybit_filled_qty"])
            if last_row.get("bybit_filled_qty") is not None
            else None
        ),
        coin_filled_qty=(
            str(last_row["coin_qty"]) if last_row.get("coin_qty") is not None else None
        ),
    )
    return position, False, coin, side


class ThetaTradeJournalWriter:
    """Append-only would_send trades under ``{data_root}/theta_trades/``."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = Path(data_root)
        text = str(self.data_root.resolve())
        for bad in ("/data/live", "/data/bars", "/data/compacted", "/data/spool"):
            if text == bad or text.startswith(bad + os.sep):
                raise RuntimeError(
                    f"ThetaTradeJournalWriter refuses D path: {self.data_root}"
                )

    def append_rows(self, rows: Sequence[Mapping[str, Any]]) -> list[Path]:
        if not rows:
            return []
        by_date: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            ts_ms = int(
                row.get("signal_ts_ms")
                or row.get("fill_ts_ms")
                or row.get("computed_at_ms")
                or 0
            )
            event_date = datetime.fromtimestamp(
                ts_ms / 1000.0, tz=timezone.utc
            ).date().isoformat()
            by_date.setdefault(event_date, []).append(row)
        written: list[Path] = []
        for event_date, batch in by_date.items():
            path = theta_trades_jsonl_path(self.data_root, event_date)
            with path.open("a", encoding="utf-8") as fh:
                for rec in batch:
                    line = json.dumps(
                        dict(rec), separators=(",", ":"), ensure_ascii=False
                    )
                    fh.write(line)
                    fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            written.append(path)
        return written


class ThetaTradeManager:
    """K=1 would_send manager hooked from the theta emit loop.

    When ``live_send`` is True, open/close calls ``place_fn`` immediately
    (Contour B dual-leg). The 70 ms sleep is would_send-only.
    """

    def __init__(
        self,
        *,
        data_root: Path,
        config: Optional[ThetaTradeConfig] = None,
        journal: Optional[ThetaTradeJournalWriter] = None,
        log: Optional[LogFn] = None,
        sleep_fn: Optional[Callable[[float], Any]] = None,
        live_send: bool = False,
        place_fn: Optional[PlaceFn] = None,
        meta_fn: Optional[MetaFn] = None,
        decide_fn: Optional[Callable[..., Any]] = None,
        execution_mode: str = "inline",
        pre_send_guard_fn: Optional[Callable[..., Optional[str]]] = None,
        entry_allowed_fn: Optional[Callable[[], bool]] = None,
        state_change_fn: Optional[Callable[[], None]] = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.config = config or ThetaTradeConfig.from_env()
        self.journal = journal or ThetaTradeJournalWriter(self.data_root)
        self._log = log or (lambda _m: None)
        # sleep_fn(seconds) — sync sleep; async runtime wraps with asyncio.sleep.
        self._sleep_fn = sleep_fn or time.sleep
        self.live_send = bool(live_send)
        self._place_fn = place_fn
        self._meta_fn = meta_fn
        self._pre_send_guard_fn = pre_send_guard_fn
        self._entry_allowed_fn = entry_allowed_fn
        self._state_change_fn = state_change_fn
        self.last_policy_status: dict[str, Any] = {}
        self.execution_mode = str(execution_mode).strip().lower()
        if self.execution_mode not in {"inline", "terminal_private"}:
            raise ValueError("execution_mode must be inline|terminal_private")
        if self.execution_mode == "terminal_private" and (
            self._place_fn is None or self._meta_fn is None
        ):
            raise ValueError("terminal_private requires place_fn and meta_fn")
        self.execution_halt_reason: Optional[str] = None
        self._terminal_place_task: Any = None
        # Policy and execution routing are independent; None retains gear22.
        self._decide_fn = decide_fn
        if self.live_send and (self._place_fn is None or self._meta_fn is None):
            raise ThetaLiveSendError(
                "theta live send requires place_fn and meta_fn (fail closed)"
            )
        self.slot = SlotState(k=int(self.config.slot_k))
        self._skip_log_budget = 0
        position, pending, coin, side = restore_synthetic_slot(
            self.data_root, notional_usdt=float(self.config.notional_usdt)
        )
        self.slot.position = position
        self.slot.pending = pending
        if pending or position is not None:
            self._log(
                "theta_trade_slot_restored | pending=%s | coin=%s | side=%s"
                % (str(pending).lower(), coin, side)
            )

    def _fee_round_trip_pp(self) -> float:
        """Same round-trip fee ``potential_pp`` subtracts (frozen default 0.30)."""
        params = self.config.policy_params
        if params is None:
            return float(DEFAULT_OBSERVE_PARAMS.fee_round_trip_pp)
        return float(params.fee_round_trip_pp)

    def abort_coin_if_held(self, coin: str, *, reason: str = "hot_drop") -> bool:
        """Clear K=1 slot when it holds/pends ``coin`` (hot-drop tear-down)."""
        coin_u = str(coin).strip().upper()
        if not coin_u:
            return False
        pos = self.slot.position
        held = pos is not None and str(pos.base_coin).upper() == coin_u
        if not held:
            return False
        self.slot.position = None
        self.slot.pending = False
        self._log(
            f"theta_trade_abort_coin | base_coin={coin_u} | reason={reason}"
        )
        return True

    def _books_for(self, quotes: Mapping[str, Any], coin: str) -> tuple[dict, dict]:
        books = quotes.get(coin) or {}
        return dict(books.get("okx") or {}), dict(books.get("bybit") or {})

    def _metrics_from_snaps(
        self,
        snapshots: Sequence[ThetaSnapshot],
        coin: str,
        side: str,
    ) -> dict[str, Any]:
        by_key = {(s.base_coin, s.side): s for s in snapshots}
        own = by_key.get((coin, side))
        opp = by_key.get((coin, opposite_side(side)))
        return {
            "theta_1m": own.theta_1m if own else None,
            "theta_5m": own.theta_5m if own else None,
            "floor": own.floor_tf_select_a25 if own else None,
            "p50_1m": own.p50_1m if own else None,
            "p50_5m": own.p50_5m if own else None,
            "opposite_theta_1m": opp.theta_1m if opp else None,
        }

    def build_event_row(
        self,
        *,
        trade_id: str,
        base_coin: str,
        side: str,
        event: str,
        reason: str,
        signal_ts_ms: int,
        fill_ts_ms: int,
        snapshots: Sequence[ThetaSnapshot],
        signal_okx: Mapping[str, Any],
        signal_bybit: Mapping[str, Any],
        fill_okx: Mapping[str, Any],
        fill_bybit: Mapping[str, Any],
        signal_size: Mapping[str, Any],
        fill_size: Mapping[str, Any],
        pnl_fields: Optional[Mapping[str, Any]] = None,
        reject_reason: Optional[str] = None,
        policy_decision: Optional[PolicyDecision] = None,
        would_send: bool = True,
        send: bool = False,
        intent_id: Optional[str] = None,
        live_abort: Optional[str] = None,
    ) -> dict[str, Any]:
        metrics = self._metrics_from_snaps(snapshots, base_coin, side)
        # Spreads for the legs we execute at this event.
        trade_side = side if event == "open" else opposite_side(side)
        spread_signal = spread_for_side(signal_okx, signal_bybit, trade_side)
        spread_fill = spread_for_side(fill_okx, fill_bybit, trade_side)
        spread_side = spread_side_for(side, event=event)
        okx_leg, bybit_leg = legs_for_spread_side(spread_side)
        slip = slip_spread(signal_spread=spread_signal, fill_spread=spread_fill)
        okx_slip = slip_leg_bps(
            signal_book=signal_okx, fill_book=fill_okx, leg_side=okx_leg
        )
        bybit_slip = slip_leg_bps(
            signal_book=signal_bybit, fill_book=fill_bybit, leg_side=bybit_leg
        )
        latency_ms = int(fill_ts_ms) - int(signal_ts_ms)
        row: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "trade_id": trade_id,
            "base_coin": str(base_coin).upper(),
            "side": side,
            "event": event,
            "reason": reason,
            "would_send": bool(would_send),
            "send": bool(send),
            "intent_id": intent_id or trade_id,
            "live_send": bool(self.live_send),
            "fill_model": "venue_ack" if self.live_send else "synthetic_delay",
            "signal_ts_ms": int(signal_ts_ms),
            "fill_ts_ms": int(fill_ts_ms),
            "latency_ms": int(latency_ms),
            "fill_delay_ms_cfg": int(self.config.fill_delay_ms),
            "theta_thr": float(self.config.theta_thr),
            "slot_k": int(self.config.slot_k),
            "theta_1m": metrics["theta_1m"],
            "theta_5m": metrics["theta_5m"],
            "floor": metrics["floor"],
            "p50_1m": metrics["p50_1m"],
            "p50_5m": metrics["p50_5m"],
            "opposite_theta_1m": metrics["opposite_theta_1m"],
            "leg_buy_ex": signal_size.get("leg_buy_ex"),
            "leg_sell_ex": signal_size.get("leg_sell_ex"),
            "okx_leg_side": okx_leg,
            "bybit_leg_side": bybit_leg,
            "spread_signal": spread_signal,
            "spread_fill": spread_fill,
            "slip_spread": slip,
            "slip_leg_bps": {
                "okx": okx_slip,
                "bybit": bybit_slip,
            },
            "notional_usdt": float(self.config.notional_usdt),
            "signal_size_ok": bool(signal_size.get("size_ok")),
            "fill_size_ok": bool(fill_size.get("size_ok")),
            "signal_okx_available_size": signal_size.get("okx_available_size"),
            "signal_bybit_available_size": signal_size.get("bybit_available_size"),
            "fill_okx_available_size": fill_size.get("okx_available_size"),
            "fill_bybit_available_size": fill_size.get("bybit_available_size"),
            "signal_okx_planned_qty": signal_size.get("okx_planned_qty"),
            "signal_bybit_planned_qty": signal_size.get("bybit_planned_qty"),
            "book_signal": {
                "okx": snapshot_book(signal_okx),
                "bybit": snapshot_book(signal_bybit),
            },
            "book_fill": {
                "okx": snapshot_book(fill_okx),
                "bybit": snapshot_book(fill_bybit),
            },
            "reject_reason": reject_reason,
            "computed_at_ms": int(time.time() * 1000),
        }
        # Full bid/ask (+size) that figure in the spread (flat convenience fields).
        for exch, book, prefix in (
            ("okx", signal_okx, "signal_okx"),
            ("bybit", signal_bybit, "signal_bybit"),
            ("okx", fill_okx, "fill_okx"),
            ("bybit", fill_bybit, "fill_bybit"),
        ):
            snap = snapshot_book(book)
            row[f"{prefix}_bid_price"] = snap["bid_price"]
            row[f"{prefix}_bid_size"] = snap["bid_size"]
            row[f"{prefix}_ask_price"] = snap["ask_price"]
            row[f"{prefix}_ask_size"] = snap["ask_size"]
        if pnl_fields:
            row.update(dict(pnl_fields))
        if live_abort:
            row["live_abort"] = live_abort
        if policy_decision is not None:
            row["policy_id"] = POLICY_ID
            row["policy_action"] = getattr(policy_decision, "action", None)
            row["policy_reason"] = getattr(policy_decision, "reason", None)
        return row

    def execute_decision(
        self,
        decision: ThetaDecision,
        *,
        snapshots: Sequence[ThetaSnapshot],
        quotes: Mapping[str, Mapping[str, Mapping[str, Any]]],
        now_ms: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Apply decide + fill delay; return journal rows (0–1).

        Signal insufficient size on open or close → skip row with
        ``reject_reason=insufficient_size`` and ``would_send=false``. A close
        reject leaves the slot occupied. Signal OK but fill size bad → still
        journal the fill with ``fill_size_ok=false``.
        """
        if decision.action == "skip":
            if decision.reject_reason == "insufficient_size":
                self._log(
                    "theta_trade_reject | reason=insufficient_size | "
                    f"coin={decision.base_coin} | side={decision.side} | "
                    f"size={decision.size_info}"
                )
                # Log a skip audit row (not an open/close trade).
                signal_ts = int(now_ms if now_ms is not None else time.time() * 1000)
                okx, bybit = self._books_for(quotes, decision.base_coin)
                size_event = insufficient_size_event(
                    decision, held=self.slot.position
                )
                size_info = decision.size_info or size_check(
                    okx=okx,
                    bybit=bybit,
                    side=decision.side,
                    event=size_event,
                    notional_usdt=self.config.notional_usdt,
                    book_depth=self.config.book_depth,
                )
                row = {
                    "schema_version": SCHEMA_VERSION,
                    "trade_id": None,
                    "base_coin": decision.base_coin,
                    "side": decision.side,
                    "event": "skip",
                    "reason": "reject",
                    "reject_reason": "insufficient_size",
                    "size_event": size_event,
                    "would_send": False,
                    "send": False,
                    "signal_ts_ms": signal_ts,
                    "fill_ts_ms": None,
                    "latency_ms": None,
                    "theta_1m": decision.theta_1m,
                    "opposite_theta_1m": decision.opposite_theta_1m,
                    "notional_usdt": float(self.config.notional_usdt),
                    "signal_size_ok": False,
                    "signal_okx_available_size": size_info.get("okx_available_size"),
                    "signal_bybit_available_size": size_info.get("bybit_available_size"),
                    "okx_planned_qty": size_info.get("okx_planned_qty"),
                    "bybit_planned_qty": size_info.get("bybit_planned_qty"),
                    "book_signal": {
                        "okx": snapshot_book(okx),
                        "bybit": snapshot_book(bybit),
                    },
                    "computed_at_ms": signal_ts,
                }
                self.journal.append_rows([row])
                return [row]
            if decision.reason == "slot_busy":
                self.slot.skip_counts["slot_busy"] = (
                    self.slot.skip_counts.get("slot_busy", 0) + 1
                )
                self._skip_log_budget += 1
                if self._skip_log_budget % 10 == 1:
                    self._log(
                        f"theta_trade_skip | reason=slot_busy | "
                        f"n={self.slot.skip_counts['slot_busy']}"
                    )
            return []

        if decision.action not in ("open", "close"):
            return []
        if self.execution_mode == "terminal_private":
            raise RuntimeError("terminal_private execution requires async entry")

        if (
            self._decide_fn is not None and self._slot_blocks(decision)
        ):
            return []

        signal_ts = int(now_ms if now_ms is not None else time.time() * 1000)
        okx_s, bybit_s = self._books_for(quotes, decision.base_coin)
        signal_size = decision.size_info or size_check(
            okx=okx_s,
            bybit=bybit_s,
            side=decision.side,
            event=decision.action,
            notional_usdt=self.config.notional_usdt,
            book_depth=self.config.book_depth,
        )
        if not signal_size.get("size_ok"):
            # Belt-and-suspenders (decide already gated) for open and close.
            decision = ThetaDecision(
                action="skip",
                base_coin=decision.base_coin,
                side=decision.side,
                reason="reject",
                theta_1m=decision.theta_1m,
                opposite_theta_1m=decision.opposite_theta_1m,
                reject_reason="insufficient_size",
                size_info=signal_size,
                policy_decision=decision.policy_decision,
                size_event=decision.action,
            )
            return self.execute_decision(
                decision, snapshots=snapshots, quotes=quotes, now_ms=signal_ts
            )

        if self._decide_fn is not None:
            return self._execute_injected_place(
                decision,
                signal_ts=signal_ts,
                okx_s=okx_s,
                bybit_s=bybit_s,
            )

        if self.live_send:
            return self._execute_live_send(
                decision,
                snapshots=snapshots,
                quotes=quotes,
                signal_ts=signal_ts,
                okx_s=okx_s,
                bybit_s=bybit_s,
                signal_size=signal_size,
            )

        self.slot.pending = True
        try:
            delay_s = max(0.0, float(self.config.fill_delay_ms) / 1000.0)
            if delay_s > 0:
                self._sleep_fn(delay_s)
            fill_ts = signal_ts + int(self.config.fill_delay_ms)
            okx_f, bybit_f = self._books_for(quotes, decision.base_coin)
            fill_size = size_check(
                okx=okx_f,
                bybit=bybit_f,
                side=decision.side,
                event=decision.action,
                notional_usdt=self.config.notional_usdt,
                book_depth=self.config.book_depth,
            )
            pnl_fields: dict[str, Any] = {}
            if decision.action == "open":
                trade_id = str(uuid.uuid4())
                fill_spread = spread_for_side(
                    okx_f, bybit_f, decision.side
                )
                row = self.build_event_row(
                    trade_id=trade_id,
                    base_coin=decision.base_coin,
                    side=decision.side,
                    event="open",
                    reason=decision.reason,
                    signal_ts_ms=signal_ts,
                    fill_ts_ms=fill_ts,
                    snapshots=snapshots,
                    signal_okx=okx_s,
                    signal_bybit=bybit_s,
                    fill_okx=okx_f,
                    fill_bybit=bybit_f,
                    signal_size=signal_size,
                    fill_size=fill_size,
                    policy_decision=decision.policy_decision,
                )
                self.slot.position = OpenPosition(
                    trade_id=trade_id,
                    base_coin=decision.base_coin,
                    side=decision.side,
                    open_signal_ts_ms=signal_ts,
                    open_fill_ts_ms=fill_ts,
                    open_fill_spread=fill_spread,
                    open_notional=float(self.config.notional_usdt),
                    open_theta_1m=decision.theta_1m,
                    fill_spread_pp=fill_spread,
                )
            else:
                pos = self.slot.position
                if pos is None:
                    return []
                trade_id = pos.trade_id
                close_spread = spread_for_side(
                    okx_f, bybit_f, opposite_side(pos.side)
                )
                open_spread = pos.open_fill_spread
                # Gear 2.2: open + opposite close − fee. close_spread is
                # already the unwind side; open − close flips a negative close.
                pnl_spread = journal_close_pnl_spread(
                    open_spread, close_spread, self._fee_round_trip_pp()
                )
                pnl_fields = {
                    "open_fill_spread": open_spread,
                    "close_fill_spread": close_spread,
                    "pnl_spread": pnl_spread,
                    "pnl_usdt_approx": (
                        float(pnl_spread) / 100.0 * float(pos.open_notional)
                        if pnl_spread is not None
                        else None
                    ),
                    "open_signal_ts_ms": pos.open_signal_ts_ms,
                    "open_fill_ts_ms": pos.open_fill_ts_ms,
                    "potential_pp": decision.potential_pp,
                    "policy_id": POLICY_ID,
                }
                row = self.build_event_row(
                    trade_id=trade_id,
                    base_coin=pos.base_coin,
                    side=pos.side,
                    event="close",
                    reason=decision.reason,
                    signal_ts_ms=signal_ts,
                    fill_ts_ms=fill_ts,
                    snapshots=snapshots,
                    signal_okx=okx_s,
                    signal_bybit=bybit_s,
                    fill_okx=okx_f,
                    fill_bybit=bybit_f,
                    signal_size=signal_size,
                    fill_size=fill_size,
                    pnl_fields=pnl_fields,
                    policy_decision=decision.policy_decision,
                )
                self.slot.position = None
            self.journal.append_rows([row])
            self._log(
                f"theta_trade_{decision.action} | trade_id={row['trade_id']} | "
                f"coin={row['base_coin']} | side={row['side']} | "
                f"fill_size_ok={row.get('fill_size_ok')} | "
                f"slip_spread={row.get('slip_spread')}"
            )
            
            # Emit to Sentry (trade lifecycle events).
            sentry_extras = {
                "signal_ts_ms": row.get("signal_ts_ms"),
                "fill_ts_ms": row.get("fill_ts_ms"),
                "latency_ms": row.get("latency_ms"),
                "spread_signal": row.get("spread_signal"),
                "spread_fill": row.get("spread_fill"),
                "slip_spread": row.get("slip_spread"),
                "theta_1m": row.get("theta_1m"),
                "theta_5m": row.get("theta_5m"),
                "floor": row.get("floor"),
                "p50_1m": row.get("p50_1m"),
                "signal_size_ok": row.get("signal_size_ok"),
                "fill_size_ok": row.get("fill_size_ok"),
            }
            if decision.action == "close":
                sentry_extras.update({
                    "pnl_spread": row.get("pnl_spread"),
                    "pnl_usdt_approx": row.get("pnl_usdt_approx"),
                    "open_fill_spread": row.get("open_fill_spread"),
                    "close_fill_spread": row.get("close_fill_spread"),
                })
            
            capture_trade_event(
                event=decision.action,
                trade_id=str(row["trade_id"]),
                coin=str(row["base_coin"]),
                side=str(row["side"]),
                extras=sentry_extras,
            )
            
            return [row]
        finally:
            self.slot.pending = False

    def _execute_live_send(
        self,
        decision: ThetaDecision,
        *,
        snapshots: Sequence[ThetaSnapshot],
        quotes: Mapping[str, Mapping[str, Mapping[str, Any]]],
        signal_ts: int,
        okx_s: Mapping[str, Any],
        bybit_s: Mapping[str, Any],
        signal_size: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Place Contour B dual-leg immediately at signal. No 70 ms sleep.

        ACK-aware open/flatten is owned by ``place_fn`` (LiveBroker). A
        non-None abort keeps the theta slot flat on open and occupied on
        a failed close. insufficient_size never reaches this path.
        """
        if self._place_fn is None or self._meta_fn is None:
            raise ThetaLiveSendError(
                "theta live send requires place_fn and meta_fn (fail closed)"
            )
        if decision.action not in ("open", "close"):
            return []

        if decision.action == "open":
            trade_id = str(uuid.uuid4())
            intent_id = trade_id
            coin = decision.base_coin
            side = decision.side
            spread_side = spread_side_for(side, event="open")
            close_of: Optional[str] = None
        else:
            pos = self.slot.position
            if pos is None:
                return []
            trade_id = pos.trade_id
            intent_id = str(uuid.uuid4())
            coin = pos.base_coin
            side = pos.side
            spread_side = "close"
            close_of = "open_long" if side == "long" else "open_short"

        try:
            meta = self._meta_fn(str(coin).upper())
        except Exception as exc:  # noqa: BLE001
            from app.bot.sentry_setup import capture_exception

            capture_exception(
                exc,
                extras={"trade_id": trade_id, "coin": coin, "event": decision.action},
            )
            raise ThetaLiveSendError(
                f"theta live send meta lookup failed for {coin}: {type(exc).__name__}"
            ) from exc

        extra = {
            "trade_id": trade_id,
            "intent_id": intent_id,
            "theta_live_canary": True,
            "policy_id": POLICY_ID,
        }
        self.slot.pending = True
        abort: Optional[str] = None
        try:
            try:
                abort = self._place_fn(
                    spread_side=spread_side,
                    base_coin=str(coin).upper(),
                    signal_ts_ms=int(signal_ts),
                    okx_book=dict(okx_s),
                    bybit_book=dict(bybit_s),
                    meta=meta,
                    close_of=close_of,
                    extra=extra,
                    intent_id=intent_id,
                )
            except TypeError as exc:
                if "intent_id" not in str(exc):
                    from app.bot.sentry_setup import capture_exception

                    capture_exception(
                        exc,
                        extras={
                            "trade_id": trade_id,
                            "intent_id": intent_id,
                            "coin": coin,
                            "event": decision.action,
                        },
                    )
                    abort = f"place_raised:{type(exc).__name__}"
                else:
                    abort = self._place_fn(
                        spread_side=spread_side,
                        base_coin=str(coin).upper(),
                        signal_ts_ms=int(signal_ts),
                        okx_book=dict(okx_s),
                        bybit_book=dict(bybit_s),
                        meta=meta,
                        close_of=close_of,
                        extra=extra,
                    )
            except Exception as exc:  # noqa: BLE001
                from app.bot.sentry_setup import capture_exception

                capture_exception(
                    exc,
                    extras={
                        "trade_id": trade_id,
                        "intent_id": intent_id,
                        "coin": coin,
                        "event": decision.action,
                    },
                )
                abort = f"place_raised:{type(exc).__name__}"

            fill_ts = int(time.time() * 1000)
            okx_f, bybit_f = self._books_for(quotes, coin)
            fill_size = size_check(
                okx=okx_f,
                bybit=bybit_f,
                side=side,
                event=decision.action,
                notional_usdt=self.config.notional_usdt,
                book_depth=self.config.book_depth,
            )
            sent_ok = abort is None
            pnl_fields: dict[str, Any] = {}
            if decision.action == "close":
                pos = self.slot.position
                close_spread = spread_for_side(okx_f, bybit_f, opposite_side(side))
                open_spread = pos.open_fill_spread if pos is not None else None
                # Same formula as would_send (open + opposite close − fee).
                pnl_spread = journal_close_pnl_spread(
                    open_spread, close_spread, self._fee_round_trip_pp()
                )
                pnl_fields = {
                    "open_fill_spread": open_spread,
                    "close_fill_spread": close_spread,
                    "pnl_spread": pnl_spread,
                    "pnl_usdt_approx": (
                        float(pnl_spread) / 100.0 * float(pos.open_notional)
                        if pnl_spread is not None and pos is not None
                        else None
                    ),
                    "open_signal_ts_ms": pos.open_signal_ts_ms if pos is not None else None,
                    "open_fill_ts_ms": pos.open_fill_ts_ms if pos is not None else None,
                    "potential_pp": decision.potential_pp,
                    "policy_id": POLICY_ID,
                    "close_intent_id": intent_id,
                }

            row = self.build_event_row(
                trade_id=trade_id,
                base_coin=coin,
                side=side,
                event=decision.action,
                reason=decision.reason if sent_ok else "live_abort",
                signal_ts_ms=signal_ts,
                fill_ts_ms=fill_ts,
                snapshots=snapshots,
                signal_okx=okx_s,
                signal_bybit=bybit_s,
                fill_okx=okx_f,
                fill_bybit=bybit_f,
                signal_size=signal_size,
                fill_size=fill_size,
                pnl_fields=pnl_fields or None,
                reject_reason=None if sent_ok else str(abort),
                policy_decision=decision.policy_decision,
                would_send=True,
                send=bool(sent_ok),
                intent_id=intent_id,
                live_abort=None if sent_ok else str(abort),
            )
            if sent_ok and decision.action == "open":
                fill_spread = spread_for_side(okx_f, bybit_f, side)
                self.slot.position = OpenPosition(
                    trade_id=trade_id,
                    base_coin=str(coin).upper(),
                    side=side,
                    open_signal_ts_ms=signal_ts,
                    open_fill_ts_ms=fill_ts,
                    open_fill_spread=fill_spread,
                    open_notional=float(self.config.notional_usdt),
                    open_theta_1m=decision.theta_1m,
                    fill_spread_pp=fill_spread,
                )
            elif sent_ok and decision.action == "close":
                self.slot.position = None

            self.journal.append_rows([row])
            self._log(
                f"theta_trade_{decision.action} | trade_id={row['trade_id']} | "
                f"intent_id={intent_id} | coin={row['base_coin']} | "
                f"side={row['side']} | send={row.get('send')} | "
                f"live_abort={row.get('live_abort')}"
            )
            sentry_extras: dict[str, Any] = {
                "signal_ts_ms": row.get("signal_ts_ms"),
                "fill_ts_ms": row.get("fill_ts_ms"),
                "latency_ms": row.get("latency_ms"),
                "spread_signal": row.get("spread_signal"),
                "spread_fill": row.get("spread_fill"),
                "slip_spread": row.get("slip_spread"),
                "theta_1m": row.get("theta_1m"),
                "intent_id": intent_id,
                "send": row.get("send"),
                "live_abort": row.get("live_abort"),
                "notional_usdt": row.get("notional_usdt"),
            }
            if decision.action == "close":
                sentry_extras.update({
                    "pnl_spread": row.get("pnl_spread"),
                    "pnl_usdt_approx": row.get("pnl_usdt_approx"),
                })
            capture_trade_event(
                event=decision.action if sent_ok else "send_abort",
                trade_id=str(trade_id),
                coin=str(row["base_coin"]),
                side=str(row["side"]),
                extras=sentry_extras,
            )
            return [row]
        finally:
            self.slot.pending = False

    def _slot_blocks(self, decision: ThetaDecision) -> bool:
        """K=1: no second open while busy/pending, no close while flat or pending."""
        if decision.action == "open" and self.slot.slot_busy():
            return True
        if decision.action == "close" and (
            self.slot.position is None or self.slot.pending
        ):
            return True
        return False

    def _decide(
        self,
        snapshots: Sequence[ThetaSnapshot],
        *,
        quotes: Mapping[str, Mapping[str, Mapping[str, Any]]],
        coin_order: Optional[Sequence[str]],
        ts_s: int,
    ) -> ThetaDecision:
        if self._decide_fn is None:
            return decide_theta_k1(
                snapshots,
                slot=self.slot,
                thr=self.config.theta_thr,
                quotes=quotes,
                notional_usdt=self.config.notional_usdt,
                book_depth=self.config.book_depth,
                coin_order=coin_order,
                policy_params=self.config.policy_params,
                ts_s=ts_s,
            )
        raw = self._decide_fn(
            snapshots=snapshots,
            slot=self.slot,
            quotes=quotes,
            coin_order=coin_order,
            notional_usdt=float(self.config.notional_usdt),
            book_depth=int(self.config.book_depth),
            thr=float(self.config.theta_thr),
            policy_params=self.config.policy_params,
            ts_s=int(ts_s),
        )
        return coerce_external_decision(raw)

    def _execute_injected_place(
        self,
        decision: ThetaDecision,
        *,
        signal_ts: int,
        signal_mono_ns: Optional[int] = None,
        okx_s: Mapping[str, Any],
        bybit_s: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Call the injected place_fn. Journal rows belong to that function.

        A partial fill (``keep_pending``) leaves the slot pending and does not
        clear or open the position. Size rejects never reach here.
        """
        if self._place_fn is None or decision.action not in ("open", "close"):
            return []
        if decision.action == "open":
            trade_id = str(uuid.uuid4())
            intent_id = trade_id
            coin = decision.base_coin
            side = decision.side
            spread_side = spread_side_for(side, event="open")
            close_of: Optional[str] = None
            close_qty: Optional[dict[str, str]] = None
        else:
            pos = self.slot.position
            if pos is None:
                return []
            trade_id = pos.trade_id
            intent_id = str(uuid.uuid4())
            coin = pos.base_coin
            side = pos.side
            spread_side = "close"
            close_of = "open_long" if side == "long" else "open_short"
            close_qty = None
            if pos.okx_filled_qty is not None and pos.bybit_filled_qty is not None:
                close_qty = {
                    "okx_filled_qty": pos.okx_filled_qty,
                    "bybit_filled_qty": pos.bybit_filled_qty,
                }

        meta = None
        if self._meta_fn is not None:
            try:
                meta = self._meta_fn(str(coin).upper())
            except Exception as exc:  # noqa: BLE001
                from app.bot.sentry_setup import capture_exception

                capture_exception(
                    exc,
                    extras={"trade_id": trade_id, "coin": coin, "event": decision.action},
                )
                if self.execution_mode == "terminal_private":
                    self.slot.pending = False
                return []

        if self.execution_mode == "terminal_private" and self._pre_send_guard_fn is not None:
            try:
                reason = self._pre_send_guard_fn(
                    coin=str(coin).upper(),
                    event=decision.action,
                    okx_book=dict(okx_s),
                    bybit_book=dict(bybit_s),
                )
            except Exception as exc:  # malformed/stale pre-send data means no send
                self.slot.pending = False
                self.last_policy_status = {
                    **self.last_policy_status,
                    "action": "skip",
                    "reason": "pre_send_guard_error",
                    "reject_reason": type(exc).__name__,
                    "size_event": decision.action,
                }
                self._log(f"theta_trade_pre_send_reject | reason={type(exc).__name__} | coin={coin}")
                return []
            if reason:
                self.slot.pending = False
                self.last_policy_status = {
                    **self.last_policy_status,
                    "action": "skip",
                    "reason": "pre_send_guard_reject",
                    "reject_reason": str(reason),
                    "size_event": decision.action,
                }
                self._log(f"theta_trade_pre_send_reject | reason={reason} | coin={coin}")
                return []

        extra = {
            "trade_id": trade_id,
            "intent_id": intent_id,
            "synthetic_roll": self._decide_fn is not None,
            "signal_mono_ns": signal_mono_ns,
        }
        self.slot.pending = True
        keep_pending = False
        try:
            result = self._place_fn(
                spread_side=spread_side,
                base_coin=str(coin).upper(),
                signal_ts_ms=int(signal_ts),
                okx_book=dict(okx_s),
                bybit_book=dict(bybit_s),
                meta=meta,
                close_of=close_of,
                close_qty=close_qty,
                extra=extra,
                intent_id=intent_id,
            )
            (
                _abort,
                completed,
                keep_pending,
                fill_ts,
                okx_filled_qty,
                bybit_filled_qty,
            ) = _read_place_result(result)
            if (
                self.execution_mode == "terminal_private"
                and not completed
                and bool(getattr(result, "send_attempted", False))
            ):
                keep_pending = True
                self.slot.pending = True
                self.execution_halt_reason = str(
                    _abort or getattr(result, "status", None) or "terminal_incomplete"
                )
                self._log(
                    f"theta_trade_execution_halted | reason={self.execution_halt_reason}"
                )
            if completed and decision.action == "open":
                fill_ts_i = int(fill_ts if fill_ts is not None else time.time() * 1000)
                fill_spread = spread_for_side(okx_s, bybit_s, side)
                self.slot.position = OpenPosition(
                    trade_id=trade_id,
                    base_coin=str(coin).upper(),
                    side=side,
                    open_signal_ts_ms=int(signal_ts),
                    open_fill_ts_ms=fill_ts_i,
                    open_fill_spread=fill_spread,
                    open_notional=float(self.config.notional_usdt),
                    open_theta_1m=decision.theta_1m,
                    fill_spread_pp=fill_spread,
                    okx_filled_qty=okx_filled_qty,
                    bybit_filled_qty=bybit_filled_qty,
                    coin_filled_qty=(
                        str(getattr(result, "coin_qty", None))
                        if getattr(result, "coin_qty", None) is not None
                        else None
                    ),
                )
            elif completed and decision.action == "close":
                self.slot.position = None
            return []
        finally:
            if not keep_pending:
                self.slot.pending = False

    def _schedule_terminal_private_place(
        self,
        decision: ThetaDecision,
        *,
        signal_ts: int,
        signal_mono_ns: Optional[int],
        okx_s: Mapping[str, Any],
        bybit_s: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Reserve K=1 before handing blocking terminal handling to one worker."""
        import asyncio

        if self.slot.pending or self._terminal_place_task is not None:
            return []
        self.slot.pending = True

        async def run() -> None:
            import asyncio

            worker = asyncio.create_task(
                asyncio.to_thread(
                    self._execute_injected_place,
                    decision,
                    signal_ts=signal_ts,
                    signal_mono_ns=signal_mono_ns,
                    okx_s=okx_s,
                    bybit_s=bybit_s,
                ),
                name="theta-private-place-worker",
            )
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                self._terminal_place_task = worker
                worker.add_done_callback(
                    lambda done: setattr(
                        self,
                        "_terminal_place_task",
                        None,
                    )
                    if self._terminal_place_task is done
                    else None
                )
                raise
            except Exception as exc:  # uncertain result must retain pending exposure
                self.slot.pending = True
                self.execution_halt_reason = f"execution_exception:{type(exc).__name__}"
                self._log(f"theta_trade_execution_halted | reason={self.execution_halt_reason}")
            finally:
                if self._state_change_fn is not None:
                    try:
                        self._state_change_fn()
                    except Exception as exc:
                        self.slot.pending = True
                        self.execution_halt_reason = (
                            f"state_checkpoint_failed:{type(exc).__name__}"
                        )
                        self._log(
                            "theta_trade_execution_halted | "
                            f"reason={self.execution_halt_reason}"
                        )
                if self._terminal_place_task is asyncio.current_task():
                    self._terminal_place_task = None

        self._terminal_place_task = asyncio.create_task(run(), name="theta-private-place")
        return []

    def _record_policy_status(
        self, decision: ThetaDecision, *, evaluated_at_ms: int
    ) -> ThetaDecision:
        if (
            self.execution_mode == "terminal_private"
            and decision.action == "open"
            and self._entry_allowed_fn is not None
            and not self._entry_allowed_fn()
        ):
            decision = ThetaDecision(
                action="skip",
                base_coin=decision.base_coin,
                side=decision.side,
                reason="canary_open_window_elapsed",
                theta_1m=decision.theta_1m,
                opposite_theta_1m=decision.opposite_theta_1m,
                policy_decision=decision.policy_decision,
            )
        pd = decision.policy_decision
        self.last_policy_status = {
            "evaluated_at_ms": int(evaluated_at_ms),
            "action": decision.action,
            "base_coin": decision.base_coin,
            "side": decision.side,
            "reason": decision.reason,
            "reject_reason": decision.reject_reason,
            "size_event": decision.size_event,
            "potential_pp": decision.potential_pp,
            "theta_1m": decision.theta_1m,
            "policy_action": getattr(pd, "action", None),
            "policy_reason": getattr(pd, "reason", None),
        }
        return decision

    def on_theta_snapshots(
        self,
        snapshots: Sequence[ThetaSnapshot],
        *,
        quotes: Mapping[str, Mapping[str, Mapping[str, Any]]],
        coin_order: Optional[Sequence[str]] = None,
        now_ms: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Sync entry (unit tests / offline). Prefer ``on_theta_snapshots_async`` live."""
        tick_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        decision = self._decide(
            snapshots,
            quotes=quotes,
            coin_order=coin_order,
            ts_s=tick_ms // 1000,
        )
        decision = self._record_policy_status(decision, evaluated_at_ms=tick_ms)
        return self.execute_decision(
            decision, snapshots=snapshots, quotes=quotes, now_ms=now_ms
        )

    async def on_theta_snapshots_async(
        self,
        snapshots: Sequence[ThetaSnapshot],
        *,
        quotes: Mapping[str, Mapping[str, Mapping[str, Any]]],
        coin_order: Optional[Sequence[str]] = None,
        now_ms: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Async emit-loop entry: ``fill_ts = signal_ts + BBOT_FILL_DELAY_MS``."""
        import asyncio

        tick_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        decision = self._decide(
            snapshots,
            quotes=quotes,
            coin_order=coin_order,
            ts_s=tick_ms // 1000,
        )
        decision = self._record_policy_status(decision, evaluated_at_ms=tick_ms)
        signal_ts_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        signal_mono_ns = time.monotonic_ns()

        async def _async_sleep(seconds: float) -> None:
            await asyncio.sleep(seconds)

        prev = self._sleep_fn
        self._sleep_fn = _async_sleep  # type: ignore[assignment]
        try:
            # execute_decision may call sleep_fn — support awaitable.
            return await self._execute_decision_async(
                decision,
                snapshots=snapshots,
                quotes=quotes,
                now_ms=signal_ts_ms,
                signal_mono_ns=signal_mono_ns,
            )
        finally:
            self._sleep_fn = prev

    async def _execute_decision_async(
        self,
        decision: ThetaDecision,
        *,
        snapshots: Sequence[ThetaSnapshot],
        quotes: Mapping[str, Mapping[str, Mapping[str, Any]]],
        now_ms: Optional[int] = None,
        signal_mono_ns: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Async twin of ``execute_decision`` (await fill delay)."""
        import asyncio
        from inspect import isawaitable

        if self.execution_mode == "terminal_private" and self.execution_halt_reason:
            return []
        if decision.action == "skip":
            if decision.reason == "canary_open_window_elapsed":
                return []
            return self.execute_decision(
                decision, snapshots=snapshots, quotes=quotes, now_ms=now_ms
            )
        if decision.action not in ("open", "close"):
            return []

        if (
            (self._decide_fn is not None or self.execution_mode == "terminal_private")
            and self._slot_blocks(decision)
        ):
            return []

        signal_ts = int(now_ms if now_ms is not None else time.time() * 1000)
        okx_s, bybit_s = self._books_for(quotes, decision.base_coin)
        if self.execution_mode == "terminal_private" and decision.action == "close":
            pos = self.slot.position
            signal_size = size_check(
                okx=okx_s,
                bybit=bybit_s,
                side=decision.side,
                event="close",
                notional_usdt=self.config.notional_usdt,
                book_depth=self.config.book_depth,
                okx_ct_val=okx_s.get("ct_val"),
                required_okx_contracts=(pos.okx_filled_qty if pos else None),
                required_bybit_qty=(pos.bybit_filled_qty if pos else None),
            )
        else:
            signal_size = decision.size_info or size_check(
                okx=okx_s,
                bybit=bybit_s,
                side=decision.side,
                event=decision.action,
                notional_usdt=self.config.notional_usdt,
                book_depth=self.config.book_depth,
            )
        if not signal_size.get("size_ok"):
            decision = ThetaDecision(
                action="skip",
                base_coin=decision.base_coin,
                side=decision.side,
                reason="reject",
                theta_1m=decision.theta_1m,
                opposite_theta_1m=decision.opposite_theta_1m,
                reject_reason="insufficient_size",
                size_info=signal_size,
                policy_decision=decision.policy_decision,
                size_event=decision.action,
            )
            self._record_policy_status(decision, evaluated_at_ms=signal_ts)
            return self.execute_decision(
                decision, snapshots=snapshots, quotes=quotes, now_ms=signal_ts
            )

        if self.execution_mode == "terminal_private":
            return self._schedule_terminal_private_place(
                decision,
                signal_ts=signal_ts,
                signal_mono_ns=signal_mono_ns,
                okx_s=okx_s,
                bybit_s=bybit_s,
            )

        if self._decide_fn is not None:
            return self._execute_injected_place(
                decision,
                signal_ts=signal_ts,
                signal_mono_ns=signal_mono_ns,
                okx_s=okx_s,
                bybit_s=bybit_s,
            )

        if self.live_send:
            return self._execute_live_send(
                decision,
                snapshots=snapshots,
                quotes=quotes,
                signal_ts=signal_ts,
                okx_s=okx_s,
                bybit_s=bybit_s,
                signal_size=signal_size,
            )

        self.slot.pending = True
        try:
            delay_s = max(0.0, float(self.config.fill_delay_ms) / 1000.0)
            if delay_s > 0:
                awaited = self._sleep_fn(delay_s)
                if isawaitable(awaited):
                    await awaited
            fill_ts = signal_ts + int(self.config.fill_delay_ms)
            okx_f, bybit_f = self._books_for(quotes, decision.base_coin)
            fill_size = size_check(
                okx=okx_f,
                bybit=bybit_f,
                side=decision.side,
                event=decision.action,
                notional_usdt=self.config.notional_usdt,
                book_depth=self.config.book_depth,
            )
            pnl_fields: dict[str, Any] = {}
            if decision.action == "open":
                trade_id = str(uuid.uuid4())
                fill_spread = spread_for_side(okx_f, bybit_f, decision.side)
                row = self.build_event_row(
                    trade_id=trade_id,
                    base_coin=decision.base_coin,
                    side=decision.side,
                    event="open",
                    reason=decision.reason,
                    signal_ts_ms=signal_ts,
                    fill_ts_ms=fill_ts,
                    snapshots=snapshots,
                    signal_okx=okx_s,
                    signal_bybit=bybit_s,
                    fill_okx=okx_f,
                    fill_bybit=bybit_f,
                    signal_size=signal_size,
                    fill_size=fill_size,
                    policy_decision=decision.policy_decision,
                )
                self.slot.position = OpenPosition(
                    trade_id=trade_id,
                    base_coin=decision.base_coin,
                    side=decision.side,
                    open_signal_ts_ms=signal_ts,
                    open_fill_ts_ms=fill_ts,
                    open_fill_spread=fill_spread,
                    open_notional=float(self.config.notional_usdt),
                    open_theta_1m=decision.theta_1m,
                    fill_spread_pp=fill_spread,
                )
            else:
                pos = self.slot.position
                if pos is None:
                    return []
                trade_id = pos.trade_id
                close_spread = spread_for_side(
                    okx_f, bybit_f, opposite_side(pos.side)
                )
                open_spread = pos.open_fill_spread
                # Same formula as the sync would_send close (open + opposite − fee).
                pnl_spread = journal_close_pnl_spread(
                    open_spread, close_spread, self._fee_round_trip_pp()
                )
                pnl_fields = {
                    "open_fill_spread": open_spread,
                    "close_fill_spread": close_spread,
                    "pnl_spread": pnl_spread,
                    "pnl_usdt_approx": (
                        float(pnl_spread) / 100.0 * float(pos.open_notional)
                        if pnl_spread is not None
                        else None
                    ),
                    "open_signal_ts_ms": pos.open_signal_ts_ms,
                    "open_fill_ts_ms": pos.open_fill_ts_ms,
                    "potential_pp": decision.potential_pp,
                    "policy_id": POLICY_ID,
                }
                row = self.build_event_row(
                    trade_id=trade_id,
                    base_coin=pos.base_coin,
                    side=pos.side,
                    event="close",
                    reason=decision.reason,
                    signal_ts_ms=signal_ts,
                    fill_ts_ms=fill_ts,
                    snapshots=snapshots,
                    signal_okx=okx_s,
                    signal_bybit=bybit_s,
                    fill_okx=okx_f,
                    fill_bybit=bybit_f,
                    signal_size=signal_size,
                    fill_size=fill_size,
                    pnl_fields=pnl_fields,
                    policy_decision=decision.policy_decision,
                )
                self.slot.position = None
            await asyncio.to_thread(self.journal.append_rows, [row])
            self._log(
                f"theta_trade_{decision.action} | trade_id={row['trade_id']} | "
                f"coin={row['base_coin']} | side={row['side']} | "
                f"fill_size_ok={row.get('fill_size_ok')} | "
                f"slip_spread={row.get('slip_spread')}"
            )
            
            # Emit to Sentry (trade lifecycle events).
            sentry_extras = {
                "signal_ts_ms": row.get("signal_ts_ms"),
                "fill_ts_ms": row.get("fill_ts_ms"),
                "latency_ms": row.get("latency_ms"),
                "spread_signal": row.get("spread_signal"),
                "spread_fill": row.get("spread_fill"),
                "slip_spread": row.get("slip_spread"),
                "theta_1m": row.get("theta_1m"),
                "theta_5m": row.get("theta_5m"),
                "floor": row.get("floor"),
                "p50_1m": row.get("p50_1m"),
                "signal_size_ok": row.get("signal_size_ok"),
                "fill_size_ok": row.get("fill_size_ok"),
            }
            if decision.action == "close":
                sentry_extras.update({
                    "pnl_spread": row.get("pnl_spread"),
                    "pnl_usdt_approx": row.get("pnl_usdt_approx"),
                    "open_fill_spread": row.get("open_fill_spread"),
                    "close_fill_spread": row.get("close_fill_spread"),
                })
            
            capture_trade_event(
                event=decision.action,
                trade_id=str(row["trade_id"]),
                coin=str(row["base_coin"]),
                side=str(row["side"]),
                extras=sentry_extras,
            )
            
            return [row]
        finally:
            self.slot.pending = False
