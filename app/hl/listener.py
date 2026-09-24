"""One Hyperliquid websocket, ``bbo`` subscribe per matched coin.

Same public URL and subscribe shape as ``ping_hyper.py``. This process does
not open Bybit or OKX sockets.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Iterable

import websocket

from app.hl.bbo import (
    HL_WS_URL,
    BboParse,
    bbo_subscribe_payload,
    parse_bbo_message,
    ping_payload,
)

RecordOffer = Callable[[dict], None]


class HlBboListener:
    def __init__(
        self,
        coins: Iterable[str],
        offer: RecordOffer,
        logger: logging.Logger,
        *,
        url: str = HL_WS_URL,
        ping_sec: float = 30.0,
        reconnect_sec: float = 5.0,
    ) -> None:
        names = []
        seen: set[str] = set()
        for raw in coins:
            coin = str(raw).strip()
            if not coin or coin in seen:
                continue
            seen.add(coin)
            names.append(coin)
        if not names:
            raise ValueError("HL listener requires at least one coin")
        self.coins = tuple(names)
        self._coin_set = set(self.coins)
        self.offer = offer
        self.logger = logger
        self.url = url
        self.ping_sec = ping_sec
        self.reconnect_sec = reconnect_sec
        self._stop = threading.Event()
        self._ws: websocket.WebSocketApp | None = None
        self._ws_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._ping_thread: threading.Thread | None = None
        self.frames_total = 0
        self.parsed_total = 0
        self.ignored_total = 0
        self.incomplete_total = 0
        self.unexpected_coin_total = 0
        self.invalid_total = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run_loop,
            name="hl-l1-ws",
            daemon=True,
        )
        self._ping_thread = threading.Thread(
            target=self._ping_loop,
            name="hl-l1-ping",
            daemon=True,
        )
        self._thread.start()
        self._ping_thread.start()
        self.logger.info(
            "hl_ws_started | url=%s | coins=%s",
            self.url,
            len(self.coins),
        )

    def close(self) -> None:
        self._stop.set()
        with self._ws_lock:
            ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                self.logger.exception("hl_ws_close_error")

    def join(self, timeout_sec: float = 5.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout_sec)
        if self._ping_thread is not None:
            self._ping_thread.join(timeout=timeout_sec)

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            app = websocket.WebSocketApp(
                self.url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            with self._ws_lock:
                self._ws = app
            try:
                app.run_forever()
            except Exception:
                self.logger.exception("hl_ws_run_error")
            with self._ws_lock:
                if self._ws is app:
                    self._ws = None
            if self._stop.is_set():
                return
            self.logger.warning(
                "hl_ws_reconnect | delay_sec=%s",
                self.reconnect_sec,
            )
            self._stop.wait(self.reconnect_sec)

    def _ping_loop(self) -> None:
        while not self._stop.wait(self.ping_sec):
            with self._ws_lock:
                ws = self._ws
            if ws is None:
                continue
            try:
                ws.send(json.dumps(ping_payload()))
            except Exception:
                self.logger.warning("hl_ws_ping_failed")

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        self.logger.info("hl_ws_open | subscribes=%s", len(self.coins))
        for coin in self.coins:
            ws.send(json.dumps(bbo_subscribe_payload(coin)))

    def _on_message(self, ws: websocket.WebSocketApp, message: str) -> None:
        del ws
        self.frames_total += 1
        if '"channel":"error"' in message or '"channel": "error"' in message:
            self.logger.warning("hl_ws_error_frame | message=%s", message[:500])
        recv_ts_ms = int(time.time() * 1000)
        try:
            parsed = parse_bbo_message(message, recv_ts_ms=recv_ts_ms)
        except Exception:
            self.invalid_total += 1
            self.logger.exception("hl_bbo_parse_error")
            return
        self._count(parsed)
        if parsed.record is None:
            return
        coin = str(parsed.record.get("base_coin", ""))
        if coin not in self._coin_set:
            self.unexpected_coin_total += 1
            return
        self.offer(parsed.record)

    def _count(self, parsed: BboParse) -> None:
        if parsed.kind == "bbo":
            self.parsed_total += 1
        elif parsed.kind == "incomplete":
            self.incomplete_total += 1
        elif parsed.kind == "invalid":
            self.invalid_total += 1
        else:
            self.ignored_total += 1

    def _on_error(self, ws: websocket.WebSocketApp, error: object) -> None:
        del ws
        if self._stop.is_set():
            return
        self.logger.warning("hl_ws_error | error=%r", error)

    def _on_close(
        self,
        ws: websocket.WebSocketApp,
        status_code: object,
        close_msg: object,
    ) -> None:
        del ws
        if self._stop.is_set():
            return
        self.logger.warning(
            "hl_ws_close | code=%s | message=%s",
            status_code,
            close_msg,
        )
