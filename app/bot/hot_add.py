"""B1 bot hot-add — BBOT_* env only. Never SPREAD_HOT_ADD_* in the bot process.

Polls hand-editable delta/drop snapshots (collector-compatible columns).
Spawns the bot's own public WS tasks (run_okx_books5 / run_bybit_orderbook1).
Does not call discovery REST, does not read /data/live, does not share D sockets.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Optional, Sequence

from research.is_crypto import is_hot_add_crypto

from app.bot.stub_broker import InstrumentMeta
from app.utils.hot_add import run_hot_add_poller  # reuse poller; not SPREAD_* env
from app.utils.universe_delta import DELTA_FIELDNAMES

HOT_ADD_ENV = "BBOT_HOT_ADD"
DELTA_ENV = "BBOT_HOT_ADD_DELTA"
DROP_ENV = "BBOT_HOT_ADD_DROP"
MAX_EXTRA_ENV = "BBOT_HOT_ADD_MAX_EXTRA"
POLL_SEC_ENV = "BBOT_HOT_ADD_POLL_SEC"
SET_LEVERAGE_ENV = "BBOT_HOT_ADD_SET_LEVERAGE"

DEFAULT_DELTA_NAME = "hot_add_delta.csv"
DEFAULT_DROP_NAME = "hot_add_drop.csv"
DEFAULT_MAX_EXTRA = 8
DEFAULT_POLL_SEC = 30.0

# Lot/tick columns required for fail-closed meta (B1 seam).
_LOT_TICK_FIELDS = (
    "okx_tick_size",
    "okx_lot_size",
    "okx_min_size",
    "bybit_tick_size",
    "bybit_qty_step",
    "bybit_min_order_qty",
)

SpawnFn = Callable[[Mapping[str, str]], None]


def _flag_on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def bbot_hot_add_enabled() -> bool:
    """Default OFF. Deploying this code must not change the bot pool."""
    return _flag_on(HOT_ADD_ENV)


def bbot_hot_add_set_leverage_enabled() -> bool:
    """Default OFF; opt in to verified 1x setup for newly hot-added coins."""
    return _flag_on(SET_LEVERAGE_ENV)


def bbot_hot_add_max_extra() -> int:
    raw = os.environ.get(MAX_EXTRA_ENV, str(DEFAULT_MAX_EXTRA)).strip()
    value = int(raw)
    if value < 0:
        raise ValueError(f"{MAX_EXTRA_ENV} must be >= 0, got {value}")
    return value


def bbot_hot_add_poll_sec() -> float:
    raw = os.environ.get(POLL_SEC_ENV, str(DEFAULT_POLL_SEC)).strip()
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{POLL_SEC_ENV} must be > 0, got {value}")
    return value


def _resolve_under_data_root(raw: str, data_root: Path, default_name: str) -> Path:
    """Absolute path as-is; relative → under data_root. Never under /data/live."""
    text = (raw or "").strip() or default_name
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = data_root / path
    resolved = path
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    text_res = str(resolved)
    for prefix in ("/data/live", "/data/bars", "/data/compacted", "/data/spool"):
        if text_res == prefix or text_res.startswith(prefix + os.sep):
            raise RuntimeError(
                f"BBOT hot-add path refuses D collector tree: {resolved}"
            )
    return path


def bbot_hot_add_delta_path(data_root: Path) -> Path:
    raw = os.environ.get(DELTA_ENV, DEFAULT_DELTA_NAME)
    return _resolve_under_data_root(raw, data_root, DEFAULT_DELTA_NAME)


def bbot_hot_add_drop_path(data_root: Path) -> Path:
    raw = os.environ.get(DROP_ENV, DEFAULT_DROP_NAME)
    return _resolve_under_data_root(raw, data_root, DEFAULT_DROP_NAME)


def _positive_float(raw: object) -> Optional[float]:
    text = str(raw if raw is not None else "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if value != value or value <= 0:  # NaN or non-positive
        return None
    return value


def lot_tick_complete(row: Mapping[str, object]) -> bool:
    """True when every required lot/tick field parses to a positive float."""
    for name in _LOT_TICK_FIELDS:
        if _positive_float(row.get(name)) is None:
            return False
    return True


def instrument_meta_from_row(row: Mapping[str, object]) -> InstrumentMeta:
    """Build InstrumentMeta from a delta (or universe) row. Raises if incomplete."""
    coin = str(row.get("base_coin", "")).strip().upper()
    okx_symbol = str(row.get("okx_symbol", "")).strip()
    bybit_symbol = str(row.get("bybit_symbol", "")).strip()
    if not coin or not okx_symbol or not bybit_symbol:
        raise ValueError("instrument_meta_from_row requires base_coin/okx/bybit symbols")
    if not lot_tick_complete(row):
        raise ValueError(f"lot/tick incomplete for {coin}")
    min_notional = _positive_float(row.get("bybit_min_notional_value")) or 0.0
    return InstrumentMeta(
        base_coin=coin,
        okx_symbol=okx_symbol,
        bybit_symbol=bybit_symbol,
        okx_lot_size=float(_positive_float(row["okx_lot_size"])),
        okx_min_size=float(_positive_float(row["okx_min_size"])),
        bybit_qty_step=float(_positive_float(row["bybit_qty_step"])),
        bybit_min_order_qty=float(_positive_float(row["bybit_min_order_qty"])),
        okx_tick_size=float(_positive_float(row["okx_tick_size"])),
        bybit_tick_size=float(_positive_float(row["bybit_tick_size"])),
        bybit_min_notional_value=float(min_notional),
    )


def resolve_hot_add_meta(
    row: Mapping[str, str],
    universe: Mapping[str, InstrumentMeta],
) -> tuple[Optional[InstrumentMeta], Optional[str]]:
    """Fail-closed meta resolution for a delta row.

    Prefer universe CSV (must have positive lot/tick). If coin is absent from
    the universe map, require complete lot/tick on the delta row itself.
    Returns (meta, skip_reason). skip_reason set → do not spawn.
    """
    coin = str(row.get("base_coin", "")).strip().upper()
    if not coin:
        return None, "missing_base_coin"
    existing = universe.get(coin)
    if existing is not None:
        # Universe hit: still fail-closed if lot/tick were never populated.
        probe = {
            "base_coin": existing.base_coin,
            "okx_symbol": existing.okx_symbol,
            "bybit_symbol": existing.bybit_symbol,
            "okx_tick_size": existing.okx_tick_size,
            "okx_lot_size": existing.okx_lot_size,
            "okx_min_size": existing.okx_min_size,
            "bybit_tick_size": existing.bybit_tick_size,
            "bybit_qty_step": existing.bybit_qty_step,
            "bybit_min_order_qty": existing.bybit_min_order_qty,
            "bybit_min_notional_value": existing.bybit_min_notional_value,
        }
        if not lot_tick_complete(probe):
            return None, "universe_lot_tick_missing"
        return existing, None
    # Missing from universe CSV: only accept when delta carries full lot/tick.
    if not lot_tick_complete(row):
        return None, "missing_from_universe_and_lot_tick"
    try:
        return instrument_meta_from_row(row), None
    except ValueError:
        return None, "lot_tick_invalid"


class BotHotAddController:
    """Apply a delta snapshot into the bot pool with B1 fail-closed meta rules.

    Compatible with ``run_hot_add_poller`` (apply_rows / extra_count).
    """

    def __init__(
        self,
        *,
        quotes: MutableMapping[str, Any],
        universe: MutableMapping[str, InstrumentMeta],
        spawn: SpawnFn,
        max_extra: int,
        initial_pair_count: int,
        logger: logging.Logger,
    ) -> None:
        if max_extra < 0:
            raise ValueError(f"max_extra must be >= 0, got {max_extra}")
        self.quotes = quotes
        self.universe = universe
        self._spawn = spawn
        self.max_extra = max_extra
        self.initial_pair_count = initial_pair_count
        self.logger = logger
        self.spawned: list[str] = []

    def extra_count(self) -> int:
        return max(0, len(self.quotes) - self.initial_pair_count)

    def apply_rows(self, rows: Sequence[Mapping[str, str]]) -> list[str]:
        added: list[str] = []
        for row in rows:
            coin = str(row.get("base_coin", "")).strip().upper()
            okx_symbol = str(row.get("okx_symbol", "")).strip()
            bybit_symbol = str(row.get("bybit_symbol", "")).strip()
            if not coin or not okx_symbol or not bybit_symbol:
                self.logger.error(
                    "bbot_hot_add_row_invalid | base_coin=%s | okx_symbol=%s | bybit_symbol=%s",
                    coin or "-",
                    okx_symbol or "-",
                    bybit_symbol or "-",
                )
                continue
            if not is_hot_add_crypto(coin):
                self.logger.info(
                    "bbot_hot_add_skip | base_coin=%s | reason=non_crypto",
                    coin,
                )
                continue
            if coin in self.quotes:
                self.logger.info(
                    "bbot_hot_add_skip | base_coin=%s | reason=already_in_quotes",
                    coin,
                )
                continue
            meta, skip_reason = resolve_hot_add_meta(row, self.universe)
            if skip_reason is not None or meta is None:
                self.logger.error(
                    "bbot_hot_add_fail_closed | base_coin=%s | reason=%s",
                    coin,
                    skip_reason or "meta_none",
                )
                continue
            if self.extra_count() >= self.max_extra:
                self.logger.error(
                    "bbot_hot_add_cap_hit | base_coin=%s | extra=%s | max_extra=%s",
                    coin,
                    self.extra_count(),
                    self.max_extra,
                )
                break
            # Register meta before spawn so _meta(coin) works on first book tick.
            if coin not in self.universe:
                self.universe[coin] = meta
            # Normalize symbols on the spawn row from resolved meta.
            spawn_row = {
                "base_coin": coin,
                "okx_symbol": meta.okx_symbol,
                "bybit_symbol": meta.bybit_symbol,
            }
            for name in DELTA_FIELDNAMES:
                if name not in spawn_row and name in row:
                    spawn_row[name] = str(row.get(name, "")).strip()
            self._spawn(spawn_row)
            if coin not in self.quotes:
                raise RuntimeError(
                    f"spawn_coin did not init quotes[{coin!r}]; refusing to continue"
                )
            self.spawned.append(coin)
            added.append(coin)
            self.logger.info(
                "bbot_hot_add_applied | base_coin=%s | okx_symbol=%s | "
                "bybit_symbol=%s | extra=%s",
                coin,
                meta.okx_symbol,
                meta.bybit_symbol,
                self.extra_count(),
            )
        return added


# Re-export poller for callers that import from app.bot.hot_add only.
__all__ = [
    "HOT_ADD_ENV",
    "DELTA_ENV",
    "DROP_ENV",
    "MAX_EXTRA_ENV",
    "POLL_SEC_ENV",
    "BotHotAddController",
    "bbot_hot_add_delta_path",
    "bbot_hot_add_drop_path",
    "bbot_hot_add_enabled",
    "bbot_hot_add_max_extra",
    "bbot_hot_add_poll_sec",
    "instrument_meta_from_row",
    "lot_tick_complete",
    "resolve_hot_add_meta",
    "run_hot_add_poller",
]
