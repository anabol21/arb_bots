#!/usr/bin/env python3
"""Run OKX + Bybit WS market-data latency probes concurrently for a fixed duration.

Reuses the same measurement logic as repo-root ping_okx.py / ping_bybit.py:
  - OKX: books5 <instId> -> local_ms - exchange_ts
  - Bybit: orderbook.1.<symbol> -> local_ms - ts / cts

Symbols are configurable (CLI or env). Defaults match universe base_coin=XRP:
  OKX XRP-USDT-SWAP + Bybit XRPUSDT.

Does not touch /data/live or collector state. Logs only.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import websocket

DEFAULT_DURATION_SEC = 2 * 60 * 60
DEFAULT_OKX_INST = "XRP-USDT-SWAP"
DEFAULT_BYBIT_SYMBOL = "XRPUSDT"
OKX_URL = "wss://ws.okx.com:8443/ws/v5/public"
BYBIT_URL = "wss://stream.bybit.com/v5/public/linear"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def local_ms() -> int:
    return time.time_ns() // 1_000_000


class DualLatencyRunner:
    def __init__(
        self,
        duration_sec: float,
        log_path: Path,
        okx_inst: str = DEFAULT_OKX_INST,
        bybit_symbol: str = DEFAULT_BYBIT_SYMBOL,
    ) -> None:
        self.duration_sec = duration_sec
        self.log_path = log_path
        self.okx_inst = okx_inst
        self.bybit_symbol = bybit_symbol
        self.bybit_topic = f"orderbook.1.{bybit_symbol}"
        self.stop_event = threading.Event()
        self.start_monotonic = 0.0
        self._lock = threading.Lock()
        self.counts = {"okx": 0, "bybit": 0}
        self.last = {"okx": None, "bybit": None}
        self.ws_okx: websocket.WebSocketApp | None = None
        self.ws_bybit: websocket.WebSocketApp | None = None

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger("ping_dual")
        self.logger.setLevel(logging.INFO)
        self.logger.handlers.clear()
        self.logger.propagate = False

        fmt = logging.Formatter("%(message)s")
        fh = logging.FileHandler(self.log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        self.logger.addHandler(fh)
        self.logger.addHandler(sh)

    def remaining_sec(self) -> float:
        return max(0.0, self.duration_sec - (time.monotonic() - self.start_monotonic))

    def should_stop(self) -> bool:
        return self.stop_event.is_set() or self.remaining_sec() <= 0

    def emit(self, exchange: str, fields: dict) -> None:
        row = {
            "ts_utc": utc_now_iso(),
            "exchange": exchange,
            "remaining_sec": round(self.remaining_sec(), 1),
            **fields,
        }
        line = (
            f"{row['ts_utc']}\t{exchange}"
            f"\tlatency_ms={fields.get('latency_ms')}"
            f"\tage_ts_ms={fields.get('age_ts_ms')}"
            f"\tage_cts_ms={fields.get('age_cts_ms')}"
            f"\tremaining_sec={row['remaining_sec']}"
        )
        self.logger.info(line)
        with self._lock:
            self.counts[exchange] = self.counts.get(exchange, 0) + 1
            self.last[exchange] = row

    # --- OKX ---
    def on_okx_message(self, _ws, message: str) -> None:
        if self.should_stop():
            try:
                _ws.close()
            except Exception:
                pass
            return
        try:
            data = json.loads(message)
            if "data" in data and data["data"] and "ts" in data["data"][0]:
                ts_exchange = int(data["data"][0]["ts"])
                latency = local_ms() - ts_exchange
                self.emit("okx", {"latency_ms": latency, "age_ts_ms": latency, "age_cts_ms": None})
        except Exception as exc:
            self.logger.info(f"{utc_now_iso()}\tokx\terror={exc!r}")

    def on_okx_open(self, ws) -> None:
        self.logger.info(
            f"{utc_now_iso()}\tokx\tevent=ws_open\tinstId={self.okx_inst}\tchannel=books5"
        )
        ws.send(
            json.dumps(
                {
                    "op": "subscribe",
                    "args": [{"channel": "books5", "instId": self.okx_inst}],
                }
            )
        )

    def on_okx_error(self, _ws, error) -> None:
        self.logger.info(f"{utc_now_iso()}\tokx\tevent=ws_error\terror={error!r}")

    def on_okx_close(self, _ws, status_code, msg) -> None:
        self.logger.info(
            f"{utc_now_iso()}\tokx\tevent=ws_close\tstatus={status_code}\tmsg={msg!r}"
        )

    # --- Bybit ---
    def on_bybit_message(self, _ws, message: str) -> None:
        if self.should_stop():
            try:
                _ws.close()
            except Exception:
                pass
            return
        try:
            data = json.loads(message)
            topic = str(data.get("topic", ""))
            if topic == self.bybit_topic or topic.startswith("orderbook.1."):
                ts_exchange = int(data["ts"])
                cts_exchange = int(data.get("cts", ts_exchange))
                now = local_ms()
                age_ts = now - ts_exchange
                age_cts = now - cts_exchange
                self.emit(
                    "bybit",
                    {
                        "latency_ms": age_ts,
                        "age_ts_ms": age_ts,
                        "age_cts_ms": age_cts,
                    },
                )
        except Exception as exc:
            self.logger.info(f"{utc_now_iso()}\tbybit\terror={exc!r}")

    def on_bybit_open(self, ws) -> None:
        self.logger.info(
            f"{utc_now_iso()}\tbybit\tevent=ws_open\ttopic={self.bybit_topic}"
        )
        ws.send(json.dumps({"op": "subscribe", "args": [self.bybit_topic]}))

    def on_bybit_error(self, _ws, error) -> None:
        self.logger.info(f"{utc_now_iso()}\tbybit\tevent=ws_error\terror={error!r}")

    def on_bybit_close(self, _ws, status_code, msg) -> None:
        self.logger.info(
            f"{utc_now_iso()}\tbybit\tevent=ws_close\tstatus={status_code}\tmsg={msg!r}"
        )

    def _run_ws(self, name: str, app: websocket.WebSocketApp) -> None:
        while not self.should_stop():
            try:
                app.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                self.logger.info(f"{utc_now_iso()}\t{name}\tevent=run_forever_error\terror={exc!r}")
            if self.should_stop():
                break
            self.logger.info(f"{utc_now_iso()}\t{name}\tevent=reconnect_sleep\tsleep_sec=3")
            time.sleep(3)

    def request_stop(self, reason: str = "signal") -> None:
        if self.stop_event.is_set():
            return
        self.logger.info(f"{utc_now_iso()}\tmeta\tevent=stop_requested\treason={reason}")
        self.stop_event.set()
        for ws in (self.ws_okx, self.ws_bybit):
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass

    def run(self) -> int:
        self.start_monotonic = time.monotonic()
        end_utc = datetime.now(timezone.utc).timestamp() + self.duration_sec
        end_iso = datetime.fromtimestamp(end_utc, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self.logger.info(
            f"{utc_now_iso()}\tmeta\tevent=start\tduration_sec={self.duration_sec}"
            f"\tend_utc≈{end_iso}\tlog={self.log_path}"
            f"\tokx_inst={self.okx_inst}\tbybit_symbol={self.bybit_symbol}"
        )
        self.logger.info(
            f"{utc_now_iso()}\tmeta\tevent=note\tmsg="
            f"okx=books5:{self.okx_inst} bybit={self.bybit_topic} "
            "latency=local_ms-exchange_ts (market data age, not ICMP)"
        )

        self.ws_okx = websocket.WebSocketApp(
            OKX_URL,
            on_message=self.on_okx_message,
            on_open=self.on_okx_open,
            on_error=self.on_okx_error,
            on_close=self.on_okx_close,
        )
        self.ws_bybit = websocket.WebSocketApp(
            BYBIT_URL,
            on_message=self.on_bybit_message,
            on_open=self.on_bybit_open,
            on_error=self.on_bybit_error,
            on_close=self.on_bybit_close,
        )

        t_okx = threading.Thread(
            target=self._run_ws, args=("okx", self.ws_okx), name="okx-ws", daemon=True
        )
        t_bybit = threading.Thread(
            target=self._run_ws, args=("bybit", self.ws_bybit), name="bybit-ws", daemon=True
        )
        t_okx.start()
        t_bybit.start()

        def _handle_sig(_signum, _frame) -> None:
            self.request_stop("signal")

        signal.signal(signal.SIGINT, _handle_sig)
        signal.signal(signal.SIGTERM, _handle_sig)

        while not self.should_stop():
            time.sleep(1.0)
            # heartbeat every ~60s so a quiet log is still observable
            elapsed = time.monotonic() - self.start_monotonic
            if int(elapsed) > 0 and int(elapsed) % 60 == 0:
                with self._lock:
                    self.logger.info(
                        f"{utc_now_iso()}\tmeta\tevent=heartbeat"
                        f"\telapsed_sec={int(elapsed)}"
                        f"\tokx_samples={self.counts['okx']}"
                        f"\tbybit_samples={self.counts['bybit']}"
                        f"\tlast_okx={self.last['okx']}"
                        f"\tlast_bybit={self.last['bybit']}"
                    )
                time.sleep(1.0)  # avoid double heartbeat in same second

        self.request_stop("duration_elapsed")
        t_okx.join(timeout=5)
        t_bybit.join(timeout=5)
        with self._lock:
            self.logger.info(
                f"{utc_now_iso()}\tmeta\tevent=finished"
                f"\tokx_samples={self.counts['okx']}"
                f"\tbybit_samples={self.counts['bybit']}"
                f"\tokx_inst={self.okx_inst}\tbybit_symbol={self.bybit_symbol}"
            )
        return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    env_okx = os.environ.get("PING_OKX_INST", DEFAULT_OKX_INST)
    env_bybit = os.environ.get("PING_BYBIT_SYMBOL", DEFAULT_BYBIT_SYMBOL)
    p = argparse.ArgumentParser(description="OKX+Bybit dual WS latency logger (2h default)")
    p.add_argument(
        "--duration-sec",
        type=float,
        default=DEFAULT_DURATION_SEC,
        help="Wall-clock run duration (default: 7200 = 2h)",
    )
    p.add_argument(
        "--log-file",
        type=Path,
        default=Path("/tmp/ping_okx_bybit_2h.log"),
        help="Output log path",
    )
    p.add_argument(
        "--okx-inst",
        default=env_okx,
        help=f"OKX SWAP instId (default/env PING_OKX_INST: {DEFAULT_OKX_INST})",
    )
    p.add_argument(
        "--bybit-symbol",
        default=env_bybit,
        help=f"Bybit linear symbol (default/env PING_BYBIT_SYMBOL: {DEFAULT_BYBIT_SYMBOL})",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    runner = DualLatencyRunner(
        duration_sec=args.duration_sec,
        log_path=args.log_file,
        okx_inst=args.okx_inst,
        bybit_symbol=args.bybit_symbol,
    )
    return runner.run()


if __name__ == "__main__":
    raise SystemExit(main())
