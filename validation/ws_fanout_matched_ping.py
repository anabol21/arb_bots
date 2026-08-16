#!/usr/bin/env python3
"""Fresh isolated XRP OKX/Bybit market-data ping for the fan-out probe."""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import websockets

OKX_URL = "wss://ws.okx.com:8443/ws/v5/public"
BYBIT_URL = "wss://stream.bybit.com/v5/public/linear"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Ping:
    def __init__(self, duration_sec: int, log_file: Path, retry_delay_sec: float, max_queue: int | None) -> None:
        self.duration_sec = duration_sec
        self.log_file = log_file
        self.stop = asyncio.Event()
        self.counts = {"okx": 0, "bybit": 0}
        self.retry_delay_sec = retry_delay_sec
        self.max_queue = max_queue
        self.attempts: dict[str, int] = defaultdict(int)
        self.reconnect_events: dict[str, deque[float]] = defaultdict(deque)

    def emit(self, exchange: str, **fields: object) -> None:
        line = {"ts_utc": now_iso(), "exchange": exchange, **fields}
        with self.log_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, separators=(",", ":"), sort_keys=True) + "\n")

    def wave(self, exchange: str) -> dict[str, int]:
        now = time.monotonic()
        events = self.reconnect_events[exchange]
        events.append(now)
        while events and now - events[0] > 60:
            events.popleft()
        return {"reconnects_10s": sum(now - value <= 10 for value in events), "reconnects_60s": len(events)}

    @staticmethod
    def close_fields(ws: object | None, exc: BaseException | None, age_sec: float | None) -> dict[str, object | None]:
        close_code = getattr(ws, "close_code", None) if ws is not None else None
        close_reason = getattr(ws, "close_reason", None) if ws is not None else None
        clean = close_code in {1000, 1001} or (exc is not None and type(exc).__name__ == "ConnectionClosedOK")
        return {
            "exception_class": type(exc).__name__ if exc is not None else None,
            "exception_str": str(exc) if exc is not None else None,
            "exception_repr": repr(exc) if exc is not None else None,
            "ws_close_code": close_code,
            "ws_close_reason": close_reason,
            "close_classification": "clean" if clean else "abrupt",
            "connection_age_sec": round(age_sec, 6) if age_sec is not None else None,
        }

    async def listener(self, exchange: str) -> None:
        url = OKX_URL if exchange == "okx" else BYBIT_URL
        while not self.stop.is_set():
            ws = None
            connected = None
            close_exc = None
            self.attempts[exchange] += 1
            attempt = self.attempts[exchange]
            if attempt > 1:
                self.emit(exchange, event="unplanned_reconnect", attempt=attempt, retry_delay_sec=self.retry_delay_sec, **self.wave(exchange))
            try:
                connect_started = time.monotonic()
                kwargs: dict[str, object] = {"ping_interval": 20, "ping_timeout": 20, "close_timeout": 2}
                if self.max_queue is not None:
                    kwargs["max_queue"] = self.max_queue
                async with websockets.connect(url, **kwargs) as ws:
                    connected = time.monotonic()
                    self.emit(exchange, event="connection_opened", attempt=attempt, connect_elapsed_ms=round((connected - connect_started) * 1000, 3))
                    subscription = (
                        {"op": "subscribe", "args": [{"channel": "books5", "instId": "XRP-USDT-SWAP"}]}
                        if exchange == "okx"
                        else {"op": "subscribe", "args": ["orderbook.1.XRPUSDT"]}
                    )
                    await ws.send(json.dumps(subscription))
                    self.emit(exchange, event="subscription_sent", attempt=attempt)
                    async for message in ws:
                        recv_ms = time.time_ns() // 1_000_000
                        data = json.loads(message)
                        if exchange == "okx":
                            values = data.get("data") or []
                            if not values or values[0].get("ts") is None:
                                continue
                            ts = int(values[0]["ts"])
                            self.emit(exchange, latency_ms=recv_ms - ts, age_ts_ms=recv_ms - ts)
                        else:
                            if data.get("topic") != "orderbook.1.XRPUSDT" or data.get("ts") is None:
                                continue
                            ts, cts = int(data["ts"]), int(data.get("cts", data["ts"]))
                            self.emit(
                                exchange,
                                latency_ms=recv_ms - ts,
                                age_ts_ms=recv_ms - ts,
                                age_cts_ms=recv_ms - cts,
                            )
                        self.counts[exchange] += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                close_exc = exc
                self.emit(exchange, event="connection_error", attempt=attempt, **self.close_fields(ws, exc, time.monotonic() - connected if connected else None))
                if not self.stop.is_set():
                    self.emit(exchange, event="reconnect_scheduled", attempt=attempt, retry_delay_sec=self.retry_delay_sec)
                    await asyncio.sleep(self.retry_delay_sec)
            finally:
                if ws is not None and not self.stop.is_set():
                    fields = self.close_fields(ws, close_exc, time.monotonic() - connected if connected else None)
                    self.emit(exchange, event="connection_closed", attempt=attempt, **fields)
                    self.emit(exchange, event="unplanned_close", attempt=attempt, **fields)

    async def run(self) -> int:
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.emit(
            "meta",
            event="start",
            duration_sec=self.duration_sec,
            retry_delay_sec=self.retry_delay_sec,
            max_queue=self.max_queue,
            max_queue_rationale="None means websockets library default; explicit value is recorded.",
        )
        tasks = [asyncio.create_task(self.listener(exchange)) for exchange in self.counts]
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=self.duration_sec)
        except asyncio.TimeoutError:
            pass
        self.stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.emit("meta", event="finished", samples=self.counts)
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-sec", type=int, default=3600)
    parser.add_argument("--log-file", type=Path, required=True)
    parser.add_argument("--retry-delay-sec", type=float, default=10.0)
    parser.add_argument("--max-queue", type=int, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ping = Ping(args.duration_sec, args.log_file, args.retry_delay_sec, args.max_queue)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, ping.stop.set)
    return loop.run_until_complete(ping.run())


if __name__ == "__main__":
    raise SystemExit(main())
