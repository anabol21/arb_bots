"""Gear 2 v0 market replay: frozen 1.0 elif, K=1 slot, optional 1.5 Top-N on open.

Extracted from ``model_gear2.ipynb`` so pytest can import the engine without
notebook state. Elif order matches Gear 1.0. Do not retune VARIATION here.
"""
from __future__ import annotations

import gc
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

import numpy as np
import pandas as pd

from research import gap_fill as _gap_fill
from research.gear2_regime_topn import coin_in_topn

Side = Literal["long", "short"]
TradeStatus = Literal["closed", "open"]
LATENCY_WINDOW_RADIUS = 5
BOOK_SIZE_COLS = (
    "okx_bid_size",
    "okx_ask_size",
    "bybit_bid_size",
    "bybit_ask_size",
)


@dataclass
class Trade:
    """Одна сделка; `status='open'` в `metric` не входит."""

    side: Side
    status: TradeStatus
    open_price: float
    open_ts: float
    open_dt: object
    quantity: float
    base_coin: str
    close_price: Optional[float] = None
    close_ts: Optional[float] = None
    close_dt: Optional[object] = None
    pnl: Optional[float] = None
    fees: float = 0.0
    okx_latency_ms_open: Optional[float] = None
    bybit_latency_ms_open: Optional[float] = None
    okx_latency_ms_close: Optional[float] = None
    bybit_latency_ms_close: Optional[float] = None
    okx_latency_ms_open_window: Optional[list[Optional[float]]] = None
    bybit_latency_ms_open_window: Optional[list[Optional[float]]] = None
    latency_window_offsets_open: Optional[list[int]] = None
    latency_window_truncated_open: bool = False
    okx_latency_ms_close_window: Optional[list[Optional[float]]] = None
    bybit_latency_ms_close_window: Optional[list[Optional[float]]] = None
    latency_window_offsets_close: Optional[list[int]] = None
    latency_window_truncated_close: bool = False
    signal_open_price: Optional[float] = None
    signal_open_ts: Optional[float] = None
    signal_open_dt: Optional[object] = None
    signal_close_price: Optional[float] = None
    signal_close_ts: Optional[float] = None
    signal_close_dt: Optional[object] = None
    open_fill_delay_ticks: int = 0
    close_fill_delay_ticks: int = 0


@dataclass
class BacktestResult:
    trades: list[Trade]
    metric: float
    open_position: Optional[Trade] = None
    n_signals_raw: int = 0
    n_signals_passed: int = 0
    n_filtered_by_freshness: int = 0
    n_filtered_by_latency: int = 0
    n_filtered_by_avg: int = 0
    n_filtered_by_size: int = 0
    n_filtered_slot_busy: int = 0
    n_filtered_pending_skip: int = 0
    n_filtered_not_topn: int = 0
    n_pending_missed: int = 0
    fees_total: float = 0.0
    n_ticks: int = 0
    n_coins: int = 0


@dataclass
class MarketCarryState:
    """K=1 slot + pending fill across chunk boundaries (timestamps, not row indices)."""

    pos: float = 0.0
    whatpos: Optional[Side] = None
    held_coin: Optional[str] = None
    open_price: Optional[float] = None
    open_ts: Optional[float] = None
    open_dt: object = None
    open_okx_lat: Optional[float] = None
    open_bybit_lat: Optional[float] = None
    open_okx_win: Optional[list] = None
    open_bybit_win: Optional[list] = None
    open_win_offsets: Optional[list] = None
    open_win_trunc: bool = False
    signal_open_price: Optional[float] = None
    signal_open_ts: Optional[float] = None
    signal_open_dt: object = None
    open_fill_delay_ticks: int = 0
    open_fees: float = 0.0
    pending: Optional[dict] = None
    metric: float = 0.0
    fees_total: float = 0.0
    trades: list[Trade] = field(default_factory=list)
    n_signals_raw: int = 0
    n_signals_passed: int = 0
    n_filtered_by_freshness: int = 0
    n_filtered_by_latency: int = 0
    n_filtered_by_avg: int = 0
    n_filtered_by_size: int = 0
    n_filtered_slot_busy: int = 0
    n_filtered_pending_skip: int = 0
    n_filtered_not_topn: int = 0
    n_pending_missed: int = 0
    n_ticks: int = 0
    coins_seen: set[str] = field(default_factory=set)


def _latency_window_at_idx(
    okx_lat: np.ndarray,
    bybit_lat: np.ndarray,
    local_i: int,
    *,
    radius: int = LATENCY_WINDOW_RADIUS,
) -> tuple[list[Optional[float]], list[Optional[float]], list[int], bool]:
    n = len(okx_lat)
    lo = max(0, local_i - radius)
    hi = min(n, local_i + radius + 1)
    truncated = lo > (local_i - radius) or hi < (local_i + radius + 1)
    offsets = list(range(lo - local_i, hi - local_i))

    def _vals(arr: np.ndarray) -> list[Optional[float]]:
        out: list[Optional[float]] = []
        for v in arr[lo:hi]:
            out.append(None if v != v else float(v))
        return out

    return _vals(okx_lat), _vals(bybit_lat), offsets, truncated


def _fee_cost_pct(quantity: float, fee_rate: float, *, legs: int = 2) -> float:
    if fee_rate == 0.0 or quantity == 0.0:
        return 0.0
    return float(fee_rate) * 100.0 * float(legs) * (float(quantity) / 100.0)


