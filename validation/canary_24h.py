"""24h production-path canary launcher + daily accounting helper.

Launches (or attaches evidence for) a long-running collector on production
paths and periodically accounts:
  manifest rows == local consolidated rows == remote confirmed rows

This script is intentionally operator-driven: a full 24h wall-clock canary
cannot always finish inside one agent session. It writes a status file that
clearly states launched / running / complete / failed.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq


PYTHON = "/root/venv/bin/python"
CODE_ROOT = Path("/root/spread_staging")
RCLONE = "/opt/rclone-1.74.4/rclone"
KEY = "/root/.ssh/id_ed25519_uploader"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def emit(log_path: Path, event: str, **fields: Any) -> None:
    payload = {"timestamp": time.time(), "event": event, **fields}
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def account(
    *,
    compacted: Path,
    remote: str,
    remote_path: str,
) -> dict[str, Any]:
    state = compacted / ".state"
    sent = compacted / "sent"
    manifest_rows = 0
    local_rows = 0
    missing_local: list[str] = []
    for manifest_path in sorted(state.glob("spread_*.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("status") != "complete":
            continue
        manifest_rows += int(manifest["total_rows"])
        name = str(manifest["output"])
        candidates = [compacted / name, sent / name]
        output = next((path for path in candidates if path.is_file()), None)
        if output is None:
            missing_local.append(name)
            continue
        local_rows += int(pq.ParquetFile(output).metadata.num_rows)

    remote_confirmed = 0
    sqlite_path = state / "backup_manifest.sqlite3"
    if sqlite_path.exists():
        connection = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        try:
            try:
                remote_confirmed = int(
                    connection.execute(
                        "SELECT COALESCE(SUM(row_count), 0) FROM transfers "
                        "WHERE state = 'confirmed'"
                    ).fetchone()[0]
                )
            except sqlite3.Error:
                remote_confirmed = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM transfers WHERE state = 'confirmed'"
                    ).fetchone()[0]
                )
                # COUNT(*) of files is not row accounting; mark separately.
                return {
                    "manifest_rows": manifest_rows,
                    "local_output_rows": local_rows,
                    "remote_confirmed_rows": None,
                    "remote_confirmed_files": remote_confirmed,
                    "missing_local_outputs": missing_local,
                    "row_delta_local": local_rows - manifest_rows,
                    "accounting_note": "transfers table lacks row_count; file count only",
                    "remote": remote,
                    "remote_path": remote_path,
                }
        finally:
            connection.close()

    return {
        "manifest_rows": manifest_rows,
        "local_output_rows": local_rows,
        "remote_confirmed_rows": remote_confirmed,
        "missing_local_outputs": missing_local,
        "row_delta_local": local_rows - manifest_rows,
        "row_delta_remote": (
            None if remote_confirmed is None else remote_confirmed - manifest_rows
        ),
        "remote": remote,
        "remote_path": remote_path,
        "accounted_at": utc_now(),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--action",
        choices=("launch", "status", "account", "stop"),
        required=True,
    )
    parser.add_argument(
        "--canary-root",
        type=Path,
        default=Path("/data/experiments/canary_24h"),
    )
    parser.add_argument("--live", type=Path, default=Path("/data/live"))
    parser.add_argument("--compacted", type=Path, default=Path("/data/compacted"))
    parser.add_argument("--spool", type=Path, default=Path("/data/spool"))
    parser.add_argument(
        "--runtime-log", type=Path, default=Path("/var/log/spread/runtime.log")
    )
    parser.add_argument(
        "--failed-log",
        type=Path,
        default=Path("/var/log/spread/failed_batches.log"),
    )
    parser.add_argument("--remote", default="backup1tb")
    parser.add_argument("--remote-path", default="spread-canary-24h")
    parser.add_argument(
        "--duration-hours",
        type=float,
        default=24.0,
        help="Expected canary duration for status reporting",
    )
    parser.add_argument(
        "--use-systemd",
        action="store_true",
        help="Prefer systemctl start spread-collector.service when available",
    )
    return parser.parse_args()


def status_payload(root: Path) -> dict[str, Any]:
    path = root / "canary-status.json"
    if not path.exists():
        return {"state": "missing", "path": str(path)}
    return json.loads(path.read_text(encoding="utf-8"))


def launch(args: argparse.Namespace) -> int:
    root = args.canary_root
    root.mkdir(parents=True, exist_ok=True)
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    event_log = logs / "canary.jsonl"
    for path in (args.live, args.spool, args.compacted, args.runtime_log.parent):
        path.mkdir(parents=True, exist_ok=True)

    existing = status_payload(root)
    if existing.get("state") == "running":
        pid = int(existing.get("collector_pid") or 0)
        if pid:
            try:
                os.kill(pid, 0)
                print(json.dumps(existing, indent=2, sort_keys=True))
                return 0
            except OSError:
                pass

    env = {
        **os.environ,
        "SPREAD_PARQUET_ROOT": str(args.live),
        "SPREAD_RUNTIME_LOG": str(args.runtime_log),
        "SPREAD_FAILED_BATCHES_LOG": str(args.failed_log),
        "SPREAD_SPOOL_ROOT": str(args.spool),
        "BACKUP_RCLONE_BINARY": RCLONE,
        "BACKUP_RCLONE_REMOTE": args.remote,
        "BACKUP_RCLONE_PATH": args.remote_path,
        "BACKUP_SFTP_KEY_PATH": KEY,
        "BACKUP_RCLONE_SFTP_CONCURRENCY": "8",
        "BACKUP_RCLONE_SFTP_CHUNK_SIZE": "128k",
    }

    launched_via = "subprocess"
    collector_pid: int | None = None
    if args.use_systemd and Path("/etc/systemd/system/spread-collector.service").exists():
        subprocess.run(
            ["systemctl", "start", "spread-collector.service"], check=False
        )
        launched_via = "systemd"
        # Best-effort PID discovery.
        show = subprocess.run(
            ["systemctl", "show", "-p", "MainPID", "--value", "spread-collector.service"],
            text=True,
            capture_output=True,
            check=False,
        )
        try:
            collector_pid = int((show.stdout or "0").strip() or "0") or None
        except ValueError:
            collector_pid = None
    else:
        handle = (logs / "collector-console.log").open("ab", buffering=0)
        process = subprocess.Popen(
            [PYTHON, "app/screaner_b_o.py"],
            cwd=str(CODE_ROOT),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        collector_pid = process.pid

    payload = {
        "state": "running",
        "launched_at": utc_now(),
        "launched_via": launched_via,
        "collector_pid": collector_pid,
        "expected_end_epoch": time.time() + args.duration_hours * 3600.0,
        "duration_hours": args.duration_hours,
        "live": str(args.live),
        "compacted": str(args.compacted),
        "spool": str(args.spool),
        "runtime_log": str(args.runtime_log),
        "remote": args.remote,
        "remote_path": args.remote_path,
        "note": (
            "24h canary launched. Full completion requires wall-clock time; "
            "use --action status/account periodically. Do not claim READY "
            "until accounting deltas are 0 after expected_end_epoch."
        ),
    }
    write_json(root / "canary-status.json", payload)
    emit(event_log, "canary_launched", **payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def do_account(args: argparse.Namespace) -> int:
    root = args.canary_root
    root.mkdir(parents=True, exist_ok=True)
    result = account(
        compacted=args.compacted,
        remote=args.remote,
        remote_path=args.remote_path,
    )
    write_json(root / "daily-accounting.json", result)
    emit(root / "logs" / "canary.jsonl", "daily_accounting", **result)
    status = status_payload(root)
    if status.get("state") == "running":
        expected_end = float(status.get("expected_end_epoch") or 0)
        if expected_end and time.time() >= expected_end:
            status["state"] = "ready_for_final_accounting"
            status["final_accounting"] = result
            write_json(root / "canary-status.json", status)
    print(json.dumps(result, indent=2, sort_keys=True))
    delta_local = int(result.get("row_delta_local") or 0)
    delta_remote = result.get("row_delta_remote")
    if delta_local != 0:
        return 1
    if delta_remote not in (None, 0):
        return 1
    return 0


def do_status(args: argparse.Namespace) -> int:
    status = status_payload(args.canary_root)
    pid = int(status.get("collector_pid") or 0)
    alive = False
    if pid:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            alive = False
    status["collector_alive"] = alive
    status["checked_at"] = utc_now()
    if status.get("expected_end_epoch"):
        status["seconds_remaining"] = max(
            0.0, float(status["expected_end_epoch"]) - time.time()
        )
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0 if status.get("state") != "missing" else 1


def do_stop(args: argparse.Namespace) -> int:
    status = status_payload(args.canary_root)
    if args.use_systemd and Path("/etc/systemd/system/spread-collector.service").exists():
        subprocess.run(["systemctl", "stop", "spread-collector.service"], check=False)
    pid = int(status.get("collector_pid") or 0)
    if pid:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    status["state"] = "stopped"
    status["stopped_at"] = utc_now()
    write_json(args.canary_root / "canary-status.json", status)
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0


def main() -> int:
    args = parse_args()
    if args.action == "launch":
        return launch(args)
    if args.action == "status":
        return do_status(args)
    if args.action == "account":
        return do_account(args)
    if args.action == "stop":
        return do_stop(args)
    raise SystemExit(f"unknown action: {args.action}")


if __name__ == "__main__":
    raise SystemExit(main())
