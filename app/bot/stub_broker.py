"""Dual-leg stub broker: would_send only; fill on next live valid tick after Trade_Lat."""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from app.bot.journal import JournalWriter, build_leg_record
from app.bot.paths import pending_state_path

FEE_RATE = 0.00075
K_LIVE = 1


def snap_to_lot(raw_qty: float, lot: float) -> float:
    """Floor qty to exchange lot/step."""
    if lot <= 0:
        return float(raw_qty)
    n = math.floor(float(raw_qty) / float(lot) + 1e-12)
    return float(n) * float(lot)


def legs_for_spread_side(spread_side: str) -> tuple[tuple[str, str], tuple[str, str]]:
    """Return ((okx_side, bybit_side)) for open_long / open_short / close.

    open_long:  buy OKX ask, sell Bybit bid
    open_short: buy Bybit ask, sell OKX bid
    close: reverse of the open that created the position (caller passes open side
    via ``close_of`` when needed — see ``StubBroker.place``).
    """
    if spread_side == "open_long":
        return ("buy", "sell")
    if spread_side == "open_short":
        return ("sell", "buy")
    raise ValueError(f"legs_for_spread_side expects open_*; got {spread_side}")


def reverse_sides(okx_side: str, bybit_side: str) -> tuple[str, str]:
    flip = {"buy": "sell", "sell": "buy"}
    return flip[okx_side], flip[bybit_side]


def signal_price_for_leg(book: dict[str, Any], leg_side: str) -> float:
    if leg_side == "buy":
        return float(book["ask_price"])
    return float(book["bid_price"])


@dataclass
class InstrumentMeta:
    base_coin: str
    okx_symbol: str
    bybit_symbol: str
    okx_lot_size: float
    okx_min_size: float
    bybit_qty_step: float
    bybit_min_order_qty: float
    okx_tick_size: float = 0.0
    bybit_tick_size: float = 0.0
    bybit_min_notional_value: float = 0.0


@dataclass
class PendingLeg:
    exchange: str
    leg_side: str
    signal_price: float
    qty: float
    notional: float


@dataclass
class PendingIntent:
    intent_id: str
    base_coin: str
    spread_side: str
    signal_ts_ms: int
    place_ts_ms: int
    ack_ts_ms: int
    trade_lat_ms: int
    notional: float
    okx_symbol: str
    bybit_symbol: str
    legs: list[PendingLeg] = field(default_factory=list)
    # For close: which open we reverse
    open_spread_side: Optional[str] = None
    status: str = "acked"  # pending|acked in-memory only
    extra: dict[str, Any] = field(default_factory=dict)


