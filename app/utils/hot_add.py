"""In-process hot-add controller. Source = delta file, never REST."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Optional, Sequence

from .task_supervisor import TaskSupervisor
from .universe_delta import read_delta_rows

HOT_ADD_ENV = "SPREAD_HOT_ADD"
DELTA_ENV = "SPREAD_HOT_ADD_DELTA"
MAX_EXTRA_ENV = "SPREAD_HOT_ADD_MAX_EXTRA"
POLL_SEC_ENV = "SPREAD_HOT_ADD_POLL_SEC"

DEFAULT_DELTA_PATH = "hot_add_delta.csv"
DEFAULT_MAX_EXTRA = 8
DEFAULT_POLL_SEC = 30.0

SpawnFn = Callable[[Mapping[str, str]], None]


def _flag_on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def hot_add_enabled() -> bool:
    """Default OFF. Deploying this code must not change the live pool."""
    return _flag_on(HOT_ADD_ENV)


def hot_add_delta_path() -> Path:
    raw = os.environ.get(DELTA_ENV, DEFAULT_DELTA_PATH).strip() or DEFAULT_DELTA_PATH
    return Path(raw)


def hot_add_max_extra() -> int:
    raw = os.environ.get(MAX_EXTRA_ENV, str(DEFAULT_MAX_EXTRA)).strip()
    value = int(raw)
    if value < 0:
        raise ValueError(f"{MAX_EXTRA_ENV} must be >= 0, got {value}")
    return value


def hot_add_poll_sec() -> float:
    raw = os.environ.get(POLL_SEC_ENV, str(DEFAULT_POLL_SEC)).strip()
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{POLL_SEC_ENV} must be > 0, got {value}")
    return value


def quote_state_for_row(row: Mapping[str, str]) -> dict[str, Any]:
    """Same quotes[coin] shape as import-time collector init."""
    return {
        "okx_symbol": str(row["okx_symbol"]).strip(),
        "bybit_symbol": str(row["bybit_symbol"]).strip(),
        "okx": {
            "bid_price": None,
            "bid_size": None,
            "ask_price": None,
            "ask_size": None,
            "ts_exchange": None,
            "local_recv_ts_ms": None,
            "delivery_latency_ms": None,
        },
        "bybit": {
            "bid_price": None,
            "bid_size": None,
            "ask_price": None,
            "ask_size": None,
            "ts_exchange": None,
            "cts_exchange": None,
            "local_recv_ts_ms": None,
            "delivery_latency_ms": None,
        },
    }


class HotAddController:
    """Apply a delta snapshot: init quotes, spawn book tasks, honor extra cap."""

    def __init__(
        self,
        *,
        quotes: MutableMapping[str, Any],
        spawn: SpawnFn,
        max_extra: int,
        initial_pair_count: int,
        logger: logging.Logger,
    ) -> None:
        if max_extra < 0:
            raise ValueError(f"max_extra must be >= 0, got {max_extra}")
        self.quotes = quotes
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
            coin = str(row.get("base_coin", "")).strip()
            okx_symbol = str(row.get("okx_symbol", "")).strip()
            bybit_symbol = str(row.get("bybit_symbol", "")).strip()
            if not coin or not okx_symbol or not bybit_symbol:
                self.logger.error(
                    "hot_add_row_invalid | base_coin=%s | okx_symbol=%s | bybit_symbol=%s",
                    coin or "-",
                    okx_symbol or "-",
                    bybit_symbol or "-",
                )
                continue
            if coin in self.quotes:
                self.logger.info(
                    "hot_add_skip | base_coin=%s | reason=already_in_quotes",
                    coin,
                )
                continue
            if self.extra_count() >= self.max_extra:
                self.logger.error(
                    "hot_add_cap_hit | base_coin=%s | extra=%s | max_extra=%s",
                    coin,
                    self.extra_count(),
                    self.max_extra,
                )
                break
            self._spawn(
                {
                    "base_coin": coin,
                    "okx_symbol": okx_symbol,
                    "bybit_symbol": bybit_symbol,
                }
            )
            if coin not in self.quotes:
                raise RuntimeError(
                    f"spawn_coin did not init quotes[{coin!r}]; refusing to continue"
                )
            self.spawned.append(coin)
            added.append(coin)
            self.logger.info(
                "hot_add_applied | base_coin=%s | okx_symbol=%s | bybit_symbol=%s | extra=%s",
                coin,
                okx_symbol,
                bybit_symbol,
                self.extra_count(),
            )
        return added


async def run_hot_add_poller(
    controller: HotAddController,
    delta_path: Path,
    *,
    interval_sec: float,
    reload_event: asyncio.Event,
    logger: logging.Logger,
    supervisor: Optional[TaskSupervisor] = None,
) -> None:
    """Poll / SIGHUP-reload the delta file. Never REST. Missing file is not fatal."""
    last_mtime: Optional[float] = None
    while True:
        if supervisor is not None and supervisor.closed:
            return
        try:
            if not delta_path.exists():
                logger.warning("hot_add_delta_missing | path=%s", delta_path)
            else:
                mtime = delta_path.stat().st_mtime
                if mtime != last_mtime:
                    rows = read_delta_rows(delta_path)
                    added = controller.apply_rows(rows)
                    logger.info(
                        "hot_add_delta_read | path=%s | rows=%s | added=%s | extra=%s",
                        delta_path,
                        len(rows),
                        len(added),
                        controller.extra_count(),
                    )
                    last_mtime = mtime
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "hot_add_poll_failed | path=%s | error=%s",
                delta_path,
                exc,
            )
        try:
            await asyncio.wait_for(reload_event.wait(), timeout=interval_sec)
            reload_event.clear()
            logger.info("hot_add_reload | source=sighup_or_event | path=%s", delta_path)
        except asyncio.TimeoutError:
            pass
