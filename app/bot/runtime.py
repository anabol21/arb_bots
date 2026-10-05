"""Asyncio main loop for the B stub bot."""

from __future__ import annotations

import asyncio
import ast
import json
import importlib
import logging
import math
import os
import random
import re
import signal
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

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
    DEFAULT_LIVE_CANARY_NOTIONAL_USDT,
    GEAR22_HTML_TOP30,
    ThetaTradeConfig,
    ThetaTradeManager,
    assert_theta_live_send_gates,
    theta_live_send_requested,
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
from app.utils.tick_validity import (
    TickValidityGate,
    book_l1_complete,
)
from app.utils.universe_csv import load_take_yes_base_coins, read_universe_dicts

ensure_repo_on_syspath()
from research.is_crypto import is_crypto  # noqa: E402


def _assert_bybit_account_snapshot(response: Any) -> None:
    if not isinstance(response, dict) or str(response.get("retCode")) != "0":
        raise RuntimeError("bybit_startup_snapshot_rejected")
    result = response.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("list"), list):
        raise RuntimeError("bybit_startup_snapshot_malformed")


def _assert_okx_account_snapshot(response: Any) -> None:
    if not isinstance(response, dict) or str(response.get("code")) != "0":
        raise RuntimeError("okx_startup_snapshot_rejected")
    if not isinstance(response.get("data"), list):
        raise RuntimeError("okx_startup_snapshot_malformed")


def _nonnegative_int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _utc_ms(raw: str) -> int:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("canary timestamp must include timezone")
    return int(value.timestamp() * 1000)


def _read_canary_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("canary_manifest_missing_or_symlink")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("canary_manifest_invalid") from exc
    if not isinstance(value, dict):
        raise RuntimeError("canary_manifest_invalid")
    return value


