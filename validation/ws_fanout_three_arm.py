#!/usr/bin/env python3
"""Isolated A/B/C/C+ WebSocket fan-out latency probe.

Arms A/B/C never persist market data. Arm C+ additionally batch-writes
processed records as local pickle files under an isolated experiment
directory. It does not touch production /data/live or spool.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import pickle
import queue
import signal
import threading
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import websockets

FULL_UNIVERSE_ARMS = {"B", "C", "C+"}
WRITE_ARMS = {"C+"}
_WRITE_SENTINEL = object()

OKX_URL = "wss://ws.okx.com:8443/ws/v5/public"
BYBIT_URL = "wss://stream.bybit.com/v5/public/linear"
XRP = "XRP"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * q)
    return ordered[index]


def stats(values: list[float]) -> dict[str, float | int | None]:
    return {
        "n": len(values),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": max(values) if values else None,
        "negative_n": sum(value < 0 for value in values),
    }


def read_proc_status() -> dict[str, int | str | None]:
    result: dict[str, int | str | None] = {
        "fd_count": None,
        "fd_limit": None,
        "rss_bytes": None,
        "threads": None,
        "cpu_user_sec": None,
        "cpu_system_sec": None,
        "host_load_1": None,
        "host_rx_bytes": None,
        "mem_available_bytes": None,
    }
    try:
        result["fd_count"] = len(os.listdir("/proc/self/fd"))
        limits = Path("/proc/self/limits").read_text()
        for line in limits.splitlines():
            if line.startswith("Max open files"):
                result["fd_limit"] = int(line.split()[3])
                break
        for line in Path("/proc/self/status").read_text().splitlines():
            key, value = line.split(":", 1)
            if key == "VmRSS":
                result["rss_bytes"] = int(value.split()[0]) * 1024
            elif key == "Threads":
                result["threads"] = int(value.strip())
        ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        proc = Path("/proc/self/stat").read_text().split()
        result["cpu_user_sec"] = int(proc[13]) / ticks
        result["cpu_system_sec"] = int(proc[14]) / ticks
        result["host_load_1"] = os.getloadavg()[0]
        rx_bytes = 0
        for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
            rx_bytes += int(line.split(":", 1)[1].split()[0])
        result["host_rx_bytes"] = rx_bytes
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                result["mem_available_bytes"] = int(line.split()[1]) * 1024
                break
    except (FileNotFoundError, IndexError, OSError, ValueError):
        pass
    return result


def load_or_create_manifest(source: Path, output: Path, count: int) -> list[dict[str, str]]:
    if output.exists():
        manifest = json.loads(output.read_text())
        pairs = manifest["pairs"]
    else:
        with source.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        pairs = [
            {"base_coin": row["base_coin"], "okx_symbol": row["okx_symbol"], "bybit_symbol": row["bybit_symbol"]}
            for row in rows
            if row.get("base_coin") and row.get("okx_symbol") and row.get("bybit_symbol")
        ]
        xrp = [row for row in pairs if row["base_coin"] == XRP]
        if len(xrp) != 1:
            raise ValueError(f"expected exactly one XRP row, got {len(xrp)}")
        others = [row for row in pairs if row["base_coin"] != XRP]
        if len(others) < count - 1:
            raise ValueError(f"universe has only {len(others) + 1} usable pairs, need {count}")
        # CSV order is the production subscription order. XRP is first so both
        # exchanges get the measured pair before connection fan-out ramps up.
        pairs = xrp + others[: count - 1]
        payload = {"version": 1, "pair_count": count, "pairs": pairs}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        payload["sha256"] = hashlib.sha256(canonical).hexdigest()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2) + "\n")
    if len(pairs) != count or sum(row["base_coin"] == XRP for row in pairs) != 1:
        raise ValueError("manifest does not have requested count and exactly one XRP")
    return pairs


def normalize_arm(value: str) -> str:
    aliases = {
        "A": "A",
        "B": "B",
        "C": "C",
        "C+": "C+",
        "c+": "C+",
        "Cplus": "C+",
        "cplus": "C+",
        "CPLUS": "C+",
    }
    key = value.strip()
    if key not in aliases:
        raise argparse.ArgumentTypeError(f"unknown arm {value!r}; expected A, B, C, or C+")
    return aliases[key]


class LocalPickleWriter:
    """Batch pickle writer on a dedicated thread; never blocks the drain task."""

    def __init__(
        self,
        root: Path,
        *,
        batch_size: int,
        interval_sec: float,
        queue_max: int,
        start_monotonic: float,
    ) -> None:
        self.root = root
        self.batch_size = batch_size
        self.interval_sec = interval_sec
        self.start_monotonic = start_monotonic
        self.queue: queue.Queue[Any] = queue.Queue(maxsize=queue_max)
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self.metrics: dict[str, Any] = {
            "format": "pickle",
            "batch_size": batch_size,
            "interval_sec": interval_sec,
            "queue_max": queue_max,
            "enqueued": 0,
            "written_records": 0,
            "dropped": 0,
            "backpressure_hits": 0,
            "bytes": 0,
            "files": 0,
            "queue_depth": 0,
            "queue_depth_max": 0,
            "last_write_latency_ms": None,
            "write_latency_p50_ms": None,
            "write_latency_p99_ms": None,
            "write_latency_max_ms": None,
            "write_errors": 0,
            "fsync": False,
        }
        self._latencies: list[float] = []
        self.flush_path = root / "write_flush.csv"
        self.thread = threading.Thread(target=self._run, name="cplus-pickle-writer", daemon=True)

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.flush_path.open("w", newline="", encoding="utf-8") as handle:
            handle.write("ts_utc,elapsed_sec,seq,records,bytes,write_latency_ms,queue_depth,path\n")
        self.thread.start()

    def offer(self, record: tuple[Any, ...]) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            with self._lock:
                self.metrics["dropped"] += 1
                self.metrics["backpressure_hits"] += 1
                self.metrics["queue_depth"] = self.queue.qsize()
            return
        depth = self.queue.qsize()
        with self._lock:
            self.metrics["enqueued"] += 1
            self.metrics["queue_depth"] = depth
            if depth > self.metrics["queue_depth_max"]:
                self.metrics["queue_depth_max"] = depth

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.metrics)

    def close(self) -> None:
        if self.stop.is_set():
            if self.thread.is_alive():
                self.thread.join(timeout=15)
            return
        self.stop.set()
        try:
            self.queue.put_nowait(_WRITE_SENTINEL)
        except queue.Full:
            pass
        self.thread.join(timeout=15)

    def _run(self) -> None:
        batch: list[tuple[Any, ...]] = []
        last_flush = time.monotonic()
        seq = 0
        while True:
            timeout = max(0.01, self.interval_sec - (time.monotonic() - last_flush))
            item: Any = _WRITE_SENTINEL
            try:
                item = self.queue.get(timeout=timeout)
            except queue.Empty:
                item = None
            if item is _WRITE_SENTINEL or (item is None and self.stop.is_set() and self.queue.empty()):
                while True:
                    try:
                        leftover = self.queue.get_nowait()
                    except queue.Empty:
                        break
                    if leftover is not _WRITE_SENTINEL:
                        batch.append(leftover)
                if batch:
                    seq += 1
                    self._flush(batch, seq)
                break
            if item is not None:
                batch.append(item)
            now = time.monotonic()
            if batch and (len(batch) >= self.batch_size or now - last_flush >= self.interval_sec):
                seq += 1
                self._flush(batch, seq)
                batch = []
                last_flush = now

    def _flush(self, batch: list[tuple[Any, ...]], seq: int) -> None:
        path = self.root / f"part-{seq:06d}.pkl"
        tmp = path.with_suffix(".pkl.tmp")
        started = time.perf_counter()
        try:
            payload = pickle.dumps(batch, protocol=pickle.HIGHEST_PROTOCOL)
            tmp.write_bytes(payload)
            tmp.replace(path)
            latency_ms = (time.perf_counter() - started) * 1000
            depth = self.queue.qsize()
            with self._lock:
                self.metrics["written_records"] += len(batch)
                self.metrics["bytes"] += len(payload)
                self.metrics["files"] += 1
                self.metrics["last_write_latency_ms"] = round(latency_ms, 3)
                self.metrics["queue_depth"] = depth
                self._latencies.append(latency_ms)
                self.metrics["write_latency_p50_ms"] = percentile(self._latencies, 0.50)
                self.metrics["write_latency_p99_ms"] = percentile(self._latencies, 0.99)
                self.metrics["write_latency_max_ms"] = max(self._latencies)
            with self.flush_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"{utc_now()},{time.monotonic() - self.start_monotonic:.6f},{seq},"
                    f"{len(batch)},{len(payload)},{latency_ms:.3f},{depth},{path.name}\n"
                )
        except Exception:
            with self._lock:
                self.metrics["write_errors"] += 1
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


class Probe:
    def __init__(self, args: argparse.Namespace, pairs: list[dict[str, str]]) -> None:
        self.args = args
        self.pairs = pairs if args.arm in FULL_UNIVERSE_ARMS else [row for row in pairs if row["base_coin"] == XRP]
        self.log_path: Path = args.log_file
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.stop_event = asyncio.Event()
        self.start_monotonic = time.monotonic()
        self.start_wall_ms = time.time_ns() // 1_000_000
        self.expected = len(self.pairs) * 2
        self.active: set[tuple[str, str]] = set()
        self.pending_recoveries: set[tuple[str, str]] = set()
        self.counters: dict[str, Counter[str]] = defaultdict(Counter)
        self.reconnect_events: dict[str, deque[float]] = defaultdict(deque)
        self.minute_latency: dict[str, list[float]] = defaultdict(list)
        self.minute_lag: list[float] = []
        self.minute_trigger_max: dict[str, float] = {}
        self.quotes: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)
        self.tasks: list[asyncio.Task[Any]] = []
        self.start_proc = read_proc_status()
        self.last_cpu_total_sec = self._cpu_total_sec(self.start_proc)
        self.last_cpu_sample_monotonic = self.start_monotonic
        self.last_cpu_percent: float | None = None
        self.latency_samples: dict[str, Any] = {}
        self.loop_lag_samples: Any | None = None
        self.write_metrics_samples: Any | None = None
        self.writer: LocalPickleWriter | None = None
        if args.arm in WRITE_ARMS:
            write_dir = args.write_dir
            if write_dir is None:
                raise ValueError("arm C+ requires --write-dir")
            self.writer = LocalPickleWriter(
                write_dir,
                batch_size=args.write_batch_size,
                interval_sec=args.write_interval_sec,
                queue_max=args.write_queue_max,
                start_monotonic=self.start_monotonic,
            )
            self.writer.start()
        self._open_sample_artifacts()

    @staticmethod
    def _cpu_total_sec(proc: dict[str, int | str | None]) -> float | None:
        user, system = proc.get("cpu_user_sec"), proc.get("cpu_system_sec")
        if isinstance(user, (int, float)) and isinstance(system, (int, float)):
            return float(user) + float(system)
        return None

    def _open_sample_artifacts(self) -> None:
        for exchange, path in (
            ("okx", self.args.xrp_okx_samples),
            ("bybit", self.args.xrp_bybit_samples),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("w", newline="", encoding="utf-8")
            writer = csv.DictWriter(
                handle,
                fieldnames=("ts_utc", "elapsed_sec", "exchange", "delivery_latency_ms"),
            )
            writer.writeheader()
            self.latency_samples[exchange] = (handle, writer)
        self.args.loop_lag_samples.parent.mkdir(parents=True, exist_ok=True)
        self.loop_lag_samples = self.args.loop_lag_samples.open("w", newline="", encoding="utf-8")
        self.loop_lag_samples.write("ts_utc,elapsed_sec,lag_ms\n")
        if self.writer is not None:
            write_metrics = self.args.write_metrics_samples
            write_metrics.parent.mkdir(parents=True, exist_ok=True)
            self.write_metrics_samples = write_metrics.open("w", newline="", encoding="utf-8")
            self.write_metrics_samples.write(
                "ts_utc,elapsed_sec,enqueued,written_records,dropped,backpressure_hits,"
                "bytes,files,queue_depth,queue_depth_max,last_write_latency_ms,write_errors\n"
            )

    def _close_sample_artifacts(self) -> None:
        for handle, _writer in self.latency_samples.values():
            handle.flush()
            handle.close()
        if self.loop_lag_samples is not None:
            self.loop_lag_samples.flush()
            self.loop_lag_samples.close()
        if self.writer is not None:
            self.writer.close()
        if self.write_metrics_samples is not None:
            self.write_metrics_samples.flush()
            self.write_metrics_samples.close()

    def emit(self, event: str, **fields: Any) -> None:
        row = {
            "ts_utc": utc_now(),
            "event": event,
            "run_id": self.args.run_id,
            "arm": self.args.arm,
            **fields,
        }
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")

    def record_reconnect_wave(self, exchange: str, now: float) -> dict[str, int]:
        events = self.reconnect_events[exchange]
        events.append(now)
        while events and now - events[0] > 60:
            events.popleft()
        ten_sec = sum(now - timestamp <= 10 for timestamp in events)
        return {"reconnects_10s": ten_sec, "reconnects_60s": len(events)}

    @staticmethod
    def close_fields(ws: Any | None, exc: BaseException | None, connection_age_sec: float | None) -> dict[str, Any]:
        close_code = getattr(ws, "close_code", None) if ws is not None else None
        close_reason = getattr(ws, "close_reason", None) if ws is not None else None
        clean = close_code in {1000, 1001} or exc.__class__.__name__ == "ConnectionClosedOK" if exc else close_code in {1000, 1001}
        return {
            "exception_class": type(exc).__name__ if exc is not None else None,
            "exception_str": str(exc) if exc is not None else None,
            "exception_repr": repr(exc) if exc is not None else None,
            "ws_close_code": close_code,
            "ws_close_reason": close_reason,
            "close_classification": "clean" if clean else "abrupt",
            "connection_age_sec": round(connection_age_sec, 6) if connection_age_sec is not None else None,
        }

    def count_frame(self, exchange: str, base_coin: str, payload: str | bytes) -> None:
        counter = self.counters[exchange]
        counter["frames_received"] += 1
        counter["bytes_received"] += len(payload)
        counter["xrp_frames" if base_coin == XRP else "non_xrp_frames"] += 1

    def record_latency(self, exchange: str, latency_ms: float) -> None:
        self.minute_latency[exchange].append(latency_ms)
        handle, writer = self.latency_samples[exchange]
        writer.writerow(
            {
                "ts_utc": utc_now(),
                "elapsed_sec": round(time.monotonic() - self.start_monotonic, 6),
                "exchange": exchange,
                "delivery_latency_ms": latency_ms,
            }
        )
        self.counters[exchange]["xrp_delivery_samples_raw"] += 1
        if self.counters[exchange]["xrp_delivery_samples_raw"] % 100 == 0:
            handle.flush()
        current = self.minute_trigger_max.get(exchange)
        self.minute_trigger_max[exchange] = latency_ms if current is None else max(current, latency_ms)

    def full_handle(self, exchange: str, pair: dict[str, str], message: str | bytes) -> tuple[Any, ...] | None:
        data = json.loads(message)
        recv_ms = time.time_ns() // 1_000_000
        if exchange == "okx":
            payloads = data.get("data")
            if not payloads:
                return
            payload = payloads[0]
            bids, asks, exchange_ts = payload.get("bids", []), payload.get("asks", []), payload.get("ts")
        else:
            payload = data.get("data")
            if not isinstance(payload, dict):
                return
            bids, asks, exchange_ts = payload.get("b", []), payload.get("a", []), data.get("ts")
        if not bids or not asks or exchange_ts is None:
            return
        quote = {
            "bid_price": float(bids[0][0]),
            "bid_size": float(bids[0][1]),
            "ask_price": float(asks[0][0]),
            "ask_size": float(asks[0][1]),
            "ts_exchange": float(exchange_ts),
            "local_recv_ts_ms": float(recv_ms),
        }
        quote["delivery_latency_ms"] = quote["local_recv_ts_ms"] - quote["ts_exchange"]
        base_coin = pair["base_coin"]
        self.quotes[base_coin][exchange] = quote
        self.counters[exchange]["full_quote_updates"] += 1
        if base_coin == XRP:
            self.record_latency(exchange, float(quote["delivery_latency_ms"]))
        spread_long = None
        spread_short = None
        other = "bybit" if exchange == "okx" else "okx"
        if other in self.quotes[base_coin]:
            opposite = self.quotes[base_coin][other]
            spread_long = (opposite["bid_price"] - quote["ask_price"]) * 100 / opposite["bid_price"]
            spread_short = (quote["bid_price"] - opposite["ask_price"]) * 100 / quote["bid_price"]
            self.counters[exchange]["spread_calculations"] += 1
            if base_coin == XRP:
                self.counters[exchange]["xrp_spread_calculations"] += 1
                self.counters[exchange]["last_xrp_spread_long_bp"] = round(spread_long * 100, 4)
                self.counters[exchange]["last_xrp_spread_short_bp"] = round(spread_short * 100, 4)
        return (
            exchange,
            base_coin,
            quote["ts_exchange"],
            quote["local_recv_ts_ms"],
            quote["bid_price"],
            quote["ask_price"],
            quote["bid_size"],
            quote["ask_size"],
            spread_long,
            spread_short,
        )

    def discard_non_xrp(self, exchange: str, message: str | bytes) -> None:
        # One market-data topic is subscribed per socket. websockets handles
        # WS ping/pong/close below this callback, so raw data can be released.
        text = message.decode(errors="replace") if isinstance(message, bytes) else message
        counter = self.counters[exchange]
        if '"event"' in text[:256] or '"success"' in text[:256] or '"op"' in text[:256]:
            counter["control_frames"] += 1
        else:
            counter["discarded_data_frames"] += 1

    async def listener(self, exchange: str, pair: dict[str, str], delay_sec: float, batch_index: int) -> None:
        await asyncio.sleep(delay_sec)
        base_coin = pair["base_coin"]
        conn_key = (exchange, base_coin)
        url = OKX_URL if exchange == "okx" else BYBIT_URL
        attempts = 0
        while not self.stop_event.is_set():
            ws = None
            connected_monotonic: float | None = None
            close_exc: BaseException | None = None
            attempts += 1
            counter = self.counters[exchange]
            counter["connection_attempts"] += 1
            if attempts > 1:
                counter["unplanned_reconnects"] += 1
                wave = self.record_reconnect_wave(exchange, time.monotonic())
                self.emit(
                    "unplanned_reconnect",
                    exchange=exchange,
                    base_coin=base_coin,
                    attempt=attempts,
                    retry_delay_sec=self.args.retry_delay_sec,
                    subscribe_batch=batch_index,
                    **wave,
                )
            try:
                connect_started = time.monotonic()
                connect_kwargs: dict[str, Any] = {
                    "ping_interval": 20,
                    "ping_timeout": 20,
                    "close_timeout": 2,
                }
                if self.args.max_queue is not None:
                    connect_kwargs["max_queue"] = self.args.max_queue
                async with websockets.connect(url, **connect_kwargs) as ws:
                    connected_monotonic = time.monotonic()
                    counter["socket_sequence"] += 1
                    socket_sequence = counter["socket_sequence"]
                    self.emit(
                        "connection_opened",
                        exchange=exchange,
                        base_coin=base_coin,
                        attempt=attempts,
                        socket_sequence=socket_sequence,
                        subscribe_batch=batch_index,
                        connect_elapsed_ms=round((connected_monotonic - connect_started) * 1000, 3),
                        monotonic_elapsed_sec=round(connected_monotonic - self.start_monotonic, 6),
                    )
                    if exchange == "okx":
                        subscription = {"op": "subscribe", "args": [{"channel": "books5", "instId": pair["okx_symbol"]}]}
                    else:
                        subscription = {"op": "subscribe", "args": [f"orderbook.1.{pair['bybit_symbol']}"]}
                    await ws.send(json.dumps(subscription))
                    self.active.add(conn_key)
                    self.pending_recoveries.discard(conn_key)
                    counter["connections_opened"] += 1
                    counter["subscription_sends"] += 1
                    self.emit(
                        "subscription_sent",
                        exchange=exchange,
                        base_coin=base_coin,
                        attempt=attempts,
                        socket_sequence=socket_sequence,
                        subscribe_batch=batch_index,
                        active_connections=len(self.active),
                        monotonic_elapsed_sec=round(time.monotonic() - self.start_monotonic, 6),
                    )
                    # Drain the library receive queue immediately so WS ping/pong
                    # keep being read even while this arm parses. C's inline
                    # json/parse/calc previously let max_queue fill, which pauses
                    # socket reads and trips keepalive ping timeout (1011/1006).
                    incoming: asyncio.Queue[Any] = asyncio.Queue()

                    async def drain_socket() -> None:
                        try:
                            async for incoming_message in ws:
                                incoming.put_nowait(incoming_message)
                                pending = incoming.qsize()
                                if pending > counter["recv_pending_max"]:
                                    counter["recv_pending_max"] = pending
                        except asyncio.CancelledError:
                            raise
                        except Exception as drain_exc:
                            incoming.put_nowait(drain_exc)
                        else:
                            incoming.put_nowait(None)

                    drain_task = asyncio.create_task(
                        drain_socket(),
                        name=f"drain:{exchange}:{base_coin}",
                    )
                    try:
                        while not self.stop_event.is_set():
                            item = await incoming.get()
                            if item is None:
                                break
                            if isinstance(item, BaseException):
                                raise item
                            message = item
                            self.count_frame(exchange, base_coin, message)
                            if self.args.arm == "B" and base_coin != XRP:
                                self.discard_non_xrp(exchange, message)
                                continue
                            try:
                                self.counters[exchange]["json_loads"] += 1
                                record = self.full_handle(exchange, pair, message)
                            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                                self.counters[exchange]["protocol_errors"] += 1
                            else:
                                if self.writer is not None and record is not None:
                                    self.writer.offer(record)
                    finally:
                        if not drain_task.done():
                            drain_task.cancel()
                        await asyncio.gather(drain_task, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                close_exc = exc
                counter["connection_errors"] += 1
                self.emit(
                    "connection_error",
                    exchange=exchange,
                    base_coin=base_coin,
                    attempt=attempts,
                    subscribe_batch=batch_index,
                    **self.close_fields(ws, exc, time.monotonic() - connected_monotonic if connected_monotonic else None),
                )
                if not self.stop_event.is_set():
                    self.emit(
                        "reconnect_scheduled",
                        exchange=exchange,
                        base_coin=base_coin,
                        attempt=attempts,
                        retry_delay_sec=self.args.retry_delay_sec,
                        subscribe_batch=batch_index,
                    )
                    await asyncio.sleep(self.args.retry_delay_sec)
            finally:
                self.active.discard(conn_key)
                if ws is not None:
                    counter["connections_closed"] += 1
                    if not self.stop_event.is_set():
                        self.pending_recoveries.add(conn_key)
                        counter["unplanned_closes"] += 1
                        fields = self.close_fields(
                            ws, close_exc, time.monotonic() - connected_monotonic if connected_monotonic else None
                        )
                        self.emit(
                            "connection_closed",
                            exchange=exchange,
                            base_coin=base_coin,
                            attempt=attempts,
                            subscribe_batch=batch_index,
                            **fields,
                        )
                        self.emit(
                            "unplanned_close",
                            exchange=exchange,
                            base_coin=base_coin,
                            attempt=attempts,
                            subscribe_batch=batch_index,
                            **fields,
                        )

    async def lag_probe(self) -> None:
        interval = 0.2
        expected = time.monotonic() + interval
        while not self.stop_event.is_set():
            await asyncio.sleep(max(0, expected - time.monotonic()))
            now = time.monotonic()
            lag_ms = max(0.0, (now - expected) * 1000)
            self.minute_lag.append(lag_ms)
            assert self.loop_lag_samples is not None
            self.loop_lag_samples.write(
                f"{utc_now()},{now - self.start_monotonic:.6f},{lag_ms:.6f}\n"
            )
            bucket = self.counters["loop"]
            bucket["lag_samples_raw"] += 1
            if bucket["lag_samples_raw"] % 100 == 0:
                self.loop_lag_samples.flush()
            bucket["lag_gt_200"] += lag_ms > 200
            bucket["lag_gt_500"] += lag_ms > 500
            bucket["lag_gt_1000"] += lag_ms > 1000
            expected += interval
            if expected < now:
                expected = now + interval

    async def metrics_loop(self) -> None:
        last = time.monotonic()
        while not self.stop_event.is_set():
            await asyncio.sleep(1)
            now = time.monotonic()
            interval = now - last
            last = now
            proc = read_proc_status()
            cpu_total_sec = self._cpu_total_sec(proc)
            elapsed = max(now - self.last_cpu_sample_monotonic, 0.001)
            if cpu_total_sec is not None and self.last_cpu_total_sec is not None:
                self.last_cpu_percent = 100 * (cpu_total_sec - self.last_cpu_total_sec) / elapsed
            self.last_cpu_total_sec = cpu_total_sec
            self.last_cpu_sample_monotonic = now
            exchanges = {}
            for exchange in ("okx", "bybit"):
                counter = dict(self.counters[exchange])
                exchanges[exchange] = {
                    **counter,
                    "frames_per_sec": round(counter.get("frames_received", 0) / max(1, now - self.start_monotonic), 3),
                    "bytes_per_sec": round(counter.get("bytes_received", 0) / max(1, now - self.start_monotonic), 3),
                }
            write_snapshot = self.writer.snapshot() if self.writer is not None else None
            if write_snapshot is not None and self.write_metrics_samples is not None:
                self.write_metrics_samples.write(
                    f"{utc_now()},{now - self.start_monotonic:.6f},"
                    f"{write_snapshot['enqueued']},{write_snapshot['written_records']},"
                    f"{write_snapshot['dropped']},{write_snapshot['backpressure_hits']},"
                    f"{write_snapshot['bytes']},{write_snapshot['files']},"
                    f"{write_snapshot['queue_depth']},{write_snapshot['queue_depth_max']},"
                    f"{write_snapshot['last_write_latency_ms']},{write_snapshot['write_errors']}\n"
                )
            self.emit(
                "metrics_1s",
                elapsed_sec=round(now - self.start_monotonic, 3),
                interval_sec=round(interval, 3),
                expected_connections=self.expected,
                active_connections=len(self.active),
                pending_recoveries=len(self.pending_recoveries),
                process=proc,
                cpu_percent=self.last_cpu_percent,
                exchanges=exchanges,
                write=write_snapshot,
            )

    async def safety_loop(self) -> None:
        while not self.stop_event.is_set():
            await asyncio.sleep(5)
            proc = read_proc_status()
            violations = []
            if (rss := proc["rss_bytes"]) is not None and rss > self.args.max_rss_mib * 1024 * 1024:
                violations.append(f"probe_rss_bytes={rss}")
            if (load := proc["host_load_1"]) is not None and load > self.args.max_load_1:
                violations.append(f"host_load_1={load}")
            if (available := proc["mem_available_bytes"]) is not None and available < self.args.min_mem_available_mib * 1024 * 1024:
                violations.append(f"mem_available_bytes={available}")
            if (fds := proc["fd_count"]) is not None and fds > self.args.max_fds:
                violations.append(f"probe_fd_count={fds}")
            if self.last_cpu_percent is not None and self.last_cpu_percent > self.args.max_cpu_percent:
                violations.append(f"probe_cpu_percent={self.last_cpu_percent:.2f}")
            if violations:
                self.emit("safety_abort", violations=violations, process=proc)
                self.stop_event.set()
                return

    async def minute_loop(self) -> None:
        while not self.stop_event.is_set():
            await asyncio.sleep(60)
            dual_500 = all(self.minute_trigger_max.get(leg, -float("inf")) > 500 for leg in ("okx", "bybit"))
            dual_1000 = all(self.minute_trigger_max.get(leg, -float("inf")) > 1000 for leg in ("okx", "bybit"))
            self.emit(
                "metrics_minute",
                xrp_delivery_latency={leg: stats(self.minute_latency[leg]) for leg in ("okx", "bybit")},
                loop_lag_ms=stats(self.minute_lag),
                dual_gt_500=dual_500,
                dual_gt_1000=dual_1000,
                counters={name: dict(counter) for name, counter in self.counters.items()},
                expected_connections=self.expected,
                active_connections=len(self.active),
                write=self.writer.snapshot() if self.writer is not None else None,
            )
            self.minute_latency.clear()
            self.minute_lag.clear()
            self.minute_trigger_max.clear()

    async def run(self) -> int:
        self.emit(
            "start",
            duration_sec=self.args.duration_sec,
            universe_count=len(self.pairs),
            expected_connections=self.expected,
            manifest=str(self.args.manifest),
            manifest_sha256=hashlib.sha256(self.args.manifest.read_bytes()).hexdigest(),
            full_handling=self.args.arm in {"A", "C", "C+"},
            non_xrp_json_loads_expected=0 if self.args.arm == "B" else None,
            sampling={
                "xrp_delivery": "all valid XRP full-handle delivery samples; CSV per exchange",
                "loop_lag": "every 200ms scheduled lag callback; CSV",
                "xrp_okx_samples": str(self.args.xrp_okx_samples),
                "xrp_bybit_samples": str(self.args.xrp_bybit_samples),
                "loop_lag_samples": str(self.args.loop_lag_samples),
            },
            reconnect_failure_gate="measurement_failed if any exchange has >1 unplanned_reconnect or unrecovered connection",
            reconnect_validity_contract={
                "max_unplanned_reconnects_per_exchange": 1,
                "max_unrecovered_connections": 0,
                "max_connection_wave_events_per_exchange_60s": 3,
            },
            connection_parameters={
                "subscription_batch_pairs": self.args.subscription_batch_pairs,
                "subscription_batch_pause_sec": self.args.subscription_batch_pause_sec,
                "retry_delay_sec": self.args.retry_delay_sec,
                "max_queue": self.args.max_queue,
                "max_queue_rationale": "None means websockets library default; explicit value is recorded for this standalone probe.",
                "receive_mode": "immediate_drain_unbounded_app_queue",
                "receive_mode_rationale": (
                    "A dedicated drain task recvs into an unbounded asyncio.Queue so the "
                    "websockets library never pauses TCP reads when max_queue fills. "
                    "Application parse/calc still sees every frame; B and C share this path. "
                    "ping_interval/ping_timeout/close_timeout remain 20/20/2; batch 30/3s; "
                    "retry 10s; max_queue still omitted."
                ),
            },
            resource_abort_budgets={
                "max_rss_mib": self.args.max_rss_mib,
                "max_load_1": self.args.max_load_1,
                "min_mem_available_mib": self.args.min_mem_available_mib,
                "max_fds": self.args.max_fds,
                "max_cpu_percent": self.args.max_cpu_percent,
            },
            persistence=(
                "cplus_local_pickle_batch_no_fsync_isolated_dir"
                if self.writer is not None
                else "disabled_no_parquet_no_spool_no_publisher"
            ),
            write_policy=(
                {
                    "format": "pickle",
                    "why": (
                        "pickle of compact tuples avoids pandas/pyarrow on the tick path; "
                        "encode+write run on a dedicated thread; no fsync per row"
                    ),
                    "batch_size": self.args.write_batch_size,
                    "interval_sec": self.args.write_interval_sec,
                    "queue_max": self.args.write_queue_max,
                    "fsync": False,
                    "thread": "cplus-pickle-writer",
                    "dir": str(self.args.write_dir),
                }
                if self.writer is not None
                else None
            ),
            process_start=self.start_proc,
        )
        self.tasks = [
            asyncio.create_task(self.lag_probe(), name="loop-lag"),
            asyncio.create_task(self.metrics_loop(), name="metrics-1s"),
            asyncio.create_task(self.minute_loop(), name="metrics-minute"),
            asyncio.create_task(self.safety_loop(), name="resource-safety"),
        ]
        for index, pair in enumerate(self.pairs):
            batch_index = index // self.args.subscription_batch_pairs
            delay = batch_index * self.args.subscription_batch_pause_sec
            self.tasks.append(asyncio.create_task(self.listener("okx", pair, delay, batch_index), name=f"okx:{pair['base_coin']}"))
            self.tasks.append(asyncio.create_task(self.listener("bybit", pair, delay, batch_index), name=f"bybit:{pair['base_coin']}"))
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=self.args.duration_sec)
        except asyncio.TimeoutError:
            self.emit("stop_requested", reason="duration_elapsed")
        self.stop_event.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.writer is not None:
            self.writer.close()
        self.emit(
            "finished",
            expected_connections=self.expected,
            active_connections=len(self.active),
            pending_recoveries=len(self.pending_recoveries),
            counters={name: dict(counter) for name, counter in self.counters.items()},
            write=self.writer.snapshot() if self.writer is not None else None,
            process_end=read_proc_status(),
            clean_shutdown=True,
        )
        self._close_sample_artifacts()
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", type=normalize_arm, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--duration-sec", type=int, default=3600)
    parser.add_argument("--universe-csv", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--log-file", type=Path, required=True)
    parser.add_argument("--pair-count", type=int, default=300)
    parser.add_argument("--subscription-batch-pairs", type=int, default=30)
    parser.add_argument("--subscription-batch-pause-sec", type=float, default=3.0)
    parser.add_argument("--retry-delay-sec", type=float, default=10.0)
    parser.add_argument("--max-queue", type=int, default=None)
    parser.add_argument("--max-rss-mib", type=int, default=2048)
    parser.add_argument("--max-load-1", type=float, default=8.0)
    parser.add_argument("--min-mem-available-mib", type=int, default=4096)
    parser.add_argument("--max-fds", type=int, default=4000)
    parser.add_argument("--max-cpu-percent", type=float, default=95.0)
    parser.add_argument("--xrp-okx-samples", type=Path, required=True)
    parser.add_argument("--xrp-bybit-samples", type=Path, required=True)
    parser.add_argument("--loop-lag-samples", type=Path, required=True)
    parser.add_argument("--write-dir", type=Path, default=None)
    parser.add_argument("--write-format", choices=("pickle",), default="pickle")
    parser.add_argument("--write-batch-size", type=int, default=1000)
    parser.add_argument("--write-interval-sec", type=float, default=1.0)
    parser.add_argument("--write-queue-max", type=int, default=20000)
    parser.add_argument("--write-metrics-samples", type=Path, default=Path("write_metrics.csv"))
    args = parser.parse_args()
    if args.arm in WRITE_ARMS and args.write_dir is None:
        parser.error("arm C+ requires --write-dir")
    if args.arm not in WRITE_ARMS and args.write_dir is not None:
        parser.error("--write-dir is only valid for arm C+")
    if args.write_batch_size < 1 or args.write_queue_max < 1 or args.write_interval_sec <= 0:
        parser.error("write batch/queue/interval must be positive")
    return args


async def async_main(args: argparse.Namespace) -> int:
    pairs = load_or_create_manifest(args.universe_csv, args.manifest, args.pair_count)
    probe = Probe(args, pairs)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, probe.stop_event.set)
    return await probe.run()


def main() -> int:
    return asyncio.run(async_main(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
