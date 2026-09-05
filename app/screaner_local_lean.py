"""Local lean spread collector — parallel track, not the VPS canary.

Simple direct parquet writes (early screaner style). Writes lean tick fields
only; optional closed 5m candle volume on a separate root.

Defaults never touch ``/data/live`` or production spool/publisher paths.

Run from repo root:
  python3 app/screaner_local_lean.py --row-end 2 --persist-every 50
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import websockets

_REPO_ROOT = Path(__file__).resolve().parents[1]
_APP_DIR = Path(__file__).resolve().parent
for _p in (_REPO_ROOT, _APP_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from schema.lean_event import (  # noqa: E402
    BAR_INTERVAL_MS,
    LEAN_BAR_5M_BODY_COLS,
    LEAN_TICK_BODY_COLS,
)
from utils.universe_csv import filter_take_yes_rows  # noqa: E402

# --- paths / config (local only) ------------------------------------------------

DEFAULT_TICK_ROOT = _REPO_ROOT / "output" / "lean_ticks"
DEFAULT_BARS_ROOT = _REPO_ROOT / "output" / "lean_bars"
DEFAULT_UNIVERSE = _REPO_ROOT / "bybit_okx_universe.csv"
DEFAULT_LOG = _REPO_ROOT / "output" / "lean_runtime.log"

OKX_BOOKS_WS = "wss://ws.okx.com:8443/ws/v5/public"
OKX_CANDLE_WS = "wss://ws.okx.com:8443/ws/v5/business"
BYBIT_LINEAR_WS = "wss://stream.bybit.com/v5/public/linear"

# OKX SWAP candle: use volCcy (base currency), not vol (contracts).
OKX_VOLUME_FIELD_INDEX = 6  # [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]
OKX_CONFIRM_INDEX = 8


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


TICK_ROOT = _env_path("SPREAD_LEAN_PARQUET_ROOT", DEFAULT_TICK_ROOT)
BARS_ROOT = _env_path("SPREAD_LEAN_BARS_ROOT", DEFAULT_BARS_ROOT)
RUNTIME_LOG_PATH = _env_path("SPREAD_LEAN_RUNTIME_LOG", DEFAULT_LOG)

runtime_logger = logging.getLogger("lean_runtime")
runtime_logger.setLevel(logging.INFO)
runtime_logger.propagate = False


def _setup_logging(log_path: Path) -> None:
    if runtime_logger.handlers:
        return
    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%y-%m-%d %H:%M:%S",
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_path)
    fh.setFormatter(formatter)
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    runtime_logger.addHandler(fh)
    runtime_logger.addHandler(ch)


# --- mutable runtime state ------------------------------------------------------

pairs: list[dict[str, str]] = []
quotes: dict[str, Any] = {}

tick_buffer: list[dict[str, Any]] = []
bar_buffer: list[dict[str, Any]] = []
tick_batch_seq = 0
bar_batch_seq = 0
saved_tick_rows = 0
saved_tick_files = 0
saved_bar_rows = 0
saved_bar_files = 0
seen_bar_keys: set[tuple[str, str, int]] = set()

PERSIST_EVERY_N = 5_000
BAR_PERSIST_EVERY_N = 50
SUBSCRIBE_BATCH_SIZE = 30
SUBSCRIBE_BATCH_PAUSE_SEC = 3
COLLECT_BARS = True
COLLECT_BYBIT_BARS = False  # optional; model canon is ref_exchange=okx


def load_pairs_from_csv(path: Path, row_start: int, row_end: int) -> list[dict[str, str]]:
    """Load lean pairs. Live screen is take=yes; row_start/row_end slice that list."""
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = filter_take_yes_rows(
            list(reader),
            fieldnames=reader.fieldnames,
            path=path,
        )
    subset = rows[row_start:row_end]
    out: list[dict[str, str]] = []
    for row in subset:
        out.append(
            {
                "base_coin": row["base_coin"].strip(),
                "okx_symbol": row["okx_symbol"].strip(),
                "bybit_symbol": row["bybit_symbol"].strip(),
            }
        )
    return out


def _init_quotes(loaded: list[dict[str, str]]) -> dict[str, Any]:
    return {
        row["base_coin"]: {
            "okx_symbol": row["okx_symbol"],
            "bybit_symbol": row["bybit_symbol"],
            "okx": {
                "bid_price": None,
                "bid_size": None,
                "ask_price": None,
                "ask_size": None,
                "ts_exchange": None,
                "local_recv_ts_ms": None,
            },
            "bybit": {
                "bid_price": None,
                "bid_size": None,
                "ask_price": None,
                "ask_size": None,
                "ts_exchange": None,
                "local_recv_ts_ms": None,
            },
        }
        for row in loaded
    }


def _event_date_from_ms(ts_ms: float) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


def persist_ticks() -> None:
    global tick_batch_seq, saved_tick_rows, saved_tick_files

    if not tick_buffer:
        return

    df = pd.DataFrame(tick_buffer)
    tick_buffer.clear()
    if df.empty:
        return

    for col in LEAN_TICK_BODY_COLS:
        if col not in df.columns and col != "event_local_ts_ms":
            runtime_logger.warning("persist_ticks | missing column=%s", col)
            return

    # event_local_ts_ms = recv ts of the triggering exchange
    if "event_local_ts_ms" not in df.columns or df["event_local_ts_ms"].isna().all():
        df["event_local_ts_ms"] = df["okx_local_recv_ts_ms"]
        mask = df["trigger"].eq("bybit")
        df.loc[mask, "event_local_ts_ms"] = df.loc[mask, "bybit_local_recv_ts_ms"]

    num_cols = [c for c in LEAN_TICK_BODY_COLS if c not in ("base_coin", "trigger")]
    for col in num_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["base_coin"] = df["base_coin"].astype(str).str.strip()
    df = df[df["base_coin"] != ""]
    df = df[df["event_local_ts_ms"].notna()]
    if df.empty:
        return

    df["event_date"] = df["event_local_ts_ms"].map(_event_date_from_ms)
    df = df[list(LEAN_TICK_BODY_COLS) + ["event_date"]]

    written_rows = 0
    written_files = 0
    for (base_coin, event_date), sub in df.groupby(["base_coin", "event_date"], sort=False):
        sub = sub.sort_values("event_local_ts_ms").reset_index(drop=True)
        part_dir = TICK_ROOT / f"base_coin={base_coin}" / f"event_date={event_date}"
        part_dir.mkdir(parents=True, exist_ok=True)
        path = part_dir / f"batch_{tick_batch_seq:09d}.parquet"
        tick_batch_seq += 1
        table = pa.Table.from_pandas(
            sub.drop(columns=["event_date"]),
            preserve_index=False,
        )
        pq.write_table(table, path, compression="zstd")
        written_rows += len(sub)
        written_files += 1

    saved_tick_rows += written_rows
    saved_tick_files += written_files
    runtime_logger.info(
        "persisted_ticks | rows=%s | files=%s | total_rows=%s | root=%s",
        written_rows,
        written_files,
        saved_tick_rows,
        TICK_ROOT,
    )


def persist_bars() -> None:
    global bar_batch_seq, saved_bar_rows, saved_bar_files

    if not bar_buffer:
        return

    df = pd.DataFrame(bar_buffer)
    bar_buffer.clear()
    if df.empty:
        return

    for col in LEAN_BAR_5M_BODY_COLS:
        if col not in df.columns:
            runtime_logger.warning("persist_bars | missing column=%s", col)
            return

    for col in ("bar_start_ts_ms", "bar_end_ts_ms", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["base_coin"] = df["base_coin"].astype(str).str.strip()
    df["ref_exchange"] = df["ref_exchange"].astype(str).str.strip()
    df = df[df["bar_start_ts_ms"].notna() & (df["base_coin"] != "")]
    if df.empty:
        return

    df["event_date"] = df["bar_start_ts_ms"].map(_event_date_from_ms)
    df = df[list(LEAN_BAR_5M_BODY_COLS) + ["event_date"]]

    written_rows = 0
    written_files = 0
    for (base_coin, event_date), sub in df.groupby(["base_coin", "event_date"], sort=False):
        sub = sub.sort_values("bar_start_ts_ms").reset_index(drop=True)
        part_dir = (
            BARS_ROOT
            / "bar_5m"
            / f"base_coin={base_coin}"
            / f"event_date={event_date}"
        )
        part_dir.mkdir(parents=True, exist_ok=True)
        path = part_dir / f"batch_{bar_batch_seq:09d}.parquet"
        bar_batch_seq += 1
        table = pa.Table.from_pandas(
            sub.drop(columns=["event_date"]),
            preserve_index=False,
        )
        pq.write_table(table, path, compression="zstd")
        written_rows += len(sub)
        written_files += 1

    saved_bar_rows += written_rows
    saved_bar_files += written_files
    runtime_logger.info(
        "persisted_bars | rows=%s | files=%s | total_rows=%s | root=%s",
        written_rows,
        written_files,
        saved_bar_rows,
        BARS_ROOT,
    )


def build_lean_tick_record(base_coin: str, trigger: str) -> Optional[dict[str, Any]]:
    state = quotes[base_coin]
    okx = state["okx"]
    bybit = state["bybit"]
    if not (
        okx["bid_price"] is not None
        and okx["ask_price"] is not None
        and bybit["bid_price"] is not None
        and bybit["ask_price"] is not None
        and okx["ts_exchange"] is not None
        and bybit["ts_exchange"] is not None
        and okx["local_recv_ts_ms"] is not None
        and bybit["local_recv_ts_ms"] is not None
    ):
        return None

    calc_local_ts_ms = time.time() * 1000
    event_local_ts_ms = (
        bybit["local_recv_ts_ms"] if trigger == "bybit" else okx["local_recv_ts_ms"]
    )
    return {
        "event_local_ts_ms": event_local_ts_ms,
        "base_coin": base_coin,
        "trigger": trigger,
        "calc_local_ts_ms": calc_local_ts_ms,
        "okx_local_recv_ts_ms": okx["local_recv_ts_ms"],
        "okx_ts_ms": float(okx["ts_exchange"]),
        "bybit_local_recv_ts_ms": bybit["local_recv_ts_ms"],
        "bybit_ts_ms": float(bybit["ts_exchange"]),
        "okx_bid_price": okx["bid_price"],
        "okx_bid_size": okx["bid_size"],
        "okx_ask_price": okx["ask_price"],
        "okx_ask_size": okx["ask_size"],
        "bybit_bid_price": bybit["bid_price"],
        "bybit_bid_size": bybit["bid_size"],
        "bybit_ask_price": bybit["ask_price"],
        "bybit_ask_size": bybit["ask_size"],
    }


def store_lean_tick(base_coin: str, trigger: str) -> None:
    record = build_lean_tick_record(base_coin, trigger)
    if record is None:
        return
    tick_buffer.append(record)
    if len(tick_buffer) >= PERSIST_EVERY_N:
        persist_ticks()


def store_closed_bar(
    *,
    base_coin: str,
    ref_exchange: str,
    bar_start_ts_ms: int,
    volume: float,
) -> None:
    key = (base_coin, ref_exchange, int(bar_start_ts_ms))
    if key in seen_bar_keys:
        return
    seen_bar_keys.add(key)
    bar_buffer.append(
        {
            "bar_start_ts_ms": int(bar_start_ts_ms),
            "bar_end_ts_ms": int(bar_start_ts_ms) + BAR_INTERVAL_MS,
            "base_coin": base_coin,
            "ref_exchange": ref_exchange,
            "volume": float(volume),
        }
    )
    if len(bar_buffer) >= BAR_PERSIST_EVERY_N:
        persist_bars()


# --- websocket listeners --------------------------------------------------------


async def bybit_orderbook_listener(base_coin: str, bybit_symbol: str) -> None:
    while True:
        try:
            async with websockets.connect(
                BYBIT_LINEAR_WS,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=2,
            ) as ws:
                await ws.send(
                    json.dumps(
                        {"op": "subscribe", "args": [f"orderbook.1.{bybit_symbol}"]}
                    )
                )
                runtime_logger.info(
                    "%s | Bybit subscribed orderbook.1.%s", base_coin, bybit_symbol
                )
                async for message in ws:
                    local_recv_ts_ms = time.time() * 1000
                    data = json.loads(message)
                    payload = data.get("data")
                    if not isinstance(payload, dict):
                        continue
                    bids = payload.get("b", [])
                    asks = payload.get("a", [])
                    if bids and len(bids[0]) >= 2:
                        quotes[base_coin]["bybit"]["bid_price"] = float(bids[0][0])
                        quotes[base_coin]["bybit"]["bid_size"] = float(bids[0][1])
                    if asks and len(asks[0]) >= 2:
                        quotes[base_coin]["bybit"]["ask_price"] = float(asks[0][0])
                        quotes[base_coin]["bybit"]["ask_size"] = float(asks[0][1])
                    exchange_ts_ms = data.get("ts")
                    quotes[base_coin]["bybit"]["ts_exchange"] = (
                        float(exchange_ts_ms) if exchange_ts_ms is not None else None
                    )
                    quotes[base_coin]["bybit"]["local_recv_ts_ms"] = local_recv_ts_ms
                    store_lean_tick(base_coin, "bybit")
        except asyncio.CancelledError:
            runtime_logger.info("%s | Bybit orderbook cancelled", base_coin)
            raise
        except Exception as exc:
            runtime_logger.error("%s | Bybit orderbook error: %s", base_coin, exc)
            await asyncio.sleep(10)


async def okx_books_listener(base_coin: str, okx_symbol: str) -> None:
    while True:
        try:
            async with websockets.connect(
                OKX_BOOKS_WS,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=2,
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "op": "subscribe",
                            "args": [{"channel": "books5", "instId": okx_symbol}],
                        }
                    )
                )
                runtime_logger.info("%s | OKX subscribed books5 %s", base_coin, okx_symbol)
                async for message in ws:
                    local_recv_ts_ms = time.time() * 1000
                    data = json.loads(message)
                    rows = data.get("data")
                    if not rows:
                        continue
                    payload = rows[0]
                    bids = payload.get("bids", [])
                    asks = payload.get("asks", [])
                    if bids and len(bids[0]) >= 2:
                        quotes[base_coin]["okx"]["bid_price"] = float(bids[0][0])
                        quotes[base_coin]["okx"]["bid_size"] = float(bids[0][1])
                    if asks and len(asks[0]) >= 2:
                        quotes[base_coin]["okx"]["ask_price"] = float(asks[0][0])
                        quotes[base_coin]["okx"]["ask_size"] = float(asks[0][1])
                    exchange_ts_ms = payload.get("ts")
                    quotes[base_coin]["okx"]["ts_exchange"] = (
                        float(exchange_ts_ms) if exchange_ts_ms is not None else None
                    )
                    quotes[base_coin]["okx"]["local_recv_ts_ms"] = local_recv_ts_ms
                    store_lean_tick(base_coin, "okx")
        except asyncio.CancelledError:
            runtime_logger.info("%s | OKX books cancelled", base_coin)
            raise
        except Exception as exc:
            runtime_logger.error("%s | OKX books error: %s", base_coin, exc)
            await asyncio.sleep(10)


async def okx_candle5m_listener(base_coin: str, okx_symbol: str) -> None:
    """Closed 5m bars from OKX business WS; volume = volCcy (base coin)."""
    while True:
        try:
            async with websockets.connect(
                OKX_CANDLE_WS,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=2,
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "op": "subscribe",
                            "args": [{"channel": "candle5m", "instId": okx_symbol}],
                        }
                    )
                )
                runtime_logger.info(
                    "%s | OKX subscribed candle5m %s (business WS)",
                    base_coin,
                    okx_symbol,
                )
                async for message in ws:
                    data = json.loads(message)
                    if data.get("event"):
                        continue
                    rows = data.get("data") or []
                    for row in rows:
                        if not isinstance(row, list) or len(row) <= OKX_CONFIRM_INDEX:
                            continue
                        if str(row[OKX_CONFIRM_INDEX]) != "1":
                            continue
                        bar_start = int(float(row[0]))
                        volume = float(row[OKX_VOLUME_FIELD_INDEX])
                        store_closed_bar(
                            base_coin=base_coin,
                            ref_exchange="okx",
                            bar_start_ts_ms=bar_start,
                            volume=volume,
                        )
        except asyncio.CancelledError:
            runtime_logger.info("%s | OKX candle5m cancelled", base_coin)
            raise
        except Exception as exc:
            runtime_logger.error("%s | OKX candle5m error: %s", base_coin, exc)
            await asyncio.sleep(10)


async def bybit_kline5m_listener(base_coin: str, bybit_symbol: str) -> None:
    """Optional closed 5m bars; volume = base coin for linear USDT."""
    while True:
        try:
            async with websockets.connect(
                BYBIT_LINEAR_WS,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=2,
            ) as ws:
                topic = f"kline.5.{bybit_symbol}"
                await ws.send(json.dumps({"op": "subscribe", "args": [topic]}))
                runtime_logger.info("%s | Bybit subscribed %s", base_coin, topic)
                async for message in ws:
                    data = json.loads(message)
                    rows = data.get("data") or []
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        if not row.get("confirm"):
                            continue
                        store_closed_bar(
                            base_coin=base_coin,
                            ref_exchange="bybit",
                            bar_start_ts_ms=int(row["start"]),
                            volume=float(row["volume"]),
                        )
        except asyncio.CancelledError:
            runtime_logger.info("%s | Bybit kline cancelled", base_coin)
            raise
        except Exception as exc:
            runtime_logger.error("%s | Bybit kline error: %s", base_coin, exc)
            await asyncio.sleep(10)


async def heartbeat() -> None:
    while True:
        runtime_logger.info(
            "heartbeat | pairs=%s | tick_buf=%s | bar_buf=%s | "
            "tick_rows=%s | bar_rows=%s | ticks_root=%s | bars_root=%s",
            len(pairs),
            len(tick_buffer),
            len(bar_buffer),
            saved_tick_rows,
            saved_bar_rows,
            TICK_ROOT,
            BARS_ROOT,
        )
        await asyncio.sleep(30)


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


async def run_collector() -> None:
    TICK_ROOT.mkdir(parents=True, exist_ok=True)
    if COLLECT_BARS:
        BARS_ROOT.mkdir(parents=True, exist_ok=True)

    runtime_logger.info(
        "lean_start | pairs=%s | persist_every=%s | collect_bars=%s | "
        "collect_bybit_bars=%s | tick_root=%s | bars_root=%s",
        len(pairs),
        PERSIST_EVERY_N,
        COLLECT_BARS,
        COLLECT_BYBIT_BARS,
        TICK_ROOT,
        BARS_ROOT,
    )
    for row in pairs:
        runtime_logger.info(
            "PAIR | base_coin=%s | okx=%s | bybit=%s",
            row["base_coin"],
            row["okx_symbol"],
            row["bybit_symbol"],
        )

    tasks: list[asyncio.Task] = [asyncio.create_task(heartbeat(), name="heartbeat")]
    loop = asyncio.get_running_loop()

    def request_shutdown() -> None:
        runtime_logger.info("shutdown signal received")
        for task in tasks:
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except NotImplementedError:
            signal.signal(sig, lambda *_: request_shutdown())

    try:
        pair_batches = list(chunked(pairs, SUBSCRIBE_BATCH_SIZE))
        for batch_idx, pair_batch in enumerate(pair_batches, start=1):
            for row in pair_batch:
                base_coin = row["base_coin"]
                tasks.append(
                    asyncio.create_task(
                        okx_books_listener(base_coin, row["okx_symbol"]),
                        name=f"okx-books:{base_coin}",
                    )
                )
                tasks.append(
                    asyncio.create_task(
                        bybit_orderbook_listener(base_coin, row["bybit_symbol"]),
                        name=f"bybit-ob:{base_coin}",
                    )
                )
                if COLLECT_BARS:
                    tasks.append(
                        asyncio.create_task(
                            okx_candle5m_listener(base_coin, row["okx_symbol"]),
                            name=f"okx-candle:{base_coin}",
                        )
                    )
                if COLLECT_BARS and COLLECT_BYBIT_BARS:
                    tasks.append(
                        asyncio.create_task(
                            bybit_kline5m_listener(base_coin, row["bybit_symbol"]),
                            name=f"bybit-kline:{base_coin}",
                        )
                    )
            if batch_idx < len(pair_batches):
                await asyncio.sleep(SUBSCRIBE_BATCH_PAUSE_SEC)

        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        runtime_logger.info("main | cancelled, draining buffers")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        persist_ticks()
        persist_bars()
        runtime_logger.info(
            "lean_exit | tick_rows=%s | bar_rows=%s",
            saved_tick_rows,
            saved_bar_rows,
        )


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Local lean OKX/Bybit spread collector")
    p.add_argument(
        "--universe",
        type=Path,
        default=Path(os.environ.get("SPREAD_LEAN_UNIVERSE", str(DEFAULT_UNIVERSE))),
    )
    p.add_argument(
        "--row-start",
        type=int,
        default=int(os.environ.get("SPREAD_LEAN_ROW_START", "0")),
        help="slice start over take=yes coins",
    )
    p.add_argument(
        "--row-end",
        type=int,
        default=int(os.environ.get("SPREAD_LEAN_ROW_END", "337")),
        help="slice end over take=yes coins",
    )
    p.add_argument(
        "--persist-every",
        type=int,
        default=int(os.environ.get("SPREAD_LEAN_PERSIST_EVERY", "5000")),
    )
    p.add_argument(
        "--no-bars",
        action="store_true",
        help="Disable 5m candle volume collection",
    )
    p.add_argument(
        "--bybit-bars",
        action="store_true",
        help="Also collect Bybit kline.5 (default is OKX-only bars)",
    )
    p.add_argument(
        "--tick-root",
        type=Path,
        default=None,
        help="Override SPREAD_LEAN_PARQUET_ROOT / output/lean_ticks",
    )
    p.add_argument(
        "--bars-root",
        type=Path,
        default=None,
        help="Override SPREAD_LEAN_BARS_ROOT / output/lean_bars",
    )
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    global pairs, quotes, PERSIST_EVERY_N, COLLECT_BARS, COLLECT_BYBIT_BARS
    global TICK_ROOT, BARS_ROOT

    args = parse_args(argv)
    if args.tick_root is not None:
        TICK_ROOT = args.tick_root
    if args.bars_root is not None:
        BARS_ROOT = args.bars_root
    PERSIST_EVERY_N = max(1, args.persist_every)
    COLLECT_BARS = not args.no_bars
    COLLECT_BYBIT_BARS = bool(args.bybit_bars)

    _setup_logging(RUNTIME_LOG_PATH)

    if not args.universe.exists():
        raise FileNotFoundError(f"Universe file not found: {args.universe}")

    # Safety: never write into production live root by accident.
    forbidden = {"/data/live", "/data/spool", "/mnt/storage"}
    for root in (TICK_ROOT.resolve(), BARS_ROOT.resolve()):
        as_str = str(root)
        for bad in forbidden:
            if as_str == bad or as_str.startswith(bad + os.sep):
                raise RuntimeError(
                    f"Refusing to write lean collector into production path: {root}"
                )

    pairs = load_pairs_from_csv(args.universe, args.row_start, args.row_end)
    quotes = _init_quotes(pairs)
    if not pairs:
        raise RuntimeError("No pairs loaded from universe CSV")

    try:
        import uvloop

        uvloop.install()
    except Exception:
        pass

    try:
        asyncio.run(run_collector())
    except KeyboardInterrupt:
        runtime_logger.info("KeyboardInterrupt — stopped")


if __name__ == "__main__":
    main()
