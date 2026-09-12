"""Asyncio main loop for the B stub bot."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from app.bot.broker import make_broker
from app.bot.floor_watcher import (
    FloorJournalWriter,
    LiveFloorObserver,
    floor_watch_enabled,
)
from app.bot.journal import JournalWriter
from app.bot.paths import (
    ensure_repo_on_syspath,
    repo_root,
    resolve_data_root,
    resolve_log_path,
)
from app.bot.tw_p50_watcher import (
    EMIT_INTERVAL_SEC,
    LiveTwP50Observer,
    TwP50JournalWriter,
    tw_p50_watch_enabled,
)
from app.bot.theta_screener import (
    LiveThetaScreener,
    ThetaJournalWriter,
    theta_watch_enabled,
)
from app.bot.theta_trade_manager import (
    ThetaTradeConfig,
    ThetaTradeManager,
    theta_trade_enabled,
)
from app.bot.floor_warm import (
    apply_warm_pickle_to_observer,
    export_observer_warm_pickle,
    resolve_floor_warm_path,
)
from app.bot.stub_broker import InstrumentMeta
from app.bot.ws_books import (
    books_ready,
    compute_spreads,
    empty_book,
    run_bybit_orderbook1,
    run_okx_books5,
)
from app.utils.tick_validity import TickValidityGate, book_l1_complete
from app.utils.universe_csv import load_take_yes_base_coins, read_universe_dicts

ensure_repo_on_syspath()
from research.is_crypto import is_crypto  # noqa: E402


def _setup_logger(log_path: Path) -> logging.Logger:
    logger = logging.getLogger("bbot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    # Never attach runtime.log
    if log_path.name == "runtime.log":
        raise RuntimeError("refusing to log to runtime.log")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def load_universe(path: Optional[Path] = None) -> dict[str, InstrumentMeta]:
    """Full instrument meta map, including take=no (lot/tick still needed)."""
    csv_path = path or (repo_root() / "bybit_okx_universe.csv")
    out: dict[str, InstrumentMeta] = {}
    for row in read_universe_dicts(csv_path):
        coin = str(row["base_coin"]).strip().upper()
        out[coin] = InstrumentMeta(
            base_coin=coin,
            okx_symbol=str(row["okx_symbol"]).strip(),
            bybit_symbol=str(row["bybit_symbol"]).strip(),
            okx_lot_size=float(row["okx_lot_size"]),
            okx_min_size=float(row["okx_min_size"]),
            bybit_qty_step=float(row["bybit_qty_step"]),
            bybit_min_order_qty=float(row["bybit_min_order_qty"]),
            okx_tick_size=float(row.get("okx_tick_size") or 0),
            bybit_tick_size=float(row.get("bybit_tick_size") or 0),
            bybit_min_notional_value=float(row.get("bybit_min_notional_value") or 0),
        )
    return out


def load_take_yes_coins(path: Optional[Path] = None) -> list[str]:
    """CSV-driven tradeable coins: take=yes only. Live pair screen."""
    csv_path = path or (repo_root() / "bybit_okx_universe.csv")
    return load_take_yes_base_coins(csv_path)


def parse_coins(raw: str) -> list[str]:
    """Parse an explicit coin list. CSV-driven discovery must use load_take_yes_coins()."""
    coins = [c.strip().upper() for c in raw.split(",") if c.strip()]
    return [c for c in coins if is_crypto(c)]


def _try_load_policy():
    try:
        from app.policy.trade_manager import (
            BotState,
            CANARY_WAL_EDEN_COINS,
            DEFAULT_HYPER,
            DEFAULT_VARIATION,
            GEAR2_WOULD_SEND_COINS,
            SIGNAL_TEST_COINS,
            TickView,
            decide,
            hyper_for_profile,
            live_size_coin_allowed,
            update_causal_ma,
            uses_gear2_market_manager,
            variation_for_profile,
        )
        from app.policy.features import CausalMaWindow
        from app.policy.gear2_market_manager import MarketState, decide_market_tick

        return {
            "decide": decide,
            "decide_market_tick": decide_market_tick,
            "MarketState": MarketState,
            "TickView": TickView,
            "BotState": BotState,
            "CausalMaWindow": CausalMaWindow,
            "update_causal_ma": update_causal_ma,
            "DEFAULT_VARIATION": DEFAULT_VARIATION,
            "DEFAULT_HYPER": DEFAULT_HYPER,
            "variation_for_profile": variation_for_profile,
            "hyper_for_profile": hyper_for_profile,
            "SIGNAL_TEST_COINS": SIGNAL_TEST_COINS,
            "GEAR2_WOULD_SEND_COINS": GEAR2_WOULD_SEND_COINS,
            "CANARY_WAL_EDEN_COINS": CANARY_WAL_EDEN_COINS,
            "uses_gear2_market_manager": uses_gear2_market_manager,
            "live_size_coin_allowed": live_size_coin_allowed,
        }
    except Exception:
        return None


def _jsonable_extra(extra: dict[str, Any]) -> dict[str, Any]:
    """Drop non-JSON values so journal append cannot fail closed on extras."""
    out: dict[str, Any] = {}
    for key, value in extra.items():
        if value is None or isinstance(value, (str, int, bool)):
            out[key] = value
        elif isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                out[key] = None
            else:
                out[key] = value
    return out


def _normalize_intent(raw: Any) -> str:
    if raw is None:
        return "flat"
    if isinstance(raw, str):
        return raw.strip().lower()
    action = getattr(raw, "action", None)
    if action is not None:
        return str(action).strip().lower()
    if isinstance(raw, dict):
        for key in ("intent", "spread_side", "action", "decision"):
            if key in raw and raw[key] is not None:
                return str(raw[key]).strip().lower()
    return "flat"


class BotRuntime:
    def __init__(self) -> None:
        self.mode = (os.environ.get("BBOT_MODE") or "probe").strip().lower()
        if self.mode not in ("probe", "policy"):
            raise ValueError(f"BBOT_MODE must be probe|policy, got {self.mode!r}")
        self.profile = (os.environ.get("BBOT_PROFILE") or "gear1").strip().lower()
        if self.profile not in (
            "gear1",
            "signal_test",
            "default",
            "gear2_would_send",
            "gear2",
            "canary_wal_eden",
            "canary",
            "gear22_would_send",
            "gear22",
        ):
            raise ValueError(
                f"BBOT_PROFILE must be gear1|signal_test|gear2_would_send|"
                f"canary_wal_eden|gear22_would_send, got {self.profile!r}"
            )
        if self.profile == "default":
            self.profile = "gear1"
        if self.profile == "gear2":
            self.profile = "gear2_would_send"
        if self.profile == "gear22":
            self.profile = "gear22_would_send"
        if self.profile == "canary":
            self.profile = "canary_wal_eden"
        
        # Initialize Sentry early (requires profile).
        from app.bot.sentry_setup import init_sentry
        self.sentry_enabled = init_sentry(profile=self.profile)
        coins_raw = (os.environ.get("BBOT_COINS") or "").strip()
        if not coins_raw:
            if self.mode == "policy" and self.profile == "signal_test":
                coins_raw = "BTC,ETH,LA,DOGE"
            elif self.mode == "policy" and self.profile == "gear2_would_send":
                coins_raw = "BTC,ETH,SOL,XRP"
            elif self.mode == "policy" and self.profile == "canary_wal_eden":
                coins_raw = "WAL,EDEN"
            elif self.mode == "policy" and self.profile == "gear22_would_send":
                # August-std HTML top30 — require explicit BBOT_COINS in canary.
                coins_raw = "BTC,ETH,SOL,XRP"
            else:
                coins_raw = "BTC,ETH"
        self.coins = parse_coins(coins_raw)
        if not self.coins:
            raise RuntimeError("BBOT_COINS empty after is_crypto filter")
        if self.profile == "canary_wal_eden":
            from app.policy.trade_manager import live_size_coin_allowed

            bad = [c for c in self.coins if not live_size_coin_allowed(c, self.profile)]
            if bad:
                raise ValueError(
                    f"canary_wal_eden refuses coins {bad}; allowed WAL,EDEN"
                )
        notional_raw = (os.environ.get("BBOT_NOTIONAL_USDT") or "").strip()
        if notional_raw:
            self.notional = float(notional_raw)
        elif self.profile == "canary_wal_eden":
            self.notional = 10.0
        else:
            self.notional = 100.0
        self.trade_lat_ms = int(os.environ.get("BBOT_TRADE_LAT_MS") or "100")
        self.data_root = resolve_data_root()
        self.log_path = resolve_log_path(self.data_root)
        self.log = _setup_logger(self.log_path)
        self.universe = load_universe()
        self.gate = TickValidityGate()
        self.quotes: dict[str, dict[str, dict[str, Any]]] = {
            c: {"okx": empty_book(), "bybit": empty_book()} for c in self.coins
        }
        self.journal = JournalWriter(self.data_root)
        self.broker = make_broker(
            data_root=self.data_root,
            journal=self.journal,
            trade_lat_ms=self.trade_lat_ms,
            notional_usdt=self.notional,
            log=lambda m: self.log.info(m),
        )
        self.policy = _try_load_policy() if self.mode == "policy" else None
        self.variation: dict[str, float] | None = None
        self.hyper: dict[str, object] | None = None
        if self.policy is not None:
            self.variation = self.policy["variation_for_profile"](self.profile)
            self.hyper = self.policy["hyper_for_profile"](self.profile)
            if self.profile == "canary_wal_eden" and self.hyper is not None:
                # Gate planned qty must match the broker notional ($10/leg).
                self.hyper["position_size"] = float(self.notional)
        self.ma_windows: dict[str, Any] = {}
        if self.policy is not None:
            CausalMaWindow = self.policy["CausalMaWindow"]
            avg_sec = float((self.hyper or self.policy["DEFAULT_HYPER"]).get("avg_window_sec") or 2.0)
            for c in self.coins:
                self.ma_windows[c] = CausalMaWindow(avg_window_sec=avg_sec)
        self.market_state: Any = None
        if self.policy is not None and self._uses_market_manager():
            MarketState = self.policy["MarketState"]
            pos = self.broker.position
            position_side = None
            if pos == "open_long":
                position_side = "long"
            elif pos == "open_short":
                position_side = "short"
            self.market_state = MarketState(
                position_side=position_side,
                held_coin=getattr(self.broker, "held_coin", None),
                pending_fill=self.broker.has_pending(),
                pending_coin=(
                    self.broker.pending.base_coin if self.broker.pending is not None else None
                ),
                k_live=1,
            )
        self.probe_done = False
        self.probe_intent_placed = False
        self._lock = asyncio.Lock()
        # Coalesce book ticks: at most one in-flight _handle_book per base_coin.
        self._book_inflight: dict[str, bool] = {c: False for c in self.coins}
        self._book_dirty: dict[str, bool] = {c: False for c in self.coins}
        self._book_last_exchange: dict[str, str] = {c: "okx" for c in self.coins}
        self.stop_event = asyncio.Event()
        self._heartbeat_n = 0
        self._ma_cache: dict[str, tuple[Optional[float], Optional[float]]] = {
            c: (None, None) for c in self.coins
        }
        self._private_warm: Any = None
        self._l1_ring_warned = False
        # Gear 2.2 floor observer (5m bar metrics). Off critical decide/send path.
        self.floor_enabled = floor_watch_enabled(self.profile)
        self.floor_observer: LiveFloorObserver | None = None
        self.floor_journal: FloorJournalWriter | None = None
        self._floor_pending: list[dict[str, Any]] = []
        self._floor_flush_warned = False
        if self.floor_enabled:
            self.floor_observer = LiveFloorObserver(self.coins)
            self.floor_journal = FloorJournalWriter(self.data_root)
        # Rolling TW p50 (1m/5m) observer — alongside floor, not instead of it.
        self.tw_p50_enabled = tw_p50_watch_enabled(self.profile)
        self.tw_p50_observer: LiveTwP50Observer | None = None
        self.tw_p50_journal: TwP50JournalWriter | None = None
        self._tw_p50_flush_warned = False
        if self.tw_p50_enabled:
            self.tw_p50_observer = LiveTwP50Observer(self.coins)
            self.tw_p50_journal = TwP50JournalWriter(self.data_root)
        # Theta = p50 − floor; ~1 Hz follow-on to tw_p50 emit (never ticks).
        self.theta_enabled = theta_watch_enabled(self.profile)
        self.theta_screener: LiveThetaScreener | None = None
        self.theta_journal: ThetaJournalWriter | None = None
        self._theta_flush_warned = False
        if self.theta_enabled:
            self.theta_screener = LiveThetaScreener(
                self.coins,
                floor_observer=self.floor_observer,
                tw_p50_observer=self.tw_p50_observer,
            )
            self.theta_journal = ThetaJournalWriter(self.data_root)
        # Gear 2.2 θ K=1 would_send (separate journal; no StubBroker.place).
        self.theta_trade_enabled = theta_trade_enabled(self.profile)
        self.theta_trade: ThetaTradeManager | None = None
        self._theta_trade_warned = False
        if self.theta_trade_enabled:
            self.theta_trade = ThetaTradeManager(
                data_root=self.data_root,
                config=ThetaTradeConfig.from_env(),
                log=lambda m: self.log.info(m),
            )
        # Floor warm-start (standard for gear22 / when pickle present).
        self._floor_warm_path = resolve_floor_warm_path(self.data_root)
        self._floor_warm_loaded = False
        if self.floor_observer is not None:
            warm_path = self._floor_warm_path
            force_warm = str(os.environ.get("BBOT_FLOOR_WARM") or "").strip().lower()
            want_warm = force_warm in ("1", "true", "on", "yes") or (
                self.profile == "gear22_would_send" and force_warm not in ("0", "false", "off", "no")
            )
            if want_warm or warm_path.is_file():
                if warm_path.is_file():
                    try:
                        n = apply_warm_pickle_to_observer(self.floor_observer, warm_path)
                        self._floor_warm_loaded = True
                        self.log.info(
                            "floor_warm_loaded | path=%s | touched=%s",
                            warm_path,
                            n,
                        )
                    except Exception as exc:  # noqa: BLE001
                        self.log.warning(
                            "floor_warm_load_failed | path=%s | err=%s",
                            warm_path,
                            type(exc).__name__,
                        )
                elif want_warm:
                    self.log.warning(
                        "floor_warm_missing | path=%s | theta may stay null until "
                        "~12h SMA-12 history; build via python -m app.bot.floor_warm",
                        warm_path,
                    )

    def _uses_market_manager(self) -> bool:
        if self.policy is not None:
            fn = self.policy.get("uses_gear2_market_manager")
            if fn is not None:
                return bool(fn(self.profile))
        return self.profile in ("gear2_would_send", "canary_wal_eden")

    def start_private_warm_if_live_send(self, **overrides: Any) -> Any:
        """Warm private WS before the signal loop when live private send is armed.

        ON BY DEFAULT for ``VENUE=live`` + ``LIVE_ORDERS=1`` (no opt-in flag).
        Stub / would_send units leave LIVE_ORDERS off and get ``None``.
        """
        from app.bot.private.ws_warm_session import start_warm_private_for_bot_process

        return start_warm_private_for_bot_process(**overrides)

    def _meta(self, coin: str) -> InstrumentMeta:
        if coin not in self.universe:
            raise KeyError(f"{coin} missing from bybit_okx_universe.csv")
        return self.universe[coin]

    def _on_lifecycle(self, base_coin: str, exchange: str, event: str) -> None:
        channel = "books5" if exchange == "okx" else "orderbook.1"
        if event == "subscribe_ok":
            gen = self.gate.note_subscribe_ok(base_coin, channel)
            self.log.info(
                f"ws_subscribe_ok | coin={base_coin} | exchange={exchange} | "
                f"channel={channel} | generation={gen}"
            )
        elif event == "disconnect":
            self.gate.note_disconnect(base_coin, exchange)
            self.log.warning(f"ws_disconnect | coin={base_coin} | exchange={exchange}")
            if (
                self.broker.pending is not None
                and self.broker.pending.base_coin.upper() == base_coin.upper()
            ):
                self.broker.abort_pending(
                    abort_reason="disconnect",
                    suppress_reason="disconnect",
                )
        elif event == "cancelled":
            self.gate.note_disconnect(base_coin, exchange)
            self.log.info(f"ws_cancelled | coin={base_coin} | exchange={exchange}")

    def _maybe_record_l1(self, base_coin: str, exchange: str, book: dict[str, Any]) -> None:
        """Cheap public-book ring append. Before coalesce so intermediate ticks stay."""
        try:
            from app.bot.private.l1_tick_ring import record_public_l1

            record_public_l1(
                coin=base_coin,
                venue=exchange,
                book=book,
                profile=self.profile,
            )
        except Exception:  # noqa: BLE001 — never stall the public book path
            if not getattr(self, "_l1_ring_warned", False):
                self._l1_ring_warned = True
                self.log.warning("l1_ring_append_failed | coin=%s | exchange=%s", base_coin, exchange)

    def _on_book(self, base_coin: str, exchange: str, book: dict[str, Any]) -> None:
        """Coalesce: one in-flight handler per coin; dirty flag drains one more run."""
        self._maybe_record_l1(base_coin, exchange, book)
        coin = base_coin.upper()
        self._book_last_exchange[coin] = exchange
        if self._book_inflight.get(coin):
            self._book_dirty[coin] = True
            return
        self._book_inflight[coin] = True
        self._book_dirty[coin] = False
        asyncio.create_task(self._coalesced_handle_book(coin), name=f"tick-{coin}")

    async def _coalesced_handle_book(self, base_coin: str) -> None:
        try:
            while True:
                exchange = self._book_last_exchange.get(base_coin, "okx")
                pending_floor: list[dict[str, Any]] = []
                async with self._lock:
                    # Refresh gate for the other exchange (book may have updated while coalesced).
                    other = "bybit" if exchange == "okx" else "okx"
                    other_book = self.quotes[base_coin][other]
                    self.gate.note_book_update(
                        base_coin,
                        other,
                        complete_l1=book_l1_complete(other_book),
                    )
                    self._handle_book_sync(base_coin, exchange)
                    # Drain bar-close rows inside the lock; flush I/O after release.
                    if self._floor_pending:
                        pending_floor = self._floor_pending
                        self._floor_pending = []
                if pending_floor:
                    asyncio.create_task(
                        self._flush_floor_rows(pending_floor),
                        name=f"floor-{base_coin}",
                    )
                if not self._book_dirty.get(base_coin):
                    break
                self._book_dirty[base_coin] = False
        finally:
            self._book_inflight[base_coin] = False
            # Tick may have arrived after dirty check but before inflight clear.
            if self._book_dirty.get(base_coin):
                self._book_dirty[base_coin] = False
                self._book_inflight[base_coin] = True
                asyncio.create_task(
                    self._coalesced_handle_book(base_coin), name=f"tick-{base_coin}"
                )

    async def _flush_floor_rows(self, rows: list[dict[str, Any]]) -> None:
        """Persist floor metric rows off the book/decide lock."""
        if not rows or self.floor_journal is None:
            return
        try:
            await asyncio.to_thread(self.floor_journal.append_rows, rows)
        except Exception as exc:  # noqa: BLE001 — never stall the public book path
            if not self._floor_flush_warned:
                self._floor_flush_warned = True
                self.log.warning(
                    "floor_journal_flush_failed | err=%s | n=%s",
                    type(exc).__name__,
                    len(rows),
                )

    async def _handle_book(self, base_coin: str, exchange: str) -> None:
        pending_floor: list[dict[str, Any]] = []
        async with self._lock:
            self._handle_book_sync(base_coin, exchange)
            if self._floor_pending:
                pending_floor = self._floor_pending
                self._floor_pending = []
        if pending_floor:
            await self._flush_floor_rows(pending_floor)

    def _handle_book_sync(self, base_coin: str, exchange: str) -> None:
        book = self.quotes[base_coin][exchange]
        complete = book_l1_complete(book)
        self.gate.note_book_update(base_coin, exchange, complete_l1=complete)
        okx = self.quotes[base_coin]["okx"]
        bybit = self.quotes[base_coin]["bybit"]
        if not books_ready(okx, bybit):
            return

        # event_local_ts_ms: local time when both books complete and validity evaluated
        event_local_ts_ms = int(time.time() * 1000)
        suppress = self.gate.evaluate(base_coin, okx, bybit, float(event_local_ts_ms))
        if suppress is not None:
            # Do not trade suppress/stale; pending waits for next valid tick.
            # Disconnect abort is handled in lifecycle; generation suppress alone waits.
            return

        try:
            spread_long, spread_short = compute_spreads(okx, bybit)
        except (TypeError, ZeroDivisionError, KeyError):
            return

        # Gear 2.2 floor: cheap in-memory note on the same valid spread stream.
        # Bar-close rows are queued; journal flush runs after the lock (async).
        self._floor_note_spreads(
            base_coin, event_local_ts_ms, spread_long, spread_short
        )
        # Rolling TW p50: append-only on the same stream; 1 Hz task computes/journals.
        self._tw_p50_note_spreads(
            base_coin, event_local_ts_ms, spread_long, spread_short
        )

        # Fill pending on next live VALID tick after Trade_Lat (no asyncio.sleep).
        if self.broker.has_pending():
            filled = self.broker.on_valid_tick(
                base_coin=base_coin,
                event_local_ts_ms=event_local_ts_ms,
                okx_book=okx,
                bybit_book=bybit,
            )
            if filled:
                self._sync_market_state_from_broker()
                if self.mode == "probe" and self.probe_intent_placed:
                    self.probe_done = True
                return
            if self.mode == "policy" and self._uses_market_manager():
                self._policy_maybe_act(
                    base_coin=base_coin,
                    event_local_ts_ms=event_local_ts_ms,
                    okx=okx,
                    bybit=bybit,
                    spread_long=spread_long,
                    spread_short=spread_short,
                )
            return

        if self.mode == "probe":
            self._probe_maybe_open(
                base_coin=base_coin,
                event_local_ts_ms=event_local_ts_ms,
                okx=okx,
                bybit=bybit,
                spread_long=spread_long,
                spread_short=spread_short,
            )
            return

        # policy mode
        self._policy_maybe_act(
            base_coin=base_coin,
            event_local_ts_ms=event_local_ts_ms,
            okx=okx,
            bybit=bybit,
            spread_long=spread_long,
            spread_short=spread_short,
        )

    def _probe_maybe_open(
        self,
        *,
        base_coin: str,
        event_local_ts_ms: int,
        okx: dict[str, Any],
        bybit: dict[str, Any],
        spread_long: float,
        spread_short: float,
    ) -> None:
        if self.probe_done or self.probe_intent_placed:
            return
        if not self.broker.can_open():
            return
        meta = self._meta(base_coin)
        # Prefer open_long; if long mapping not usable, open_short.
        side = "open_long"
        if not self._long_usable(okx, bybit):
            if not self._short_usable(okx, bybit):
                self.log.warning(
                    f"probe_skip | coin={base_coin} | reason=spreads_not_usable"
                )
                return
            side = "open_short"
        abort = self.broker.place(
            spread_side=side,
            base_coin=base_coin,
            signal_ts_ms=event_local_ts_ms,
            okx_book=okx,
            bybit_book=bybit,
            meta=meta,
        )
        self.probe_intent_placed = True
        if abort:
            self.log.warning(
                f"probe_aborted | coin={base_coin} | side={side} | reason={abort} | "
                f"long={spread_long:.6f} | short={spread_short:.6f}"
            )
            self.probe_done = True
        else:
            self.log.info(
                f"probe_placed | coin={base_coin} | side={side} | "
                f"long={spread_long:.6f} | short={spread_short:.6f}"
            )

        # Mark probe complete once terminal fill/abort happens; watch pending.
        if not self.broker.has_pending():
            self.probe_done = True

    def _long_usable(self, okx: dict[str, Any], bybit: dict[str, Any]) -> bool:
        try:
            return (
                bybit.get("bid_price") is not None
                and okx.get("ask_price") is not None
                and float(bybit["bid_price"]) > 0
                and float(okx["ask_price"]) > 0
            )
        except (TypeError, ValueError):
            return False

    def _short_usable(self, okx: dict[str, Any], bybit: dict[str, Any]) -> bool:
        try:
            return (
                okx.get("bid_price") is not None
                and bybit.get("ask_price") is not None
                and float(okx["bid_price"]) > 0
                and float(bybit["ask_price"]) > 0
            )
        except (TypeError, ValueError):
            return False

    def _sync_market_state_from_broker(self) -> None:
        if self.market_state is None:
            return
        pos = self.broker.position
        position_side = None
        if pos == "open_long":
            position_side = "long"
        elif pos == "open_short":
            position_side = "short"
        self.market_state.position_side = position_side
        self.market_state.held_coin = getattr(self.broker, "held_coin", None)
        self.market_state.pending_fill = self.broker.has_pending()
        self.market_state.pending_coin = (
            self.broker.pending.base_coin if self.broker.pending is not None else None
        )

    def _floor_note_spreads(
        self,
        base_coin: str,
        event_local_ts_ms: int,
        spread_long: float,
        spread_short: float,
    ) -> None:
        """In-memory floor bar update; queue closed rows for async flush."""
        if self.floor_observer is None:
            return
        try:
            rows = self.floor_observer.note_spreads(
                base_coin,
                event_local_ts_ms,
                spread_long,
                spread_short,
            )
        except Exception as exc:  # noqa: BLE001 — observer must not break decide
            if not self._floor_flush_warned:
                self._floor_flush_warned = True
                self.log.warning(
                    "floor_note_failed | coin=%s | err=%s",
                    base_coin,
                    type(exc).__name__,
                )
            return
        if rows:
            self._floor_pending.extend(rows)

    def _tw_p50_note_spreads(
        self,
        base_coin: str,
        event_local_ts_ms: int,
        spread_long: float,
        spread_short: float,
    ) -> None:
        """Cheap ring append for rolling TW p50 (no compute / I/O)."""
        if self.tw_p50_observer is None:
            return
        try:
            self.tw_p50_observer.note_spreads(
                base_coin,
                event_local_ts_ms,
                spread_long,
                spread_short,
            )
        except Exception as exc:  # noqa: BLE001 — observer must not break decide
            if not self._tw_p50_flush_warned:
                self._tw_p50_flush_warned = True
                self.log.warning(
                    "tw_p50_note_failed | coin=%s | err=%s",
                    base_coin,
                    type(exc).__name__,
                )

    async def _flush_tw_p50_rows(self, rows: list[dict[str, Any]]) -> None:
        """Persist TW p50 metric rows off the book/decide lock."""
        if not rows or self.tw_p50_journal is None:
            return
        try:
            await asyncio.to_thread(self.tw_p50_journal.append_rows, rows)
        except Exception as exc:  # noqa: BLE001 — never stall the public book path
            if not self._tw_p50_flush_warned:
                self._tw_p50_flush_warned = True
                self.log.warning(
                    "tw_p50_journal_flush_failed | err=%s | n=%s",
                    type(exc).__name__,
                    len(rows),
                )

    async def _tw_p50_emit_loop(self) -> None:
        """~1 Hz: compute TW p50 snapshots, journal under tw_p50/ (never ticks).

        When theta is enabled, emit theta rows immediately after (same cadence).
        """
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=EMIT_INTERVAL_SEC
                )
                break
            except asyncio.TimeoutError:
                pass
            if self.tw_p50_observer is None:
                continue
            try:
                snapshots = await asyncio.to_thread(
                    self.tw_p50_observer.compute_snapshots
                )
            except Exception as exc:  # noqa: BLE001
                if not self._tw_p50_flush_warned:
                    self._tw_p50_flush_warned = True
                    self.log.warning(
                        "tw_p50_compute_failed | err=%s",
                        type(exc).__name__,
                    )
                continue
            # Skip sides that have never received a tick (n_5m==0 and no coverage).
            rows = [
                s.as_row()
                for s in snapshots
                if s.n_5m > 0 or s.coverage_5m > 0.0 or s.n_1m > 0
            ]
            if rows:
                await self._flush_tw_p50_rows(rows)
            if self.theta_enabled and self.theta_screener is not None:
                await self._emit_theta_from_tw(snapshots)

    async def _theta_emit_loop(self) -> None:
        """~1 Hz theta when TW p50 watch is off but theta is on."""
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=EMIT_INTERVAL_SEC
                )
                break
            except asyncio.TimeoutError:
                pass
            if self.theta_screener is None:
                continue
            try:
                snaps = await asyncio.to_thread(self.theta_screener.compute_snapshots)
            except Exception as exc:  # noqa: BLE001
                if not self._theta_flush_warned:
                    self._theta_flush_warned = True
                    self.log.warning(
                        "theta_compute_failed | err=%s",
                        type(exc).__name__,
                    )
                continue
            if snaps:
                await self._flush_theta_rows([s.as_row() for s in snaps])
                if self.theta_trade_enabled and self.theta_trade is not None:
                    await self._run_theta_trade(snaps)

    async def _emit_theta_from_tw(self, tw_snapshots: list[Any]) -> None:
        """Follow-on: theta from TW snapshots + last floor (never ticks)."""
        if self.theta_screener is None:
            return
        try:
            theta_snaps = await asyncio.to_thread(
                self.theta_screener.compute_from_tw_snapshots, tw_snapshots
            )
        except Exception as exc:  # noqa: BLE001
            if not self._theta_flush_warned:
                self._theta_flush_warned = True
                self.log.warning(
                    "theta_compute_failed | err=%s",
                    type(exc).__name__,
                )
            return
        # Only journal sides that had some TW activity (same filter as tw_p50).
        active_keys = {
            (s.base_coin, s.side)
            for s in tw_snapshots
            if s.n_5m > 0 or s.coverage_5m > 0.0 or s.n_1m > 0
        }
        rows = [
            s.as_row()
            for s in theta_snaps
            if (s.base_coin, s.side) in active_keys
        ]
        if rows:
            await self._flush_theta_rows(rows)
        if self.theta_trade_enabled and self.theta_trade is not None:
            await self._run_theta_trade(theta_snaps)

    async def _run_theta_trade(self, theta_snaps: list[Any]) -> None:
        """K=1 would_send decide+fill off the theta emit (never tick WAL)."""
        if self.theta_trade is None or not theta_snaps:
            return
        try:
            await self.theta_trade.on_theta_snapshots_async(
                theta_snaps,
                quotes=self.quotes,
                coin_order=self.coins,
            )
        except Exception as exc:  # noqa: BLE001 — never stall the public book path
            if not self._theta_trade_warned:
                self._theta_trade_warned = True
                self.log.warning(
                    "theta_trade_failed | err=%s",
                    type(exc).__name__,
                )

    async def _flush_theta_rows(self, rows: list[dict[str, Any]]) -> None:
        """Persist theta metric rows off the book/decide lock."""
        if not rows or self.theta_journal is None:
            return
        try:
            await asyncio.to_thread(self.theta_journal.append_rows, rows)
        except Exception as exc:  # noqa: BLE001 — never stall the public book path
            if not self._theta_flush_warned:
                self._theta_flush_warned = True
                self.log.warning(
                    "theta_journal_flush_failed | err=%s | n=%s",
                    type(exc).__name__,
                    len(rows),
                )

    def _policy_maybe_act(
        self,
        *,
        base_coin: str,
        event_local_ts_ms: int,
        okx: dict[str, Any],
        bybit: dict[str, Any],
        spread_long: float,
        spread_short: float,
    ) -> None:
        # Theta K=1 would_send owns the slot when enabled — no tick-path opens.
        if self.theta_trade_enabled:
            return
        if self.policy is None:
            # Refuse to open; keep WS heartbeat alive.
            if self._heartbeat_n % 100 == 0:
                self.log.error(
                    "policy_missing | app.policy.trade_manager.decide not importable; "
                    "refusing opens"
                )
            return
        if self.broker.has_pending() and not self._uses_market_manager():
            return

        TickView = self.policy["TickView"]
        update_causal_ma = self.policy["update_causal_ma"]
        hyper = self.hyper if self.hyper is not None else self.policy["DEFAULT_HYPER"]

        okx_lat = okx.get("delivery_latency_ms")
        bybit_lat = bybit.get("delivery_latency_ms")
        okx_fresh = None
        bybit_fresh = None
        if okx.get("local_recv_ts_ms") is not None:
            okx_fresh = float(event_local_ts_ms) - float(okx["local_recv_ts_ms"])
        if bybit.get("local_recv_ts_ms") is not None:
            bybit_fresh = float(event_local_ts_ms) - float(bybit["local_recv_ts_ms"])

        # Update MA before decide so Gate B sees causal window.
        tick_for_ma = TickView(
            event_local_ts_ms=float(event_local_ts_ms),
            okx_bid=okx.get("bid_price"),
            okx_ask=okx.get("ask_price"),
            bybit_bid=bybit.get("bid_price"),
            bybit_ask=bybit.get("ask_price"),
            okx_bid_size=okx.get("bid_size"),
            okx_ask_size=okx.get("ask_size"),
            bybit_bid_size=bybit.get("bid_size"),
            bybit_ask_size=bybit.get("ask_size"),
            spread_long=spread_long,
            spread_short=spread_short,
            okx_latency_ms=okx_lat,
            bybit_latency_ms=bybit_lat,
            okx_freshness_ms=okx_fresh,
            bybit_freshness_ms=bybit_fresh,
            suppressed=False,
            stale=False,
            valid=True,
        )
        ma_long, ma_short = update_causal_ma(
            self.ma_windows[base_coin],
            tick_for_ma,
            hyper,
        )
        self._ma_cache[base_coin] = (ma_long, ma_short)

        tick = TickView(
            event_local_ts_ms=float(event_local_ts_ms),
            okx_bid=okx.get("bid_price"),
            okx_ask=okx.get("ask_price"),
            bybit_bid=bybit.get("bid_price"),
            bybit_ask=bybit.get("ask_price"),
            okx_bid_size=okx.get("bid_size"),
            okx_ask_size=okx.get("ask_size"),
            bybit_bid_size=bybit.get("bid_size"),
            bybit_ask_size=bybit.get("ask_size"),
            spread_long=spread_long,
            spread_short=spread_short,
            ma_long=ma_long,
            ma_short=ma_short,
            okx_latency_ms=okx_lat,
            bybit_latency_ms=bybit_lat,
            okx_freshness_ms=okx_fresh,
            bybit_freshness_ms=bybit_fresh,
            suppressed=False,
            stale=False,
            valid=True,
        )

        extra = {
            "spread_long": spread_long,
            "spread_short": spread_short,
            "ma_long": ma_long,
            "ma_short": ma_short,
            "okx_latency_ms": okx_lat,
            "bybit_latency_ms": bybit_lat,
            "bbot_profile": self.profile,
            "avg_window_sec": hyper.get("avg_window_sec"),
            "max_latency_okx_ms": hyper.get("max_latency_okx_ms"),
            "max_latency_bybit_ms": hyper.get("max_latency_bybit_ms"),
        }
        if self._uses_market_manager():
            extra["gear2_arm"] = "A"
            extra["k_policy"] = 1
        if self.profile == "canary_wal_eden":
            extra["canary_contour"] = "wal_eden"
            extra["check_l1_depth"] = True
        if self.variation is not None:
            extra["thresh_open_long"] = self.variation["thresh_open_long"]
            extra["thresh_open_short"] = self.variation["thresh_open_short"]
            extra["thresh_close_long"] = self.variation["thresh_close_long"]
            extra["thresh_close_short"] = self.variation["thresh_close_short"]
            extra["open_frac"] = self.variation["open_frac"]
            extra["close_frac"] = self.variation["close_frac"]

        if self._uses_market_manager():
            self._gear2_maybe_act(
                base_coin=base_coin,
                event_local_ts_ms=event_local_ts_ms,
                okx=okx,
                bybit=bybit,
                tick=tick,
                extra=extra,
            )
            return

        BotState = self.policy["BotState"]
        decide = self.policy["decide"]
        pos = self.broker.position
        position_side = None
        if pos == "open_long":
            position_side = "long"
        elif pos == "open_short":
            position_side = "short"
        state = BotState(
            position_side=position_side,
            pending_fill=False,
            k_live=1,
        )
        try:
            raw = decide(
                tick,
                state,
                self.variation if self.variation is not None else self.policy["DEFAULT_VARIATION"],
                hyper,
            )
        except Exception as exc:
            self.log.error(f"policy_error | {exc}")
            from app.bot.sentry_setup import capture_exception
            capture_exception(exc, extras={"profile": self.profile, "coin": base_coin})
            return

        intent = _normalize_intent(raw)
        self._place_from_intent(
            intent=intent,
            reason=getattr(raw, "reason", ""),
            base_coin=base_coin,
            event_local_ts_ms=event_local_ts_ms,
            okx=okx,
            bybit=bybit,
            extra=extra,
        )

    def _gear2_maybe_act(
        self,
        *,
        base_coin: str,
        event_local_ts_ms: int,
        okx: dict[str, Any],
        bybit: dict[str, Any],
        tick: Any,
        extra: dict[str, Any],
    ) -> None:
        decide_market_tick = self.policy["decide_market_tick"]
        self._sync_market_state_from_broker()
        try:
            decision = decide_market_tick(
                tick,
                base_coin,
                self.market_state,
                self.variation,
                self.hyper,
            )
        except Exception as exc:
            self.log.error(f"gear2_policy_error | {exc}")
            from app.bot.sentry_setup import capture_exception
            capture_exception(exc, extras={"profile": self.profile, "coin": base_coin})
            return
        extra["held_coin"] = self.market_state.held_coin
        extra["ordering_key"] = decision.ordering_key
        extra["decision_reason"] = decision.reason
        extra.update(decision.counters)
        extra = _jsonable_extra(extra)
        intent = _normalize_intent(decision.action)
        if intent in ("flat", "hold", "none", ""):
            if decision.reason in ("pending_skip", "slot_busy") and self._heartbeat_n % 20 == 0:
                self.log.info(
                    f"gear2_{decision.reason} | coin={base_coin} | held={self.market_state.held_coin}"
                )
            return
        self._place_from_intent(
            intent=intent,
            reason=decision.reason,
            base_coin=base_coin,
            event_local_ts_ms=event_local_ts_ms,
            okx=okx,
            bybit=bybit,
            extra=extra,
        )
        self._sync_market_state_from_broker()

    def _place_from_intent(
        self,
        *,
        intent: str,
        reason: str,
        base_coin: str,
        event_local_ts_ms: int,
        okx: dict[str, Any],
        bybit: dict[str, Any],
        extra: dict[str, Any],
    ) -> None:
        if intent in ("flat", "hold", "none", ""):
            return
        extra = _jsonable_extra(extra)
        if intent in ("open_long", "open_short"):
            if not self.broker.can_open():
                return
            abort = self.broker.place(
                spread_side=intent,
                base_coin=base_coin,
                signal_ts_ms=event_local_ts_ms,
                okx_book=okx,
                bybit_book=bybit,
                meta=self._meta(base_coin),
                extra=extra,
            )
            if abort:
                self.log.warning(f"policy_place_abort | coin={base_coin} | reason={abort}")
            else:
                self.log.info(
                    f"policy_placed | coin={base_coin} | side={intent} | reason={reason}"
                )
            return
        if intent == "close":
            if self.broker.position is None:
                return
            abort = self.broker.place(
                spread_side="close",
                base_coin=base_coin,
                signal_ts_ms=event_local_ts_ms,
                okx_book=okx,
                bybit_book=bybit,
                meta=self._meta(base_coin),
                close_of=self.broker.position,
                extra=extra,
            )
            if abort:
                self.log.warning(f"policy_close_abort | coin={base_coin} | reason={abort}")
            return
        self.log.warning(f"policy_unknown_intent | intent={intent!r}")

    async def _heartbeat(self) -> None:
        while not self.stop_event.is_set():
            self._heartbeat_n += 1
            snap = self.gate.heartbeat_fields()
            pending = self.broker.pending.intent_id if self.broker.pending else None
            # Mark probe done after fill cleared pending
            if self.mode == "probe" and self.probe_intent_placed and not self.broker.has_pending():
                self.probe_done = True
            held = getattr(self.broker, "held_coin", None)
            counters = ""
            if self.market_state is not None:
                c = self.market_state.snapshot_counters()
                counters = (
                    f" | held={held} | seq={c.get('seq')} | "
                    f"raw={c.get('n_signals_raw')} | "
                    f"slot_busy={c.get('n_filtered_slot_busy')} | "
                    f"pending_skip={c.get('n_filtered_pending_skip')}"
                )
            self.log.info(
                "heartbeat | mode=%s | profile=%s | coins=%s | data_root=%s | accepted=%s | "
                "sup_stale=%s | sup_gen=%s | pending=%s | position=%s | probe_done=%s%s"
                % (
                    self.mode,
                    self.profile,
                    ",".join(self.coins),
                    self.data_root,
                    snap["ticks_accepted"],
                    snap["ticks_suppressed_stale"],
                    snap["ticks_suppressed_generation"],
                    pending,
                    self.broker.position,
                    self.probe_done,
                    counters,
                )
            )
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        thresh = None
        thresh_cs = None
        avg_sec = None
        if self.variation is not None:
            thresh = self.variation.get("thresh_open_long")
            thresh_cs = (
                f"{self.variation.get('thresh_open_long')}/"
                f"{self.variation.get('thresh_open_short')}/"
                f"{self.variation.get('thresh_close_long')}/"
                f"{self.variation.get('thresh_close_short')}"
            )
        if self.hyper is not None:
            avg_sec = self.hyper.get("avg_window_sec")
        self.log.info(
            f"bbot_start | mode={self.mode} | profile={self.profile} | "
            f"coins={','.join(self.coins)} | thresh_open={thresh} | "
            f"thresh={thresh_cs} | avg_window_sec={avg_sec} | "
            f"data_root={self.data_root} | log={self.log_path} | "
            f"notional={self.notional} | trade_lat_ms={self.trade_lat_ms} | "
            f"floor_watch={'on' if self.floor_enabled else 'off'} | "
            f"tw_p50_watch={'on' if self.tw_p50_enabled else 'off'} | "
            f"theta_watch={'on' if self.theta_enabled else 'off'} | "
            f"theta_trade={'on' if self.theta_trade_enabled else 'off'} | "
            f"floor_warm={'loaded' if self._floor_warm_loaded else 'off'}"
        )
        if self.mode == "policy" and self.policy is None:
            self.log.error(
                "policy_missing | continuing WS-only; will not open intents"
            )

        # Private WS: process-lifetime like public L1 when live private send is on.
        try:
            self._private_warm = self.start_private_warm_if_live_send(
                stop_event=self.stop_event
            )
        except Exception as exc:
            self.log.error(
                "private_warm_failed | err=%s | refusing cold signal loop",
                type(exc).__name__,
            )
            raise
        if self._private_warm is not None:
            self.log.info(
                "private_warm_started | run_id=%s | ready=%s | handshake_count=%s | keepalive=%s",
                self._private_warm.run_id,
                self._private_warm.is_ready(),
                self._private_warm._handshake_count,  # noqa: SLF001
                self._private_warm.keepalive_running,
            )
        else:
            self.log.info("private_warm_skipped | live_private_send=false")

        tasks: list[asyncio.Task] = [asyncio.create_task(self._heartbeat())]
        if self.tw_p50_enabled and self.tw_p50_observer is not None:
            tasks.append(
                asyncio.create_task(self._tw_p50_emit_loop(), name="tw-p50-emit")
            )
        elif self.theta_enabled and self.theta_screener is not None:
            # Theta alone (TW off): still emit ~1 Hz from last RAM snapshots.
            tasks.append(
                asyncio.create_task(self._theta_emit_loop(), name="theta-emit")
            )
        for coin in self.coins:
            meta = self._meta(coin)
            tasks.append(
                asyncio.create_task(
                    run_okx_books5(
                        base_coin=coin,
                        okx_symbol=meta.okx_symbol,
                        book_store=self.quotes[coin]["okx"],
                        on_book=self._on_book,
                        on_lifecycle=self._on_lifecycle,
                        stop_event=self.stop_event,
                    ),
                    name=f"okx-{coin}",
                )
            )
            tasks.append(
                asyncio.create_task(
                    run_bybit_orderbook1(
                        base_coin=coin,
                        bybit_symbol=meta.bybit_symbol,
                        book_store=self.quotes[coin]["bybit"],
                        on_book=self._on_book,
                        on_lifecycle=self._on_lifecycle,
                        stop_event=self.stop_event,
                    ),
                    name=f"bybit-{coin}",
                )
            )

        self.log.info(
            f"ws_tasks_started | n={len(tasks) - 1} | expect={2 * len(self.coins)}"
        )
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            self.stop_event.set()
            raise
        finally:
            # Persist floor warm pickle for next start (B path only).
            if self.floor_observer is not None and (
                self.profile == "gear22_would_send" or self._floor_warm_loaded
            ):
                try:
                    export_observer_warm_pickle(
                        self.floor_observer, self._floor_warm_path
                    )
                    self.log.info(
                        "floor_warm_saved | path=%s", self._floor_warm_path
                    )
                except Exception as exc:  # noqa: BLE001
                    self.log.warning(
                        "floor_warm_save_failed | err=%s", type(exc).__name__
                    )
            from app.bot.private.ws_warm_session import clear_process_warm_session

            clear_process_warm_session(stop=True)
            self._private_warm = None


def main() -> int:
    ensure_repo_on_syspath()
    runtime = BotRuntime()
    try:
        asyncio.run(runtime.run())
    except KeyboardInterrupt:
        runtime.log.info("bbot_stop | keyboard_interrupt")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