class StubBroker:
    """K_live=1 stub: place/ack in memory+state; terminal rows only in legs.jsonl."""

    def __init__(
        self,
        *,
        data_root: Path,
        journal: JournalWriter,
        trade_lat_ms: int = 100,
        notional_usdt: float = 100.0,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.journal = journal
        self.trade_lat_ms = int(trade_lat_ms)
        self.notional_usdt = float(notional_usdt)
        self._log = log or (lambda _m: None)
        self.pending: Optional[PendingIntent] = None
        self.position: Optional[str] = None  # open_long | open_short | None
        self.held_coin: Optional[str] = None
        self._load_pending()

    def has_pending(self) -> bool:
        return self.pending is not None

    def can_open(self) -> bool:
        return self.pending is None and self.position is None

    def _state_path(self) -> Path:
        return pending_state_path(self.data_root)

    def _persist_pending(self) -> None:
        path = self._state_path()
        if self.pending is None:
            if path.exists():
                path.unlink()
            # also persist flat position marker
            pos_path = path.parent / "position.json"
            if self.position is None:
                if pos_path.exists():
                    pos_path.unlink()
            else:
                pos_path.write_text(
                    json.dumps(
                        {"position": self.position, "held_coin": self.held_coin},
                        separators=(",", ":"),
                    ),
                    encoding="utf-8",
                )
            return
        payload = {
            "pending": asdict(self.pending),
            "position": self.position,
            "held_coin": self.held_coin,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)

    def _load_pending(self) -> None:
        path = self._state_path()
        pos_path = path.parent / "position.json"
        if pos_path.exists():
            try:
                raw = json.loads(pos_path.read_text(encoding="utf-8"))
                self.position = raw.get("position")
                held = raw.get("held_coin")
                self.held_coin = str(held).upper() if held else None
            except (OSError, json.JSONDecodeError):
                pass
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._log(f"stub_broker | pending_state_load_failed | err={exc}")
            return
        if raw.get("position"):
            self.position = raw["position"]
        if raw.get("held_coin"):
            self.held_coin = str(raw["held_coin"]).upper()
        p = raw.get("pending")
        if not p:
            return
        legs = [PendingLeg(**leg) for leg in p.get("legs", [])]
        self.pending = PendingIntent(
            intent_id=p["intent_id"],
            base_coin=p["base_coin"],
            spread_side=p["spread_side"],
            signal_ts_ms=int(p["signal_ts_ms"]),
            place_ts_ms=int(p["place_ts_ms"]),
            ack_ts_ms=int(p["ack_ts_ms"]),
            trade_lat_ms=int(p["trade_lat_ms"]),
            notional=float(p["notional"]),
            okx_symbol=p["okx_symbol"],
            bybit_symbol=p["bybit_symbol"],
            legs=legs,
            open_spread_side=p.get("open_spread_side"),
            status=p.get("status", "acked"),
            extra=dict(p.get("extra") or {}),
        )

    def _qty_plan(
        self,
        *,
        meta: InstrumentMeta,
        okx_side: str,
        bybit_side: str,
        okx_book: dict[str, Any],
        bybit_book: dict[str, Any],
        notional: float,
    ) -> tuple[Optional[tuple[float, float, float, float]], Optional[str]]:
        okx_px = signal_price_for_leg(okx_book, okx_side)
        bybit_px = signal_price_for_leg(bybit_book, bybit_side)
        if okx_px <= 0 or bybit_px <= 0:
            return None, "non_positive_signal_price"
        okx_qty = snap_to_lot(notional / okx_px, meta.okx_lot_size)
        bybit_qty = snap_to_lot(notional / bybit_px, meta.bybit_qty_step)
        if okx_qty < meta.okx_min_size or okx_qty <= 0:
            return None, "okx_qty_below_min"
        if bybit_qty < meta.bybit_min_order_qty or bybit_qty <= 0:
            return None, "bybit_qty_below_min"
        if meta.bybit_min_notional_value > 0 and bybit_qty * bybit_px < meta.bybit_min_notional_value:
            return None, "bybit_notional_below_min"
        return (okx_qty, bybit_qty, okx_px, bybit_px), None

    def place(
        self,
        *,
        spread_side: str,
        base_coin: str,
        signal_ts_ms: int,
        okx_book: dict[str, Any],
        bybit_book: dict[str, Any],
        meta: InstrumentMeta,
        close_of: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> Optional[str]:
        """Create dual-leg pending intent. Returns abort_reason or None on success.

        would_send=true always; send never happens (no private network).
        """
        if self.pending is not None:
            return "k_live_blocked"
        if spread_side in ("open_long", "open_short") and self.position is not None:
            return "already_in_position"
        if spread_side == "close":
            if self.position is None:
                return "flat_cannot_close"
            if (
                self.held_coin is not None
                and str(base_coin).upper() != self.held_coin.upper()
            ):
                return "not_held_coin"
            open_side = close_of or self.position
            okx_side, bybit_side = reverse_sides(*legs_for_spread_side(open_side))
            journal_side = "close"
            open_spread_side = open_side
        else:
            okx_side, bybit_side = legs_for_spread_side(spread_side)
            journal_side = spread_side
            open_spread_side = None

        notional = self.notional_usdt
        plan, abort = self._qty_plan(
            meta=meta,
            okx_side=okx_side,
            bybit_side=bybit_side,
            okx_book=okx_book,
            bybit_book=bybit_book,
            notional=notional,
        )
        place_ts = max(int(time.time() * 1000), int(signal_ts_ms))
        if plan is None:
            # Abort both legs immediately into journal
            intent_id = str(uuid.uuid4())
            try:
                okx_px = signal_price_for_leg(okx_book, okx_side)
            except (KeyError, TypeError, ValueError):
                okx_px = 0.0
            try:
                bybit_px = signal_price_for_leg(bybit_book, bybit_side)
            except (KeyError, TypeError, ValueError):
                bybit_px = 0.0
            self._journal_aborted_pair(
                intent_id=intent_id,
                base_coin=base_coin,
                spread_side=journal_side,
                signal_ts_ms=int(signal_ts_ms),
                place_ts_ms=place_ts,
                okx_side=okx_side,
                bybit_side=bybit_side,
                okx_signal_price=okx_px,
                bybit_signal_price=bybit_px,
                okx_qty=0.0,
                bybit_qty=0.0,
                notional=notional,
                abort_reason=abort or "qty_abort",
                meta=meta,
                extra=extra,
            )
            return abort or "qty_abort"

        okx_qty, bybit_qty, okx_px, bybit_px = plan
        intent_id = str(uuid.uuid4())
        pending = PendingIntent(
            intent_id=intent_id,
            base_coin=base_coin.upper(),
            spread_side=journal_side,
            signal_ts_ms=int(signal_ts_ms),
            place_ts_ms=place_ts,
            ack_ts_ms=place_ts,
            trade_lat_ms=self.trade_lat_ms,
            notional=notional,
            okx_symbol=meta.okx_symbol,
            bybit_symbol=meta.bybit_symbol,
            legs=[
                PendingLeg("okx", okx_side, okx_px, okx_qty, notional),
                PendingLeg("bybit", bybit_side, bybit_px, bybit_qty, notional),
            ],
            open_spread_side=open_spread_side,
            status="acked",
            extra=dict(extra or {}),
        )
        self.pending = pending
        self._persist_pending()
        self._log(
            f"stub_broker | would_send | intent_id={intent_id} | side={journal_side} | "
            f"coin={base_coin} | send=false | k_live={K_LIVE}"
        )
        return None

    def on_valid_tick(
        self,
        *,
        base_coin: str,
        event_local_ts_ms: int,
        okx_book: dict[str, Any],
        bybit_book: dict[str, Any],
    ) -> bool:
        """Fill pending if this live valid tick is at/after signal+Trade_Lat. Returns True if filled."""
        p = self.pending
        if p is None:
            return False
        if p.base_coin.upper() != base_coin.upper():
            return False
        if int(event_local_ts_ms) < int(p.signal_ts_ms) + int(p.trade_lat_ms):
            return False

        fill_ts = int(event_local_ts_ms)
        records = []
        for leg in p.legs:
            book = okx_book if leg.exchange == "okx" else bybit_book
            fill_price = signal_price_for_leg(book, leg.leg_side)
            records.append(
                build_leg_record(
                    intent_id=p.intent_id,
                    base_coin=p.base_coin,
                    exchange=leg.exchange,
                    leg_side=leg.leg_side,
                    spread_side=p.spread_side,
                    signal_ts_ms=p.signal_ts_ms,
                    place_ts_ms=p.place_ts_ms,
                    ack_ts_ms=p.ack_ts_ms,
                    fill_ts_ms=fill_ts,
                    trade_lat_ms=p.trade_lat_ms,
                    signal_price=leg.signal_price,
                    fill_price=fill_price,
                    qty=leg.qty,
                    notional=p.notional,
                    status="filled",
                    tick_valid=True,
                    okx_symbol=p.okx_symbol,
                    bybit_symbol=p.bybit_symbol,
                    extra=p.extra or None,
                )
            )
        okx_rec = next(r for r in records if r["exchange"] == "okx")
        bybit_rec = next(r for r in records if r["exchange"] == "bybit")
        self.journal.append_dual_legs(okx_rec, bybit_rec)

        if p.spread_side in ("open_long", "open_short"):
            self.position = p.spread_side
            self.held_coin = p.base_coin.upper()
        elif p.spread_side == "close":
            self.position = None
            self.held_coin = None
        self.pending = None
        self._persist_pending()
        self._log(
            f"stub_broker | filled | intent_id={okx_rec['intent_id']} | "
            f"fill_ts_ms={fill_ts} | coin={base_coin}"
        )
        return True

    def abort_pending(
        self,
        *,
        abort_reason: str,
        suppress_reason: Optional[str] = None,
    ) -> None:
        p = self.pending
        if p is None:
            return
        self._journal_aborted_from_pending(
            p, abort_reason=abort_reason, suppress_reason=suppress_reason
        )
        self.pending = None
        self._persist_pending()
        self._log(
            f"stub_broker | aborted | intent_id={p.intent_id} | reason={abort_reason}"
        )

    def _journal_aborted_from_pending(
        self,
        p: PendingIntent,
        *,
        abort_reason: str,
        suppress_reason: Optional[str],
    ) -> None:
        records = []
        for leg in p.legs:
            records.append(
                build_leg_record(
                    intent_id=p.intent_id,
                    base_coin=p.base_coin,
                    exchange=leg.exchange,
                    leg_side=leg.leg_side,
                    spread_side=p.spread_side,
                    signal_ts_ms=p.signal_ts_ms,
                    place_ts_ms=p.place_ts_ms,
                    ack_ts_ms=p.ack_ts_ms,
                    fill_ts_ms=None,
                    trade_lat_ms=p.trade_lat_ms,
                    signal_price=leg.signal_price,
                    fill_price=None,
                    qty=leg.qty,
                    notional=p.notional,
                    status="aborted",
                    abort_reason=abort_reason,
                    suppress_reason=suppress_reason,
                    tick_valid=False,
                    okx_symbol=p.okx_symbol,
                    bybit_symbol=p.bybit_symbol,
                    extra=p.extra or None,
                )
            )
        okx_rec = next(r for r in records if r["exchange"] == "okx")
        bybit_rec = next(r for r in records if r["exchange"] == "bybit")
        self.journal.append_dual_legs(okx_rec, bybit_rec)

    def _journal_aborted_pair(
        self,
        *,
        intent_id: str,
        base_coin: str,
        spread_side: str,
        signal_ts_ms: int,
        place_ts_ms: int,
        okx_side: str,
        bybit_side: str,
        okx_signal_price: float,
        bybit_signal_price: float,
        okx_qty: float,
        bybit_qty: float,
        notional: float,
        abort_reason: str,
        meta: InstrumentMeta,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        okx = build_leg_record(
            intent_id=intent_id,
            base_coin=base_coin,
            exchange="okx",
            leg_side=okx_side,
            spread_side=spread_side,
            signal_ts_ms=signal_ts_ms,
            place_ts_ms=place_ts_ms,
            ack_ts_ms=place_ts_ms,
            fill_ts_ms=None,
            trade_lat_ms=self.trade_lat_ms,
            signal_price=float(okx_signal_price),
            fill_price=None,
            qty=float(okx_qty),
            notional=float(notional),
            status="aborted",
            abort_reason=abort_reason,
            tick_valid=False,
            okx_symbol=meta.okx_symbol,
            bybit_symbol=meta.bybit_symbol,
            extra=extra,
        )
        bybit = build_leg_record(
            intent_id=intent_id,
            base_coin=base_coin,
            exchange="bybit",
            leg_side=bybit_side,
            spread_side=spread_side,
            signal_ts_ms=signal_ts_ms,
            place_ts_ms=place_ts_ms,
            ack_ts_ms=place_ts_ms,
            fill_ts_ms=None,
            trade_lat_ms=self.trade_lat_ms,
            signal_price=float(bybit_signal_price),
            fill_price=None,
            qty=float(bybit_qty),
            notional=float(notional),
            status="aborted",
            abort_reason=abort_reason,
            tick_valid=False,
            okx_symbol=meta.okx_symbol,
            bybit_symbol=meta.bybit_symbol,
            extra=extra,
        )
        self.journal.append_dual_legs(okx, bybit)
