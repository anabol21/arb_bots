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
    SYNTHETIC_CLOSE_ROLL,
    SYNTHETIC_OPEN_ROLL,
    SYNTHETIC_ROLL_POLICY_ID,
    decide as policy_decide,
    potential_profit_pp,
    synthetic_roll_enabled,
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
DEFAULT_LIVE_CANARY_NOTIONAL_USDT = 10.0
DEFAULT_BOOK_DEPTH = 1
POLICY_ID = "gear22_frozen_v1"
SYNTHETIC_POLICY_MODE = "synthetic_roll_v1"
DEFAULT_SYNTHETIC_ROLL_SEED = 20260921

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


class ThetaTradeRecoveryError(RuntimeError):
    """Durable trade history cannot be replayed into one unambiguous K=1 slot."""


class SyntheticPolicyGateError(RuntimeError):
    """Synthetic signal policy must remain structurally unable to send orders."""


PlaceFn = Callable[..., Optional[str]]
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
    raw_notional = str(e.get("BBOT_NOTIONAL_USDT") or DEFAULT_LIVE_CANARY_NOTIONAL_USDT)
    try:
        notional = float(raw_notional)
    except ValueError as exc:
        raise ThetaLiveSendError("invalid theta live notional") from exc
    if not math.isfinite(notional) or not 0 < notional <= DEFAULT_LIVE_CANARY_NOTIONAL_USDT:
        raise ThetaLiveSendError(
            f"theta live notional must be >0 and <= {DEFAULT_LIVE_CANARY_NOTIONAL_USDT:g} USDT per leg"
        )


def synthetic_policy_requested(env: Optional[Mapping[str, str]] = None) -> bool:
    e = env if env is not None else os.environ
    mode = str(e.get("BBOT_POLICY_MODE") or "").strip().lower()
    return mode in {SYNTHETIC_POLICY_MODE, SYNTHETIC_ROLL_POLICY_ID}