def _book_cols_for(side: Side, *, is_open: bool) -> tuple[str, str]:
    if is_open:
        if side == "long":
            return "okx_ask_size", "bybit_bid_size"
        return "okx_bid_size", "bybit_ask_size"
    if side == "long":
        return "okx_bid_size", "bybit_ask_size"
    return "okx_ask_size", "bybit_bid_size"


def _require_size_columns(data: pd.DataFrame) -> None:
    missing = [c for c in BOOK_SIZE_COLS if c not in data.columns]
    if missing:
        raise ValueError(
            "Check_volume=True requires book size columns "
            f"{list(BOOK_SIZE_COLS)}, missing: {missing}."
        )


def _volume_ok_bybit_ws(
    size_arrs: dict[str, np.ndarray],
    i: int,
    side: Side,
    *,
    is_open: bool,
    position_size: float,
) -> bool:
    c_okx, c_bybit = _book_cols_for(side, is_open=is_open)
    s_okx = size_arrs[c_okx][i]
    s_bybit = size_arrs[c_bybit][i]
    if s_okx != s_okx or s_bybit != s_bybit:
        return False
    return float(s_bybit) > float(position_size) and float(s_okx) > (
        float(position_size) / 10.0
    )


def compute_gate_b_ma(
    data: pd.DataFrame,
    *,
    avg_window_sec: float,
    max_latency_okx_ms: Optional[float] = None,
    max_latency_bybit_ms: Optional[float] = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Скользящее среднее Gate B по event_local_ts_ms (ряд одной монеты, по времени)."""
    if avg_window_sec <= 0:
        raise ValueError("avg_window_sec must be > 0")
    n = len(data)
    ts_ms = data["event_local_ts_ms"].to_numpy(dtype="float64", copy=False)
    spread_long_arr = data["spread_long"].to_numpy(dtype="float64", copy=False)
    spread_short_arr = data["spread_short"].to_numpy(dtype="float64", copy=False)
    okx_lat_arr = data["okx_latency_ms"].to_numpy(dtype="float64", copy=False)
    bybit_lat_arr = data["bybit_latency_ms"].to_numpy(dtype="float64", copy=False)
    avg_valid = np.ones(n, dtype=bool)
    if max_latency_okx_ms is not None:
        avg_valid &= okx_lat_arr <= max_latency_okx_ms
    if max_latency_bybit_ms is not None:
        avg_valid &= bybit_lat_arr <= max_latency_bybit_ms
    window_ms = float(avg_window_sec) * 1000.0
    ma_long = np.full(n, np.nan, dtype="float64")
    ma_short = np.full(n, np.nan, dtype="float64")
    left = 0
    sum_long = 0.0
    sum_short = 0.0
    win_count = 0
    for i in range(n):
        t_lo = ts_ms[i] - window_ms
        while left <= i and ts_ms[left] < t_lo:
            if avg_valid[left]:
                sum_long -= spread_long_arr[left]
                sum_short -= spread_short_arr[left]
                win_count -= 1
            left += 1
        if avg_valid[i]:
            sum_long += spread_long_arr[i]
            sum_short += spread_short_arr[i]
            win_count += 1
        if win_count > 0:
            ma_long[i] = sum_long / win_count
            ma_short[i] = sum_short / win_count
    return ma_long, ma_short


def _closed_trade(**kwargs) -> Trade:
    quantity = float(kwargs["quantity"])
    fees = float(kwargs["fees"])
    open_price = float(kwargs["open_price"])
    close_price = float(kwargs["close_price"])
    pnl = (open_price + close_price) * (quantity / 100.0) - fees
    kwargs = dict(kwargs)
    kwargs["pnl"] = pnl
    kwargs.setdefault("signal_open_price", open_price)
    kwargs.setdefault("signal_open_ts", kwargs["open_ts"])
    kwargs.setdefault("signal_open_dt", kwargs["open_dt"])
    kwargs.setdefault("signal_close_price", close_price)
    kwargs.setdefault("signal_close_ts", kwargs["close_ts"])
    kwargs.setdefault("signal_close_dt", kwargs["close_dt"])
    kwargs.setdefault("open_fill_delay_ticks", 0)
    kwargs.setdefault("close_fill_delay_ticks", 0)
    return Trade(status="closed", **kwargs)


def run_backtest_market(
    df: pd.DataFrame,
    *,
    variation: dict,
    hyper: dict,
    k: int = 1,
    regime_topn: Optional[dict] = None,
    decision_start_ms: Optional[float] = None,
    decision_end_ms: Optional[float] = None,
    carry_in: Optional[MarketCarryState] = None,
    return_carry: bool = False,
    finalize_open: bool = True,
):
    """Глобальный replay: elif 1.0 на каждом тике, слот K=1, MA и fill на монету.

    ``regime_topn``: map completed-bar start_ms → frozenset Top-N coins (1.5 arm B).
    Applied on **open** only. None → baseline without the cluster gate.

    Chunked day path (optional):
    - load DF with MA warmup before ``decision_start_ms``;
    - pass ``carry_in`` / ``return_carry=True`` / ``finalize_open=False`` between chunks;
    - pending uses ``fill_ts`` so fills can cross chunk boundaries.
    """
    if k != 1:
        raise ValueError("gear 2 v0 supports K=1 only")
    thresh_open_long = float(variation["thresh_open_long"])
    thresh_open_short = float(variation["thresh_open_short"])
    thresh_close_long = float(variation["thresh_close_long"])
    thresh_close_short = float(variation["thresh_close_short"])
    open_frac = float(variation["open_frac"])
    close_frac = float(variation["close_frac"])
    max_freshness_ms = hyper.get("max_freshness_ms")
    max_latency_okx_ms = hyper.get("max_latency_okx_ms")
    max_latency_bybit_ms = hyper.get("max_latency_bybit_ms")
    avg_window_sec = hyper.get("avg_window_sec")
    Trade_Lat = float(hyper.get("Trade_Lat", 0.0))
    Check_volume = bool(hyper.get("Check_volume", False))
    position_size = float(hyper.get("position_size", 10.0))
    position_frac = float(hyper.get("position_frac", 1.0))
    fee_rate = float(hyper.get("fee_rate", 0.0))
    reject_fill_across_gap = bool(hyper.get("reject_fill_across_gap", False))
    gap_fill_slack_ms = float(hyper.get("gap_fill_slack_ms", 1000.0))

    if avg_window_sec is not None and float(avg_window_sec) <= 0:
        raise ValueError("avg_window_sec must be > 0 when set")
    if not (0.0 < open_frac <= 1.0) or not (0.0 < close_frac <= 1.0):
        raise ValueError("frac must be in (0, 1]")
    if not (0.0 < position_frac <= 1.0):
        raise ValueError("position_frac must be in (0, 1]")
    if fee_rate < 0.0 or Trade_Lat < 0.0 or position_size <= 0.0:
        raise ValueError("invalid HYPER numeric")
    if Check_volume:
        _require_size_columns(df)

    target_qty = 100.0 * position_frac
    data = df.sort_values(["event_local_ts_ms", "base_coin"], kind="mergesort").reset_index(
        drop=True
    )
    n = len(data)
    if n == 0:
        empty = BacktestResult(trades=[], metric=0.0)
        if return_carry:
            carry = carry_in if carry_in is not None else MarketCarryState()
            return empty, carry
        return empty

    coins_arr = data["base_coin"].astype(str).to_numpy()
    ts_ms = data["event_local_ts_ms"].to_numpy(dtype="float64", copy=False)
    okx_lat_arr = data["okx_latency_ms"].to_numpy(dtype="float64", copy=False)
    bybit_lat_arr = data["bybit_latency_ms"].to_numpy(dtype="float64", copy=False)
    if max_freshness_ms is not None:
        okx_fresh_arr = data["okx_freshness_ms"].to_numpy(dtype="float64", copy=False)
        bybit_fresh_arr = data["bybit_freshness_ms"].to_numpy(dtype="float64", copy=False)
    else:
        okx_fresh_arr = bybit_fresh_arr = None
    size_arrs = (
        {col: data[col].to_numpy(dtype="float64", copy=False) for col in BOOK_SIZE_COLS}
        if Check_volume
        else None
    )

    groups: dict[str, list[int]] = defaultdict(list)
    for i, c in enumerate(coins_arr):
        groups[c].append(i)
    coin_idx = {c: np.asarray(ix, dtype=np.int64) for c, ix in groups.items()}
    local_pos = np.empty(n, dtype=np.int64)
    for c, ix in coin_idx.items():
        local_pos[ix] = np.arange(len(ix), dtype=np.int64)

    ma_long_g = np.full(n, np.nan, dtype="float64")
    ma_short_g = np.full(n, np.nan, dtype="float64")
    if avg_window_sec is not None:
        for c, ix in coin_idx.items():
            sub = data.iloc[ix]
            ml, ms = compute_gate_b_ma(
                sub,
                avg_window_sec=float(avg_window_sec),
                max_latency_okx_ms=max_latency_okx_ms,
                max_latency_bybit_ms=max_latency_bybit_ms,
            )
            ma_long_g[ix] = ml
            ma_short_g[ix] = ms

    def _resolve_fill_same_coin(signal_i: int, coin: str) -> Optional[int]:
        ix = coin_idx[coin]
        delay = float(Trade_Lat)
        if delay <= 0:
            return int(signal_i)
        target = float(ts_ms[signal_i]) + delay
        pos_i = int(np.searchsorted(ix, signal_i))
        for k in range(pos_i + 1, len(ix)):
            g = int(ix[k])
            if ts_ms[g] >= target:
                if _gap_fill.reject_fill_across_gap(
                    float(ts_ms[signal_i]),
                    float(ts_ms[g]),
                    trade_lat_ms=delay,
                    slack_ms=float(gap_fill_slack_ms),
                    enabled=bool(reject_fill_across_gap),
                ):
                    return None
                return g
        return None

    def _resolve_fill_by_ts(coin: str, fill_ts: float, signal_ts: float) -> Optional[int]:
        ix = coin_idx.get(coin)
        if ix is None or len(ix) == 0:
            return None
        # first tick of coin with ts >= fill_ts
        coin_ts = ts_ms[ix]
        j = int(np.searchsorted(coin_ts, float(fill_ts), side="left"))
        if j >= len(ix):
            return None
        g = int(ix[j])
        if _gap_fill.reject_fill_across_gap(
            float(signal_ts),
            float(ts_ms[g]),
            trade_lat_ms=float(Trade_Lat),
            slack_ms=float(gap_fill_slack_ms),
            enabled=bool(reject_fill_across_gap),
        ):
            return None
        return g

    def _gate_a_ok(i: int) -> tuple[bool, Optional[str]]:
        if max_freshness_ms is not None:
            assert okx_fresh_arr is not None and bybit_fresh_arr is not None
            of_ = okx_fresh_arr[i]
            bf_ = bybit_fresh_arr[i]
            if of_ != of_ or bf_ != bf_ or of_ > max_freshness_ms or bf_ > max_freshness_ms:
                return False, "freshness"
        if max_latency_okx_ms is not None:
            ol_ = okx_lat_arr[i]
            if ol_ != ol_ or ol_ > max_latency_okx_ms:
                return False, "latency"
        if max_latency_bybit_ms is not None:
            bl_ = bybit_lat_arr[i]
            if bl_ != bl_ or bl_ > max_latency_bybit_ms:
                return False, "latency"
        return True, None

    def _gate_b_ok(mean_val: Optional[float], frac: float, thresh: float) -> bool:
        if avg_window_sec is None:
            return True
        if mean_val is None:
            return False
        return mean_val >= frac * thresh

    def _mean_side(i: int, side_spread: str) -> Optional[float]:
        v = ma_long_g[i] if side_spread == "long" else ma_short_g[i]
        if v != v:
            return None
        return float(v)

    def _gate_volume_ok(i: int, side: Side, *, is_open: bool) -> bool:
        if not Check_volume:
            return True
        assert size_arrs is not None
        return _volume_ok_bybit_ws(
            size_arrs, i, side, is_open=is_open, position_size=position_size
        )

    def _count_gate_fail(reason: Optional[str], *, avg_fail: bool = False, size_fail: bool = False) -> None:
        nonlocal n_filtered_by_freshness, n_filtered_by_latency
        nonlocal n_filtered_by_avg, n_filtered_by_size
        if avg_fail:
            n_filtered_by_avg += 1
            return
        if size_fail:
            n_filtered_by_size += 1
            return
        if reason == "freshness":
            n_filtered_by_freshness += 1
        else:
            n_filtered_by_latency += 1

    # --- restore / init slot state ---
    trades: list[Trade] = []
    metric = 0.0
    fees_total = 0.0
    pos = 0.0
    whatpos: Optional[Side] = None
    held_coin: Optional[str] = None
    open_price: Optional[float] = None
    open_ts: Optional[float] = None
    open_dt = None
    open_okx_lat: Optional[float] = None
    open_bybit_lat: Optional[float] = None
    open_okx_win: Optional[list[Optional[float]]] = None
    open_bybit_win: Optional[list[Optional[float]]] = None
    open_win_offsets: Optional[list[int]] = None
    open_win_trunc: bool = False
    signal_open_price: Optional[float] = None
    signal_open_ts: Optional[float] = None
    signal_open_dt = None
    open_fill_delay_ticks: int = 0
    open_fees: float = 0.0
    pending: Optional[dict] = None

    n_signals_raw = 0
    n_signals_passed = 0
    n_filtered_by_freshness = 0
    n_filtered_by_latency = 0
    n_filtered_by_avg = 0
    n_filtered_by_size = 0
    n_filtered_slot_busy = 0
    n_filtered_pending_skip = 0
    n_filtered_not_topn = 0
    n_pending_missed = 0

    if carry_in is not None:
        pos = float(carry_in.pos)
        whatpos = carry_in.whatpos
        held_coin = carry_in.held_coin
        open_price = carry_in.open_price
        open_ts = carry_in.open_ts
        open_dt = carry_in.open_dt
        open_okx_lat = carry_in.open_okx_lat
        open_bybit_lat = carry_in.open_bybit_lat
        open_okx_win = carry_in.open_okx_win
        open_bybit_win = carry_in.open_bybit_win
        open_win_offsets = carry_in.open_win_offsets
        open_win_trunc = bool(carry_in.open_win_trunc)
        signal_open_price = carry_in.signal_open_price
        signal_open_ts = carry_in.signal_open_ts
        signal_open_dt = carry_in.signal_open_dt
        open_fill_delay_ticks = int(carry_in.open_fill_delay_ticks)
        open_fees = float(carry_in.open_fees)
        if carry_in.pending is not None:
            pending = dict(carry_in.pending)
            fill_ts = float(pending["fill_ts"])
            sig_ts = float(pending["signal_ts"])
            coin_p = str(pending["coin"])
            fi = _resolve_fill_by_ts(coin_p, fill_ts, sig_ts)
            if fi is not None:
                pending["fill_i"] = int(fi)
            else:
                pending.pop("fill_i", None)

    def _lat_win(signal_i: int, coin: str):
        ix = coin_idx[coin]
        li = int(local_pos[signal_i])
        return _latency_window_at_idx(okx_lat_arr[ix], bybit_lat_arr[ix], li)

    def _schedule_or_fill(kind: str, side: Side, signal_i: int, coin: str, row) -> None:
        nonlocal pending, n_pending_missed, n_signals_passed
        fill_i = _resolve_fill_same_coin(signal_i, coin)
        lat_win = _lat_win(signal_i, coin)
        signal_ts = float(row.event_local_ts_ms)
        if fill_i is None:
            # Carry only when this chunk has no later tick for the coin (not gap-reject).
            ix = coin_idx[coin]
            pos_i = int(np.searchsorted(ix, signal_i))
            no_later = pos_i + 1 >= len(ix)
            if return_carry and float(Trade_Lat) > 0 and no_later:
                n_signals_passed += 1
                pending = {
                    "kind": kind,
                    "side": side,
                    "coin": coin,
                    "signal_i": signal_i,
                    "fill_ts": signal_ts + float(Trade_Lat),
                    "delay_ticks": None,
                    "lat_win": lat_win,
                    "signal_row_spread_long": float(row.spread_long),
                    "signal_row_spread_short": float(row.spread_short),
                    "signal_ts": signal_ts,
                    "signal_dt": row.event_dt,
                    "signal_okx_lat": float(row.okx_latency_ms),
                    "signal_bybit_lat": float(row.bybit_latency_ms),
                }
                return
            n_pending_missed += 1
            return
        n_signals_passed += 1
        payload = {
            "kind": kind,
            "side": side,
            "coin": coin,
            "signal_i": signal_i,
            "fill_i": fill_i,
            "fill_ts": float(ts_ms[fill_i]),
            "delay_ticks": int(fill_i - signal_i),
            "lat_win": lat_win,
            "signal_row_spread_long": float(row.spread_long),
            "signal_row_spread_short": float(row.spread_short),
            "signal_ts": signal_ts,
            "signal_dt": row.event_dt,
            "signal_okx_lat": float(row.okx_latency_ms),
            "signal_bybit_lat": float(row.bybit_latency_ms),
        }
        if fill_i == signal_i:
            _execute_fill(payload, signal_i, row)
        else:
            pending = payload

    def _execute_fill(payload: dict, fill_i: int, fill_row) -> None:
        nonlocal pos, whatpos, held_coin, open_price, open_ts, open_dt
        nonlocal open_okx_lat, open_bybit_lat, open_okx_win, open_bybit_win
        nonlocal open_win_offsets, open_win_trunc
        nonlocal signal_open_price, signal_open_ts, signal_open_dt
        nonlocal open_fill_delay_ticks, open_fees
        nonlocal metric, fees_total

        kind = payload["kind"]
        side: Side = payload["side"]
        coin = str(payload["coin"])
        if payload.get("delay_ticks") is not None:
            delay_ticks = int(payload["delay_ticks"])
        else:
            delay_ticks = int(fill_i - int(payload.get("signal_i", fill_i)))
        if payload.get("lat_win") is not None:
            lw = payload["lat_win"]
            win_okx, win_bybit, win_off, win_trunc = lw[0], lw[1], lw[2], lw[3]
        else:
            win_okx, win_bybit, win_off, win_trunc = _lat_win(int(payload["signal_i"]), coin)

        if kind == "open":
            fill_spread = (
                float(fill_row.spread_long) if side == "long" else float(fill_row.spread_short)
            )
            sig_spread = (
                float(payload["signal_row_spread_long"])
                if side == "long"
                else float(payload["signal_row_spread_short"])
            )
            pos = target_qty
            held_coin = coin
            open_price = fill_spread
            open_ts = float(fill_row.event_local_ts_ms)
            open_dt = fill_row.event_dt
            open_okx_lat = float(payload["signal_okx_lat"])
            open_bybit_lat = float(payload["signal_bybit_lat"])
            open_okx_win, open_bybit_win = win_okx, win_bybit
            open_win_offsets, open_win_trunc = win_off, bool(win_trunc)
            signal_open_price = sig_spread
            signal_open_ts = float(payload["signal_ts"])
            signal_open_dt = payload["signal_dt"]
            open_fill_delay_ticks = delay_ticks
            open_fees = _fee_cost_pct(target_qty, fee_rate, legs=2)
            whatpos = side
            return

        assert open_price is not None and open_ts is not None and whatpos == side
        assert held_coin == coin
        fill_spread = (
            float(fill_row.spread_short) if side == "long" else float(fill_row.spread_long)
        )
        sig_spread = (
            float(payload["signal_row_spread_short"])
            if side == "long"
            else float(payload["signal_row_spread_long"])
        )
        close_fees = _fee_cost_pct(pos, fee_rate, legs=2)
        fees = open_fees + close_fees
        trade = _closed_trade(
            side=side,
            base_coin=coin,
            open_price=open_price,
            open_ts=open_ts,
            open_dt=open_dt,
            close_price=fill_spread,
            close_ts=float(fill_row.event_local_ts_ms),
            close_dt=fill_row.event_dt,
            quantity=pos,
            fees=fees,
            okx_latency_ms_open=open_okx_lat,
            bybit_latency_ms_open=open_bybit_lat,
            okx_latency_ms_close=float(payload["signal_okx_lat"]),
            bybit_latency_ms_close=float(payload["signal_bybit_lat"]),
            okx_latency_ms_open_window=open_okx_win,
            bybit_latency_ms_open_window=open_bybit_win,
            latency_window_offsets_open=open_win_offsets,
            latency_window_truncated_open=open_win_trunc,
            okx_latency_ms_close_window=win_okx,
            bybit_latency_ms_close_window=win_bybit,
            latency_window_offsets_close=win_off,
            latency_window_truncated_close=bool(win_trunc),
            signal_open_price=signal_open_price,
            signal_open_ts=signal_open_ts,
            signal_open_dt=signal_open_dt,
            signal_close_price=sig_spread,
            signal_close_ts=float(payload["signal_ts"]),
            signal_close_dt=payload["signal_dt"],
            open_fill_delay_ticks=open_fill_delay_ticks,
            close_fill_delay_ticks=delay_ticks,
        )
        trades.append(trade)
        metric += float(trade.pnl)
        fees_total += fees
        pos = 0.0
        whatpos = None
        held_coin = None
        open_price = None
        open_ts = None
        open_dt = None
        open_okx_lat = None
        open_bybit_lat = None
        open_okx_win = None
        open_bybit_win = None
        open_win_offsets = None
        open_win_trunc = False
        signal_open_price = None
        signal_open_ts = None
        signal_open_dt = None
        open_fill_delay_ticks = 0
        open_fees = 0.0

    def _in_decision(ts: float) -> bool:
        if decision_start_ms is not None and ts < float(decision_start_ms):
            return False
        if decision_end_ms is not None and ts >= float(decision_end_ms):
            return False
        return True

    for i, row in enumerate(data.itertuples(index=False)):
        coin = str(row.base_coin)
        ts_i = float(row.event_local_ts_ms)
        filled_now = False
        if pending is not None:
            fill_i = pending.get("fill_i")
            hit = False
            if fill_i is not None and i == int(fill_i):
                hit = True
            elif fill_i is None and coin == str(pending["coin"]) and ts_i >= float(pending["fill_ts"]):
                # Remap late when fill_i was missing at chunk start.
                hit = True
            if hit:
                if str(pending["coin"]) != coin:
                    raise RuntimeError("fill index is not on pending coin — engine bug")
                _execute_fill(pending, i, row)
                pending = None
                filled_now = True

        if pending is not None or filled_now:
            if (
                pending is not None
                and coin != str(pending["coin"])
                and _in_decision(ts_i)
                and (
                    float(row.spread_long) > thresh_open_long
                    or float(row.spread_short) > thresh_open_short
                )
            ):
                n_filtered_pending_skip += 1
            continue

        if not _in_decision(ts_i):
            continue

        slot_full = pos >= target_qty or whatpos is not None

        if row.spread_long > thresh_open_long and not slot_full:
            n_signals_raw += 1
            if regime_topn is not None and not coin_in_topn(
                coin, float(row.event_local_ts_ms), regime_topn
            ):
                n_filtered_not_topn += 1
            else:
                ok_a, reason = _gate_a_ok(i)
                if not ok_a:
                    _count_gate_fail(reason)
                else:
                    mean_l = _mean_side(i, "long") if avg_window_sec is not None else None
                    if not _gate_b_ok(mean_l, open_frac, thresh_open_long):
                        _count_gate_fail(None, avg_fail=True)
                    elif not _gate_volume_ok(i, "long", is_open=True):
                        _count_gate_fail(None, size_fail=True)
                    else:
                        _schedule_or_fill("open", "long", i, coin, row)
        elif row.spread_short > thresh_open_short and not slot_full:
            n_signals_raw += 1
            if regime_topn is not None and not coin_in_topn(
                coin, float(row.event_local_ts_ms), regime_topn
            ):
                n_filtered_not_topn += 1
            else:
                ok_a, reason = _gate_a_ok(i)
                if not ok_a:
                    _count_gate_fail(reason)
                else:
                    mean_s = _mean_side(i, "short") if avg_window_sec is not None else None
                    if not _gate_b_ok(mean_s, open_frac, thresh_open_short):
                        _count_gate_fail(None, avg_fail=True)
                    elif not _gate_volume_ok(i, "short", is_open=True):
                        _count_gate_fail(None, size_fail=True)
                    else:
                        _schedule_or_fill("open", "short", i, coin, row)
        elif (
            slot_full
            and (row.spread_long > thresh_open_long or row.spread_short > thresh_open_short)
            and (held_coin is None or coin != held_coin)
        ):
            n_filtered_slot_busy += 1
        elif (
            row.spread_short > thresh_close_long
            and pos > 0
            and whatpos == "long"
            and coin == held_coin
        ):
            n_signals_raw += 1
            ok_a, reason = _gate_a_ok(i)
            if not ok_a:
                _count_gate_fail(reason)
            else:
                mean_s = _mean_side(i, "short") if avg_window_sec is not None else None
                if not _gate_b_ok(mean_s, close_frac, thresh_close_long):
                    _count_gate_fail(None, avg_fail=True)
                elif not _gate_volume_ok(i, "long", is_open=False):
                    _count_gate_fail(None, size_fail=True)
                else:
                    _schedule_or_fill("close", "long", i, coin, row)
        elif (
            row.spread_long > thresh_close_short
            and pos > 0
            and whatpos == "short"
            and coin == held_coin
        ):
            n_signals_raw += 1
            ok_a, reason = _gate_a_ok(i)
            if not ok_a:
                _count_gate_fail(reason)
            else:
                mean_l = _mean_side(i, "long") if avg_window_sec is not None else None
                if not _gate_b_ok(mean_l, close_frac, thresh_close_short):
                    _count_gate_fail(None, avg_fail=True)
                elif not _gate_volume_ok(i, "short", is_open=False):
                    _count_gate_fail(None, size_fail=True)
                else:
                    _schedule_or_fill("close", "short", i, coin, row)

    # Pending at chunk/day end
    if pending is not None:
        if return_carry and not finalize_open:
            # Keep for next chunk (fill_ts already set).
            pass
        elif (
            return_carry
            and decision_end_ms is not None
            and float(pending.get("fill_ts", -1)) >= float(decision_end_ms)
        ):
            pass
        else:
            n_pending_missed += 1
            pending = None

    open_position: Optional[Trade] = None
    if finalize_open and whatpos is not None and open_price is not None and open_ts is not None and held_coin:
        open_position = Trade(
            side=whatpos,
            status="open",
            base_coin=held_coin,
            open_price=open_price,
            open_ts=open_ts,
            open_dt=open_dt,
            quantity=pos,
            fees=open_fees,
            okx_latency_ms_open=open_okx_lat,
            bybit_latency_ms_open=open_bybit_lat,
            okx_latency_ms_open_window=open_okx_win,
            bybit_latency_ms_open_window=open_bybit_win,
            latency_window_offsets_open=open_win_offsets,
            latency_window_truncated_open=open_win_trunc,
            signal_open_price=signal_open_price,
            signal_open_ts=signal_open_ts,
            signal_open_dt=signal_open_dt,
            open_fill_delay_ticks=open_fill_delay_ticks,
        )
        trades.append(open_position)

    # Decision-window tick count (exclude pure warmup rows).
    if decision_start_ms is None and decision_end_ms is None:
        n_ticks_out = n
    else:
        n_ticks_out = int(
            np.sum(
                (ts_ms >= (decision_start_ms if decision_start_ms is not None else -np.inf))
                & (ts_ms < (decision_end_ms if decision_end_ms is not None else np.inf))
            )
        )

    result = BacktestResult(
        trades=trades,
        metric=metric,
        open_position=open_position,
        n_signals_raw=n_signals_raw,
        n_signals_passed=n_signals_passed,
        n_filtered_by_freshness=n_filtered_by_freshness,
        n_filtered_by_latency=n_filtered_by_latency,
        n_filtered_by_avg=n_filtered_by_avg,
        n_filtered_by_size=n_filtered_by_size,
        n_filtered_slot_busy=n_filtered_slot_busy,
        n_filtered_pending_skip=n_filtered_pending_skip,
        n_filtered_not_topn=n_filtered_not_topn,
        n_pending_missed=n_pending_missed,
        fees_total=fees_total,
        n_ticks=n_ticks_out,
        n_coins=int(data["base_coin"].nunique()),
    )

    if not return_carry:
        return result

    carry_out = MarketCarryState(
        pos=pos,
        whatpos=whatpos,
        held_coin=held_coin,
        open_price=open_price,
        open_ts=open_ts,
        open_dt=open_dt,
        open_okx_lat=open_okx_lat,
        open_bybit_lat=open_bybit_lat,
        open_okx_win=open_okx_win,
        open_bybit_win=open_bybit_win,
        open_win_offsets=open_win_offsets,
        open_win_trunc=open_win_trunc,
        signal_open_price=signal_open_price,
        signal_open_ts=signal_open_ts,
        signal_open_dt=signal_open_dt,
        open_fill_delay_ticks=open_fill_delay_ticks,
        open_fees=open_fees,
        pending=dict(pending) if pending is not None else None,
    )
    if carry_out.pending is not None:
        carry_out.pending.pop("fill_i", None)
        carry_out.pending.pop("signal_i", None)
    return result, carry_out


def run_backtest_chunked(
    tick_dir: Path | str,
    start_ms: int,
    end_ms: int,
    *,
    variation: dict,
    hyper: dict,
    k: int = 1,
    regime_topn: Optional[dict] = None,
    coins: Optional[set[str]] = None,
    chunk_ms: int = 3_600_000,
    workers: int = 1,
) -> BacktestResult:
    """Hourly (or N-ms) load/prepare/run with K=1 state carry + MA warmup.

    Peak RAM ≈ one chunk (+warmup), not the full window. Honest across boundaries
    for open slot and pending fills; Gate B MA uses ``avg_window_sec`` warmup overlap.
    """
    from research.lean_ticks_io import gear2_lean_columns, read_and_prepare_lean_ticks

    if end_ms <= start_ms:
        raise ValueError("END must be after START")
    chunk_ms = int(chunk_ms)
    if chunk_ms <= 0:
        raise ValueError("chunk_ms must be > 0")

    check_volume = bool(hyper.get("Check_volume", False))
    need_freshness = hyper.get("max_freshness_ms") is not None
    avg_window_sec = hyper.get("avg_window_sec")
    trade_lat = float(hyper.get("Trade_Lat", 0.0))
    warmup_ms = int(
        (float(avg_window_sec) * 1000.0 if avg_window_sec is not None else 0.0)
        + trade_lat
        + 1000.0
    )
    cols = gear2_lean_columns(check_volume=check_volume)

    all_trades: list[Trade] = []
    carry: Optional[MarketCarryState] = None
    totals = dict(
        n_signals_raw=0,
        n_signals_passed=0,
        n_filtered_by_freshness=0,
        n_filtered_by_latency=0,
        n_filtered_by_avg=0,
        n_filtered_by_size=0,
        n_filtered_slot_busy=0,
        n_filtered_pending_skip=0,
        n_filtered_not_topn=0,
        n_pending_missed=0,
        n_ticks=0,
        metric=0.0,
        fees_total=0.0,
    )
    coins_seen: set[str] = set()
    chunk_i = 0
    t0 = start_ms
    while t0 < end_ms:
        t1 = min(t0 + chunk_ms, end_ms)
        load_start = t0 if chunk_i == 0 else max(start_ms, t0 - warmup_ms)
        df, _files = read_and_prepare_lean_ticks(
            Path(tick_dir),
            load_start,
            t1,
            coins=coins,
            workers=workers,
            columns=cols,
            slim_backtest=True,
            check_volume=check_volume,
            need_freshness=need_freshness,
        )
        if coins:
            coins_seen |= {c.upper() for c in coins}
        elif len(df):
            coins_seen |= set(df["base_coin"].astype(str).unique())

        last = t1 >= end_ms
        chunk_res, carry = run_backtest_market(
            df,
            variation=variation,
            hyper=hyper,
            k=k,
            regime_topn=regime_topn,
            decision_start_ms=float(t0),
            decision_end_ms=float(t1),
            carry_in=carry,
            return_carry=True,
            finalize_open=last,
        )
        # Intermediate chunks must not emit open-at-end rows.
        closed_only = [t for t in chunk_res.trades if t.status == "closed"]
        if last:
            all_trades.extend(chunk_res.trades)
        else:
            all_trades.extend(closed_only)
        for key in (
            "n_signals_raw",
            "n_signals_passed",
            "n_filtered_by_freshness",
            "n_filtered_by_latency",
            "n_filtered_by_avg",
            "n_filtered_by_size",
            "n_filtered_slot_busy",
            "n_filtered_pending_skip",
            "n_filtered_not_topn",
            "n_pending_missed",
            "n_ticks",
        ):
            totals[key] += int(getattr(chunk_res, key))
        totals["metric"] += float(chunk_res.metric)
        totals["fees_total"] += float(chunk_res.fees_total)
        print(
            f"chunk {chunk_i}: [{t0},{t1}) ticks={chunk_res.n_ticks} "
            f"closed+={sum(1 for t in closed_only)} "
            f"pending={'Y' if carry and carry.pending else 'n'} "
            f"open={'Y' if carry and carry.whatpos else 'n'}",
            flush=True,
        )
        del df, chunk_res
        gc.collect()
        chunk_i += 1
        t0 = t1

    open_position = None
    if carry is not None and carry.whatpos is not None:
        # Last chunk already appended open trade when finalize_open=True.
        opens = [t for t in all_trades if t.status == "open"]
        open_position = opens[-1] if opens else None
    elif any(t.status == "open" for t in all_trades):
        open_position = [t for t in all_trades if t.status == "open"][-1]

    return BacktestResult(
        trades=all_trades,
        metric=float(totals["metric"]),
        open_position=open_position,
        n_signals_raw=int(totals["n_signals_raw"]),
        n_signals_passed=int(totals["n_signals_passed"]),
        n_filtered_by_freshness=int(totals["n_filtered_by_freshness"]),
        n_filtered_by_latency=int(totals["n_filtered_by_latency"]),
        n_filtered_by_avg=int(totals["n_filtered_by_avg"]),
        n_filtered_by_size=int(totals["n_filtered_by_size"]),
        n_filtered_slot_busy=int(totals["n_filtered_slot_busy"]),
        n_filtered_pending_skip=int(totals["n_filtered_pending_skip"]),
        n_filtered_not_topn=int(totals["n_filtered_not_topn"]),
        n_pending_missed=int(totals["n_pending_missed"]),
        fees_total=float(totals["fees_total"]),
        n_ticks=int(totals["n_ticks"]),
        n_coins=len(coins_seen),
    )



def k1_open_intervals(trades):
    """(open_ts, close_ts_or_inf, coin) sorted by open_ts."""
    rows = []
    for t in trades:
        close = t.close_ts if t.close_ts is not None else float("inf")
        rows.append((float(t.open_ts), float(close), str(t.base_coin)))
    rows.sort(key=lambda x: x[0])
    return rows


def k1_overlaps(trades):
    """Pairs of overlapping K=1 intervals, if any."""
    opens = k1_open_intervals(trades)
    bad = []
    for (a0, a1, ca), (b0, b1, cb) in zip(opens, opens[1:]):
        if a1 > b0:
            bad.append((ca, a0, a1, cb, b0, b1))
    return bad


def assert_k1_invariants(res: BacktestResult) -> None:
    """No two positions/pending-fills as completed trades overlapping; <=1 open-at-end."""
    bad = k1_overlaps(res.trades)
    if bad:
        raise AssertionError("K=1 violated: overlapping positions %s" % (bad,))
    n_open = sum(1 for t in res.trades if t.status == "open")
    if n_open > 1:
        raise AssertionError("K=1 violated: %s open-at-end trades" % n_open)
    if res.open_position is not None and n_open != 1:
        raise AssertionError("open_position set but trades open count=%s" % n_open)
    for t in res.trades:
        if t.status == "closed":
            if t.close_ts is None or t.close_ts < t.open_ts:
                raise AssertionError("closed trade has bad close_ts: %s" % (t,))
            if not t.base_coin:
                raise AssertionError("closed trade missing base_coin")


def closeout_row(arm: str, res: BacktestResult) -> dict:
    """Filter/signal counters for Stage 3 A vs B. No PnL fields."""
    n_open = 1 if res.open_position is not None else 0
    n_closed = sum(1 for t in res.trades if t.status == "closed")
    return {
        "arm": arm,
        "closed": n_closed,
        "open_at_end": n_open,
        "raw": int(res.n_signals_raw),
        "passed": int(res.n_signals_passed),
        "slot_busy": int(res.n_filtered_slot_busy),
        "pending_skip": int(res.n_filtered_pending_skip),
        "freshness": int(res.n_filtered_by_freshness),
        "latency": int(res.n_filtered_by_latency),
        "avg": int(res.n_filtered_by_avg),
        "size": int(res.n_filtered_by_size),
        "not_topn": int(res.n_filtered_not_topn),
        "pending_missed": int(res.n_pending_missed),
        "n_ticks": int(res.n_ticks),
        "n_coins": int(res.n_coins),
    }
