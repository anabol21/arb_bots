"""Portable gear-1.0 trade manager (pure policy; no I/O, no fill scheduling).

Notebook ``model.ipynb`` still owns its own copy of VARIATION/HYPER and
``run_backtest`` for the historical baseline. This module mirrors the
signal/gate decision only so B-bot (and later M) can call the same rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Optional

from app.policy.features import (
    CausalMaWindow,
    latency_ok_for_avg,
    spread_long_pct,
    spread_short_pct,
)

IntentAction = Literal["flat", "open_long", "open_short", "close"]
Side = Literal["long", "short"]

# Frozen from model.ipynb config cell (VARIATION literals).
DEFAULT_VARIATION: dict[str, float] = {
    "thresh_open_long": 0.5,
    "thresh_open_short": 0.5,
    "thresh_close_long": 0.5,
    "thresh_close_short": 0.5,
    "open_frac": 0.7,
    "close_frac": 0.7,
}

# Test-only overlay. Not the frozen gear-1.0 vector. Used when BBOT_PROFILE=signal_test
# so BTC/ETH can produce stub intents; all four thresholds move together so the
# K_live=1 slot can also close.
SIGNAL_TEST_VARIATION: dict[str, float] = {
    "thresh_open_long": 0.05,
    "thresh_open_short": 0.05,
    "thresh_close_long": 0.05,
    "thresh_close_short": 0.05,
    "open_frac": 0.7,
    "close_frac": 0.7,
}

SIGNAL_TEST_COINS = ("BTC", "ETH", "LA", "DOGE")

# Live-data would_send experiment (Track 3). Not the frozen gear-2 close stamp
# (all-crypto, thresh 0.5). Four positive thresholds 0.02 for BTC/ETH/SOL/XRP;
# Arm A (no Top-N). 0.1 left these majors without fills.
GEAR2_WOULD_SEND_VARIATION: dict[str, float] = {
    "thresh_open_long": 0.02,
    "thresh_open_short": 0.02,
    "thresh_close_long": 0.02,
    "thresh_close_short": 0.02,
    "open_frac": 0.7,
    "close_frac": 0.7,
}

GEAR2_WOULD_SEND_COINS = ("BTC", "ETH", "SOL", "XRP")

# Existing live-size contour (not the four-coin would_send stub list).
# Canary does not replace this allowlist.
LIVE_SIZE_COINS = ("SOL", "XRP")

# Isolated WAL/EDEN canary (Track 3). Same elif / gate class as gear2_would_send;
# variation is 0.1 / 0.7, not gear2's 0.02. Not the frozen gear-1.0 vector.
CANARY_WAL_EDEN_VARIATION: dict[str, float] = {
    "thresh_open_long": 0.1,
    "thresh_open_short": 0.1,
    "thresh_close_long": 0.1,
    "thresh_close_short": 0.1,
    "open_frac": 0.7,
    "close_frac": 0.7,
}

CANARY_WAL_EDEN_COINS = ("WAL", "EDEN")

CANARY_WAL_EDEN_HYPER: dict[str, object] = {
    "max_freshness_ms": None,
    "max_latency_okx_ms": 54.0,
    "max_latency_bybit_ms": 35.0,
    "avg_window_sec": 10.0,
    "Trade_Lat": 100,
    "Check_volume": False,
    "Check_l1_depth": True,
    "position_size": 10.0,
    "position_frac": 1.0,
    "fee_rate": 0.00075,
    "k": 1,
    "regime_topn": None,
}

# Gear-2 close HYPER shape with slice p95 latency from gear-2-close-20260825.md.
# avg_window_sec=10 matches model_gear2 / close doc, not live gear1 (2s).
GEAR2_WOULD_SEND_HYPER: dict[str, object] = {
    "max_freshness_ms": None,
    "max_latency_okx_ms": 54.0,
    "max_latency_bybit_ms": 35.0,
    "avg_window_sec": 10.0,
    "Trade_Lat": 100,
    "Check_volume": False,
    "Check_l1_depth": False,
    "position_size": 10.0,
    "position_frac": 1.0,
    "fee_rate": 0.00075,
    "k": 1,
    "regime_topn": None,
}


def _norm_profile(profile: str) -> str:
    name = (profile or "gear1").strip().lower()
    if name == "default":
        return "gear1"
    if name == "gear2":
        return "gear2_would_send"
    if name == "canary":
        return "canary_wal_eden"
    return name


def uses_gear2_market_manager(profile: str) -> bool:
    """True for gear2_would_send and the WAL/EDEN canary (shared elif chain)."""
    return _norm_profile(profile) in {"gear2_would_send", "canary_wal_eden"}


def live_size_coins_for_profile(profile: str) -> tuple[str, ...]:
    """LIVE_SIZE-style allowlist for a live contour.

    ``gear2_would_send`` / default live-size: SOL, XRP.
    Canary: WAL, EDEN only (does not inherit SOL/XRP).
    """
    name = _norm_profile(profile)
    if name == "canary_wal_eden":
        return CANARY_WAL_EDEN_COINS
    return LIVE_SIZE_COINS


def live_size_coin_allowed(coin: str, profile: str) -> bool:
    """Whether ``coin`` may be used on the LIVE_SIZE-style contour for ``profile``."""
    return str(coin).strip().upper() in live_size_coins_for_profile(profile)


def variation_for_profile(profile: str) -> dict[str, float]:
    """Return VARIATION for ``gear1``, ``signal_test``, ``gear2_would_send``, or canary."""
    name = _norm_profile(profile)
    if name in ("gear1", ""):
        return dict(DEFAULT_VARIATION)
    if name == "signal_test":
        return dict(SIGNAL_TEST_VARIATION)
    if name == "gear2_would_send":
        return dict(GEAR2_WOULD_SEND_VARIATION)
    if name == "canary_wal_eden":
        return dict(CANARY_WAL_EDEN_VARIATION)
    raise ValueError(
        f"unknown BBOT_PROFILE {profile!r}; expected "
        "gear1|signal_test|gear2_would_send|canary_wal_eden"
    )


def hyper_for_profile(profile: str) -> dict[str, object]:
    """Return HYPER for the live profile. Frozen gear1 stays DEFAULT_HYPER."""
    name = _norm_profile(profile)
    if name == "gear2_would_send":
        return dict(GEAR2_WOULD_SEND_HYPER)
    if name == "canary_wal_eden":
        return dict(CANARY_WAL_EDEN_HYPER)
    return dict(DEFAULT_HYPER)

# Frozen HYPER. Latency caps = pooled p95 of trigger-leg delivery on the
# accepted L1 profile (all coins, window 1, 2026-08-16), not notebook LA
# 2026-07-21. Source: docs/latency-l1-market-w1.md (OKX 40, Bybit 25).
# Other keys remain notebook literals. Notebook still recomputes its own p95.
DEFAULT_HYPER: dict[str, object] = {
    "max_freshness_ms": None,
    "max_latency_okx_ms": 40.0,
    "max_latency_bybit_ms": 25.0,
    "avg_window_sec": 2.0,
    "Trade_Lat": 100,  # ms; fill timing belongs to broker / run_backtest
    "Check_volume": False,
    "position_size": 10.0,
    "position_frac": 1.0,
    "fee_rate": 0.00075,
}


@dataclass(frozen=True)
class TickView:
    """One L1 / feature snapshot for decide(). Spreads/MA may be precomputed."""

    event_local_ts_ms: float
    okx_bid: Optional[float] = None
    okx_ask: Optional[float] = None
    bybit_bid: Optional[float] = None
    bybit_ask: Optional[float] = None
    okx_bid_size: Optional[float] = None
    okx_ask_size: Optional[float] = None
    bybit_bid_size: Optional[float] = None
    bybit_ask_size: Optional[float] = None
    spread_long: Optional[float] = None
    spread_short: Optional[float] = None
    ma_long: Optional[float] = None
    ma_short: Optional[float] = None
    okx_latency_ms: Optional[float] = None
    bybit_latency_ms: Optional[float] = None
    okx_freshness_ms: Optional[float] = None
    bybit_freshness_ms: Optional[float] = None
    suppressed: bool = False
    stale: bool = False
    valid: bool = True


@dataclass(frozen=True)
class BotState:
    """Bot slot state. K_live=1: at most one open (or pending) position."""

    position_side: Optional[Side] = None
    pending_fill: bool = False
    k_live: int = 1


@dataclass(frozen=True)
class Intent:
    action: IntentAction
    reason: str = ""


def _book_cols_for(side: Side, *, is_open: bool) -> tuple[str, str]:
    """Return TickView size attribute names (okx, bybit) for the traded book sides."""
    if is_open:
        if side == "long":
            return "okx_ask_size", "bybit_bid_size"
        return "okx_bid_size", "bybit_ask_size"
    if side == "long":
        return "okx_bid_size", "bybit_ask_size"
    return "okx_ask_size", "bybit_bid_size"


_SIZE_TO_PRICE = {
    "okx_ask_size": "okx_ask",
    "okx_bid_size": "okx_bid",
    "bybit_ask_size": "bybit_ask",
    "bybit_bid_size": "bybit_bid",
}


def planned_coin_qty(notional_usdt: float, exec_price: Optional[float]) -> Optional[float]:
    """Coin qty for one leg: notional / execution price. None if price unusable."""
    if exec_price is None or exec_price != exec_price or float(exec_price) <= 0:
        return None
    return float(notional_usdt) / float(exec_price)


def l1_depth_covers_planned(
    size: Optional[float],
    planned_qty: Optional[float],
) -> bool:
    """True iff L1 size is present and covers planned coin qty. Missing → closed."""
    if size is None or planned_qty is None:
        return False
    if size != size or planned_qty != planned_qty:
        return False
    return float(size) >= float(planned_qty)


def _gate_l1_depth_ok(
    tick: TickView,
    side: Side,
    *,
    is_open: bool,
    check_l1_depth: bool,
    notional_usdt: float,
) -> bool:
    """Pre-signal L1 depth gate: buy→ask size, sell→bid size vs planned qty.

    Owned by the policy manager (same class as thresh / frac / avg_window).
    Not a post-intent broker check and not ``app.bot.sizing``.
    """
    if not check_l1_depth:
        return True
    c_okx, c_bybit = _book_cols_for(side, is_open=is_open)
    for size_col in (c_okx, c_bybit):
        size = getattr(tick, size_col)
        price = getattr(tick, _SIZE_TO_PRICE[size_col])
        planned = planned_coin_qty(notional_usdt, price)
        if not l1_depth_covers_planned(size, planned):
            return False
    return True


def _resolve_spreads(tick: TickView) -> tuple[Optional[float], Optional[float]]:
    sl = tick.spread_long
    ss = tick.spread_short
    if sl is not None and ss is not None:
        return float(sl), float(ss)
    if (
        tick.bybit_bid is None
        or tick.okx_ask is None
        or tick.okx_bid is None
        or tick.bybit_ask is None
    ):
        return sl if sl is None else float(sl), ss if ss is None else float(ss)
    if tick.bybit_bid == 0.0 or tick.okx_bid == 0.0:
        return None, None
    return (
        spread_long_pct(float(tick.bybit_bid), float(tick.okx_ask)),
        spread_short_pct(float(tick.okx_bid), float(tick.bybit_ask)),
    )


def _gate_a_ok(
    tick: TickView,
    hyper: Mapping[str, object],
) -> tuple[bool, Optional[str]]:
    max_freshness_ms = hyper.get("max_freshness_ms")
    if max_freshness_ms is not None:
        cap = float(max_freshness_ms)  # type: ignore[arg-type]
        of_ = tick.okx_freshness_ms
        bf_ = tick.bybit_freshness_ms
        if (
            of_ is None
            or bf_ is None
            or of_ != of_
            or bf_ != bf_
            or of_ > cap
            or bf_ > cap
        ):
            return False, "gate_a_freshness"

    max_okx = hyper.get("max_latency_okx_ms")
    if max_okx is not None:
        ol_ = tick.okx_latency_ms
        if ol_ is None or ol_ != ol_ or ol_ > float(max_okx):  # type: ignore[arg-type]
            return False, "gate_a_latency"

    max_bybit = hyper.get("max_latency_bybit_ms")
    if max_bybit is not None:
        bl_ = tick.bybit_latency_ms
        if bl_ is None or bl_ != bl_ or bl_ > float(max_bybit):  # type: ignore[arg-type]
            return False, "gate_a_latency"

    return True, None


def _gate_b_ok(
    mean_val: Optional[float],
    frac: float,
    thresh: float,
    *,
    avg_window_sec: Optional[float],
) -> bool:
    if avg_window_sec is None:
        return True
    if mean_val is None:
        return False
    return mean_val >= frac * thresh


def _gate_volume_ok(
    tick: TickView,
    side: Side,
    *,
    is_open: bool,
    check_volume: bool,
    position_size: float,
) -> bool:
    if not check_volume:
        return True
    c_okx, c_bybit = _book_cols_for(side, is_open=is_open)
    s_okx = getattr(tick, c_okx)
    s_bybit = getattr(tick, c_bybit)
    if s_okx is None or s_bybit is None:
        return False
    if s_okx != s_okx or s_bybit != s_bybit:
        return False
    return float(s_bybit) > float(position_size) and float(s_okx) > (
        float(position_size) / 10.0
    )


def _pass_gates(
    tick: TickView,
    *,
    side: Side,
    is_open: bool,
    frac: float,
    thresh: float,
    ma_val: Optional[float],
    variation: Mapping[str, float],
    hyper: Mapping[str, object],
) -> Intent | None:
    """Return a reject Intent if a gate fails; None if all pass."""
    del variation  # thresholds already applied by caller
    ok_a, reason = _gate_a_ok(tick, hyper)
    if not ok_a:
        assert reason is not None
        return Intent("flat", reason)

    avg_window = hyper.get("avg_window_sec")
    avg_f = None if avg_window is None else float(avg_window)  # type: ignore[arg-type]
    if not _gate_b_ok(ma_val, frac, thresh, avg_window_sec=avg_f):
        return Intent("flat", "gate_b_ma")

    check_volume = bool(hyper.get("Check_volume", False))
    position_size = float(hyper.get("position_size", 10.0))  # type: ignore[arg-type]
    if not _gate_volume_ok(
        tick,
        side,
        is_open=is_open,
        check_volume=check_volume,
        position_size=position_size,
    ):
        return Intent("flat", "gate_volume")

    check_l1_depth = bool(hyper.get("Check_l1_depth", False))
    if not _gate_l1_depth_ok(
        tick,
        side,
        is_open=is_open,
        check_l1_depth=check_l1_depth,
        notional_usdt=position_size,
    ):
        return Intent("flat", "gate_l1_depth")
    return None


def decide(
    tick: TickView,
    state: BotState,
    variation: Mapping[str, float] | None = None,
    hyper: Mapping[str, object] | None = None,
) -> Intent:
    """Gear-1.0 signal decision. Does not schedule fills or emit qty.

    Order matches notebook ``run_backtest`` elif chain after pending/fill skip:
    open long → open short → close long → close short; each with Gate A/B/volume.
    """
    v = variation if variation is not None else DEFAULT_VARIATION
    h = hyper if hyper is not None else DEFAULT_HYPER

    if tick.suppressed or not tick.valid:
        return Intent("flat", "suppressed")
    if tick.stale:
        return Intent("flat", "stale")
    if state.pending_fill:
        return Intent("flat", "pending")

    # K_live=1: occupied slot blocks a second open (closes still evaluated below).
    slot_full = state.position_side is not None
    if state.k_live < 1:
        return Intent("flat", "k_live_zero")

    spread_long, spread_short = _resolve_spreads(tick)
    if spread_long is None or spread_short is None:
        return Intent("flat", "missing_spread")

    thresh_ol = float(v["thresh_open_long"])
    thresh_os = float(v["thresh_open_short"])
    thresh_cl = float(v["thresh_close_long"])
    thresh_cs = float(v["thresh_close_short"])
    open_frac = float(v["open_frac"])
    close_frac = float(v["close_frac"])

    # 1) open long
    if spread_long > thresh_ol and not slot_full:
        reject = _pass_gates(
            tick,
            side="long",
            is_open=True,
            frac=open_frac,
            thresh=thresh_ol,
            ma_val=tick.ma_long,
            variation=v,
            hyper=h,
        )
        if reject is not None:
            return reject
        return Intent("open_long", "signal")

    # 2) open short
    if spread_short > thresh_os and not slot_full:
        reject = _pass_gates(
            tick,
            side="short",
            is_open=True,
            frac=open_frac,
            thresh=thresh_os,
            ma_val=tick.ma_short,
            variation=v,
            hyper=h,
        )
        if reject is not None:
            return reject
        return Intent("open_short", "signal")

    # 3) close long (uses spread_short / close_frac / thresh_close_long)
    if (
        spread_short > thresh_cl
        and state.position_side == "long"
    ):
        reject = _pass_gates(
            tick,
            side="long",
            is_open=False,
            frac=close_frac,
            thresh=thresh_cl,
            ma_val=tick.ma_short,
            variation=v,
            hyper=h,
        )
        if reject is not None:
            return reject
        return Intent("close", "signal")

    # 4) close short (uses spread_long / close_frac / thresh_close_short)
    if (
        spread_long > thresh_cs
        and state.position_side == "short"
    ):
        reject = _pass_gates(
            tick,
            side="short",
            is_open=False,
            frac=close_frac,
            thresh=thresh_cs,
            ma_val=tick.ma_long,
            variation=v,
            hyper=h,
        )
        if reject is not None:
            return reject
        return Intent("close", "signal")

    return Intent("flat", "no_signal")


def update_causal_ma(
    window: CausalMaWindow,
    tick: TickView,
    hyper: Mapping[str, object] | None = None,
) -> tuple[Optional[float], Optional[float]]:
    """Update caller-owned MA window from a valid tick; return (ma_long, ma_short).

    Uses HYPER latency caps for ``avg_valid`` (notebook Gate B filter).
    Caller should skip calling this for suppressed/stale ticks.
    """
    h = hyper if hyper is not None else DEFAULT_HYPER
    spread_long, spread_short = _resolve_spreads(tick)
    if spread_long is None or spread_short is None:
        return None, None
    avg_valid = latency_ok_for_avg(
        tick.okx_latency_ms,
        tick.bybit_latency_ms,
        max_latency_okx_ms=(
            None
            if h.get("max_latency_okx_ms") is None
            else float(h["max_latency_okx_ms"])  # type: ignore[arg-type]
        ),
        max_latency_bybit_ms=(
            None
            if h.get("max_latency_bybit_ms") is None
            else float(h["max_latency_bybit_ms"])  # type: ignore[arg-type]
        ),
    )
    return window.update(
        ts_ms=tick.event_local_ts_ms,
        spread_long=spread_long,
        spread_short=spread_short,
        avg_valid=avg_valid,
    )


if __name__ == "__main__":
    # Synthetic smoke: suppress → flat; pending → no second open; open_long clears gates.
    hyper_loose = {
        **DEFAULT_HYPER,
        "max_latency_okx_ms": 10_000.0,
        "max_latency_bybit_ms": 10_000.0,
        "avg_window_sec": None,  # Gate B off for open_long proof
        "Check_volume": False,
    }

    suppressed = decide(
        TickView(
            event_local_ts_ms=1_000.0,
            spread_long=1.0,
            spread_short=0.0,
            suppressed=True,
        ),
        BotState(),
        DEFAULT_VARIATION,
        hyper_loose,
    )
    assert suppressed.action == "flat" and suppressed.reason == "suppressed", suppressed

    pending = decide(
        TickView(
            event_local_ts_ms=2_000.0,
            spread_long=1.0,
            spread_short=0.0,
            okx_latency_ms=1.0,
            bybit_latency_ms=1.0,
        ),
        BotState(pending_fill=True),
        DEFAULT_VARIATION,
        hyper_loose,
    )
    assert pending.action == "flat" and pending.reason == "pending", pending

    # Occupied slot must not emit a second open even if spread clears.
    occupied = decide(
        TickView(
            event_local_ts_ms=3_000.0,
            spread_long=1.0,
            spread_short=0.0,
            okx_latency_ms=1.0,
            bybit_latency_ms=1.0,
        ),
        BotState(position_side="long"),
        DEFAULT_VARIATION,
        hyper_loose,
    )
    assert occupied.action == "flat", occupied

    open_long = decide(
        TickView(
            event_local_ts_ms=4_000.0,
            spread_long=1.0,
            spread_short=0.0,
            okx_latency_ms=1.0,
            bybit_latency_ms=1.0,
            ma_long=1.0,
            ma_short=0.0,
        ),
        BotState(),
        DEFAULT_VARIATION,
        hyper_loose,
    )
    assert open_long.action == "open_long" and open_long.reason == "signal", open_long

    # Gate B path: MA too low → flat
    hyper_b = {
        **DEFAULT_HYPER,
        "max_latency_okx_ms": 10_000.0,
        "max_latency_bybit_ms": 10_000.0,
        "avg_window_sec": 2.0,
        "Check_volume": False,
    }
    gate_b = decide(
        TickView(
            event_local_ts_ms=5_000.0,
            spread_long=1.0,
            spread_short=0.0,
            okx_latency_ms=1.0,
            bybit_latency_ms=1.0,
            ma_long=0.1,  # < 0.7 * 0.5
            ma_short=0.0,
        ),
        BotState(),
        DEFAULT_VARIATION,
        hyper_b,
    )
    assert gate_b.action == "flat" and gate_b.reason == "gate_b_ma", gate_b

    print("smoke ok:", suppressed, pending, occupied, open_long, gate_b)