def assert_synthetic_policy_gates(
    env: Optional[Mapping[str, str]] = None,
) -> None:
    """Synthetic canary is stub/no-order only, even under a live-data venue."""
    e = env if env is not None else os.environ
    if not synthetic_policy_requested(e):
        return
    broker = str(e.get("BBOT_BROKER") or "stub").strip().lower()
    profile = normalize_gear22_profile(str(e.get("BBOT_PROFILE") or ""))
    live_orders = str(e.get("LIVE_ORDERS") or "0").strip().lower()
    live_send = str(e.get("BBOT_THETA_LIVE_SEND") or "0").strip().lower()
    truthy = {"1", "true", "on", "yes"}
    if broker != "stub":
        raise SyntheticPolicyGateError("synthetic policy requires BBOT_BROKER=stub")
    if profile in GEAR22_LIVE_CANARY_PROFILES:
        raise SyntheticPolicyGateError("synthetic policy rejects live-canary profile")
    if live_orders in truthy:
        raise SyntheticPolicyGateError("synthetic policy requires LIVE_ORDERS=0")
    if live_send in truthy:
        raise SyntheticPolicyGateError("synthetic policy requires BBOT_THETA_LIVE_SEND=0")

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
        p50_1m_long=long_snap.p50_1m or math.nan,
        p50_1m_short=short_snap.p50_1m or math.nan,
        floor_long=long_snap.floor_tf_select_a25 or math.nan,
        floor_short=short_snap.floor_tf_select_a25 or math.nan,
        theta_1m_long=long_snap.theta_1m or math.nan,
        theta_1m_short=short_snap.theta_1m or math.nan,
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
    return name in GEAR22_WOULD_SEND_PROFILES or name in GEAR22_LIVE_CANARY_PROFILES


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
) -> dict[str, Any]:
    """Notional must fit available size on both chosen legs."""
    spread_side = spread_side_for(side, event=event)
    okx_leg, bybit_leg = legs_for_spread_side(spread_side)
    okx_px = signal_price_for_leg(dict(okx), okx_leg)
    bybit_px = signal_price_for_leg(dict(bybit), bybit_leg)
    okx_sz = available_leg_size(okx, okx_leg, depth=book_depth)
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
        "bybit_available_size": bybit_sz,
        "okx_planned_qty": planned_okx,
        "bybit_planned_qty": planned_bybit,
        "leg_buy_ex": "okx" if okx_leg == "buy" else "bybit",
        "leg_sell_ex": "okx" if okx_leg == "sell" else "bybit",
    }


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
    policy_id: str = POLICY_ID

    def __post_init__(self) -> None:
        if not isinstance(self.policy_id, str) or not self.policy_id:
            raise ValueError("policy_id must be non-empty")
        params = self.policy_params
        if params is None:
            return
        synthetic = synthetic_roll_enabled(params)
        if synthetic and self.policy_id != SYNTHETIC_ROLL_POLICY_ID:
            raise ValueError("synthetic policy params require synthetic policy_id")
        if not synthetic and self.policy_id == SYNTHETIC_ROLL_POLICY_ID:
            raise ValueError("synthetic policy_id requires synthetic policy params")

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "ThetaTradeConfig":
        e = env if env is not None else os.environ
        assert_synthetic_policy_gates(e)
        synthetic = synthetic_policy_requested(e)
        
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
            synthetic_roll_seed=(
                _env_int(e, "BBOT_SYNTHETIC_ROLL_SEED", DEFAULT_SYNTHETIC_ROLL_SEED)
                if synthetic
                else None
            ),
            synthetic_open_roll=SYNTHETIC_OPEN_ROLL,
            synthetic_close_roll=SYNTHETIC_CLOSE_ROLL,
        )
        
        return cls(
            theta_thr=_env_float(e, "BBOT_THETA_THR", DEFAULT_THETA_THR),
            fill_delay_ms=_env_int(e, "BBOT_FILL_DELAY_MS", DEFAULT_FILL_DELAY_MS),
            slot_k=max(1, _env_int(e, "BBOT_SLOT_K", DEFAULT_SLOT_K)),
            notional_usdt=_env_float(e, "BBOT_NOTIONAL_USDT", DEFAULT_NOTIONAL_USDT),
            book_depth=max(1, _env_int(e, "BBOT_BOOK_DEPTH", DEFAULT_BOOK_DEPTH)),
            policy_params=policy_params,
            policy_id=SYNTHETIC_ROLL_POLICY_ID if synthetic else POLICY_ID,
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
    decision_ts_s: Optional[int] = None,
) -> ThetaDecision:
    """Pure K=1 entry/exit decide using gear 2.2 policy (no I/O, no sleep).
    
    Uses policy.decide() from research.gear22_backtest.policy with frozen
    observation parameters. Replaces old theta_thr entry/exit logic.
    """
    params = policy_params or PolicyParams()
    by_key: dict[tuple[str, str], ThetaSnapshot] = {
        (s.base_coin, s.side): s for s in snapshots
    }
    
    now_s = int(time.time()) if decision_ts_s is None else int(decision_ts_s)
    
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
            )
            pot_pp = potential_profit_pp(feat, state, params.fee_round_trip_pp)
            own = by_key.get((pos.base_coin, pos.side))
            opp = by_key.get((pos.base_coin, opposite_side(pos.side)))
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


class ThetaTradeJournalWriter:
    """Append-only K=1 lifecycle and pending EV2 evidence under theta_trades."""

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
            created = not path.exists()
            with path.open("a", encoding="utf-8") as fh:
                for rec in batch:
                    line = json.dumps(
                        dict(rec), separators=(",", ":"), ensure_ascii=False
                    )
                    fh.write(line)
                    fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            if created:
                # Persist the file entry, date partition, and theta_trades
                # entry in data_root before a lifecycle row changes K=1.
                for directory in (path.parent, path.parent.parent, self.data_root):
                    fd = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            written.append(path)
        return written


@dataclass(frozen=True)
class ThetaTradeReplay:
    """Strict replay result for the append-only theta trade lifecycle."""

    position: Optional[OpenPosition]
    files_seen: int
    rows_seen: int
    lifecycle_rows: int
    pending_ev2_candidates: int = 0


def _replay_float(value: Any, *, field: str, optional: bool = False) -> Optional[float]:
    if value is None and optional:
        return None
    parsed = _finite(value)
    if parsed is None:
        raise ThetaTradeRecoveryError(f"invalid_{field}")
    return float(parsed)