def _canary29_source_stopped(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _heartbeat_position(log_path: Path) -> tuple[bool, dict[str, Any]]:
    for line in reversed(log_path.read_text(encoding="utf-8").splitlines()):
        if " | heartbeat | " not in line:
            continue
        match = re.search(r" \| pending=(True|False) \| position=(.*?) \| probe_done=", line)
        if match is None:
            raise RuntimeError("canary_resume_heartbeat_malformed")
        expression = ast.parse(match.group(2), mode="eval").body
        if isinstance(expression, ast.Constant) and expression.value is None:
            return match.group(1) == "True", {}
        if not isinstance(expression, ast.Call) or not isinstance(expression.func, ast.Name):
            raise RuntimeError("canary_resume_position_invalid")
        if expression.func.id != "OpenPosition" or expression.args:
            raise RuntimeError("canary_resume_position_invalid")
        allowed = {
            "trade_id", "base_coin", "side", "open_signal_ts_ms", "open_fill_ts_ms",
            "open_fill_spread", "open_notional", "open_theta_1m", "fill_spread_pp",
            "okx_filled_qty", "bybit_filled_qty", "coin_filled_qty",
        }
        values: dict[str, Any] = {}
        for kw in expression.keywords:
            if kw.arg not in allowed or kw.arg in values:
                raise RuntimeError("canary_resume_position_invalid")
            values[kw.arg] = ast.literal_eval(kw.value)
        if not {"trade_id", "base_coin", "side", "open_signal_ts_ms", "open_fill_ts_ms",
                "open_fill_spread", "open_notional", "fill_spread_pp", "okx_filled_qty",
                "bybit_filled_qty"}.issubset(values):
            raise RuntimeError("canary_resume_position_incomplete")
        return match.group(1) == "True", values
    raise RuntimeError("canary_resume_heartbeat_missing")


def _canary29_okx_metadata(
    rows: list[dict[str, Any]], symbols: set[str]
) -> tuple[dict[str, Any], dict[str, int]]:
    from app.bot.private.order_metadata import parse_decimal, parse_inst_id_code

    ct_vals: dict[str, Any] = {}
    inst_codes: dict[str, int] = {}
    for row in rows:
        symbol = str(row.get("instId") or "")
        if symbol not in symbols:
            continue
        if (
            str(row.get("instType") or "") != "SWAP"
            or str(row.get("settleCcy") or "").upper() != "USDT"
        ):
            continue
        try:
            ct_val = parse_decimal(row.get("ctVal"), field="ct_val")
        except (TypeError, ValueError):
            continue
        inst_code = parse_inst_id_code(row.get("instIdCode"))
        if ct_val <= 0 or inst_code is None:
            continue
        ct_vals[symbol] = ct_val
        inst_codes[symbol] = inst_code
    missing = symbols.difference(ct_vals).union(symbols.difference(inst_codes))
    if missing:
        raise RuntimeError(
            "okx_canary_metadata_incomplete:" + ",".join(sorted(missing))
        )
    return ct_vals, inst_codes


# Floor warm pickle periodic save interval (seconds)
FLOOR_WARM_SAVE_INTERVAL_SEC = 600  # 10 minutes


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
            "gear22_live_canary",
            "gear22_live",
            "synthetic_roll",
        ):
            raise ValueError(
                f"BBOT_PROFILE must be gear1|signal_test|gear2_would_send|"
                f"canary_wal_eden|gear22_would_send|gear22_live_canary|"
                f"synthetic_roll, "
                f"got {self.profile!r}"
            )
        if self.profile == "default":
            self.profile = "gear1"
        if self.profile == "gear2":
            self.profile = "gear2_would_send"
        if self.profile == "gear22":
            self.profile = "gear22_would_send"
        if self.profile == "gear22_live":
            self.profile = "gear22_live_canary"
        if self.profile == "canary":
            self.profile = "canary_wal_eden"

        execution = (os.environ.get("BBOT_THETA_EXECUTION") or "inline").strip().lower()
        if execution not in {"inline", "terminal_private"}:
            raise ValueError("BBOT_THETA_EXECUTION must be inline|terminal_private")
        self._terminal_private_execution = execution == "terminal_private"
        self._canary29_policy = "gear22"
        if self._terminal_private_execution:
            if self.profile != "gear22_live_canary":
                raise ValueError("terminal_private execution requires gear22_live_canary")
            if self.mode != "policy":
                raise ValueError("terminal_private execution requires BBOT_MODE=policy")
            send_flag = (os.environ.get("BBOT_THETA_LIVE_SEND") or "").strip().lower()
            if send_flag not in {"1", "true", "on", "yes"}:
                raise ValueError("terminal_private requires BBOT_THETA_LIVE_SEND=1")
            if not theta_trade_enabled(self.profile):
                raise ValueError("terminal_private requires BBOT_THETA_TRADE=1")
            self._canary29_policy = (
                os.environ.get("BBOT_THETA_POLICY") or "gear22"
            ).strip().lower()
            if self._canary29_policy not in {"gear22", "synthetic"}:
                raise ValueError("BBOT_THETA_POLICY must be gear22|synthetic")

        # Fail closed before broker/sentry when live canary is armed without LIVE_ORDERS.
        assert_theta_live_send_gates(self.profile)
        
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
                # August-std HTML top30 — require explicit BBOT_COINS in stub canary.
                coins_raw = "BTC,ETH,SOL,XRP"
            elif self.mode == "policy" and self.profile == "gear22_live_canary":
                coins_raw = ",".join(GEAR22_HTML_TOP30)
                if self._terminal_private_execution:
                    from app.bot.synthetic_policy import CANARY29_COINS

                    coins_raw = ",".join(CANARY29_COINS)
            elif self.mode == "policy" and self.profile == "synthetic_roll":
                coins_raw = "BTC,ETH,SOL,XRP"
            else:
                coins_raw = "BTC,ETH"
        self.coins = parse_coins(coins_raw)
        if not self.coins:
            raise RuntimeError("BBOT_COINS empty after is_crypto filter")
        if self._terminal_private_execution:
            from app.bot.synthetic_policy import CANARY29_COINS

            if tuple(self.coins) != CANARY29_COINS:
                raise ValueError("terminal_private requires the ordered Canary29 pool")
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
        elif self.profile == "gear22_live_canary":
            self.notional = float(DEFAULT_LIVE_CANARY_NOTIONAL_USDT)
        elif self.profile == "synthetic_roll":
            self.notional = 10.0
        else:
            self.notional = 100.0
        if self._terminal_private_execution and self.notional != 10.0:
            raise ValueError("terminal_private requires BBOT_NOTIONAL_USDT=10")
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
        if self.policy is not None and self.profile != "synthetic_roll":
            self.variation = self.policy["variation_for_profile"](self.profile)
            self.hyper = self.policy["hyper_for_profile"](self.profile)
            if self.profile == "canary_wal_eden" and self.hyper is not None:
                # Gate planned qty must match the broker notional ($10/leg).
                self.hyper["position_size"] = float(self.notional)
        self.ma_windows: dict[str, Any] = {}
        if self.policy is not None and self.profile != "synthetic_roll":
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
        self._private_stop_event: asyncio.Event | None = None
        self._synthetic_sender: Any = None
        self._synthetic_sender_session: Any = None
        self._synthetic_live_send_enabled = False
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
        if self._terminal_private_execution and not (
            self.floor_enabled and self.tw_p50_enabled and self.theta_enabled
        ):
            raise ValueError("terminal_private requires floor, TW p50, and theta watchers")
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
        # Gear 2.2 θ K=1 would_send (separate journal). Live canary injects
        # Contour B place_fn; stub would_send never calls broker.place.
        self.theta_trade_enabled = theta_trade_enabled(self.profile)
        self.theta_trade: ThetaTradeManager | None = None
        self._theta_trade_warned = False
        self._synthetic_roll_halted = False
        self._synthetic_roll_halt_reason: Optional[str] = None
        self._canary29_completed_cycles = 0
        self._canary29_done = False
        self._canary29_done_reason: Optional[str] = None
        self._canary29_max_cycles = (
            _nonnegative_int_env("BBOT_CANARY_MAX_CYCLES", 10)
            if self._terminal_private_execution
            else 10
        )
        window_raw = (
            (os.environ.get("BBOT_CANARY_OPEN_WINDOW_HOURS") or "").strip()
            if self._terminal_private_execution
            else ""
        )
        self._canary29_open_window_hours: Optional[float] = None
        if window_raw:
            try:
                hours = float(window_raw)
            except ValueError as exc:
                raise ValueError("BBOT_CANARY_OPEN_WINDOW_HOURS must be positive") from exc
            if not math.isfinite(hours) or hours <= 0:
                raise ValueError("BBOT_CANARY_OPEN_WINDOW_HOURS must be positive")
            self._canary29_open_window_hours = hours
        self._canary29_started_at_ms = int(time.time() * 1000)
        self._canary29_deadline_ms: Optional[int] = (
            self._canary29_started_at_ms + int(self._canary29_open_window_hours * 3_600_000)
            if self._canary29_open_window_hours is not None
            else None
        )
        resume_raw = (os.environ.get("BBOT_CANARY_RESUME_MANIFEST") or "").strip()
        self._canary29_resume_manifest_path = Path(resume_raw) if resume_raw else None
        self._canary29_resume_manifest: Optional[dict[str, Any]] = None
        if self._terminal_private_execution and self._canary29_resume_manifest_path is not None:
            self._canary29_resume_manifest = _read_canary_json(
                self._canary29_resume_manifest_path
            )
            self._canary29_started_at_ms = _utc_ms(
                str(self._canary29_resume_manifest["run_started_at_utc"])
            )
            deadline_raw = self._canary29_resume_manifest.get("open_window_deadline_utc")
            self._canary29_deadline_ms = _utc_ms(str(deadline_raw)) if deadline_raw else None
            self._canary29_completed_cycles = int(
                self._canary29_resume_manifest.get("completed_cycles") or 0
            )
        self._canary29_deadline_mono: Optional[float] = (
            time.monotonic()
            + max(0.0, (self._canary29_deadline_ms - int(time.time() * 1000)) / 1000.0)
            if self._canary29_deadline_ms is not None
            else None
        )
        self._canary29_state_path = self.data_root / "canary_state.json"
        self._okx_inst_id_codes: dict[str, int] = {}
        self._okx_ct_vals: dict[str, Any] = {}
        # (venue, symbol) → "1" after warmup. Never stores a lever above 1.
        self._leverage_one: dict[tuple[str, str], str] = {}
        if self.theta_trade_enabled:
            live_send = theta_live_send_requested(self.profile)
            theta_cfg = ThetaTradeConfig.from_env()
            if live_send or self.profile == "synthetic_roll":
                theta_cfg.notional_usdt = float(self.notional)
            if self.profile == "synthetic_roll" or self._terminal_private_execution:
                from app.bot.synthetic_policy import (
                    make_canary29_decide,
                    make_synthetic_decide,
                    synthetic_live_gates,
                )

                seed_name = (
                    "BBOT_CANARY29_SEED"
                    if self._terminal_private_execution
                    else "BBOT_SYNTHETIC_SEED"
                )
                seed_raw = str(os.environ.get(seed_name) or "").strip()
                rng = random.Random(int(seed_raw)) if seed_raw else random.Random()
                gates_on = synthetic_live_gates(os.environ)
                self._synthetic_live_send_enabled = gates_on
                decide_fn = (
                    make_canary29_decide(self.coins, rng)
                    if self._terminal_private_execution
                    and self._canary29_policy == "synthetic"
                    else (
                        make_synthetic_decide(self.coins, rng)
                        if self.profile == "synthetic_roll"
                        else None
                    )
                )
                self.theta_trade = ThetaTradeManager(
                    data_root=self.data_root,
                    config=theta_cfg,
                    log=lambda m: self.log.info(m),
                    live_send=False,
                    place_fn=(
                        self._synthetic_live_place
                        if gates_on or self._terminal_private_execution
                        else self._synthetic_local_place
                    ),
                    meta_fn=self._meta,
                    decide_fn=decide_fn,
                    execution_mode=(
                        "terminal_private"
                        if self._terminal_private_execution
                        else "inline"
                    ),
                    pre_send_guard_fn=(
                        self._canary29_pre_send_guard
                        if self._terminal_private_execution
                        else None
                    ),
                    entry_allowed_fn=(
                        self._canary29_entry_allowed
                        if self._terminal_private_execution
                        else None
                    ),
                    state_change_fn=(
                        self._write_canary_state
                        if self._terminal_private_execution
                        else None
                    ),
                )
            else:
                self.theta_trade = ThetaTradeManager(
                    data_root=self.data_root,
                    config=theta_cfg,
                    log=lambda m: self.log.info(m),
                    live_send=live_send,
                    place_fn=self.broker.place if live_send else None,
                    meta_fn=self._meta if live_send else None,
                )
        # Floor warm-start (standard for gear22 / when pickle present).
        self._floor_warm_path = resolve_floor_warm_path(self.data_root)
        self._floor_warm_loaded = False
        if self.floor_observer is not None:
            warm_path = self._floor_warm_path
            force_warm = str(os.environ.get("BBOT_FLOOR_WARM") or "").strip().lower()
            want_warm = force_warm in ("1", "true", "on", "yes") or (
                self.profile in {"gear22_would_send", "gear22_live_canary"}
                and force_warm not in ("0", "false", "off", "no")
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

    def _save_floor_warm_pickle(self) -> None:
        """Save floor observer state to pickle (idempotent, safe to call repeatedly)."""
        if self.floor_observer is None:
            return
        if not (
            self.profile in {"gear22_would_send", "gear22_live_canary"}
            or self._floor_warm_loaded
        ):
            return
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

    def start_private_warm_if_live_send(self, **overrides: Any) -> Any:
        """Warm private WS before the signal loop when live private send is armed.

        ON BY DEFAULT for ``VENUE=live`` + ``LIVE_ORDERS=1`` (no opt-in flag).
        Stub / would_send units leave LIVE_ORDERS off and get ``None``.
        """
        from app.bot.private.ws_warm_session import start_warm_private_for_bot_process

        return start_warm_private_for_bot_process(**overrides)

    def _prepare_synthetic_live_sender(self) -> bool:
        """Load the live place modules and ready both queues before signals."""
        session = self._private_warm
        if (
            (self.profile != "synthetic_roll" and not self._terminal_private_execution)
            or not getattr(self, "_synthetic_live_send_enabled", False)
            or session is None
        ):
            return False
        if not session.is_ready():
            raise RuntimeError("synthetic live session is not ready")
        wire = getattr(session, "wire", None)
        if wire is None or not getattr(wire, "healthy", False):
            raise RuntimeError("synthetic live wire capture is not ready")

        # Import the place path after the private session has completed startup.
        importlib.import_module("app.bot.private.place_send")
        importlib.import_module("app.bot.private.send_legs")
        from app.bot.private.ws_trivial_dual_leg import (
            TrivialDualSender,
            warm_trade_send_fn,
        )

        sender = getattr(self, "_synthetic_sender", None)
        if (
            sender is not None
            and getattr(self, "_synthetic_sender_session", None) is session
            and sender.is_ready()
        ):
            depths = sender.queue_depths()
        else:
            if sender is not None:
                sender.close()
            sender = TrivialDualSender(send_fn=warm_trade_send_fn(session))
            self._synthetic_sender = sender
            self._synthetic_sender_session = session
            depths = sender.queue_depths()

        if not sender.is_ready() or depths != {"bybit": 0, "okx": 0}:
            self._synthetic_sender = None
            self._synthetic_sender_session = None
            sender.close()
            raise RuntimeError("synthetic live sender queues not ready and empty")

        self.log.info(
            "synthetic_sender_ready | run_id=%s | handshake_count=%s | "
            "bybit_queue_depth=%s | okx_queue_depth=%s | frames_enqueued=0",
            session.run_id,
            session._handshake_count,  # noqa: SLF001
            depths["bybit"],
            depths["okx"],
        )
        return True

    def _canary29_pre_send_guard(
        self,
        *,
        coin: str,
        event: str,
        okx_book: dict[str, Any],
        bybit_book: dict[str, Any],
    ) -> Optional[str]:
        """Fail closed on stale L1 or an unready cached private send path."""
        del event  # actual open/close depth is checked by ThetaTradeManager
        if not books_ready(okx_book, bybit_book):
            return "incomplete_book"
        now_ms = time.time() * 1000.0
        for book in (okx_book, bybit_book):
            age = now_ms - float(book["local_recv_ts_ms"])
            if age < 0 or age > 2000:
                return "stale_book"
        gate_reason = self.gate.evaluate(coin, okx_book, bybit_book, now_ms)
        if gate_reason is not None:
            return f"book_{gate_reason}"
        session = self._private_warm
        sender = self._synthetic_sender
        wire = getattr(session, "wire", None) if session is not None else None
        if (
            session is None
            or not session.is_ready()
            or not sender
            or not sender.is_ready()
            or wire is None
            or not getattr(wire, "healthy", False)
        ):
            return "private_channel_down"
        try:
            meta = self._meta(coin)
        except KeyError:
            return "metadata_missing"
        symbol = str(meta.okx_symbol)
        bybit_symbol = str(meta.bybit_symbol)
        if not self._okx_ct_vals.get(symbol) or not self._okx_inst_id_codes.get(symbol):
            return "instrument_metadata_missing"
        if not (
            self._leverage_one.get(("okx", symbol)) == "1"
            and self._leverage_one.get(("bybit", bybit_symbol)) == "1"
        ):
            return "leverage_not_one"
        return None

    def _canary29_assert_flat(self, coin: str) -> None:
        """Confirm selected-symbol flatness, allowing REST a bounded projection."""
        import time as time_module
        from app.bot.private.ws_w4_baseline import (
            SignedRestFlatBaseline,
            assert_flat,
        )

        session = self._private_warm
        if session is None:
            raise RuntimeError("private_session_missing")
        deadline = time_module.monotonic() + 5.0
        last_error: Optional[Exception] = None
        while True:
            try:
                self._canary29_check_coin_flat(
                    coin, session, SignedRestFlatBaseline, assert_flat
                )
                return
            except Exception as exc:
                last_error = exc
                if time_module.monotonic() >= deadline:
                    break
                time_module.sleep(0.5)
        assert last_error is not None
        raise last_error

    def _canary29_record_close_flat(self, result: Any, coin: str) -> None:
        if (
            not self._terminal_private_execution
            or not getattr(result, "completed", False)
        ):
            return
        try:
            self._canary29_assert_flat(coin)
            self._canary29_completed_cycles += 1
            self.log.info(
                "canary29_cycle_flat | completed=%s | coin=%s",
                self._canary29_completed_cycles,
                coin,
            )
            if (
                self._canary29_max_cycles > 0
                and self._canary29_completed_cycles >= self._canary29_max_cycles
            ):
                self._canary29_done = True
                self._canary29_done_reason = "cycle_cap_reached_flat"
            elif self._canary29_window_elapsed():
                self._canary29_done = True
                self._canary29_done_reason = "open_window_elapsed_flat"
            if self._canary29_done:
                self.log.info("canary29_stopped | reason=%s", self._canary29_done_reason)
        except Exception as exc:
            result.completed = False
            result.keep_pending = True
            result.abort = f"post_close_flat_check:{type(exc).__name__}"
            self._synthetic_roll_halt_reason = result.abort

    def _canary29_check_coin_flat(
        self, coin: str, session: Any, baseline_type: Any, assert_flat_fn: Any
    ) -> None:
        meta = self._meta(coin)
        for exchange, symbol, credentials in (
            ("bybit", meta.bybit_symbol, session.bybit_credentials),
            ("okx", meta.okx_symbol, session.okx_credentials),
        ):
            result = baseline_type(
                exchange=exchange, credentials=credentials
            ).check(exchange=exchange, symbol=symbol)
            assert_flat_fn(result)

    def _canary29_read_startup_snapshot(self) -> dict[str, list[dict[str, Any]]]:
        from urllib.parse import quote

        from app.bot.private.venue import endpoints_for_venue
        from app.bot.private.ws_w4_baseline import (
            _BYBIT_OPEN,
            _BYBIT_POS,
            _OKX_OPEN,
            _OKX_POS,
            _bybit_signed_get,
            _okx_signed_get,
        )

        session = self._private_warm
        if session is None:
            raise RuntimeError("private_session_missing")
        endpoints = endpoints_for_venue("live")
        def bybit_pages(path: str, query: str) -> list[dict[str, Any]]:
            pages: list[dict[str, Any]] = []
            cursor = ""
            seen: set[str] = set()
            while len(pages) < 20:
                suffix = f"&cursor={quote(cursor, safe='')}" if cursor else ""
                page = _bybit_signed_get(
                    credentials=session.bybit_credentials,
                    base=endpoints.bybit_rest,
                    path=path,
                    query=query + suffix,
                )
                pages.append(dict(page))
                cursor = str((page.get("result") or {}).get("nextPageCursor") or "")
                if not cursor:
                    return pages
                if cursor in seen:
                    raise RuntimeError("bybit_cursor_repeated")
                seen.add(cursor)
            raise RuntimeError("bybit_pagination_limit")

        def okx_pages(path: str, query: str, cursor_key: str) -> list[dict[str, Any]]:
            pages: list[dict[str, Any]] = []
            cursor = ""
            seen: set[str] = set()
            while len(pages) < 20:
                suffix = f"&after={quote(cursor, safe='')}" if cursor else ""
                page = _okx_signed_get(
                    credentials=session.okx_credentials,
                    base=endpoints.okx_rest,
                    path_with_query=f"{path}?{query}{suffix}",
                )
                _assert_okx_account_snapshot(page)
                rows = page["data"]
                pages.append(dict(page))
                if len(rows) < 100:
                    return pages
                cursor = str(rows[-1].get(cursor_key) or "")
                if not cursor or cursor in seen:
                    raise RuntimeError("okx_cursor_missing_or_repeated")
                seen.add(cursor)
            raise RuntimeError("okx_pagination_limit")

        positions = bybit_pages(
            _BYBIT_POS, "category=linear&settleCoin=USDT&limit=200"
        )
        orders = bybit_pages(
            _BYBIT_OPEN, "category=linear&settleCoin=USDT&openOnly=0&limit=50"
        )
        bybit_position_rows: list[dict[str, Any]] = []
        bybit_order_rows: list[dict[str, Any]] = []
        for page in positions:
            _assert_bybit_account_snapshot(page)
            bybit_position_rows.extend(dict(r) for r in page["result"]["list"])
        for page in orders:
            _assert_bybit_account_snapshot(page)
            bybit_order_rows.extend(dict(r) for r in page["result"]["list"])

        okx_positions = _okx_signed_get(
            credentials=session.okx_credentials,
            base=endpoints.okx_rest,
            path_with_query=f"{_OKX_POS}?instType=SWAP",
        )
        _assert_okx_account_snapshot(okx_positions)
        okx_order_pages = okx_pages(_OKX_OPEN, "instType=SWAP&limit=100", "ordId")
        return {
            "bybit_positions": bybit_position_rows,
            "bybit_orders": bybit_order_rows,
            "okx_positions": [dict(r) for r in okx_positions["data"]],
            "okx_orders": [dict(r) for page in okx_order_pages for r in page["data"]],
        }

    def _canary29_assert_startup_flat(self) -> None:
        from decimal import Decimal

        bybit_symbols = {self._meta(coin).bybit_symbol: coin for coin in self.coins}
        okx_symbols = {self._meta(coin).okx_symbol: coin for coin in self.coins}
        snapshot = self._canary29_read_startup_snapshot()
        for row in snapshot["bybit_positions"]:
            symbol = str(row.get("symbol") or "")
            if symbol in bybit_symbols and Decimal(str(row.get("size") or "0")) != 0:
                raise RuntimeError(f"position_not_flat:bybit:{bybit_symbols[symbol]}")
        for row in snapshot["bybit_orders"]:
            symbol = str(row.get("symbol") or "")
            if symbol in bybit_symbols:
                raise RuntimeError(f"open_orders_not_flat:bybit:{bybit_symbols[symbol]}")
        for row in snapshot["okx_positions"]:
            symbol = str(row.get("instId") or "")
            if symbol in okx_symbols and Decimal(str(row.get("pos") or "0")) != 0:
                raise RuntimeError(f"position_not_flat:okx:{okx_symbols[symbol]}")
        for row in snapshot["okx_orders"]:
            symbol = str(row.get("instId") or "")
            if symbol in okx_symbols:
                raise RuntimeError(f"open_orders_not_flat:okx:{okx_symbols[symbol]}")
        self.log.info("canary29_startup_flat_confirmed | coins=%s", len(self.coins))

    def _canary29_resume_position(self) -> None:
        from decimal import Decimal
        from app.bot.theta_trade_manager import OpenPosition

        manifest = self._canary29_resume_manifest
        if not isinstance(manifest, dict):
            raise RuntimeError("canary_resume_manifest_missing")
        if self.theta_trade is None or self.theta_trade.slot.pending:
            raise RuntimeError("canary_resume_target_slot_not_empty")
        if manifest.get("schema_version") != "bbot.gear22.canary-state.v1":
            raise RuntimeError("canary_resume_manifest_schema")
        if (
            manifest.get("policy_id") != "gear22_frozen_v1"
            or manifest.get("policy_selector") != "gear22"
            or manifest.get("execution") != "terminal_private"
            or self._canary29_policy != "gear22"
        ):
            raise RuntimeError("canary_resume_policy_mismatch")
        if int(manifest.get("max_cycles", -1)) != self._canary29_max_cycles:
            raise RuntimeError("canary_resume_cycle_cap_mismatch")
        old_hours = manifest.get("open_window_hours")
        if old_hours != self._canary29_open_window_hours:
            raise RuntimeError("canary_resume_window_config_mismatch")
        try:
            source_pid = int(manifest.get("source_pid") or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("canary_resume_source_pid_invalid") from exc
        if not _canary29_source_stopped(source_pid):
            raise RuntimeError("canary_resume_source_still_running")
        source_root = Path(str(manifest.get("source_data_root") or ""))
        if (
            not source_root.is_absolute()
            or source_root.is_symlink()
            or not source_root.is_dir()
            or source_root.resolve() == self.data_root.resolve()
        ):
            raise RuntimeError("canary_resume_source_path_invalid")
        if manifest.get("execution_halt_reason"):
            raise RuntimeError("canary_resume_source_halted")
        source_log = source_root / "bbot.log"
        log_text = source_log.read_text(encoding="utf-8")
        if any(marker in log_text for marker in (
            "theta_trade_execution_halted", "execution_exception", "canary29_stopped",
        )):
            raise RuntimeError("canary_resume_source_has_halt_marker")
        pending, position = _heartbeat_position(source_log)
        if pending or manifest.get("pending") is not False or not position:
            raise RuntimeError("canary_resume_heartbeat_not_open")
        position_snapshot = manifest.get("position")
        if not isinstance(position_snapshot, dict):
            raise RuntimeError("canary_resume_checkpoint_missing_position")
        for key, value in position.items():
            if position_snapshot.get(key) != value:
                raise RuntimeError("canary_resume_heartbeat_checkpoint_mismatch")
        intent_id = str(manifest.get("source_intent_id") or "")
        if not intent_id or str(position.get("trade_id")) != intent_id:
            raise RuntimeError("canary_resume_intent_mismatch")
        restored = self.theta_trade.slot.position
        if restored is not None and (
            str(restored.trade_id) != intent_id
            or str(restored.base_coin).upper() != str(position.get("base_coin") or "").upper()
            or str(restored.side).lower() != str(position.get("side") or "").lower()
        ):
            raise RuntimeError("canary_resume_restored_slot_mismatch")
        source_state_path = source_root / "canary_state.json"
        if source_state_path.exists():
            source_state = _read_canary_json(source_state_path)
            if (
                source_state.get("policy_id") != "gear22_frozen_v1"
                or source_state.get("policy_selector") != "gear22"
                or source_state.get("execution") != "terminal_private"
                or source_state.get("pending") is not False
                or source_state.get("execution_halt_reason")
                or source_state.get("source_intent_id") != intent_id
                or int(source_state.get("source_pid") or 0) != source_pid
                or str(source_state.get("source_data_root") or "") != str(source_root)
                or source_state.get("position") != position_snapshot
            ):
                raise RuntimeError("canary_resume_checkpoint_not_safe")
        coin = str(position.get("base_coin") or "").upper()
        side = str(position.get("side") or "").lower()
        if coin not in self.coins or side not in {"long", "short"}:
            raise RuntimeError("canary_resume_coin_or_side_invalid")
        for key in ("open_fill_spread", "fill_spread_pp", "open_notional"):
            try:
                if not math.isfinite(float(position.get(key))):
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise RuntimeError("canary_resume_policy_state_invalid") from exc
        if float(position["open_notional"]) <= 0 or float(position["fill_spread_pp"]) != float(position["open_fill_spread"]):
            raise RuntimeError("canary_resume_policy_state_mismatch")
        meta = self._meta(coin)
        journal_paths = sorted((source_root / "theta_trades").glob("event_date=*/trades.jsonl"))
        if not journal_paths or any(path.is_symlink() for path in journal_paths):
            raise RuntimeError("canary_resume_trade_journal_missing")
        rows: list[dict[str, Any]] = []
        for journal_path in journal_paths:
            rows.extend(
                json.loads(line)
                for line in journal_path.read_text(encoding="utf-8").splitlines()
                if line
            )
        if any(
            str(row.get("status") or "").lower() in {"abort", "aborted", "unknown"}
            or str(row.get("event") or "").lower() in {"abort", "send_abort"}
            for row in rows
        ):
            raise RuntimeError("canary_resume_trade_journal_abort_evidence")
        private_journal = source_root / "private" / "journal"
        if private_journal.exists():
            from app.bot.private.journal_v1 import validate_event_shape

            for path in sorted(private_journal.glob("event_date=*/events.jsonl")):
                if path.is_symlink():
                    raise RuntimeError("canary_resume_private_journal_symlink")
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line:
                        continue
                    event = json.loads(line)
                    try:
                        validate_event_shape(event, require_opaque_ids=False)
                    except Exception as exc:
                        raise RuntimeError("canary_resume_private_journal_invalid") from exc
                    event_type = str(event.get("event_type") or "")
                    if event_type in {"reject", "dual_leg_abort", "cancel_requested"}:
                        raise RuntimeError("canary_resume_private_journal_abort_evidence")
                    if event_type != "reconciliation":
                        continue
                    if event.get("reconciliation_scope") != "private_stream_reseed":
                        raise RuntimeError("canary_resume_private_journal_ambiguous_reconciliation")
                    if any(event.get(key) for key in (
                        "dual_leg_id", "leg_id", "order_attempt_id", "intent_id",
                        "client_order_id", "exchange_order_id", "order_id", "orderId",
                    )):
                        raise RuntimeError("canary_resume_private_journal_order_reconciliation")
                    stream_state = (
                        event.get("observation_source"),
                        event.get("outcome"),
                        event.get("reconciliation_state"),
                        event.get("sequence_state"),
                        event.get("subscription_readiness"),
                        event.get("transport"),
                    )
                    allowed_gap = stream_state == (
                        "private_ws", "observed", "inconclusive", "reseed_required",
                        "not_ready", None,
                    ) or stream_state == (
                        "private_ws", "observed", "inconclusive", "gap", "not_ready", None,
                    )
                    allowed_reseed = stream_state == (
                        "rest_reconcile", "success", "matched", "healthy", "ready", "rest",
                    )
                    if not (allowed_gap or allowed_reseed):
                        raise RuntimeError("canary_resume_private_stream_state_unknown")
        candidate_rows = [row for row in rows if str(row.get("intent_id") or "") == intent_id]
        terminal = [row for row in candidate_rows if row.get("status") in {"pending", "open", "closed"}]
        if not terminal or terminal[-1].get("status") != "open" or terminal[-1].get("event") != "open":
            raise RuntimeError("canary_resume_intent_not_terminal_open")
        unresolved = []
        by_intent: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            iid = str(row.get("intent_id") or "")
            if iid and row.get("status") in {"pending", "open", "closed"}:
                by_intent.setdefault(iid, []).append(row)
        latest_lifecycle = [
            lifecycle[-1] for lifecycle in by_intent.values()
        ]
        for latest in latest_lifecycle:
            if latest.get("status") == "pending":
                unresolved.append(str(latest.get("intent_id") or ""))
        terminal_events = sorted(
            (r for r in rows if r.get("status") in {"open", "closed"}),
            key=lambda r: int(r.get("fill_ts_ms") or 0),
        )
        outstanding: Optional[dict[str, Any]] = None
        for item in terminal_events:
            if item.get("event") == "open" and item.get("status") == "open":
                if outstanding is not None:
                    raise RuntimeError("canary_resume_journal_overlapping_open")
                outstanding = item
            elif item.get("event") == "close" and item.get("status") == "closed":
                if outstanding is None or any(
                    str(item.get(k) or "") != str(outstanding.get(k) or "")
                    for k in ("base_coin", "side", "okx_filled_qty", "bybit_filled_qty")
                ):
                    raise RuntimeError("canary_resume_journal_close_mismatch")
                outstanding = None
        if (
            unresolved
            or outstanding is None
            or str(outstanding.get("intent_id") or "") != intent_id
            or str(manifest.get("execution_halt_reason") or "")
        ):
            raise RuntimeError("canary_resume_journal_has_other_or_unresolved_intent")
        row = terminal[-1]
        if (
            str(row.get("base_coin") or "").upper() != coin
            or str(row.get("side") or "").lower() != side
            or str(row.get("fill_ts_ms") or "") != str(position.get("open_fill_ts_ms"))
            or Decimal(str(row.get("okx_filled_qty") or "0")) != Decimal(str(position.get("okx_filled_qty") or "0"))
            or Decimal(str(row.get("bybit_filled_qty") or "0")) != Decimal(str(position.get("bybit_filled_qty") or "0"))
        ):
            raise RuntimeError("canary_resume_journal_position_mismatch")
        ct_val = Decimal(str(self._okx_ct_vals.get(meta.okx_symbol) or "0"))
        okx_qty = Decimal(str(position.get("okx_filled_qty") or "0"))
        bybit_qty = Decimal(str(position.get("bybit_filled_qty") or "0"))
        coin_qty = Decimal(str(position.get("coin_filled_qty") or "0"))
        if ct_val <= 0 or okx_qty * ct_val != bybit_qty or coin_qty != bybit_qty:
            raise RuntimeError("canary_resume_contract_units_mismatch")

        snapshot = self._canary29_read_startup_snapshot()
        bybit_open = [r for r in snapshot["bybit_positions"] if Decimal(str(r.get("size") or "0")) != 0]
        okx_open = [r for r in snapshot["okx_positions"] if Decimal(str(r.get("pos") or "0")) != 0]
        expected_bybit_side = "Sell" if side == "long" else "Buy"
        expected_okx_sign = Decimal("1") if side == "long" else Decimal("-1")
        if (
            len(bybit_open) != 1
            or bybit_open[0].get("symbol") != meta.bybit_symbol
            or bybit_open[0].get("side") != expected_bybit_side
            or (bybit_open[0].get("positionIdx") is not None and str(bybit_open[0].get("positionIdx")) != "0")
            or Decimal(str(bybit_open[0].get("size") or "0")) != bybit_qty
            or len(okx_open) != 1
            or okx_open[0].get("instId") != meta.okx_symbol
            or (okx_open[0].get("posSide") is not None and str(okx_open[0].get("posSide")) != "net")
            or Decimal(str(okx_open[0].get("pos") or "0")) * expected_okx_sign <= 0
            or abs(Decimal(str(okx_open[0].get("pos") or "0"))) != okx_qty
            or snapshot["bybit_orders"]
            or snapshot["okx_orders"]
        ):
            raise RuntimeError("canary_resume_exchange_position_mismatch")
        session = self._private_warm
        if session is None or not session.is_ready():
            raise RuntimeError("canary_resume_new_private_session_not_ready")
        self.theta_trade.slot.position = OpenPosition(**position)
        self.theta_trade.slot.pending = False
        self.log.info("canary29_resume_verified | intent=%s | coin=%s | side=%s", intent_id, coin, side)

    def _canary29_entry_allowed(self) -> bool:
        return self._canary29_deadline_mono is None or time.monotonic() < self._canary29_deadline_mono

    def _canary29_window_elapsed(self) -> bool:
        return self._canary29_deadline_mono is not None and time.monotonic() >= self._canary29_deadline_mono

    @staticmethod
    def _canary29_position_json(position: Any) -> Optional[dict[str, Any]]:
        if position is None:
            return None
        return {
            "trade_id": position.trade_id,
            "base_coin": position.base_coin,
            "side": position.side,
            "open_signal_ts_ms": position.open_signal_ts_ms,
            "open_fill_ts_ms": position.open_fill_ts_ms,
            "open_fill_spread": position.open_fill_spread,
            "open_notional": position.open_notional,
            "open_theta_1m": position.open_theta_1m,
            "fill_spread_pp": position.fill_spread_pp,
            "okx_filled_qty": position.okx_filled_qty,
            "bybit_filled_qty": position.bybit_filled_qty,
            "coin_filled_qty": position.coin_filled_qty,
        }

    def _write_canary_state(self) -> None:
        if not self._terminal_private_execution or self.theta_trade is None:
            return
        value = {
            "schema_version": "bbot.gear22.canary-state.v1",
            "policy_id": (
                "gear22_frozen_v1"
                if self._canary29_policy == "gear22"
                else "canary29_synthetic_v1"
            ),
            "policy_selector": self._canary29_policy,
            "execution": "terminal_private",
            "source_data_root": str(self.data_root),
            "source_pid": os.getpid(),
            "source_intent_id": getattr(self.theta_trade.slot.position, "trade_id", None),
            "run_started_at_utc": datetime.fromtimestamp(
                self._canary29_started_at_ms / 1000, timezone.utc
            ).isoformat().replace("+00:00", "Z"),
            "open_window_deadline_utc": (
                datetime.fromtimestamp(self._canary29_deadline_ms / 1000, timezone.utc)
                .isoformat().replace("+00:00", "Z")
                if self._canary29_deadline_ms is not None
                else None
            ),
            "completed_cycles": self._canary29_completed_cycles,
            "max_cycles": self._canary29_max_cycles,
            "open_window_hours": self._canary29_open_window_hours,
            "position": _jsonable_extra(self._canary29_position_json(self.theta_trade.slot.position) or {}),
            "pending": bool(self.theta_trade.slot.pending),
            "execution_halt_reason": self.theta_trade.execution_halt_reason or self._synthetic_roll_halt_reason,
            "done_reason": self._canary29_done_reason,
            "last_policy_status": _jsonable_extra(dict(self.theta_trade.last_policy_status)),
            "updated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        self.data_root.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".canary_state.", suffix=".tmp", dir=self.data_root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, sort_keys=True, separators=(",", ":"), allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self._canary29_state_path)
            dir_fd = os.open(self.data_root, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def _canary29_latch_checkpoint_failure(self, exc: Exception) -> None:
        if self.theta_trade is not None:
            self.theta_trade.slot.pending = True
            reason = f"state_checkpoint_failed:{type(exc).__name__}"
            self.theta_trade.execution_halt_reason = reason
            self._synthetic_roll_halt_reason = reason
            self.log.error("canary29_stopped | reason=%s", reason)
        self.stop_event.set()

    def _stop_synthetic_private_send(self) -> None:
        """Close the profile sender before stopping the shared warm session."""
        if self._private_stop_event is not None:
            self._private_stop_event.set()
        sender = getattr(self, "_synthetic_sender", None)
        self._synthetic_sender = None
        self._synthetic_sender_session = None
        try:
            if sender is not None:
                sender.close()
        finally:
            try:
                from app.bot.private.ws_warm_session import clear_process_warm_session

                clear_process_warm_session(stop=True)
            finally:
                self._private_warm = None

    def _prefetch_okx_canary_metadata(self) -> None:
        """Populate both OKX send caches from one validated instruments snapshot."""
        from app.discovery.intersection import fetch_okx_swap_instruments

        symbols = {self._meta(coin).okx_symbol for coin in self.coins}
        rows = fetch_okx_swap_instruments()
        ct_vals, inst_codes = _canary29_okx_metadata(rows, symbols)
        self._okx_ct_vals = ct_vals
        self._okx_inst_id_codes = inst_codes
        self.log.info("okx_canary_metadata_prefetched | n=%s", len(symbols))

    async def _await_terminal_place_before_shutdown(self) -> bool:
        if not self._terminal_private_execution or self.theta_trade is None:
            return True
        task = self.theta_trade._terminal_place_task
        if task is None:
            return True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=15.0)
            return True
        except asyncio.CancelledError:
            self.stop_event.set()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=15.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.log.error("terminal_place_still_active | keeping_private_session_open=true")
                return False
            raise
        except asyncio.TimeoutError:
            self.log.error("terminal_place_still_active | keeping_private_session_open=true")
            return False

    def _synthetic_local_place(self, **kwargs: Any) -> Any:
        """No sockets. Same journal and chrono as the live place path."""
        from app.bot.private.okx_ct_val import bind_okx_ct_val
        from app.bot.private.place_send import PlaceSendResult, place_local

        bound, err = bind_okx_ct_val(kwargs.get("meta"), self._okx_ct_vals)
        if err:
            return PlaceSendResult(abort=err)
        call = dict(kwargs)
        call["meta"] = bound
        return place_local(data_root=self.data_root, **call)


    def _emit_terminal_private_trade_sentry(
        self,
        result: Any,
        kwargs: Mapping[str, Any],
    ) -> None:
        """Fail-safe Sentry trade event for terminal_private place_live outcomes.

        Called only after ``place_live`` returns (outside ``place_io_section``).
        Never raises; never touches exchange I/O.
        """
        try:
            from app.bot.private.send_legs import _attempt_ids
            from app.bot.sentry_setup import capture_trade_event

            extra_raw = kwargs.get("extra")
            extra: Mapping[str, Any] = extra_raw if isinstance(extra_raw, Mapping) else {}
            coin = str(
                getattr(result, "base_coin", None)
                or kwargs.get("base_coin")
                or ""
            ).upper()
            spread_side = str(kwargs.get("spread_side") or "").strip().lower()
            side = str(getattr(result, "side", None) or "").strip().lower()
            if not side:
                if spread_side == "open_long":
                    side = "long"
                elif spread_side == "open_short":
                    side = "short"
                elif spread_side == "close":
                    close_of = str(kwargs.get("close_of") or "")
                    if "long" in close_of:
                        side = "long"
                    elif "short" in close_of:
                        side = "short"
            intent_id = str(
                getattr(result, "intent_id", None)
                or kwargs.get("intent_id")
                or extra.get("intent_id")
                or ""
            )
            trade_id = str(extra.get("trade_id") or intent_id or "")
            if not trade_id or not coin:
                return

            completed = bool(getattr(result, "completed", False))
            abort = getattr(result, "abort", None)
            abort_s = str(abort) if abort is not None else None
            keep_pending = bool(getattr(result, "keep_pending", False))
            send_attempted = bool(getattr(result, "send_attempted", False))
            status = getattr(result, "status", None)

            asymmetric = bool(
                abort_s in {"asymmetric_fill", "partial_fill", "fill_qty_mismatch"}
                or (abort_s is not None and "asymmetric" in abort_s)
            )

            if completed and spread_side == "close":
                event = "close"
            elif completed:
                event = "open"
            elif asymmetric:
                event = "asymmetric_fill"
            else:
                event = "send_abort"

            signal_ts_ms = kwargs.get("signal_ts_ms")
            fill_ts_ms = getattr(result, "fill_ts_ms", None)
            latency_ms = getattr(result, "latency_ms", None)
            if latency_ms is None and signal_ts_ms is not None and fill_ts_ms is not None:
                try:
                    latency_ms = int(fill_ts_ms) - int(signal_ts_ms)
                except (TypeError, ValueError):
                    latency_ms = None

            bybit_order_id = okx_order_id = None
            try:
                bybit_order_id, okx_order_id, _dual = _attempt_ids(intent_id or trade_id)
            except Exception:
                pass

            reduce_only = spread_side == "close" or bool(kwargs.get("close_of"))
            chronometry = self._terminal_private_step_chrono_summary(intent_id or trade_id)

            sentry_extras: dict[str, Any] = {
                "spread_side": spread_side or None,
                "direction": side or None,
                "intent_id": intent_id or None,
                "trade_id": trade_id,
                "okx_venue": "okx",
                "bybit_venue": "bybit",
                "okx_order_id": okx_order_id,
                "bybit_order_id": bybit_order_id,
                "okx_avg_px": getattr(result, "okx_fill_px", None),
                "bybit_avg_px": getattr(result, "bybit_fill_px", None),
                "okx_filled_qty": getattr(result, "okx_filled_qty", None),
                "bybit_filled_qty": getattr(result, "bybit_filled_qty", None),
                "coin_qty": getattr(result, "coin_qty", None),
                "fill_status": (
                    "filled"
                    if completed
                    else (
                        abort_s
                        or status
                        or ("keep_pending" if keep_pending else "incomplete")
                    )
                ),
                "completed": completed,
                "keep_pending": keep_pending,
                "send_attempted": send_attempted,
                "abort": abort_s,
                "status": status,
                "reduce_only": reduce_only,
                "signal_ts_ms": signal_ts_ms,
                "fill_ts_ms": fill_ts_ms,
                "latency_ms": latency_ms,
                "notional_usdt": getattr(self, "notional", None),
                "completed_cycles": getattr(self, "_canary29_completed_cycles", None),
                "execution": "terminal_private",
                "chronometry": chronometry or None,
            }
            for key in (
                "theta_1m",
                "theta_5m",
                "floor",
                "p50_1m",
                "spread_signal",
                "spread_fill",
                "slip_spread",
                "pnl_spread",
                "pnl_usdt_approx",
                "open_fill_spread",
                "close_fill_spread",
                "fees_usdt",
                "fee_usdt",
                "potential_pp",
                "signal_mono_ns",
            ):
                if key in extra and extra.get(key) is not None:
                    sentry_extras[key] = extra.get(key)

            capture_trade_event(
                event=event,
                trade_id=trade_id,
                coin=coin,
                side=side or "unknown",
                extras=sentry_extras,
                level="error",
            )
        except Exception:
            try:
                self.log.exception(
                    "sentry_trade_emit | status=fail | path=terminal_private"
                )
            except Exception:
                pass

    def _terminal_private_step_chrono_summary(
        self,
        intent_id: str,
    ) -> Optional[dict[str, Any]]:
        """Best-effort per-leg timings from step_chrono.jsonl for this intent."""
        if not intent_id:
            return None
        try:
            from app.bot.paths import theta_step_chrono_jsonl_path

            path = theta_step_chrono_jsonl_path(self.data_root)
            if not path.exists() or path.is_symlink():
                return None
            wanted = str(intent_id)
            rows: list[dict[str, Any]] = []
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if str(row.get("intent_id") or "") == wanted:
                        rows.append(row)
            if not rows:
                return None
            by_block: dict[str, dict[str, Any]] = {}
            for row in rows:
                block = str(row.get("block") or "")
                edge = str(row.get("edge") or "")
                if not block:
                    continue
                slot = by_block.setdefault(block, {})
                payload = {
                    "wall_ms": row.get("wall_ms"),
                    "mono_ns": row.get("mono_ns"),
                    "duration_ms": row.get("duration_ms"),
                }
                if edge:
                    slot[edge] = payload
                else:
                    slot.setdefault("row", payload)
            signal_ms = None
            send_ms = None
            fill_ms = None
            sig = by_block.get("signal_decision") or {}
            if "selected" in sig:
                signal_ms = sig["selected"].get("wall_ms")
            elif "row" in sig:
                signal_ms = sig["row"].get("wall_ms")
            ws = by_block.get("ws_send") or {}
            if "enter" in ws:
                send_ms = ws["enter"].get("wall_ms")
            fill = by_block.get("fill_done") or {}
            if "exit" in fill:
                fill_ms = fill["exit"].get("wall_ms")
            elif "enter" in fill:
                fill_ms = fill["enter"].get("wall_ms")
            out: dict[str, Any] = {"blocks": by_block}
            if signal_ms is not None and send_ms is not None:
                try:
                    out["signal_to_send_ms"] = int(send_ms) - int(signal_ms)
                except (TypeError, ValueError):
                    pass
            if send_ms is not None and fill_ms is not None:
                try:
                    out["send_to_fill_ms"] = int(fill_ms) - int(send_ms)
                except (TypeError, ValueError):
                    pass
            if signal_ms is not None and fill_ms is not None:
                try:
                    out["signal_to_fill_ms"] = int(fill_ms) - int(signal_ms)
                except (TypeError, ValueError):
                    pass
            return out
        except Exception:
            return None

    def _synthetic_live_place(self, **kwargs: Any) -> Any:
        """Live gates only. Uses the warm session sender; does not open a new socket."""
        from app.bot.private.place_send import (
            SYNTHETIC_FILL_WAIT_SEC,
            PlaceSendResult,
            drain_trade_fill,
            place_live,
            read_warm_trade_frame,
        )
        from app.bot.private.send_legs import _attempt_ids
        session = self._private_warm
        if session is None:
            if not self._terminal_private_execution:
                self._synthetic_roll_halt_reason = "private_channel_down"
            return PlaceSendResult(abort="private_channel_down")
        from app.bot.private.okx_ct_val import bind_okx_ct_val
        from app.bot.private.okx_inst_id import lookup_okx_inst_id_code

        meta = kwargs.get("meta")
        bound, ct_err = bind_okx_ct_val(meta, self._okx_ct_vals)
        if ct_err:
            if not self._terminal_private_execution:
                self._synthetic_roll_halt_reason = ct_err
            return PlaceSendResult(abort=ct_err)
        symbol = str(getattr(bound, "okx_symbol", "") or "")
        bybit_symbol = str(getattr(bound, "bybit_symbol", "") or "")
        inst = lookup_okx_inst_id_code(self._okx_inst_id_codes, symbol)
        if inst is None:
            if not self._terminal_private_execution:
                self._synthetic_roll_halt_reason = "okx_inst_id_code_missing"
            return PlaceSendResult(abort="okx_inst_id_code_missing")
        if not (
            self._leverage_one.get(("okx", symbol)) == "1"
            and self._leverage_one.get(("bybit", bybit_symbol)) == "1"
        ):
            if not self._terminal_private_execution:
                self._synthetic_roll_halt_reason = "leverage_not_one"
            return PlaceSendResult(abort="leverage_not_one")
        kwargs = dict(kwargs)
        kwargs["meta"] = bound
        sender = getattr(self, "_synthetic_sender", None)
        if (
            sender is None
            or getattr(self, "_synthetic_sender_session", None) is not session
            or not sender.is_ready()
        ):
            if not self._terminal_private_execution:
                self._synthetic_roll_halt_reason = "synthetic_sender_not_ready"
            return PlaceSendResult(abort="synthetic_sender_not_ready")
        wire = getattr(session, "wire", None)
        if wire is None or not getattr(wire, "healthy", False):
            self._synthetic_roll_halt_reason = "wire_capture_unavailable"
            return PlaceSendResult(abort="wire_capture_unavailable")
        deadline = time.monotonic() + SYNTHETIC_FILL_WAIT_SEC

        def _runtime_for(venue: str) -> Any:
            key = str(venue).strip().lower()
            if key == "bybit":
                return session.bybit_runtime
            if key == "okx":
                return session.okx_runtime
            raise ValueError(f"recv venue must be bybit|okx, got {venue!r}")

        def _recv(venue: str) -> Any:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            runtime = _runtime_for(venue)
            bybit_id, okx_id, _dual = _attempt_ids(str(kwargs.get("intent_id") or ""))
            return drain_trade_fill(
                lambda timeout_sec: read_warm_trade_frame(runtime, timeout_sec),
                exchange=str(getattr(runtime, "exchange", venue)),
                timeout_sec=remaining,
                order_ids={bybit_id if venue == "bybit" else okx_id},
            )

        # Holds place-inflight so reconnect does not drop the sockets and a
        # thread keepalive stashes trade frames instead of racing recv_text.
        with session.place_io_section():
            result = place_live(
                data_root=self.data_root,
                sender=sender,
                credentials=session.bybit_credentials,
                inst_id_code=inst,
                recv_fn=_recv,
                leverage_one=True,
                **kwargs,
            )
        if wire is None or not getattr(wire, "healthy", False):
            self._synthetic_roll_halt_reason = "wire_capture_failed"
        elif str(kwargs.get("spread_side") or "") == "close":
            self._canary29_record_close_flat(result, str(kwargs.get("base_coin") or ""))
        if (
            not getattr(result, "completed", False)
            and (
                getattr(result, "send_attempted", False)
                or getattr(result, "keep_pending", False)
            )
        ):
            abort = getattr(result, "abort", None)
            status = getattr(result, "status", None)
            self._synthetic_roll_halt_reason = str(
                abort or status or "incomplete_place"
            )
        # Sentry after place_io_section so trade emit never blocks/delays send.
        if self._terminal_private_execution:
            self._emit_terminal_private_trade_sentry(result, kwargs)
        return result

    def _prefetch_okx_inst_id_codes(self) -> None:
        """Public instruments lookup once, before the roll loop. Not on place."""
        from app.bot.private.okx_inst_id import (
            fetch_okx_inst_id_code,
            prefetch_okx_inst_id_codes,
        )

        symbols: list[str] = []
        for coin in self.coins:
            try:
                symbols.append(self._meta(coin).okx_symbol)
            except KeyError:
                self.log.warning("okx_inst_id_skip | coin=%s | err=KeyError", coin)
        self._okx_inst_id_codes = prefetch_okx_inst_id_codes(
            symbols,
            env=os.environ,
            fetch_fn=fetch_okx_inst_id_code,
        )
        missing = [s for s in symbols if s not in self._okx_inst_id_codes]
        self.log.info(
            "okx_inst_id_prefetched | n=%s | missing=%s",
            len(self._okx_inst_id_codes),
            ",".join(missing) if missing else "-",
        )

    def _prefetch_okx_ct_vals(self) -> None:
        """Public instruments ctVal once, before the roll loop. Not on place."""
        from app.bot.private.okx_ct_val import fetch_okx_ct_val, prefetch_okx_ct_vals

        symbols: list[str] = []
        for coin in self.coins:
            try:
                symbols.append(self._meta(coin).okx_symbol)
            except KeyError:
                self.log.warning("okx_ct_val_skip | coin=%s | err=KeyError", coin)
        self._okx_ct_vals = prefetch_okx_ct_vals(
            symbols,
            fetch_fn=fetch_okx_ct_val,
        )
        missing = [s for s in symbols if s not in self._okx_ct_vals]
        self.log.info(
            "okx_ct_val_prefetched | n=%s | missing=%s",
            len(self._okx_ct_vals),
            ",".join(missing) if missing else "-",
        )

    def _set_leverage_one(self) -> None:
        """One signed leverage=1 POST per coin per venue. Not inside ws_send."""
        from app.bot.private.leverage_one import LeverageTarget, set_leverage_one
        from app.bot.private.venue import endpoints_for_venue

        session = self._private_warm
        if session is None:
            return
        targets: list[LeverageTarget] = []
        confirmed: dict[tuple[str, str], str] = {}
        for coin in self.coins:
            try:
                meta = self._meta(coin)
            except KeyError:
                self.log.warning("leverage_one_skip | coin=%s | err=KeyError", coin)
                continue
            targets.append(
                LeverageTarget(
                    coin=coin,
                    okx_symbol=meta.okx_symbol,
                    bybit_symbol=meta.bybit_symbol,
                )
            )
        confirmed.update(set_leverage_one(
            targets,
            okx_credentials=session.okx_credentials,
            bybit_credentials=session.bybit_credentials,
            endpoints=endpoints_for_venue("live"),
        ))
        self._leverage_one = confirmed
        missing = [
            f"{venue}:{symbol}"
            for target in targets
            for venue, symbol in (
                ("okx", target.okx_symbol),
                ("bybit", target.bybit_symbol),
            )
            if self._leverage_one.get((venue, symbol)) != "1"
        ]
        self.log.info(
            "leverage_one_warmup | n=%s | missing=%s",
            len(self._leverage_one),
            ",".join(missing) if missing else "-",
        )

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
        # Keep WS parsing in raw contract units; attach the startup-cached
        # multiplier only at the private manager's runtime context boundary.
        if self.theta_trade is not None and (
            self.profile == "synthetic_roll" or self._terminal_private_execution
        ):
            okx["_private_size_gate"] = True
            try:
                symbol = self._meta(base_coin).okx_symbol
                okx["ct_val"] = self._okx_ct_vals.get(symbol)
            except (KeyError, TypeError, AttributeError):
                okx["ct_val"] = None
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

    async def _synthetic_roll_loop(self) -> None:
        """~1 Hz pool roll. Does not wait on theta snapshots."""
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=EMIT_INTERVAL_SEC
                )
                break
            except asyncio.TimeoutError:
                pass
            if self.theta_trade is None:
                continue
            try:
                await self.theta_trade.on_theta_snapshots_async(
                    [],
                    quotes=self.quotes,
                    coin_order=self.coins,
                )
            except Exception as exc:  # noqa: BLE001
                if not self._theta_trade_warned:
                    self._theta_trade_warned = True
                    self.log.warning(
                        "theta_trade_failed | err=%s",
                        type(exc).__name__,
                    )
                continue
            halt = getattr(self, "_synthetic_roll_halt_reason", None)
            if self.theta_trade.slot.pending or halt:
                if not self._synthetic_roll_halted:
                    self._synthetic_roll_halted = True
                    self.log.warning(
                        "synthetic_roll_stopped | reason=%s",
                        halt or "partial_fill",
                    )
                break

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
        if self.theta_trade is None or (
            not theta_snaps
            and not (
                self._terminal_private_execution
                and self._canary29_policy == "synthetic"
            )
        ):
            return
        if self._canary29_done:
            self.stop_event.set()
            return
        halt_reason = self.theta_trade.execution_halt_reason or self._synthetic_roll_halt_reason
        if halt_reason:
            self._synthetic_roll_halt_reason = halt_reason
            self.stop_event.set()
            return
        try:
            await self.theta_trade.on_theta_snapshots_async(
                theta_snaps,
                quotes=self.quotes,
                coin_order=self.coins,
            )
            if (
                self._canary29_window_elapsed()
                and self.theta_trade.slot.position is None
                and not self.theta_trade.slot.pending
            ):
                self._canary29_done = True
                self._canary29_done_reason = "open_window_elapsed_flat"
                self.log.info("canary29_stopped | reason=%s", self._canary29_done_reason)
                self.stop_event.set()
                self._write_canary_state()
            halt_reason = self.theta_trade.execution_halt_reason or self._synthetic_roll_halt_reason
            if halt_reason:
                self._synthetic_roll_halt_reason = halt_reason
                self.stop_event.set()
        except Exception as exc:  # noqa: BLE001 — never stall the public book path
            if self._terminal_private_execution:
                self._synthetic_roll_halt_reason = f"terminal_tick:{type(exc).__name__}"
                self.stop_event.set()
                self.log.error("canary29_stopped | reason=%s", self._synthetic_roll_halt_reason)
            elif not self._theta_trade_warned:
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
            if self._terminal_private_execution and self.theta_trade is not None:
                if (
                    self._canary29_window_elapsed()
                    and self.theta_trade.slot.position is None
                    and not self.theta_trade.slot.pending
                ):
                    self._canary29_done = True
                    self._canary29_done_reason = "open_window_elapsed_flat"
                    self.log.info("canary29_stopped | reason=%s", self._canary29_done_reason)
                    self.stop_event.set()
                try:
                    self._write_canary_state()
                except Exception as exc:
                    self._canary29_latch_checkpoint_failure(exc)
            snap = self.gate.heartbeat_fields()
            pending = self.broker.pending.intent_id if self.broker.pending else None
            position = self.broker.position
            if self._terminal_private_execution and self.theta_trade is not None:
                pending = self.theta_trade.slot.pending
                position = self.theta_trade.slot.position
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
                    position,
                    self.probe_done,
                    counters,
                )
            )
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                pass

    async def _floor_warm_periodic_save(self) -> None:
        """Periodic floor warm pickle save (every ~10 minutes)."""
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=FLOOR_WARM_SAVE_INTERVAL_SEC
                )
            except asyncio.TimeoutError:
                pass
            if not self.stop_event.is_set():
                self._save_floor_warm_pickle()

    async def run(self) -> None:
        # Register signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        
        def _signal_handler(signame: str) -> None:
            self.log.info(f"bbot_signal_received | signal={signame}")
            if self._terminal_private_execution:
                self._canary29_done_reason = f"operator_signal_{signame.lower()}"
                try:
                    self._write_canary_state()
                except Exception as exc:
                    self.log.error("canary29_state_checkpoint_failed | err=%s", type(exc).__name__)
            # Save floor warm pickle immediately (synchronously) before stop_event.
            # WS tasks may not unwind to finally before systemd timeout.
            self._save_floor_warm_pickle()
            self.stop_event.set()
        
        for sig in (signal.SIGTERM, signal.SIGHUP):
            loop.add_signal_handler(
                sig,
                lambda s=sig: _signal_handler(signal.Signals(s).name)
            )
        
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
            f"theta_execution={'terminal_private' if self._terminal_private_execution else 'inline'} | "
            f"theta_policy={self._canary29_policy if self._terminal_private_execution else 'n/a'} | "
            f"theta_live_send="
            f"{'on' if (self._terminal_private_execution or getattr(self.theta_trade, 'live_send', False)) else 'off'} | "
            f"canary_max_cycles={self._canary29_max_cycles} | "
            f"canary_open_window_hours={self._canary29_open_window_hours} | "
            f"floor_warm={'loaded' if self._floor_warm_loaded else 'off'}"
        )
        if self.mode == "policy" and self.policy is None:
            self.log.error(
                "policy_missing | continuing WS-only; will not open intents"
            )

        if self._terminal_private_execution:
            await asyncio.to_thread(lambda: None)  # start the default worker before signal ticks

        # Private WS and synthetic send path must be ready before signal tasks.
        try:
            private_stop_event = self.stop_event
            if self._terminal_private_execution:
                self._private_stop_event = asyncio.Event()
                private_stop_event = self._private_stop_event
            self._private_warm = self.start_private_warm_if_live_send(
                stop_event=private_stop_event,
                coins=self.coins,
            )
            if self._private_warm is not None:
                self.log.info(
                    "private_warm_started | run_id=%s | ready=%s | handshake_count=%s | keepalive=%s",
                    self._private_warm.run_id,
                    self._private_warm.is_ready(),
                    self._private_warm._handshake_count,  # noqa: SLF001
                    self._private_warm.keepalive_running,
                )
                if self.profile == "synthetic_roll":
                    self._prefetch_okx_ct_vals()
                    self._prefetch_okx_inst_id_codes()
                    self._set_leverage_one()
                if self._terminal_private_execution:
                    self._prefetch_okx_canary_metadata()
                    if self._canary29_resume_manifest_path is None:
                        await asyncio.to_thread(self._canary29_assert_startup_flat)
                    else:
                        await asyncio.to_thread(self._canary29_resume_position)
                    if (
                        self._canary29_resume_manifest_path is None
                        and self.theta_trade is not None
                        and self.theta_trade.slot.slot_busy()
                    ):
                        raise RuntimeError("journal_position_not_flat")
                    confirmed = frozenset(
                        coin.strip().upper()
                        for coin in os.environ.get("BBOT_CONFIRMED_1X_COINS", "").split(",")
                        if coin.strip()
                    )
                    unknown = confirmed.difference(self.coins)
                    if unknown:
                        raise RuntimeError("BBOT_CONFIRMED_1X_COINS contains coins outside active universe")
                    missing = set(self.coins).difference(confirmed)
                    if missing:
                        raise RuntimeError("terminal_private requires prep-verified 1x leverage for every coin")
                    self._leverage_one = {
                        key: "1"
                        for coin in self.coins
                        for meta in (self._meta(coin),)
                        for key in (("okx", meta.okx_symbol), ("bybit", meta.bybit_symbol))
                    }
                    self._write_canary_state()
            else:
                self.log.info("private_warm_skipped | live_private_send=false")
                if self._terminal_private_execution:
                    raise RuntimeError("terminal_private requires a warmed private session")
                if self.profile == "synthetic_roll":
                    self._prefetch_okx_ct_vals()

            if self._synthetic_live_send_enabled:
                if self._private_warm is None:
                    raise RuntimeError("synthetic live gates require a ready private session")
                self._prepare_synthetic_live_sender()
        except Exception as exc:
            self.log.error(
                "private_or_synthetic_warm_failed | err=%s | refusing signal loop",
                type(exc).__name__,
            )
            try:
                self._stop_synthetic_private_send()
            except Exception as cleanup_exc:
                self.log.error(
                    "private_warm_cleanup_failed | err=%s",
                    type(cleanup_exc).__name__,
                )
            raise

        tasks: list[asyncio.Task] = [asyncio.create_task(self._heartbeat())]
        # Periodic floor warm pickle save (if floor observer is enabled)
        if self.floor_observer is not None and (
            self.profile in {"gear22_would_send", "gear22_live_canary"}
            or self._floor_warm_loaded
        ):
            tasks.append(
                asyncio.create_task(
                    self._floor_warm_periodic_save(), name="floor-warm-save"
                )
            )
        if self.tw_p50_enabled and self.tw_p50_observer is not None:
            tasks.append(
                asyncio.create_task(self._tw_p50_emit_loop(), name="tw-p50-emit")
            )
        elif self.theta_enabled and self.theta_screener is not None:
            # Theta alone (TW off): still emit ~1 Hz from last RAM snapshots.
            tasks.append(
                asyncio.create_task(self._theta_emit_loop(), name="theta-emit")
            )
        if self.profile == "synthetic_roll" and self.theta_trade is not None:
            tasks.append(
                asyncio.create_task(self._synthetic_roll_loop(), name="synthetic-roll")
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
            self._save_floor_warm_pickle()
            if await self._await_terminal_place_before_shutdown():
                if self._terminal_private_execution:
                    try:
                        self._write_canary_state()
                    except Exception as exc:
                        self.log.error("canary29_state_checkpoint_failed | err=%s", type(exc).__name__)
                self._stop_synthetic_private_send()


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
