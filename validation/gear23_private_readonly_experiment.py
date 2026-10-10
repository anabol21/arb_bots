#!/usr/bin/env python3
"""Bounded live-private readiness test; WS order/cancel and REST writes blocked."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.bot.synthetic_policy import CANARY29_COINS
from app.utils.universe_delta import read_delta_rows, write_delta_atomic
from validation.gear23_public_stub_experiment import (
    _count_theta_rows,
    _read_metrics,
    _wait_for,
    _stop,
)


def run(args: argparse.Namespace) -> dict[str, Any]:
    data_root = Path(args.data_root).resolve()
    if data_root.exists():
        raise FileExistsError(f"experiment data root must be new: {data_root}")
    source = Path(args.source_delta)
    source_rows = {row["base_coin"].upper(): row for row in read_delta_rows(source)}
    coins = [item.strip().upper() for item in args.coins.split(",") if item.strip()]
    if len(coins) != 3 or len(set(coins)) != 3:
        raise ValueError("--coins must name exactly three distinct symbols")
    if set(coins) & set(CANARY29_COINS):
        raise ValueError("experiment candidates must be outside the fixed base")
    missing = set(coins) - set(source_rows)
    if missing:
        raise ValueError(f"candidates absent from read-only source delta: {sorted(missing)}")
    rows = [dict(source_rows[coin]) for coin in coins]
    invalid = dict(rows[0])
    invalid.update(
        base_coin="G23BAD",
        okx_symbol="G23BAD-USDT-SWAP",
        bybit_symbol="G23BADUSDT",
        bybit_qty_step="0",
    )

    data_root.mkdir(parents=True)
    delta = data_root / "manual_delta.csv"
    runtime_log = data_root / "gear23.log"
    console_log = data_root / "console.log"
    write_delta_atomic(delta, [])
    env = {
        "PATH": "/root/venv/bin:/usr/bin:/bin",
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONUNBUFFERED": "1",
        "VENUE": "testnet",
        "LIVE_ORDERS": "0",
        "BBOT_MODE": "probe",
        "BBOT_PROFILE": "gear22_would_send",
        "BBOT_BROKER": "stub",
        "BBOT_THETA_EXECUTION": "inline",
        "BBOT_THETA_LIVE_SEND": "0",
        "BBOT_THETA_TRADE": "0",
        "BBOT_PRIVATE_ENV_FILE": "/etc/spread/bbot-private-live.env",
        "BBOT_COINS": ",".join(CANARY29_COINS),
        "BBOT_DATA_ROOT": str(data_root),
        "BBOT_LOG_PATH": str(runtime_log),
        "BBOT_HOT_ADD": "1",
        "BBOT_HOT_ADD_DELTA": str(delta),
        "BBOT_HOT_ADD_MAX_EXTRA": "48",
        "BBOT_HOT_ADD_POLL_SEC": "5",
        "BBOT_HOT_ADD_WARM": "1",
        "BBOT_HOT_ADD_HISTORY_ROOT": args.history_root,
        "BBOT_FLOOR_WATCH": "1",
        "BBOT_TW_P50_WATCH": "1",
        "BBOT_THETA_WATCH": "1",
        "BBOT_L1_RING": "0",
        "BBOT_CHRONOMETRY": "0",
        "BBOT_CONFIRMED_1X_COINS": "",
    }
    summary: dict[str, Any] = {
        "data_root": str(data_root),
        "source_delta": str(source),
        "base_coin_count": len(CANARY29_COINS),
        "test_coins": coins,
        "invalid_test_coin": "G23BAD",
        "private_session": "live readonly warm session with mandatory startup REST reseed",
        "entry_manager": "disabled",
        "order_cancel_ws": "blocked at socket send",
        "rest_mutations": "no mutating transport is constructed; reseed GET allowlist enforced",
        "orders_or_settings_changes": False,
        "confirmed_1x_for_extras": False,
        "progress": [],
    }
    proc: subprocess.Popen[bytes] | None = None
    phase = "startup"
    try:
        with console_log.open("wb") as out:
            proc = subprocess.Popen(
                [sys.executable, "validation/gear23_private_readonly_child.py"],
                cwd=REPO_ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
            )
            deadline = time.monotonic() + args.timeout_sec

            def log_text() -> str:
                try:
                    return runtime_log.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return ""

            def subscribed(coin: str) -> bool:
                text = log_text()
                return all(
                    f"ws_subscribe_ok | coin={coin} | exchange={venue}" in text
                    for venue in ("okx", "bybit")
                )

            def warmed(coin: str) -> bool:
                return (
                    f"gear23_observer_warm | base_coin={coin} | ok=true" in log_text()
                    and _read_metrics(data_root, "tw_p50", {coin})[coin] > 0
                    and _count_theta_rows(data_root, {coin})[coin] > 0
                )

            def private_ready_and_blocked_by_1x(coin: str) -> bool:
                text = log_text()
                return (
                    f"validation_private_coin_ready | base_coin={coin} | ready=true" in text
                    and f"gear23_candidate_gate | base_coin={coin} | status=blocked | reason=one_x_unconfirmed" in text
                )

            def base_public_ready() -> bool:
                text = log_text()
                return (
                    any(
                        all(
                            f"ws_subscribe_ok | coin={coin} | exchange={venue}" in text
                            for venue in ("okx", "bybit")
                        )
                        for coin in CANARY29_COINS
                    )
                    and any(
                        (match := re.search(r"heartbeat \|[^\n]*accepted=(\d+)", line))
                        and int(match.group(1)) > 0
                        for line in text.splitlines()
                    )
                )

            phase = "base public/private readiness"
            _wait_for(
                lambda: "bbot_start |" in log_text()
                and "validation_private_warm_injected | ready=true" in log_text()
                and "validation_no_orders | callbacks=probe,policy" in log_text()
                and base_public_ready(),
                label="base runtime with readonly private warm session",
                proc=proc,
                deadline=deadline,
                progress=summary["progress"],
            )

            first = coins[0]
            phase = f"first candidate {first}: public, warm, private ACK and 1x block"
            write_delta_atomic(delta, [rows[0]])
            _wait_for(
                lambda: f"bbot_hot_add_applied | base_coin={first}" in log_text()
                and subscribed(first)
                and warmed(first)
                and private_ready_and_blocked_by_1x(first),
                label=phase,
                proc=proc,
                deadline=deadline,
                progress=summary["progress"],
            )

            phase = "duplicate snapshot no-op"
            offset = len(log_text())
            write_delta_atomic(delta, [rows[0]])
            _wait_for(
                lambda: "rows=1 | added=0 | extra=1" in log_text()[offset:]
                and log_text()[offset:].count(f"gear23_coin_added | base_coin={first}") == 0,
                label=phase,
                proc=proc,
                deadline=deadline,
                progress=summary["progress"],
            )

            phase = "two more additions with current private ACKs and 1x block"
            write_delta_atomic(delta, rows)
            _wait_for(
                lambda: all(
                    f"bbot_hot_add_applied | base_coin={coin}" in log_text()
                    and subscribed(coin)
                    and warmed(coin)
                    and private_ready_and_blocked_by_1x(coin)
                    for coin in coins[1:]
                ),
                label=phase,
                proc=proc,
                deadline=deadline,
                progress=summary["progress"],
            )

            phase = "invalid metadata rejection"
            write_delta_atomic(delta, [*rows, invalid])
            _wait_for(
                lambda: "bbot_hot_add_fail_closed | base_coin=G23BAD" in log_text(),
                label=phase,
                proc=proc,
                deadline=deadline,
                progress=summary["progress"],
            )
            text = log_text()
            summary.update(
                {
                    "public_subscription_ack": {
                        coin: {venue: f"ws_subscribe_ok | coin={coin} | exchange={venue}" in text
                               for venue in ("okx", "bybit")}
                        for coin in coins
                    },
                    "private_coin_ready": {
                        coin: f"validation_private_coin_ready | base_coin={coin} | ready=true" in text
                        for coin in coins
                    },
                    "blocked_by_missing_1x": {
                        coin: any(
                            f"gear23_candidate_gate | base_coin={coin} | status=blocked | reason=one_x_unconfirmed" in line
                            for line in text.splitlines()
                        )
                        for coin in coins
                    },
                    "accepted_tw_rows": _read_metrics(data_root, "tw_p50", set(coins)),
                    "theta_rows": _count_theta_rows(data_root, set(coins)),
                    "warm_ok": {
                        coin: f"gear23_observer_warm | base_coin={coin} | ok=true" in text
                        for coin in coins
                    },
                    "added_once": {
                        coin: text.count(f"gear23_coin_added | base_coin={coin}") == 1
                        for coin in coins
                    },
                    "invalid_metadata_rejected": "bbot_hot_add_fail_closed | base_coin=G23BAD" in text,
                    "invalid_candidate_not_added": "gear23_coin_added | base_coin=G23BAD" not in text,
                    "candidate_eligible_logs": {
                        coin: f"gear23_candidate_gate | base_coin={coin} | status=eligible" in text
                        for coin in coins
                    },
                }
            )
    except Exception as exc:
        summary["error"] = f"{phase}: {type(exc).__name__}: {exc}"
        summary["progress"].append({"step": phase, "state": "failed"})
    finally:
        if proc is not None:
            summary["exit_code"] = _stop(proc)
            text = runtime_log.read_text(encoding="utf-8", errors="replace") if runtime_log.exists() else ""
            summary["readonly_runtime_summary"] = next(
                (line for line in text.splitlines() if "validation_readonly_summary |" in line),
                "missing",
            )
            summary["private_runtime_summary"] = next(
                (line for line in text.splitlines() if "validation_private_summary |" in line),
                "missing",
            )
            summary["sigterm_seen"] = "bbot_signal_received | signal=SIGTERM" in text
            summary["supervised_tasks_drained"] = "gear23_tasks_drained |" in text
            summary["pass"] = all(
                [
                    "error" not in summary,
                    summary["exit_code"] == 0,
                    summary["sigterm_seen"],
                    summary["supervised_tasks_drained"],
                    summary.get("invalid_metadata_rejected", False),
                    summary.get("invalid_candidate_not_added", False),
                    all(summary.get("added_once", {}).values()),
                    all(all(v.values()) for v in summary.get("public_subscription_ack", {}).values()),
                    all(summary.get("private_coin_ready", {}).values()),
                    all(summary.get("blocked_by_missing_1x", {}).values()),
                    not any(summary.get("candidate_eligible_logs", {}).values()),
                    all(count > 0 for count in summary.get("accepted_tw_rows", {}).values()),
                    all(count > 0 for count in summary.get("theta_rows", {}).values()),
                    set(summary.get("private_coin_ready", {})) == set(summary["test_coins"]),
                    set(summary.get("blocked_by_missing_1x", {})) == set(summary["test_coins"]),
                    set(summary.get("public_subscription_ack", {})) == set(summary["test_coins"]),
                    "blocked_ws=0 | blocked_rest=0" in summary.get("readonly_runtime_summary", ""),
                    "same_sockets=true | same_auth_generation=true" in summary.get("private_runtime_summary", ""),
                    "stub_place_attempts=0" in summary.get("readonly_runtime_summary", ""),
                ]
            )
            summary["status"] = "PASS" if summary["pass"] else "PARTIAL"
        (data_root / "experiment-summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument(
        "--source-delta", default="/data/bbot-would-send-prod/hot_add_delta.csv",
        help="read-only active would-send cumulative snapshot used only for real metadata",
    )
    parser.add_argument("--coins", default="CT,AEON,ARX", help="three source-delta candidates")
    parser.add_argument(
        "--history-root", default="/data/bbot-would-send-prod-history",
        help="existing would-send history read-only source",
    )
    parser.add_argument("--timeout-sec", type=int, default=240)
    args = parser.parse_args()
    if not 30 <= args.timeout_sec <= 360:
        parser.error("--timeout-sec must be between 30 and 360")
    summary = run(args)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