def _position_from_open_row(row: Mapping[str, Any]) -> OpenPosition:
    trade_id = str(row.get("trade_id") or "").strip()
    coin = str(row.get("base_coin") or "").strip().upper()
    side = str(row.get("side") or "").strip().lower()
    if not trade_id:
        raise ThetaTradeRecoveryError("missing_trade_id")
    if not coin:
        raise ThetaTradeRecoveryError("missing_base_coin")
    if side not in {"long", "short"}:
        raise ThetaTradeRecoveryError("invalid_side")
    try:
        signal_ts_ms = int(row["signal_ts_ms"])
        fill_ts_ms = int(row["fill_ts_ms"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ThetaTradeRecoveryError("invalid_timestamps") from exc
    if signal_ts_ms <= 0 or fill_ts_ms < signal_ts_ms:
        raise ThetaTradeRecoveryError("invalid_timestamps")
    spread_raw = row.get("spread_fill")
    if spread_raw is None:
        spread_raw = row.get("open_fill_spread")
    fill_spread = _replay_float(spread_raw, field="fill_spread")
    notional = _replay_float(row.get("notional_usdt"), field="notional")
    if notional is None or notional <= 0:
        raise ThetaTradeRecoveryError("invalid_notional")
    theta = _replay_float(row.get("theta_1m"), field="theta", optional=True)
    return OpenPosition(
        trade_id=trade_id,
        base_coin=coin,
        side=side,
        open_signal_ts_ms=signal_ts_ms,
        open_fill_ts_ms=fill_ts_ms,
        open_fill_spread=fill_spread,
        open_notional=notional,
        open_theta_1m=theta,
        fill_spread_pp=fill_spread,
    )


def replay_theta_trade_history(
    data_root: Path,
    *,
    expected_policy_id: Optional[str] = None,
) -> ThetaTradeReplay:
    """Rebuild the K=1 slot from all UTC trade-history partitions.

    A live row changes state only when ``send=true``.  A no-order row changes
    the synthetic/would-send state only when ``would_send=true``.  Malformed,
    torn or contradictory lifecycle rows fail closed instead of inventing a
    flat slot.
    """

    root = Path(data_root) / "theta_trades"
    paths = sorted(root.glob("event_date=*/trades.jsonl"))
    position: Optional[OpenPosition] = None
    position_policy: Optional[str] = None
    rows_seen = 0
    lifecycle_rows = 0
    pending_ev2_ids: set[str] = set()
    for path in paths:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ThetaTradeRecoveryError("history_read_failed") from exc
        if raw and not raw.endswith("\n"):
            raise ThetaTradeRecoveryError("truncated_history_tail")
        for line in raw.splitlines():
            if not line.strip():
                continue
            rows_seen += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ThetaTradeRecoveryError("invalid_history_json") from exc
            if not isinstance(row, Mapping):
                raise ThetaTradeRecoveryError("invalid_history_row")
            if row.get("schema_version") != SCHEMA_VERSION:
                raise ThetaTradeRecoveryError("unsupported_history_schema")
            event = str(row.get("event") or "").strip().lower()
            live = bool(row.get("live_send"))
            if event == "ev2_candidate":
                candidate_id = str(row.get("candidate_id") or "")
                if (
                    len(candidate_id) != 64
                    or any(char not in "0123456789abcdef" for char in candidate_id)
                    or row.get("candidate_status") != "pending_venue_reconciliation"
                    or row.get("lifecycle_committed") is not False
                    or row.get("live_send") is not False
                    or row.get("send") is not False
                    or row.get("would_send") is not False
                ):
                    raise ThetaTradeRecoveryError("invalid_ev2_candidate")
                if candidate_id in pending_ev2_ids:
                    raise ThetaTradeRecoveryError("duplicate_ev2_candidate")
                pending_ev2_ids.add(candidate_id)
                continue
            if event in {"open_attempt", "close_attempt"}:
                if row.get("lifecycle_committed") is not False:
                    raise ThetaTradeRecoveryError("committed_attempt")
                if live or row.get("policy_id") != SYNTHETIC_ROLL_POLICY_ID:
                    raise ThetaTradeRecoveryError("invalid_unfilled_lifecycle")
                if (
                    row.get("would_send") is not True
                    or row.get("send") is not False
                    or row.get("fill_size_ok") is not False
                    or row.get("fill_ts_ms") is not None
                ):
                    raise ThetaTradeRecoveryError("invalid_attempt_evidence")
                if event == "open_attempt" and position is not None:
                    raise ThetaTradeRecoveryError("overlapping_open_attempt")
                if event == "close_attempt":
                    if position is None:
                        raise ThetaTradeRecoveryError("orphan_close_attempt")
                    if (
                        str(row.get("trade_id") or "") != position.trade_id
                        or str(row.get("base_coin") or "").upper() != position.base_coin
                        or str(row.get("side") or "").lower() != position.side
                    ):
                        raise ThetaTradeRecoveryError("close_attempt_identity_mismatch")
                continue
            if event not in {"open", "close"}:
                continue
            if row.get("lifecycle_committed") is False:
                raise ThetaTradeRecoveryError("invalid_unfilled_lifecycle")
            committed = row.get("send") is True if live else row.get("would_send") is True
            if not committed:
                # A recorded live abort / size reject did not change exposure.
                continue
            lifecycle_rows += 1
            trade_id = str(row.get("trade_id") or "").strip()
            coin = str(row.get("base_coin") or "").strip().upper()
            side = str(row.get("side") or "").strip().lower()
            if event == "open":
                if position is not None:
                    raise ThetaTradeRecoveryError("overlapping_open")
                position = _position_from_open_row(row)
                position_policy = str(row.get("policy_id") or "").strip() or None
                continue
            if position is None:
                raise ThetaTradeRecoveryError("orphan_close")
            if trade_id != position.trade_id:
                raise ThetaTradeRecoveryError("close_trade_id_mismatch")
            if coin != position.base_coin or side != position.side:
                raise ThetaTradeRecoveryError("close_identity_mismatch")
            position = None
            position_policy = None
    if (
        position is not None
        and expected_policy_id is not None
        and position_policy != str(expected_policy_id)
    ):
        raise ThetaTradeRecoveryError("open_policy_mismatch")
    return ThetaTradeReplay(
        position=position,
        files_seen=len(paths),
        rows_seen=rows_seen,
        lifecycle_rows=lifecycle_rows,
        pending_ev2_candidates=len(pending_ev2_ids),
    )


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
        restore_from_journal: bool = True,
        live_recovery_confirmed: Optional[bool] = None,
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
        if self.live_send and (self._place_fn is None or self._meta_fn is None):
            raise ThetaLiveSendError(
                "theta live send requires place_fn and meta_fn (fail closed)"
            )
        self.replay = (
            replay_theta_trade_history(
                self.data_root,
                expected_policy_id=self.config.policy_id,
            )
            if restore_from_journal
            else ThetaTradeReplay(None, 0, 0, 0)
        )
        self.slot = SlotState(
            k=int(self.config.slot_k),
            position=self.replay.position,
        )
        self.recovery_blocked = self.replay.pending_ev2_candidates > 0
        self.live_recovery_confirmed = (
            not self.live_send
            if live_recovery_confirmed is None
            else bool(live_recovery_confirmed)
        )
        restored = self.slot.position
        replay_state = "OPEN" if restored is not None else "FLAT"
        if self.recovery_blocked:
            replay_state = f"{replay_state}_PENDING_EV2"
        self._log(
            "theta_trade_replay | "
            f"files={self.replay.files_seen} | rows={self.replay.rows_seen} | "
            f"lifecycle={self.replay.lifecycle_rows} | "
            f"pending_ev2={self.replay.pending_ev2_candidates} | "
            f"state={replay_state} | "
            f"trade_id={restored.trade_id if restored is not None else '-'} | "
            f"coin={restored.base_coin if restored is not None else '-'} | "
            f"side={restored.side if restored is not None else '-'}"
        )
        self._skip_log_budget = 0

    def _assert_recovery_writable(self) -> None:
        if self.recovery_blocked:
            raise ThetaTradeRecoveryError("trade_history_unhealthy")
        if self.live_send and not self.live_recovery_confirmed:
            raise ThetaTradeRecoveryError("live_reconciliation_required")

    def confirm_live_reconciliation(self, *, matched: bool, reason: str) -> None:
        """Open the live decision gate only after signed exchange reconciliation."""

        if not self.live_send:
            return
        if self.recovery_blocked:
            raise ThetaTradeRecoveryError("trade_history_unhealthy")
        if not matched:
            self.recovery_blocked = True
            raise ThetaTradeRecoveryError(
                f"live_reconciliation_failed:{str(reason or 'unknown')}"
            )
        self.live_recovery_confirmed = True

    def assert_local_broker_state(
        self,
        *,
        broker_position: Optional[str],
        held_coin: Optional[str],
    ) -> None:
        """Require the broker cache to agree with replay before live startup."""

        if not self.live_send:
            return
        restored = self.slot.position
        expected_position = None
        expected_coin = None
        if restored is not None:
            expected_position = "open_long" if restored.side == "long" else "open_short"
            expected_coin = restored.base_coin
        actual_coin = str(held_coin).upper() if held_coin else None
        if broker_position != expected_position or actual_coin != expected_coin:
            self.recovery_blocked = True
            raise ThetaTradeRecoveryError("local_broker_state_mismatch")

    def _commit_lifecycle_row(
        self,
        row: Mapping[str, Any],
        *,
        position_after: Optional[OpenPosition],
    ) -> None:
        """Durably append before publishing the new in-memory slot state."""

        self._assert_recovery_writable()
        try:
            self.journal.append_rows([row])
        except Exception as exc:  # noqa: BLE001 - exposure must latch fail-closed
            self.recovery_blocked = True
            raise ThetaTradeRecoveryError("trade_history_write_failed") from exc
        self.slot.position = position_after

    def _synthetic_fill_position(
        self,
        row: dict[str, Any],
        *,
        fill_size: Mapping[str, Any],
        position_after: Optional[OpenPosition],
    ) -> Optional[OpenPosition]:
        """Synthetic-roll fill is conditional; frozen would_sent stays unchanged."""

        if self.config.policy_id != SYNTHETIC_ROLL_POLICY_ID:
            return position_after
        filled = bool(fill_size.get("size_ok"))
        row["lifecycle_committed"] = filled
        row["fill_outcome"] = "simulated_fill" if filled else "unfilled_insufficient_size"
        if filled:
            return position_after
        event = str(row["event"])
        row["event"] = f"{event}_attempt"
        row["attempt_ts_ms"] = row["fill_ts_ms"]
        row["attempt_spread"] = row["spread_fill"]
        row["fill_ts_ms"] = None
        row["spread_fill"] = None
        row["slip_spread"] = None
        row["latency_ms"] = None
        if event == "close":
            for key in (
                "open_fill_spread",
                "close_fill_spread",
                "pnl_spread",
                "pnl_usdt_approx",
            ):
                row.pop(key, None)
        return self.slot.position

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
            "policy_id": self.config.policy_id,
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

        Signal insufficient size → skip row with ``reject_reason`` (no trade_id
        position). Frozen would_sent keeps would-fill on a thin fill-time book;
        synthetic-roll records an unfilled attempt without changing K=1.
        """
        self._assert_recovery_writable()
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
                size_info = decision.size_info or size_check(
                    okx=okx,
                    bybit=bybit,
                    side=decision.side,
                    event="open",
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
        if decision.action == "open" and not signal_size.get("size_ok"):
            # Belt-and-suspenders (decide already gated).
            decision = ThetaDecision(
                action="skip",
                base_coin=decision.base_coin,
                side=decision.side,
                reason="reject",
                theta_1m=decision.theta_1m,
                opposite_theta_1m=decision.opposite_theta_1m,
                reject_reason="insufficient_size",
                size_info=signal_size,
            )
            return self.execute_decision(
                decision, snapshots=snapshots, quotes=quotes, now_ms=signal_ts
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
                position_after = OpenPosition(
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
                # would_send PnL proxy: open edge − close edge (pct points).
                pnl_spread = None
                if open_spread is not None and close_spread is not None:
                    pnl_spread = float(open_spread) - float(close_spread)
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
                    "policy_id": self.config.policy_id,
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
                position_after = None
            position_after = self._synthetic_fill_position(
                row, fill_size=fill_size, position_after=position_after
            )
            self._commit_lifecycle_row(row, position_after=position_after)
            self._log(
                f"theta_trade_{row['event']} | trade_id={row['trade_id']} | "
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
            
            if row.get("lifecycle_committed") is not False:
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
            "policy_id": self.config.policy_id,
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
                pnl_spread = None
                if open_spread is not None and close_spread is not None:
                    pnl_spread = float(open_spread) - float(close_spread)
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
                    "policy_id": self.config.policy_id,
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
            position_after = self.slot.position
            if sent_ok and decision.action == "open":
                fill_spread = spread_for_side(okx_f, bybit_f, side)
                position_after = OpenPosition(
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
                position_after = None

            self._commit_lifecycle_row(row, position_after=position_after)
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

    def on_theta_snapshots(
        self,
        snapshots: Sequence[ThetaSnapshot],
        *,
        quotes: Mapping[str, Mapping[str, Mapping[str, Any]]],
        coin_order: Optional[Sequence[str]] = None,
        now_ms: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Sync entry (unit tests / offline). Prefer ``on_theta_snapshots_async`` live."""
        self._assert_recovery_writable()
        decision_now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        decision = decide_theta_k1(
            snapshots,
            slot=self.slot,
            thr=self.config.theta_thr,
            quotes=quotes,
            notional_usdt=self.config.notional_usdt,
            book_depth=self.config.book_depth,
            coin_order=coin_order,
            policy_params=self.config.policy_params,
            decision_ts_s=decision_now_ms // 1000,
        )
        return self.execute_decision(
            decision, snapshots=snapshots, quotes=quotes, now_ms=decision_now_ms
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

        self._assert_recovery_writable()
        decision_now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        decision = decide_theta_k1(
            snapshots,
            slot=self.slot,
            thr=self.config.theta_thr,
            quotes=quotes,
            notional_usdt=self.config.notional_usdt,
            book_depth=self.config.book_depth,
            coin_order=coin_order,
            policy_params=self.config.policy_params,
            decision_ts_s=decision_now_ms // 1000,
        )

        async def _async_sleep(seconds: float) -> None:
            await asyncio.sleep(seconds)

        prev = self._sleep_fn
        self._sleep_fn = _async_sleep  # type: ignore[assignment]
        try:
            # execute_decision may call sleep_fn — support awaitable.
            return await self._execute_decision_async(
                decision, snapshots=snapshots, quotes=quotes, now_ms=decision_now_ms
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
    ) -> list[dict[str, Any]]:
        """Async twin of ``execute_decision`` (await fill delay)."""
        import asyncio
        from inspect import isawaitable

        if decision.action == "skip":
            return self.execute_decision(
                decision, snapshots=snapshots, quotes=quotes, now_ms=now_ms
            )
        if decision.action not in ("open", "close"):
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
        if decision.action == "open" and not signal_size.get("size_ok"):
            decision = ThetaDecision(
                action="skip",
                base_coin=decision.base_coin,
                side=decision.side,
                reason="reject",
                theta_1m=decision.theta_1m,
                opposite_theta_1m=decision.opposite_theta_1m,
                reject_reason="insufficient_size",
                size_info=signal_size,
            )
            return self.execute_decision(
                decision, snapshots=snapshots, quotes=quotes, now_ms=signal_ts
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
                position_after = OpenPosition(
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
                pnl_spread = None
                if open_spread is not None and close_spread is not None:
                    pnl_spread = float(open_spread) - float(close_spread)
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
                    "policy_id": self.config.policy_id,
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
                position_after = None
            position_after = self._synthetic_fill_position(
                row, fill_size=fill_size, position_after=position_after
            )
            try:
                await asyncio.to_thread(self.journal.append_rows, [row])
            except Exception as exc:  # noqa: BLE001 - latch exposure fail-closed
                self.recovery_blocked = True
                raise ThetaTradeRecoveryError("trade_history_write_failed") from exc
            self.slot.position = position_after
            self._log(
                f"theta_trade_{row['event']} | trade_id={row['trade_id']} | "
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
            
            if row.get("lifecycle_committed") is not False:
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
