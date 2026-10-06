#!/usr/bin/env python3
"""Bounded Gear 2.3 public-only hot-add experiment; never loads secrets or sends orders."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
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


def _read_json_rows(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                if isinstance(item, dict):
                    rows.append(item)
        except OSError:
            continue
    return rows


def _read_metrics(data_root: Path, name: str, coins: set[str]) -> dict[str, int]:
    paths = sorted((data_root / name).glob("event_date=*/metrics.jsonl"))
    counts = {coin: 0 for coin in coins}
    for row in _read_json_rows(paths):
        coin = str(row.get("base_coin") or "").upper()
        if coin not in counts:
            continue
        if name == "tw_p50":
            observed = int(row.get("n_1m") or 0) > 0 or float(row.get("coverage_5m") or 0) > 0
        else:
            observed = all(
                isinstance(row.get(field), (int, float))
                and math.isfinite(float(row[field]))
                for field in ("floor_tf_select_a25", "theta_1m", "theta_5m")
            )
        if observed:
            counts[coin] += 1
    return counts


def _count_theta_rows(data_root: Path, coins: set[str]) -> dict[str, int]:
    counts = {coin: 0 for coin in coins}
    for row in _read_json_rows(
        sorted((data_root / "theta").glob("event_date=*/metrics.jsonl"))
    ):
        coin = str(row.get("base_coin") or "").upper()
        if coin in counts:
            counts[coin] += 1
    return counts


def _wait_for(
    predicate,
    *,
    label: str,
    proc: subprocess.Popen[bytes],
    deadline: float,
    progress: list[dict[str, str]],
) -> None:
    progress.append({"step": label, "state": "waiting"})
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"bot exited before {label}: rc={proc.returncode}")
        if predicate():
            progress.append({"step": label, "state": "passed"})
            return
        time.sleep(0.25)
    raise TimeoutError(f"timed out waiting for {label}")


def _stop(proc: subprocess.Popen[bytes]) -> int:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
    try:
        return int(proc.wait(timeout=20))
    except subprocess.TimeoutExpired:
        proc.kill()  # exact child PID started by this runner only
        return int(proc.wait(timeout=5))


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
        raise ValueError("experiment candidates must be outside the fixed Canary29 base")
    missing = set(coins) - set(source_rows)
    if missing:
        raise ValueError(f"candidates absent from read-only source delta: {sorted(missing)}")
    valid_rows = [dict(source_rows[coin]) for coin in coins]
    invalid_row = dict(valid_rows[0])
    invalid_row.update(
        {
            "base_coin": "G23BAD",
            "okx_symbol": "G23BAD-USDT-SWAP",
            "bybit_symbol": "G23BADUSDT",
            "bybit_qty_step": "0",
        }
    )

    data_root.mkdir(parents=True)
    delta_path = data_root / "manual_delta.csv"
    runtime_log = data_root / "gear23.log"
    console_log = data_root / "console.log"
    write_delta_atomic(delta_path, [])

    clean_env = {
        "PATH": "/root/venv/bin:/usr/bin:/bin",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "PYTHONUNBUFFERED": "1",
        "VENUE": "testnet",
        "LIVE_ORDERS": "0",
        "BBOT_MODE": "probe",
        "BBOT_PROFILE": "gear22_would_send",
        "BBOT_BROKER": "stub",
        "BBOT_THETA_EXECUTION": "inline",
        "BBOT_THETA_LIVE_SEND": "0",
        "BBOT_THETA_TRADE": "0",
        "BBOT_COINS": ",".join(CANARY29_COINS),
        "BBOT_DATA_ROOT": str(data_root),
        "BBOT_LOG_PATH": str(runtime_log),
        "BBOT_HOT_ADD": "1",
        "BBOT_HOT_ADD_DELTA": str(delta_path),
        "BBOT_HOT_ADD_MAX_EXTRA": "48",
        "BBOT_HOT_ADD_POLL_SEC": "5",
        "BBOT_HOT_ADD_WARM": "1",
        "BBOT_HOT_ADD_HISTORY_ROOT": args.history_root,
        "BBOT_FLOOR_WATCH": "1",
        "BBOT_TW_P50_WATCH": "1",
        "BBOT_THETA_WATCH": "1",
        "BBOT_L1_RING": "0",
        "BBOT_CHRONOMETRY": "0",
    }
    summary: dict[str, Any] = {
        "data_root": str(data_root),
        "source_delta": str(source),
        "base_coin_count": len(CANARY29_COINS),
        "test_coins": coins,
        "invalid_test_coin": "G23BAD",
        "private_send": False,
        "exchange_orders": False,
        "poll_sec": 5,
        "progress": [],
    }
    proc: subprocess.Popen[bytes] | None = None
    phase = "startup"
    try:
        with console_log.open("wb") as out:
            proc = subprocess.Popen(
                [sys.executable, "validation/gear23_public_stub_child.py"],
                cwd=Path(__file__).resolve().parents[1],
                env=clean_env,
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

            first = coins[0]

            def subscribed(coin: str) -> bool:
                text = log_text()
                return all(
                    f"ws_subscribe_ok | coin={coin} | exchange={venue}" in text
                    for venue in ("okx", "bybit")
                )

            def tw_seen(coin: str) -> bool:
                return _read_metrics(data_root, "tw_p50", {coin})[coin] > 0

            def theta_seen(coin: str) -> bool:
                return _count_theta_rows(data_root, {coin})[coin] > 0

            def base_public_ready() -> bool:
                text = log_text()
                has_base_pair_ack = any(
                    all(
                        f"ws_subscribe_ok | coin={coin} | exchange={venue}" in text
                        for venue in ("okx", "bybit")
                    )
                    for coin in CANARY29_COINS
                )
                has_accepted_books = any(
                    (match := re.search(r"heartbeat \|[^\n]*accepted=(\d+)", line))
                    and int(match.group(1)) > 0
                    for line in text.splitlines()
                )
                return has_base_pair_ack and has_accepted_books

            phase = "base public runtime readiness"
            _wait_for(
                lambda: "bbot_start |" in log_text()
                and "private_warm_skipped | live_private_send=false" in log_text()
                and "hot_add_delta_read |" in log_text()
                and "validation_no_orders | callbacks=probe,policy" in log_text()
                and base_public_ready(),
                label="base public runtime readiness before manual add",
                proc=proc,
                deadline=deadline,
                progress=summary["progress"],
            )

            # First external manual addition happens only after the base pool is live.
            phase = f"first candidate {first} public subscribe ACKs"
            write_delta_atomic(delta_path, [valid_rows[0]])

            _wait_for(
                lambda: f"bbot_hot_add_applied | base_coin={first}" in log_text()
                and subscribed(first),
                label=f"first candidate {first} public subscribe ACKs",
                proc=proc,
                deadline=deadline,
                progress=summary["progress"],
            )
            phase = f"first candidate {first} warm result and accepted TW tick"
            _wait_for(
                lambda: "private_warm_skipped | live_private_send=false" in log_text()
                and (
                    f"gear23_observer_warm | base_coin={first}" in log_text()
                    or f"gear23_observer_warm_failed | base_coin={first}" in log_text()
                )
                and tw_seen(first),
                label=f"first candidate {first} warm result and accepted TW tick",
                proc=proc,
                deadline=deadline,
                progress=summary["progress"],
            )
            phase = f"first candidate {first} theta row"
            _wait_for(
                lambda: theta_seen(first),
                label=f"first candidate {first} theta row",
                proc=proc,
                deadline=deadline,
                progress=summary["progress"],
            )

            # Re-write identical snapshot: production parser must not spawn a duplicate.
            phase = "duplicate snapshot no-op"
            duplicate_log_offset = len(log_text())
            write_delta_atomic(delta_path, [valid_rows[0]])
            _wait_for(
                lambda: f"rows=1 | added=0 | extra=1" in log_text()[duplicate_log_offset:],
                label="duplicate snapshot no-op",
                proc=proc,
                deadline=deadline,
                progress=summary["progress"],
            )

            # Cumulative snapshot adds two more real metadata rows.
            phase = "two additional candidates public subscribe ACKs"
            write_delta_atomic(delta_path, valid_rows)
            _wait_for(
                lambda: all(
                    f"bbot_hot_add_applied | base_coin={coin}" in log_text()
                    and subscribed(coin)
                    for coin in coins[1:]
                ),
                label="two additional candidates public subscribe ACKs",
                proc=proc,
                deadline=deadline,
                progress=summary["progress"],
            )
            phase = "additional candidates warm results and accepted TW ticks"
            _wait_for(
                lambda: all(tw_seen(coin) for coin in coins[1:])
                and all(
                    f"gear23_observer_warm | base_coin={coin}" in log_text()
                    or f"gear23_observer_warm_failed | base_coin={coin}" in log_text()
                    for coin in coins[1:]
                ),
                label="additional candidates warm results and accepted TW ticks",
                proc=proc,
                deadline=deadline,
                progress=summary["progress"],
            )
            for coin in coins[1:]:
                phase = f"candidate {coin} theta row"
                _wait_for(
                    lambda coin=coin: theta_seen(coin),
                    label=f"candidate {coin} usable theta row",
                    proc=proc,
                    deadline=deadline,
                    progress=summary["progress"],
                )

            # Invalid instrument metadata remains in the snapshot but is fail-closed.
            phase = "invalid metadata rejection"
            write_delta_atomic(delta_path, [*valid_rows, invalid_row])
            _wait_for(
                lambda: "bbot_hot_add_fail_closed | base_coin=G23BAD" in log_text(),
                label="invalid metadata rejection",
                proc=proc,
                deadline=deadline,
                progress=summary["progress"],
            )
            text = log_text()
            warm_ok = {
                coin: f"bbot_hot_add_warm_ok | base_coin={coin} | ok=true" in text
                for coin in coins
            }
            summary.update(
                {
                    "public_subscribe_ack": {
                        coin: {venue: f"ws_subscribe_ok | coin={coin} | exchange={venue}" in text
                               for venue in ("okx", "bybit")}
                        for coin in coins
                    },
                    "accepted_tw_rows": _read_metrics(data_root, "tw_p50", set(coins)),
                    "theta_rows": _count_theta_rows(data_root, set(coins)),
                    "usable_theta_rows": _read_metrics(data_root, "theta", set(coins)),
                    "warm_results": {
                        coin: (
                            "ok" if warm_ok[coin]
                            else next(
                                (
                                    line.split("reason=", 1)[1].split(" |", 1)[0]
                                    for line in text.splitlines()
                                    if f"gear23_observer_warm | base_coin={coin}" in line
                                ),
                                "warm_result_missing",
                            )
                        )
                        for coin in coins
                    },
                    "floor_warm_ok_coins": [coin for coin, ok in warm_ok.items() if ok],
                    "added_once": {
                        coin: text.count(f"gear23_coin_added | base_coin={coin}") == 1
                        for coin in coins
                    },
                    "invalid_metadata_rejected": "bbot_hot_add_fail_closed | base_coin=G23BAD" in text,
                    "invalid_candidate_not_added": not any(
                        "gear23_coin_added | base_coin=G23BAD" in line
                        for line in text.splitlines()
                    ),
                    "private_warm_skipped": "private_warm_skipped | live_private_send=false" in text,
                    "validation_no_orders": "validation_no_orders | callbacks=probe,policy" in text,
                    "simulated_order_entry_events": sum(
                        text.count(marker)
                        for marker in ("probe_placed |", "stub_broker | would_send")
                    ),
                    "simulated_trade_journal_rows": sum(
                        len(path.read_text(encoding="utf-8").splitlines())
                        for path in (data_root / "journal").glob("**/legs.jsonl")
                    ),
                    "trade_eligibility_logs": {
                        coin: any(
                            f"gear23_coin_added | base_coin={coin}" in line
                            and "trade_eligible=false" in line
                            for line in text.splitlines()
                        )
                        for coin in coins
                    },
                    "theta_trade_journal_files": len(
                        list((data_root / "theta_trades").glob("**/*.jsonl"))
                    ),
                }
            )
    except Exception as exc:
        summary["error"] = f"{phase}: {type(exc).__name__}: {exc}"
        summary["progress"].append({"step": phase, "state": "failed"})
    finally:
        if proc is not None:
            summary["exit_code"] = _stop(proc)
            final_log = runtime_log.read_text(encoding="utf-8", errors="replace") if runtime_log.exists() else ""
            summary["sigterm_seen"] = "bbot_signal_received | signal=SIGTERM" in final_log
            summary["supervised_tasks_drained"] = "gear23_tasks_drained |" in final_log
            summary["pass"] = all(
                [
                    summary["exit_code"] == 0,
                    summary["sigterm_seen"],
                    summary["supervised_tasks_drained"],
                    summary.get("invalid_metadata_rejected", False),
                    summary.get("invalid_candidate_not_added", False),
                    summary.get("private_warm_skipped", False),
                    summary.get("validation_no_orders", False),
                    summary.get("simulated_order_entry_events") == 0,
                    summary.get("simulated_trade_journal_rows") == 0,
                    summary.get("theta_trade_journal_files") == 0,
                    all(summary.get("added_once", {}).values()),
                    all(
                        venue_ok
                        for per_coin in summary.get("public_subscribe_ack", {}).values()
                        for venue_ok in per_coin.values()
                    ),
                    all(summary.get("trade_eligibility_logs", {}).values()),
                    all(count > 0 for count in summary.get("accepted_tw_rows", {}).values()),
                    all(count > 0 for count in summary.get("theta_rows", {}).values()),
                    any(
                        summary.get("warm_results", {}).get(coin) == "ok"
                        and summary.get("usable_theta_rows", {}).get(coin, 0) > 0
                        for coin in coins
                    ),
                ]
            )
            summary["status"] = "PASS" if summary["pass"] else "PARTIAL"
        result_path = data_root / "experiment-summary.json"
        result_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument(
        "--source-delta",
        default="/data/bbot-would-send-prod/hot_add_delta.csv",
        help="read-only active would-send cumulative snapshot used only for real metadata",
    )
    parser.add_argument("--coins", default="CT,AEON,ARX", help="three source-delta candidates")
    parser.add_argument(
        "--history-root",
        default="/data/bbot-would-send-prod-history",
        help="existing would-send history, read-only source",
    )
    parser.add_argument("--timeout-sec", type=int, default=180)
    args = parser.parse_args()
    if not 30 <= args.timeout_sec <= 300:
        parser.error("--timeout-sec must be between 30 and 300")
    summary = run(args)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary.get("pass") else (2 if summary.get("status") == "PARTIAL" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
