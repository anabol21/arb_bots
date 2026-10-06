#!/usr/bin/env python3
"""Gear 2.3 private-readiness child with order-capable paths blocked."""

from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Callable

from app.bot.private import rest_readonly, ws_reseed
from app.bot.private.ws_gates import assert_ws_warm_private_gates
from app.bot.private.secrets import load_live_secrets
from app.bot.private.ws_warm_session import (
    _creds_from_live_secrets,
    _production_socket_provider,
    start_warm_private_session,
)
from app.bot.runtime import BotRuntime


_SAFE_WS_OPS = frozenset({"auth", "login", "subscribe", "ping", "pong"})
_SAFE_REST_PATHS = frozenset(
    {
        "/v5/account/wallet-balance",
        "/v5/position/list",
        "/v5/market/instruments-info",
        "/api/v5/account/balance",
        "/api/v5/account/positions",
        "/api/v5/account/instruments",
    }
)


def _operation(text: str) -> str:
    if text in {"ping", "pong"}:
        return text
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return "unknown"
    return str(value.get("op") or "unknown") if isinstance(value, dict) else "unknown"


def _guard_socket(socket: Any, blocked: list[str], allowed: Counter[str]) -> None:
    def check(text: str) -> None:
        op = _operation(text)
        if op not in _SAFE_WS_OPS:
            blocked.append(f"{socket.exchange}:{socket.channel}:{op}")
            raise RuntimeError("readonly_validation_blocked_ws_operation")
        allowed[f"{socket.exchange}:{socket.channel}:{op}"] += 1

    send = socket.send_text
    send_timed = socket.send_text_timed
    asend = socket.asend

    def guarded_send(text: str) -> None:
        check(text)
        send(text)

    def guarded_send_timed(text: str, **kwargs: Any) -> None:
        check(text)
        send_timed(text, **kwargs)

    async def guarded_asend(text: str) -> None:
        check(text)
        await asend(text)

    socket.send_text = guarded_send
    socket.send_text_timed = guarded_send_timed
    socket.asend = guarded_asend


def _start_readonly_session(*, env: dict[str, str], coins: tuple[str, ...],
                            data_root: Path, socket_provider: Callable[[], Any],
                            stop_event: Any) -> Any:
    secrets = load_live_secrets(env, require_complete=True)
    return start_warm_private_session(
        env=env,
        bybit_credentials=_creds_from_live_secrets(secrets, "bybit"),
        okx_credentials=_creds_from_live_secrets(secrets, "okx"),
        coins=coins,
        data_root=data_root,
        socket_provider=socket_provider,
        profile_gate=assert_ws_warm_private_gates,
        attach=True,
        keepalive=True,
        stop_event=stop_event,
    )


def _readonly_provider(blocked: list[str], allowed: Counter[str]) -> Callable[[], Any]:
    connect = _production_socket_provider()

    def provide() -> Any:
        bundle = connect()
        for socket in (
            bundle.bybit_private,
            bundle.bybit_trade,
            bundle.okx_private,
            bundle.okx_trade,
        ):
            _guard_socket(socket, blocked, allowed)
        return bundle

    return provide


def _private_socket_state(session: Any, allowed: Counter[str]) -> dict[str, Any]:
    sockets = {}
    for venue, rt in (("bybit", session.bybit_runtime), ("okx", session.okx_runtime)):
        sockets[venue] = {
            "generation": int(rt.reconnect_generation),
            "private_socket_id": id(rt.private_socket),
            "trade_socket_id": id(rt.trade_socket),
        }
    handshakes = {
        key: count for key, count in sorted(allowed.items())
        if key.endswith((":auth", ":login", ":subscribe"))
    }
    return {"sockets": sockets, "handshake_ops": handshakes}


def _guard_rest(blocked: list[str], counts: Counter[str]) -> None:
    original = rest_readonly._http_get

    def get(url: str, headers: Any, *, timeout_sec: float = 15.0):
        path = urlsplit(url).path
        if path not in _SAFE_REST_PATHS:
            blocked.append(path)
            raise RuntimeError("readonly_validation_blocked_rest_path")
        counts[path] += 1
        return original(url, headers, timeout_sec=timeout_sec)

    rest_readonly._http_get = get
    ws_reseed._http_get = get


