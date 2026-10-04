#!/usr/bin/env python3
"""One-shot live hot-path probe: three XRP open/close pairs, exactly 5 XRP/leg.

This intentionally injects six synthetic signals into BotRuntime's existing
``_synthetic_live_place`` path. It does not run the random/policy loop. The
script is fail-stop: it never retries a send or automatically cleans up after
an ambiguous result. A close is sent only as the next explicitly listed step,
after the preceding open was confirmed full on both accounts.

Live execution requires ``--execute-live`` and performs a fresh GET-only
account preflight while holding the shared private-experiment process lock.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping


EXPECTED_XRP = Decimal("5")
EXPECTED_BYBIT_SIGNED = Decimal("5")  # open_short buys Bybit XRPUSDT
EXPECTED_OKX_SIGNED = Decimal("-0.05")  # 100 XRP/contract, open_short sells
RUN_ID_RE = re.compile(r"^hotpath-xrp-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$")
ACTION_CYCLE = (("open_short", None), ("close", "open_short"))
DEFAULT_CYCLES = 3
STEPS = ACTION_CYCLE * DEFAULT_CYCLES
TIMING_FIELDS = (
    "queue_enqueued_ns",
    "dequeued_ns",
    "callback_started_ns",
    "callback_returned_ns",
    "owner_ws_send_started_ns",
    "owner_ws_send_returned_ns",
)
REMOTE_BASE = Path("/root/b-private-b-exp")
PREFLIGHT_SCRIPT = REMOTE_BASE / "validation" / "b0_readonly_preflight_ii7.py"
PROCESS_LOCK = REMOTE_BASE / "b0-private" / (
    "694915274b830a177486e161b90cd52a4e54b359b62e58b59f026d4255ce3a93.process.lock"
)
ENV_FILE = Path("/etc/spread/bbot-private-live.env")


class ExperimentStop(RuntimeError):
    """Safe, bounded stop with no retry or implicit close."""


def _decimal(value: object) -> Decimal:
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ExperimentStop("account_or_frame_numeric_invalid") from exc
    if not out.is_finite():
        raise ExperimentStop("account_or_frame_numeric_invalid")
    return out


def _order_frame(
    *,
    venue: str,
    text: str,
    expected_phase: str,
    okx_ct_val: Decimal,
) -> None:
    try:
        frame = json.loads(text)
        args = frame["args"][0]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise ExperimentStop("signed_frame_shape_invalid") from exc
    close = expected_phase == "close"
    if venue == "bybit":
        expected_frame = {"op": "order.create"}
        expected_args = {
            "category": "linear",
            "symbol": "XRPUSDT",
            "side": "Sell" if close else "Buy",
            "orderType": "Market",
        }
        if any(frame.get(k) != v for k, v in expected_frame.items()) or any(
            args.get(k) != v for k, v in expected_args.items()
        ):
            raise ExperimentStop("bybit_frame_not_expected_xrp_order")
        if _decimal(args.get("qty")) != EXPECTED_XRP:
            raise ExperimentStop("bybit_frame_qty_not_5_xrp")
        if bool(args.get("reduceOnly", False)) != close:
            raise ExperimentStop("bybit_reduce_only_mismatch")
        if not (frame.get("header") or {}).get("X-BAPI-SIGN"):
            raise ExperimentStop("bybit_frame_unsigned")
        return
    if venue == "okx":
        expected_frame = {"op": "order"}
        expected_args = {
            "instId": "XRP-USDT-SWAP",
            "side": "buy" if close else "sell",
            "ordType": "market",
            "tdMode": "cross",
        }
        if any(frame.get(k) != v for k, v in expected_frame.items()) or any(
            args.get(k) != v for k, v in expected_args.items()
        ):
            raise ExperimentStop("okx_frame_not_expected_xrp_order")
        if _decimal(args.get("sz")) * okx_ct_val != EXPECTED_XRP:
            raise ExperimentStop("okx_frame_qty_not_5_xrp_equivalent")
        if bool(args.get("reduceOnly", False)) != close:
            raise ExperimentStop("okx_reduce_only_mismatch")
        if type(args.get("instIdCode")) is not int or args["instIdCode"] <= 0:
            raise ExperimentStop("okx_frame_inst_id_code_invalid")
        return
    raise ExperimentStop("unknown_frame_venue")


class StrictQuantityGuard:
    """Validate both signed frames before allowing each dual queue operation."""

    def __init__(self, delegate: Any, *, okx_ct_val: Decimal) -> None:
        self.delegate = delegate
        self.prepared_sender = delegate
        self.okx_ct_val = okx_ct_val
        self.expected_phase: str | None = None
        self.dual_pairs_enqueued = 0

    def arm(self, phase: str) -> None:
        if phase not in {"open", "close"}:
            raise ExperimentStop("unexpected_phase")
        self.expected_phase = phase

    def is_ready(self) -> bool:
        return self.delegate.is_ready()

    def queue_depths(self) -> dict[str, int]:
        return self.delegate.queue_depths()

    def used_prepared_sender(self) -> bool:
        return self.dual_pairs_enqueued > 0 and self.delegate is self.prepared_sender

    def enqueue_dual(self, **kwargs: Any) -> Any:
        phase = str(kwargs.get("phase") or "")
        if phase != self.expected_phase:
            raise ExperimentStop("send_phase_mismatch")
        _order_frame(
            venue="bybit", text=str(kwargs.get("bybit_text") or ""),
            expected_phase=phase, okx_ct_val=self.okx_ct_val,
        )
        _order_frame(
            venue="okx", text=str(kwargs.get("okx_text") or ""),
            expected_phase=phase, okx_ct_val=self.okx_ct_val,
        )
        result = self.delegate.enqueue_dual(**kwargs)
        self.dual_pairs_enqueued += 1
        return result

    def close(self) -> None:
        self.delegate.close()


def _write_json_new(path: Path, payload: Mapping[str, Any]) -> None:
    raw = (json.dumps(dict(payload), sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())


def _append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    raw = (json.dumps(dict(payload), sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "ab") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())


def _fresh_readonly_preflight(repo_root: Path, run_root: Path) -> Path:
    evidence = run_root / "account-preflight.json"
    env = dict(os.environ)
    env.update({
        "VENUE": "live",
        "LIVE_ORDERS": "0",
        "BBOT_PRIVATE_ENV_FILE": str(ENV_FILE),
        "PYTHONPATH": str(repo_root),
    })
    proc = subprocess.run(
        [sys.executable, str(PREFLIGHT_SCRIPT), str(evidence)],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0 or not evidence.is_file():
        raise ExperimentStop(f"readonly_preflight_failed_rc_{proc.returncode}")
    try:
        result = json.loads(evidence.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentStop("readonly_preflight_unreadable") from exc
    try:
        stdout_result = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise ExperimentStop("readonly_preflight_output_invalid") from exc
    if (
        result.get("ready") is not True
        or stdout_result.get("orders_sent") != 0
        or stdout_result.get("ready") is not True
    ):
        raise ExperimentStop("readonly_preflight_not_ready")
    if not isinstance(result.get("gates"), dict) or not all(result["gates"].values()):
        raise ExperimentStop("readonly_preflight_gates_not_all_true")
    if time.time() - evidence.stat().st_mtime > 120:
        raise ExperimentStop("readonly_preflight_stale")
    return evidence


def _exact_account_snapshot(
    *,
    bybit_credentials: Any,
    okx_credentials: Any,
) -> dict[str, Any]:
    from app.bot.private.venue import endpoints_for_venue
    from app.bot.private.ws_w4_baseline import (
        _OKX_OPEN,
        _OKX_POS,
        _BYBIT_OPEN,
        _BYBIT_POS,
        _bybit_open_orders_flat,
        _bybit_signed_get,
        _okx_open_orders_flat,
        _okx_signed_get,
    )

    ep = endpoints_for_venue("live")
    bp = _bybit_signed_get(
        credentials=bybit_credentials, base=ep.bybit_rest, path=_BYBIT_POS,
        query="category=linear&symbol=XRPUSDT&settleCoin=USDT&limit=200",
    )
    bo = _bybit_signed_get(
        credentials=bybit_credentials, base=ep.bybit_rest, path=_BYBIT_OPEN,
        query="category=linear&symbol=XRPUSDT&openOnly=0&limit=50",
    )
    op = _okx_signed_get(
        credentials=okx_credentials, base=ep.okx_rest,
        path_with_query=f"{_OKX_POS}?instId=XRP-USDT-SWAP",
    )
    oo = _okx_signed_get(
        credentials=okx_credentials, base=ep.okx_rest,
        path_with_query=f"{_OKX_OPEN}?instId=XRP-USDT-SWAP&instType=SWAP&limit=100",
    )
    if bp.get("retCode") not in (0, "0") or op.get("code") != "0":
        raise ExperimentStop("account_position_get_rejected")
    by_rows = (bp.get("result") or {}).get("list")
    ok_rows = op.get("data")
    if not isinstance(by_rows, list) or not isinstance(ok_rows, list):
        raise ExperimentStop("account_position_shape_invalid")
    if bp.get("nextPageCursor") not in ("", None):
        raise ExperimentStop("bybit_position_page_incomplete")
    by_target = [r for r in by_rows if isinstance(r, dict) and r.get("symbol") == "XRPUSDT"]
    if len(by_target) > 1:
        raise ExperimentStop("bybit_position_rows_ambiguous")
    by_signed = Decimal("0")
    by_idx = None
    if by_target:
        row = by_target[0]
        by_idx = row.get("positionIdx")
        size = _decimal(row.get("size") or "0")
        side = str(row.get("side") or "")
        if side == "Buy":
            by_signed = size
        elif side == "Sell":
            by_signed = -size
        elif size != 0:
            raise ExperimentStop("bybit_position_side_ambiguous")
    if op.get("code") != "0":
        raise ExperimentStop("okx_position_get_rejected")
    ok_target = [r for r in ok_rows if isinstance(r, dict) and r.get("instId") == "XRP-USDT-SWAP"]
    ok_signed = Decimal("0")
    if len(ok_target) > 1:
        raise ExperimentStop("okx_position_rows_ambiguous")
    if ok_target:
        row = ok_target[0]
        if str(row.get("posSide") or "") != "net":
            raise ExperimentStop("okx_position_mode_unexpected")
        ok_signed = _decimal(row.get("pos") or "0")
    try:
        by_orders_flat = _bybit_open_orders_flat(bo, "XRPUSDT")
        ok_orders_flat = _okx_open_orders_flat(oo, "XRP-USDT-SWAP")
    except Exception as exc:  # noqa: BLE001 — do not expose remote response text
        raise ExperimentStop("account_open_orders_get_rejected") from exc
    if bo.get("retCode") not in (0, "0") or oo.get("code") != "0":
        raise ExperimentStop("account_open_orders_get_rejected")
    if bo.get("nextPageCursor") not in ("", None):
        raise ExperimentStop("bybit_open_orders_page_incomplete")
    return {
        "bybit_signed_xrp": str(by_signed),
        "bybit_position_idx": by_idx,
        "bybit_open_orders_flat": bool(by_orders_flat),
        "okx_signed_contracts": str(ok_signed),
        "okx_open_orders_flat": bool(ok_orders_flat),
    }


def _state_matches(snapshot: Mapping[str, Any], *, phase: str) -> bool:
    by = _decimal(snapshot.get("bybit_signed_xrp"))
    ok = _decimal(snapshot.get("okx_signed_contracts"))
    if phase == "open":
        return (
            by == EXPECTED_BYBIT_SIGNED
            and ok == EXPECTED_OKX_SIGNED
            and snapshot.get("bybit_position_idx") in (0, "0")
            and snapshot.get("bybit_open_orders_flat") is True
            and snapshot.get("okx_open_orders_flat") is True
        )
    return (
        by == 0
        and ok == 0
        and snapshot.get("bybit_open_orders_flat") is True
        and snapshot.get("okx_open_orders_flat") is True
    )


async def _wait_account_state(
    *,
    bybit_credentials: Any,
    okx_credentials: Any,
    phase: str,
    timeout_sec: float = 4.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = await asyncio.to_thread(
            _exact_account_snapshot,
            bybit_credentials=bybit_credentials,
            okx_credentials=okx_credentials,
        )
        if _state_matches(last, phase=phase):
            return last
        await asyncio.sleep(0.25)
    raise ExperimentStop("account_state_did_not_match_expected_after_send")


async def _wait_books(runtime: Any, *, timeout_sec: float = 20.0) -> tuple[dict[str, Any], dict[str, Any]]:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        okx = dict(runtime.quotes["XRP"]["okx"])
        bybit = dict(runtime.quotes["XRP"]["bybit"])
        now_ms = time.time() * 1000
        ages = [now_ms - float(b.get("local_recv_ts_ms") or 0) for b in (okx, bybit)]
        if all(
            b.get("bid_price") and b.get("ask_price")
            and age >= -500 and age <= 1500
            for b, age in zip((okx, bybit), ages)
        ):
            return okx, bybit
        await asyncio.sleep(0.05)
    raise ExperimentStop("fresh_public_books_unavailable")


def _summarize_timings(data_root: Path, actions: list[dict[str, Any]]) -> dict[str, Any]:
    event_date = datetime.now(timezone.utc).date().isoformat()
    from app.bot.paths import theta_step_chrono_jsonl_path

    path = theta_step_chrono_jsonl_path(data_root, event_date)
    if not path.is_file():
        return {"timing_log_confirmed": False, "reason": "step_chrono_missing", "actions": []}
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_intent = {r.get("intent_id"): r for r in rows if r.get("block") == "send_timing"}
    results: list[dict[str, Any]] = []
    confirmed = len(by_intent) == len(actions)
    for action in actions:
        timing = by_intent.get(action["intent_id"])
        send_exit = next((r for r in rows if r.get("intent_id") == action["intent_id"]
                          and r.get("block") == "ws_send" and r.get("edge") == "exit"), None)
        if timing is None or send_exit is None:
            confirmed = False
            results.append({"action": action["action"], "logged": False})
            continue
        stamps = timing.get("send_timing_monotonic_ns") or {}
        venue_result: dict[str, Any] = {}
        valid = timing.get("phase") == ("close" if action["action"] == "close" else "open")
        for venue in ("bybit", "okx"):
            m = stamps.get(venue) or {}
            valid = valid and all(type(m.get(k)) is int for k in TIMING_FIELDS)
            if not all(type(m.get(k)) is int for k in TIMING_FIELDS):
                continue
            q = m["queue_enqueued_ns"]
            start = m["owner_ws_send_started_ns"]
            end = m["owner_ws_send_returned_ns"]
            valid = valid and q <= m["dequeued_ns"] <= m["callback_started_ns"] <= start <= end <= m["callback_returned_ns"]
            venue_result[venue] = {
                "signal_to_queue_ms": round((q - action["signal_mono_ns"]) / 1e6, 3),
                "queue_to_ws_send_ms": round((start - q) / 1e6, 3),
                "signal_to_ws_send_ms": round((start - action["signal_mono_ns"]) / 1e6, 3),
                "ws_send_call_ms": round((end - start) / 1e6, 3),
                "callback_tail_ms": round((m["callback_returned_ns"] - end) / 1e6, 3),
            }
        valid = valid and timing.get("mono_ns", 0) >= send_exit.get("mono_ns", 0)
        confirmed = confirmed and valid and len(venue_result) == 2
        results.append({"action": action["action"], "logged": valid, "venues": venue_result})
    return {"timing_log_confirmed": confirmed, "step_chrono_path": str(path), "actions": results}


async def _run_experiment(
    run_id: str, run_root: Path, repo_root: Path, *, cycles: int
) -> int:
    steps = ACTION_CYCLE * cycles
    os.environ.update({
        "VENUE": "live",
        "LIVE_ORDERS": "0",
        "BBOT_PRIVATE_ENV_FILE": str(ENV_FILE),
        "BBOT_PROFILE": "synthetic_roll",
        "BBOT_MODE": "policy",
        "BBOT_COINS": "XRP",
        "BBOT_BROKER": "private_live",
        "BBOT_PRIVATE_SEND_PATH": "trivial",
        "BBOT_DATA_ROOT": str(run_root / "data"),
        "BBOT_PRIVATE_DATA_ROOT": str(run_root / "private"),
        "BBOT_LOG_PATH": str(run_root / "bbot.log"),
        "BBOT_NOTIONAL_USDT": "7.5",
    })
    preflight = _fresh_readonly_preflight(repo_root, run_root)
    _write_json_new(run_root / "preflight_ref.json", {
        "path": str(preflight), "mtime_utc": datetime.fromtimestamp(preflight.stat().st_mtime, timezone.utc).isoformat(),
        "orders_sent": 0,
    })

    # Switch on the private sender only after the fresh GET-only gate passes.
    os.environ["LIVE_ORDERS"] = "1"
    from app.bot.private.secrets import load_live_secrets
    import app.bot.private.coin_qty as coin_qty
    from app.bot.runtime import BotRuntime
    from app.bot.ws_books import run_bybit_orderbook1, run_okx_books5

    runtime = BotRuntime()
    public_stop = asyncio.Event()
    public_tasks: list[asyncio.Task[Any]] = []
    session = None
    guard: StrictQuantityGuard | None = None
    events_path = run_root / "actions.jsonl"
    actions: list[dict[str, Any]] = []
    outcome = "failed"
    stop_reason: str | None = None
    final_state: dict[str, Any] | None = None
    try:
        session = runtime.start_private_warm_if_live_send(
            stop_event=runtime.stop_event, coins=runtime.coins,
        )
        runtime._private_warm = session
        if session is None:
            raise ExperimentStop("private_warm_session_not_started")
        runtime._prefetch_okx_ct_vals()
        runtime._prefetch_okx_inst_id_codes()
        meta = runtime._meta("XRP")
        ct_raw = runtime._okx_ct_vals.get(meta.okx_symbol)
        ct_val = _decimal(ct_raw)
        if ct_val != Decimal("100"):
            raise ExperimentStop("unexpected_okx_xrp_ct_val")

        # Leverage and position mode were freshly read and gated by preflight;
        # do not call _set_leverage_one (a POST) in this experiment.
        runtime._leverage_one[("okx", meta.okx_symbol)] = "1"
        runtime._leverage_one[("bybit", meta.bybit_symbol)] = "1"
        creds = load_live_secrets(require_complete=True)

        ready_deadline = time.monotonic() + 30
        while time.monotonic() < ready_deadline:
            if session.is_ready():
                break
            await asyncio.sleep(0.1)
        if not session.is_ready():
            raise ExperimentStop("private_warm_session_not_ready")
        initial = await asyncio.to_thread(
            _exact_account_snapshot,
            bybit_credentials=session.bybit_credentials,
            okx_credentials=session.okx_credentials,
        )
        if not _state_matches(initial, phase="close"):
            raise ExperimentStop("account_not_flat_before_first_send")
        initial_state = initial

        if session._handshake_count != 1:
            raise ExperimentStop("private_handshake_count_not_one")
        if not runtime._prepare_synthetic_live_sender():
            raise ExperimentStop("synthetic_sender_preparation_skipped")
        prepared_sender = runtime._synthetic_sender
        queue_depths = prepared_sender.queue_depths()
        if not prepared_sender.is_ready() or queue_depths != {"bybit": 0, "okx": 0}:
            raise ExperimentStop("synthetic_sender_not_ready_or_not_empty")
        _append_jsonl(events_path, {
            "event": "synthetic_sender_ready",
            "warm_session_run_id": session.run_id,
            "handshake_count": session._handshake_count,
            "queues_ready": True,
            "queue_depths": queue_depths,
            "frames_enqueued": 0,
            "before_public_tasks": True,
        })

        public_tasks = [
            asyncio.create_task(run_okx_books5(
                base_coin="XRP", okx_symbol=meta.okx_symbol,
                book_store=runtime.quotes["XRP"]["okx"], on_book=lambda *_: None,
                stop_event=public_stop,
            )),
            asyncio.create_task(run_bybit_orderbook1(
                base_coin="XRP", bybit_symbol=meta.bybit_symbol,
                book_store=runtime.quotes["XRP"]["bybit"], on_book=lambda *_: None,
                stop_event=public_stop,
            )),
        ]

        guard = StrictQuantityGuard(prepared_sender, okx_ct_val=ct_val)
        runtime._synthetic_sender = guard

        for index, (spread_side, close_of) in enumerate(steps, start=1):
            if not session.is_ready() or session._handshake_count != 1:
                raise ExperimentStop("private_warm_disconnected_between_actions")
            okx_book, bybit_book = await _wait_books(runtime)
            is_close = spread_side == "close"
            okx_side = "buy" if is_close else "sell"
            bybit_side = "sell" if is_close else "buy"
            okx_px = _decimal(okx_book["ask_price"] if okx_side == "buy" else okx_book["bid_price"])
            bybit_px = _decimal(bybit_book["ask_price"] if bybit_side == "buy" else bybit_book["bid_price"])
            if min(okx_px, bybit_px) * EXPECTED_XRP < Decimal("5"):
                raise ExperimentStop("five_xrp_below_bybit_min_notional")
            coin_qty.TARGET_NOTIONAL_USD = EXPECTED_XRP * (okx_px + bybit_px) / Decimal("2")
            phase = "close" if is_close else "open"
            guard.arm(phase)
            intent_id = str(uuid.uuid4())
            signal_ts_ms = time.time_ns() // 1_000_000
            signal_mono_ns = time.monotonic_ns()
            action = {
                "index": index,
                "action": phase,
                "close_of": close_of,
                "intent_id": intent_id,
                "signal_ts_ms": signal_ts_ms,
                "signal_mono_ns": signal_mono_ns,
                "target_xrp_per_leg": "5",
            }
            actions.append(action)
            _append_jsonl(events_path, {"event": "synthetic_signal", **action})
            try:
                placed = await asyncio.to_thread(
                    runtime._synthetic_live_place,
                    base_coin="XRP", spread_side=spread_side, close_of=close_of,
                    signal_ts_ms=signal_ts_ms, intent_id=intent_id,
                    okx_book=okx_book, bybit_book=bybit_book, meta=meta,
                )
            except Exception as exc:  # noqa: BLE001 — avoid recording remote text
                _append_jsonl(events_path, {
                    "event": "place_exception", "index": index,
                    "intent_id": intent_id, "exception": type(exc).__name__,
                })
                raise ExperimentStop("place_exception_after_or_before_send") from exc
            if (
                getattr(placed, "abort", None) is not None
                or getattr(placed, "completed", False) is not True
                or getattr(placed, "keep_pending", False)
                or getattr(placed, "status", None) != ("closed" if is_close else "open")
            ):
                _append_jsonl(events_path, {
                    "event": "place_not_terminal_success", "index": index,
                    "intent_id": intent_id,
                    "abort": getattr(placed, "abort", None),
                    "completed": bool(getattr(placed, "completed", False)),
                    "keep_pending": bool(getattr(placed, "keep_pending", False)),
                    "status": getattr(placed, "status", None),
                })
                raise ExperimentStop("place_not_terminal_success")
            expected_state = await _wait_account_state(
                bybit_credentials=session.bybit_credentials,
                okx_credentials=session.okx_credentials,
                phase=phase,
            )
            bad_fill_price = (
                _decimal(getattr(placed, "okx_fill_px", "0") or "0") <= 0
                or _decimal(getattr(placed, "bybit_fill_px", "0") or "0") <= 0
            )
            record = {
                "event": "action_verified",
                "index": index,
                "action": phase,
                "intent_id": intent_id,
                "completed": True,
                "status": getattr(placed, "status", None),
                "latency_ms": getattr(placed, "latency_ms", None),
                "account_state": expected_state,
                "fill_price_field_anomaly": bad_fill_price,
                "dual_pairs_enqueued": guard.dual_pairs_enqueued,
                "sender_same_cached_instance": guard.used_prepared_sender(),
            }
            action.update({
                "completed": True,
                "status": getattr(placed, "status", None),
                "latency_ms": getattr(placed, "latency_ms", None),
                "account_state": expected_state,
                "fill_price_field_anomaly": bad_fill_price,
                "sender_same_cached_instance": guard.used_prepared_sender(),
            })
            _append_jsonl(events_path, record)
            if guard.dual_pairs_enqueued != index:
                raise ExperimentStop("dual_send_count_mismatch")

        outcome = f"{len(steps)}_actions_verified"
    except Exception as exc:  # noqa: BLE001 — bounded stop, no retry/cleanup order
        stop_reason = str(exc) if isinstance(exc, ExperimentStop) else type(exc).__name__
        if not isinstance(exc, ExperimentStop):
            stop_reason = "unexpected_" + type(exc).__name__
        _append_jsonl(events_path, {"event": "experiment_stop", "reason": stop_reason})
    finally:
        if session is not None:
            try:
                final_state = await asyncio.to_thread(
                    _exact_account_snapshot,
                    bybit_credentials=session.bybit_credentials,
                    okx_credentials=session.okx_credentials,
                )
                _append_jsonl(events_path, {"event": "final_account_state", **final_state})
            except Exception as exc:  # noqa: BLE001 — only safe type is persisted
                final_state = {"reconciliation_error": type(exc).__name__}
                _append_jsonl(events_path, {"event": "final_account_state", **final_state})
        public_stop.set()
        for task in public_tasks:
            task.cancel()
        if public_tasks:
            await asyncio.gather(*public_tasks, return_exceptions=True)
        runtime.stop_event.set()
        runtime._stop_synthetic_private_send()

    timing = _summarize_timings(runtime.data_root, actions)
    report = {
        "run_id": run_id,
        "status": outcome,
        "stop_reason": stop_reason,
        "venue": "Bybit live + OKX live",
        "scope": "synthetic signal → BotRuntime._synthetic_live_place → warm dual sender",
        "actions_requested": len(steps),
        "dual_pairs_enqueued": guard.dual_pairs_enqueued if guard else 0,
        "order_frames_enqueued": (guard.dual_pairs_enqueued * 2) if guard else 0,
        "quantity_per_leg_xrp": "5",
        "initial_account_state": initial_state if "initial_state" in locals() else None,
        "final_account_state": final_state,
        "actions": actions,
        "timing": timing,
    }
    _write_json_new(run_root / "report.json", report)
    print(json.dumps({
        "run_id": run_id,
        "status": outcome,
        "stop_reason": stop_reason,
        "dual_pairs_enqueued": report["dual_pairs_enqueued"],
        "timing_log_confirmed": timing.get("timing_log_confirmed", False),
        "report": str(run_root / "report.json"),
    }, sort_keys=True))
    if outcome != f"{len(steps)}_actions_verified" or not timing.get("timing_log_confirmed"):
        return 2
    return 0


async def _execute(args: argparse.Namespace) -> int:
    run_root_base = Path(args.run_root).resolve()
    if str(run_root_base) != str(REMOTE_BASE / "hotpath-six"):
        raise ExperimentStop("run_root_must_use_isolated_hotpath_directory")
    run_root = run_root_base / args.run_id
    lock_fd = os.open(PROCESS_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ExperimentStop("another_private_experiment_holds_scope_lock") from exc
        run_root.mkdir(mode=0o700, parents=True, exist_ok=False)
        repo_root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(repo_root))
        try:
            return await _run_experiment(args.run_id, run_root, repo_root, cycles=args.cycles)
        except Exception as exc:  # noqa: BLE001 — bounded preflight/startup failure
            reason = str(exc) if isinstance(exc, ExperimentStop) else "unexpected_" + type(exc).__name__
            report_path = run_root / "report.json"
            if not report_path.exists():
                _write_json_new(report_path, {
                    "run_id": args.run_id,
                    "status": "failed_before_or_during_runtime_start",
                    "stop_reason": reason,
                    "actions_requested": len(ACTION_CYCLE) * args.cycles,
                    "dual_pairs_enqueued": None,
                    "order_frames_enqueued": None,
                    "orders_may_have_been_sent": True,
                    "timing_log_confirmed": False,
                })
            print(json.dumps({
                "run_id": args.run_id,
                "status": "failed_before_or_during_runtime_start",
                "stop_reason": reason,
                "dual_pairs_enqueued": None,
                "report": str(report_path),
            }, sort_keys=True))
            return 2
    finally:
        os.close(lock_fd)


def _selftest() -> None:
    bybit_open = json.dumps({
        "op": "order.create", "header": {"X-BAPI-SIGN": "test-only"},
        "args": [{"category": "linear", "symbol": "XRPUSDT", "side": "Buy",
                  "orderType": "Market", "qty": "5"}],
    })
    okx_open = json.dumps({
        "op": "order", "args": [{"instId": "XRP-USDT-SWAP", "side": "sell",
                                    "ordType": "market", "tdMode": "cross",
                                    "sz": "0.05", "instIdCode": 1}],
    })
    _order_frame(venue="bybit", text=bybit_open, expected_phase="open", okx_ct_val=Decimal("100"))
    _order_frame(venue="okx", text=okx_open, expected_phase="open", okx_ct_val=Decimal("100"))
    bybit_close = json.dumps({
        "op": "order.create", "header": {"X-BAPI-SIGN": "test-only"},
        "args": [{"category": "linear", "symbol": "XRPUSDT", "side": "Sell",
                  "orderType": "Market", "qty": "5", "reduceOnly": True}],
    })
    okx_close = json.dumps({
        "op": "order", "args": [{"instId": "XRP-USDT-SWAP", "side": "buy",
                                    "ordType": "market", "tdMode": "cross",
                                    "sz": "0.05", "instIdCode": 1, "reduceOnly": True}],
    })
    _order_frame(venue="bybit", text=bybit_close, expected_phase="close", okx_ct_val=Decimal("100"))
    _order_frame(venue="okx", text=okx_close, expected_phase="close", okx_ct_val=Decimal("100"))
    bad = bybit_open.replace('"qty": "5"', '"qty": "6"')
    try:
        _order_frame(venue="bybit", text=bad, expected_phase="open", okx_ct_val=Decimal("100"))
    except ExperimentStop:
        pass
    else:
        raise AssertionError("qty guard accepted a non-5 XRP frame")

    class Delegate:
        calls = 0
        def enqueue_dual(self, **kwargs):
            self.calls += 1
            return kwargs.get("phase")
        def close(self):
            pass

    delegate = Delegate()
    guard = StrictQuantityGuard(delegate, okx_ct_val=Decimal("100"))
    guard.arm("open")
    assert guard.enqueue_dual(
        phase="open", bybit_text=bybit_open, okx_text=okx_open,
    ) == "open"
    try:
        guard.enqueue_dual(phase="open", bybit_text=bad, okx_text=okx_open)
    except ExperimentStop:
        pass
    else:
        raise AssertionError("dual guard delegated a non-5 XRP frame")
    assert delegate.calls == 1 and guard.dual_pairs_enqueued == 1
    assert len(STEPS) == 6 and sum(1 for side, _ in STEPS if side == "close") == 3
    print("quantity guard and fixed six-action schedule: PASS")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-live", action="store_true", help="required to arm the six live actions")
    parser.add_argument("--selftest", action="store_true", help="offline frame guard smoke test")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--run-root", default=str(REMOTE_BASE / "hotpath-six"))
    parser.add_argument("--cycles", type=int, choices=(1, 2, 3), default=DEFAULT_CYCLES)
    args = parser.parse_args()
    if args.selftest:
        _selftest()
        return 0
    if not args.execute_live:
        parser.error("live execution requires --execute-live")
    if args.run_id is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        args.run_id = f"hotpath-xrp-{stamp}-{uuid.uuid4().hex[:8]}"
    if not RUN_ID_RE.fullmatch(args.run_id):
        parser.error("run-id has invalid format")
    return asyncio.run(_execute(args))


if __name__ == "__main__":
    raise SystemExit(main())
