"""Gear 2.2 θ K=1 would_send contour (NO real orders).

Driven off the live theta emit (~1 Hz) inside one BotRuntime. Journals
``theta_trades/`` only. Does not call StubBroker.place / private APIs.
Does not write ticks or D trees.
"""

from __future__ import annotations

import json
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
DEFAULT_BOOK_DEPTH = 1

_GEAR22_TRADE_PROFILES = frozenset(
    {"gear22_would_send", "gear22", "gear2_would_send", "gear2"}
)

LogFn = Callable[[str], None]


def theta_trade_enabled(
    profile: str,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """``BBOT_THETA_TRADE``: 1/0 override; default on for ``gear22_would_send``."""
    e = env if env is not None else os.environ
    raw = str(e.get("BBOT_THETA_TRADE") or "").strip().lower()
    if raw in ("0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    return str(profile).strip().lower() in {"gear22_would_send", "gear22"}


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

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "ThetaTradeConfig":
        e = env if env is not None else os.environ
        return cls(
            theta_thr=_env_float(e, "BBOT_THETA_THR", DEFAULT_THETA_THR),
            fill_delay_ms=_env_int(e, "BBOT_FILL_DELAY_MS", DEFAULT_FILL_DELAY_MS),
            slot_k=max(1, _env_int(e, "BBOT_SLOT_K", DEFAULT_SLOT_K)),
            notional_usdt=_env_float(e, "BBOT_NOTIONAL_USDT", DEFAULT_NOTIONAL_USDT),
            book_depth=max(1, _env_int(e, "BBOT_BOOK_DEPTH", DEFAULT_BOOK_DEPTH)),
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


def decide_theta_k1(
    snapshots: Sequence[ThetaSnapshot],
    *,
    slot: SlotState,
    thr: float,
    quotes: Mapping[str, Mapping[str, Mapping[str, Any]]],
    notional_usdt: float,
    book_depth: int = 1,
    coin_order: Optional[Sequence[str]] = None,
) -> ThetaDecision:
    """Pure K=1 entry/exit decide (no I/O, no sleep).

    Entry: ``theta_1m(side) >= thr``, slot free, book size OK.
    Exit: opposite-side ``theta_1m >= thr`` on the held coin.
    While in position: new entries → ``slot_busy`` skip.
    """
    by_key: dict[tuple[str, str], ThetaSnapshot] = {
        (s.base_coin, s.side): s for s in snapshots
    }

    # Exit first when holding.
    if slot.position is not None and not slot.pending:
        pos = slot.position
        opp = opposite_side(pos.side)
        snap = by_key.get((pos.base_coin, opp))
        opp_th = snap.theta_1m if snap is not None else None
        own = by_key.get((pos.base_coin, pos.side))
        own_th = own.theta_1m if own is not None else None
        if opp_th is not None and float(opp_th) >= float(thr):
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
            return ThetaDecision(
                action="close",
                base_coin=pos.base_coin,
                side=pos.side,
                reason="theta_exit_opposite",
                theta_1m=own_th,
                opposite_theta_1m=float(opp_th),
                size_info=size_info,
            )
        # In position, not exiting — ignore new entries (spec: slot_busy).
        return ThetaDecision(
            action="skip",
            base_coin=pos.base_coin,
            side=pos.side,
            reason="slot_busy",
            theta_1m=own_th,
            opposite_theta_1m=opp_th,
            reject_reason="slot_busy",
        )

    if slot.slot_busy():
        # Pending fill or occupied — ignore entries.
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
        for side in ("long", "short"):
            snap = by_key.get((coin, side))
            if snap is None or snap.theta_1m is None:
                continue
            if float(snap.theta_1m) < float(thr):
                continue
            opp = opposite_side(side)
            opp_snap = by_key.get((coin, opp))
            opp_th = opp_snap.theta_1m if opp_snap is not None else None
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
                        theta_1m=float(snap.theta_1m),
                        opposite_theta_1m=opp_th,
                        reject_reason="insufficient_size",
                        size_info=size_info,
                    )
                continue
            return ThetaDecision(
                action="open",
                base_coin=coin,
                side=side,
                reason="theta_entry",
                theta_1m=float(snap.theta_1m),
                opposite_theta_1m=opp_th,
                size_info=size_info,
            )
    if first_size_reject is not None:
        return first_size_reject
    return ThetaDecision(action="skip", base_coin="", side="", reason="no_signal")


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
    """K=1 would_send manager hooked from the theta emit loop."""

    def __init__(
        self,
        *,
        data_root: Path,
        config: Optional[ThetaTradeConfig] = None,
        journal: Optional[ThetaTradeJournalWriter] = None,
        log: Optional[LogFn] = None,
        sleep_fn: Optional[Callable[[float], Any]] = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.config = config or ThetaTradeConfig.from_env()
        self.journal = journal or ThetaTradeJournalWriter(self.data_root)
        self._log = log or (lambda _m: None)
        # sleep_fn(seconds) — sync sleep; async runtime wraps with asyncio.sleep.
        self._sleep_fn = sleep_fn or time.sleep
        self.slot = SlotState(k=int(self.config.slot_k))
        self._skip_log_budget = 0

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
            "would_send": True,
            "send": False,
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
        position). Signal OK but fill size bad → still journal would_fill with
        ``fill_size_ok=false``.
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

    def on_theta_snapshots(
        self,
        snapshots: Sequence[ThetaSnapshot],
        *,
        quotes: Mapping[str, Mapping[str, Mapping[str, Any]]],
        coin_order: Optional[Sequence[str]] = None,
        now_ms: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Sync entry (unit tests / offline). Prefer ``on_theta_snapshots_async`` live."""
        decision = decide_theta_k1(
            snapshots,
            slot=self.slot,
            thr=self.config.theta_thr,
            quotes=quotes,
            notional_usdt=self.config.notional_usdt,
            book_depth=self.config.book_depth,
            coin_order=coin_order,
        )
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

        decision = decide_theta_k1(
            snapshots,
            slot=self.slot,
            thr=self.config.theta_thr,
            quotes=quotes,
            notional_usdt=self.config.notional_usdt,
            book_depth=self.config.book_depth,
            coin_order=coin_order,
        )

        async def _async_sleep(seconds: float) -> None:
            await asyncio.sleep(seconds)

        prev = self._sleep_fn
        self._sleep_fn = _async_sleep  # type: ignore[assignment]
        try:
            # execute_decision may call sleep_fn — support awaitable.
            return await self._execute_decision_async(
                decision, snapshots=snapshots, quotes=quotes, now_ms=now_ms
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
                )
                self.slot.position = None
            await asyncio.to_thread(self.journal.append_rows, [row])
            self._log(
                f"theta_trade_{decision.action} | trade_id={row['trade_id']} | "
                f"coin={row['base_coin']} | side={row['side']} | "
                f"fill_size_ok={row.get('fill_size_ok')} | "
                f"slip_spread={row.get('slip_spread')}"
            )
            return [row]
        finally:
            self.slot.pending = False

