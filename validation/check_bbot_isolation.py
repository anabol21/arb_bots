#!/usr/bin/env python3
"""Read-only validation of B-bot unit, journal, and D-tree isolation.

This check is intentionally observational: it never starts, restarts, enables,
or otherwise changes a service, log, or data file.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


D_TREES = (Path("/data/live"), Path("/data/bars"), Path("/data/compacted"), Path("/data/spool"))
REQUIRED_FIELDS: dict[str, type | tuple[type, ...]] = {
    "schema_version": str,
    "intent_id": str,
    "base_coin": str,
    "exchange": str,
    "leg_side": str,
    "spread_side": str,
    "event_date": str,
    "signal_ts_ms": (int, float),
    "place_ts_ms": (int, float),
    "ack_ts_ms": (int, float),
    "fill_ts_ms": (int, float, type(None)),
    "Trade_Lat_ms": (int, float),
    "signal_price": (int, float),
    "fill_price": (int, float, type(None)),
    "qty": (int, float),
    "notional": (int, float),
    "fee": (int, float),
    "tick_valid": bool,
    "suppress_reason": (str, type(None)),
    "status": str,
    "abort_reason": (str, type(None)),
    "would_send": bool,
    "send": bool,
    "k_live": (int, float),
}
SENSITIVE_PATTERN = re.compile(
    r"api[\s_-]?key|order[\s_-]?id|private[\s_-]?(?:url|endpoint)",
    re.IGNORECASE,
)


def add_error(errors: list[str], message: str) -> None:
    errors.append(message)
    print(f"ERROR: {message}")


def numeric(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def matches_required_type(value: object, expected_type: type | tuple[type, ...]) -> bool:
    if not isinstance(value, expected_type):
        return False
    if isinstance(value, bool) and bool not in (
        expected_type if isinstance(expected_type, tuple) else (expected_type,)
    ):
        return False
    return True


def run_systemctl(args: list[str]) -> tuple[int, str]:
    completed = subprocess.run(
        ["systemctl", *args],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    output = (completed.stdout + completed.stderr).strip()
    return completed.returncode, output


def systemd_is_usable() -> bool:
    if shutil.which("systemctl") is None:
        return False
    try:
        _, output = run_systemctl(["is-system-running"])
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "System has not been booted with systemd" not in output


def show_unit(name: str) -> tuple[bool, dict[str, str]]:
    _, state = run_systemctl(["is-active", name])
    _, shown = run_systemctl(
        ["show", name, "--property=MainPID,NRestarts,ActiveEnterTimestamp,LoadState"]
    )
    fields: dict[str, str] = {}
    for line in shown.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value
    present = fields.get("LoadState") not in (None, "not-found")
    print(
        f"{name}: active={state or 'unknown'} "
        f"MainPID={fields.get('MainPID', 'unknown')} "
        f"NRestarts={fields.get('NRestarts', 'unknown')} "
        f"ActiveEnterTimestamp={fields.get('ActiveEnterTimestamp', 'unknown')}"
    )
    return present, {"active": state, **fields}


def validate_bbot_log_path(errors: list[str]) -> None:
    _, unit_text = run_systemctl(["cat", "spread-bbot.service"])
    if "runtime.log" in unit_text:
        add_error(errors, "spread-bbot.service references forbidden runtime.log")
    elif "/var/log/spread/bbot.log" in unit_text:
        print("bbot_log_path: /var/log/spread/bbot.log (ok)")
    else:
        add_error(errors, "spread-bbot.service does not confirm /var/log/spread/bbot.log")


def find_bbot_in_d_trees(errors: list[str]) -> int:
    matches: list[Path] = []
    for tree in D_TREES:
        if not tree.exists():
            print(f"d_tree: {tree} absent (no bbot names found)")
            continue
        try:
            tree_matches = [path for path in tree.rglob("*") if "bbot" in path.name.lower()]
        except OSError as exc:
            add_error(errors, f"cannot inspect D tree {tree}: {exc}")
            continue
        matches.extend(tree_matches)
    for path in matches:
        print(f"forbidden_d_tree_match: {path}")
    print(f"bbot_names_in_d_trees={len(matches)}")
    if matches:
        add_error(errors, "B-bot-named paths exist in D trees")
    return len(matches)


def find_sensitive_strings(value: Any, location: str, errors: list[str]) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if SENSITIVE_PATTERN.search(str(key)):
                add_error(errors, f"{location}: forbidden sensitive field name {key!r}")
            find_sensitive_strings(nested, f"{location}.{key}", errors)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            find_sensitive_strings(nested, f"{location}[{index}]", errors)
    elif isinstance(value, str) and SENSITIVE_PATTERN.search(value):
        add_error(errors, f"{location}: forbidden API-key/order-id/private-url pattern")


def validate_record(record: dict[str, Any], location: str, partition_date: str, errors: list[str]) -> None:
    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in record:
            add_error(errors, f"{location}: missing required field {field}")
        elif not matches_required_type(record[field], expected_type):
            add_error(errors, f"{location}: invalid type for {field}")

    if record.get("schema_version") != "bbot.journal.v0":
        add_error(errors, f"{location}: schema_version must be bbot.journal.v0")
    if record.get("exchange") not in ("okx", "bybit"):
        add_error(errors, f"{location}: exchange must be okx or bybit")
    if record.get("leg_side") not in ("buy", "sell"):
        add_error(errors, f"{location}: invalid leg_side")
    if record.get("spread_side") not in ("open_long", "open_short", "close"):
        add_error(errors, f"{location}: invalid spread_side")
    if record.get("status") not in ("filled", "aborted"):
        add_error(errors, f"{location}: status must be filled or aborted")
    if record.get("would_send") is not True:
        add_error(errors, f"{location}: would_send must be true")
    if record.get("send") is not False:
        add_error(errors, f"{location}: send must be false")
    if record.get("k_live") != 1:
        add_error(errors, f"{location}: k_live must equal 1")

    if numeric(record.get("signal_ts_ms")):
        utc_date = datetime.fromtimestamp(record["signal_ts_ms"] / 1000, tz=timezone.utc).date().isoformat()
        if record.get("event_date") != utc_date:
            add_error(errors, f"{location}: event_date does not match UTC signal_ts_ms")
        if partition_date != utc_date:
            add_error(errors, f"{location}: partition date does not match UTC signal_ts_ms")
    if numeric(record.get("place_ts_ms")) and numeric(record.get("signal_ts_ms")):
        if record["place_ts_ms"] < record["signal_ts_ms"]:
            add_error(errors, f"{location}: place_ts_ms precedes signal_ts_ms")

    filled = record.get("status") == "filled"
    if record.get("tick_valid") is not filled:
        add_error(errors, f"{location}: tick_valid must be true iff status is filled")
    if filled:
        if not numeric(record.get("fill_ts_ms")) or not numeric(record.get("fill_price")):
            add_error(errors, f"{location}: filled leg requires fill_ts_ms and fill_price")
        elif numeric(record.get("signal_ts_ms")) and numeric(record.get("Trade_Lat_ms")):
            if record["fill_ts_ms"] < record["signal_ts_ms"] + record["Trade_Lat_ms"]:
                add_error(errors, f"{location}: fill_ts_ms violates Trade_Lat_ms")
    elif record.get("fill_ts_ms") is not None or record.get("fill_price") is not None:
        add_error(errors, f"{location}: aborted leg must have null fill_ts_ms and fill_price")

    find_sensitive_strings(record, location, errors)


def journal_files(data_root: Path, errors: list[str]) -> list[tuple[Path, str]]:
    root = data_root / "journal"
    if not root.exists():
        print(f"journal: absent ({root})")
        return []
    if not root.is_dir():
        add_error(errors, f"journal path is not a directory: {root}")
        return []

    files: list[tuple[Path, str]] = []
    try:
        paths = list(root.rglob("*"))
    except OSError as exc:
        add_error(errors, f"cannot inspect journal root {root}: {exc}")
        return []
    for path in paths:
        if path.is_dir():
            continue
        if path.is_symlink():
            add_error(errors, f"journal must not use a symlink: {path}")
            continue
        relative = path.relative_to(root)
        if len(relative.parts) != 2 or path.name != "legs.jsonl":
            add_error(errors, f"journal file outside contract layout: {path}")
            continue
        partition = relative.parts[0]
        match = re.fullmatch(r"event_date=(\d{4}-\d{2}-\d{2})", partition)
        if not match:
            add_error(errors, f"journal file has invalid partition path: {path}")
            continue
        files.append((path, match.group(1)))
    print(f"journal_files={len(files)} under {root}")
    return files


def validate_journal(data_root: Path, errors: list[str]) -> bool:
    files = journal_files(data_root, errors)
    if not files:
        return False

    by_intent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    truncated_last_lines = 0
    for path, partition_date in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            add_error(errors, f"cannot read journal file {path}: {exc}")
            continue
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                add_error(errors, f"{path}:{line_number}: blank JSONL line")
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                if line_number == len(lines):
                    truncated_last_lines += 1
                    add_error(errors, f"{path}:{line_number}: truncated last JSONL line skipped ({exc.msg})")
                else:
                    add_error(errors, f"{path}:{line_number}: invalid JSONL ({exc.msg})")
                continue
            if not isinstance(record, dict):
                add_error(errors, f"{path}:{line_number}: JSONL value must be an object")
                continue
            location = f"{path}:{line_number}"
            validate_record(record, location, partition_date, errors)
            if isinstance(record.get("intent_id"), str):
                by_intent[record["intent_id"]].append(record)

    for intent_id, legs in by_intent.items():
        if len(legs) != 2:
            add_error(errors, f"intent {intent_id!r}: expected two legs, found {len(legs)}")
            continue
        exchanges = [leg.get("exchange") for leg in legs]
        if set(exchanges) != {"okx", "bybit"} or len(set(exchanges)) != 2:
            add_error(errors, f"intent {intent_id!r}: legs must be one okx and one bybit")
        if legs[0].get("notional") != legs[1].get("notional"):
            add_error(errors, f"intent {intent_id!r}: leg notionals differ")
    print(f"journal_intents={len(by_intent)} truncated_last_lines={truncated_last_lines}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path("/data/bbot"))
    parser.add_argument("--dry-run", action="store_true", help="local filesystem-only validation")
    args = parser.parse_args()

    errors: list[str] = []
    environment = "local_dry_run" if args.dry_run else "vps_systemd" if systemd_is_usable() else "not_vps"
    print(f"environment={environment}")
    print(f"data_root={args.data_root}")
    print("runtime_log_expected=/var/log/spread/bbot.log (must not be runtime.log)")
    print("first_materialization_and_durable_destination=data_root/journal/event_date=YYYY-MM-DD/legs.jsonl")

    if environment == "vps_systemd":
        _, collector = show_unit("spread-collector.service")
        if collector.get("active") != "active":
            add_error(errors, "collector is not active; isolation verdict is refused")
        bbot_present, _ = show_unit("spread-bbot.service")
        if bbot_present:
            validate_bbot_log_path(errors)
        else:
            print("spread-bbot.service: absent")
    else:
        print("collector: skipped (not_vps)")
        print("spread-bbot: skipped (not_vps)")

    find_bbot_in_d_trees(errors)
    journal_present = validate_journal(args.data_root, errors)

    if errors:
        print(f"verdict=FAIL errors={len(errors)}")
        print("not_proven=collector p99 latency/interference, mount-failure recovery, restart behavior")
        return 1
    verdict = "isolation_ok_journal_present" if journal_present else "isolation_ok_journal_absent"
    print(f"verdict={verdict}")
    print("not_proven=collector p99 latency/interference, mount-failure recovery, restart behavior")
    return 0


if __name__ == "__main__":
    sys.exit(main())