async def _run() -> int:
    runtime = BotRuntime()
    if runtime._terminal_private_execution or runtime.theta_trade is not None:
        raise RuntimeError("readonly child must not construct terminal order manager")
    if runtime.broker is None or runtime._synthetic_sender is not None:
        raise RuntimeError("readonly child unexpectedly has an order sender")
    stub_place_attempts: list[str] = []

    def reject_stub_place(*_args: Any, **_kwargs: Any) -> None:
        stub_place_attempts.append("place")
        raise RuntimeError("readonly_validation_blocked_stub_place")

    runtime.broker.place = reject_stub_place

    def refuse_entry(**_kwargs: Any) -> None:
        return None

    runtime._probe_maybe_open = refuse_entry
    runtime._policy_maybe_act = refuse_entry
    runtime.log.info("validation_no_orders | callbacks=probe,policy | theta_trade=disabled")

    blocked_ws: list[str] = []
    allowed_ws: Counter[str] = Counter()
    blocked_rest: list[str] = []
    rest_gets: Counter[str] = Counter()
    _guard_rest(blocked_rest, rest_gets)
    private_env = dict(os.environ)
    private_env.update(
        {
            "VENUE": "live",
            "LIVE_ORDERS": "1",
            "BBOT_PROFILE": "gear22_live_canary",
            "BBOT_BROKER": "private_live",
            "BBOT_THETA_LIVE_SEND": "1",
            "BBOT_THETA_TRADE": "1",
        }
    )
    private_root = Path(os.environ["BBOT_DATA_ROOT"]) / "private-readonly"
    provider = _readonly_provider(blocked_ws, allowed_ws)
    session = _start_readonly_session(
        env=private_env, coins=runtime.coins, data_root=private_root,
        socket_provider=provider, stop_event=runtime.stop_event,
    )
    runtime.start_private_warm_if_live_send = lambda **_kwargs: session
    initial_private_state = _private_socket_state(session, allowed_ws)
    final_private_state: dict[str, Any] | None = None
    original_stop_private = runtime._stop_synthetic_private_send

    def snapshot_before_private_cleanup() -> None:
        nonlocal final_private_state
        final_private_state = _private_socket_state(session, allowed_ws)
        original_stop_private()

    runtime._stop_synthetic_private_send = snapshot_before_private_cleanup
    runtime.log.info(
        "validation_private_warm_injected | ready=%s | base_coin_count=%s | "
        "rest_reseed=mandatory_readonly_gets",
        str(session.is_ready()).lower(),
        len(runtime.coins),
    )

    if runtime.theta_screener is not None:
        compute = runtime.theta_screener.compute_from_tw_snapshots
        observed: dict[str, bool] = {}

        def compute_and_check(tw_snapshots: Any) -> Any:
            snapshots = compute(tw_snapshots)
            for coin in runtime.coins:
                if coin in runtime._gear23_base_coins:
                    continue
                meta = runtime._meta(coin)
                ready = session.coin_ready(
                    bybit_symbol=meta.bybit_symbol,
                    okx_symbol=meta.okx_symbol,
                )
                if observed.get(coin) != ready:
                    observed[coin] = ready
                    runtime.log.info(
                        "validation_private_coin_ready | base_coin=%s | ready=%s",
                        coin,
                        str(ready).lower(),
                    )
                runtime._gear23_candidate_ready(coin, snapshots)
            return snapshots

        runtime.theta_screener.compute_from_tw_snapshots = compute_and_check

    try:
        await runtime.run()
        if blocked_ws or blocked_rest:
            return 3
        return 0
    finally:
        final_private_state = final_private_state or _private_socket_state(session, allowed_ws)
        same_sockets = initial_private_state["sockets"] == final_private_state["sockets"]
        same_auth_generation = all(
            initial_private_state["handshake_ops"].get(key, 0)
            == final_private_state["handshake_ops"].get(key, 0)
            for key in set(initial_private_state["handshake_ops"]) | set(final_private_state["handshake_ops"])
            if key.endswith((":auth", ":login"))
        )
        candidate_symbols = {
            runtime._meta(coin).okx_symbol
            for coin in runtime.coins
            if coin not in runtime._gear23_base_coins
        }
        okx_acks = {
            f"{symbol}:{channel}": {
                "generation": gen,
                "ok": ok,
                "request_ids": sorted(
                    req_id for req_id, (req_gen, keys) in session.okx_runtime._okx_subscription_requests.items()
                    if req_gen == gen and (symbol, channel) in keys
                ),
                "rejected_request_ids": sorted(
                    req_id for rejected_symbol, rejected_channel, rejected_gen, req_id
                    in session.okx_runtime._symbol_subscription_rejected
                    if (rejected_symbol, rejected_channel, rejected_gen) == (symbol, channel, gen)
                ),
            }
            for (symbol, channel), (gen, ok) in sorted(
                session.okx_runtime._symbol_subscription_acks.items()
            )
            if symbol in candidate_symbols
        }
        okx_requests = {
            req_id: {
                "generation": gen,
                "keys": sorted(key for key in keys if key[0] in candidate_symbols),
            }
            for req_id, (gen, keys) in sorted(
                session.okx_runtime._okx_subscription_requests.items()
            )
            if any(key[0] in candidate_symbols for key in keys)
        }
        runtime.log.info(
            "validation_private_summary | initial=%s | final=%s | "
            "same_sockets=%s | same_auth_generation=%s | okx_ack_map=%s | "
            "okx_pending_requests=%s",
            json.dumps(initial_private_state, sort_keys=True),
            json.dumps(final_private_state, sort_keys=True),
            str(same_sockets).lower(),
            str(same_auth_generation).lower(),
            json.dumps(okx_acks, sort_keys=True),
            json.dumps(okx_requests, sort_keys=True),
        )
        runtime.log.info(
            "validation_readonly_summary | blocked_ws=%s | blocked_rest=%s | "
            "rest_gets=%s | allowed_ws=%s | stub_place_attempts=%s",
            len(blocked_ws), len(blocked_rest),
            json.dumps(dict(sorted(rest_gets.items())), sort_keys=True),
            json.dumps(dict(sorted(allowed_ws.items())), sort_keys=True),
            len(stub_place_attempts),
        )
        session.stop()


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
