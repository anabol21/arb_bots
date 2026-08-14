"""Narrow backup-only network outage check (no sshfs).

Pre-arms cleanup, blocks traffic only to the backup host for a short window,
confirms the collector/writer remains healthy, then restores connectivity and
verifies backlog drains via backup_transfer.

Safe defaults:
- backup host only (default 5.45.77.77)
- short duration (default 120s)
- cleanup always attempted in finally
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any


BACKUP_IP_DEFAULT = "5.45.77.77"
PYTHON = "/root/venv/bin/python"
CODE_ROOT = Path("/root/spread_staging")


def emit(path: Path, event: str, **fields: Any) -> None:
    payload = {"timestamp": time.time(), "event": event, **fields}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    print(json.dumps(payload, sort_keys=True), flush=True)


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def pending_backlog(compacted: Path) -> tuple[int, int]:
    files = list(compacted.glob("spread_*.parquet"))
    size = 0
    for path in files:
        try:
            size += path.stat().st_size
        except FileNotFoundError:
            continue
    return len(files), size


def heartbeat_alive(runtime_log: Path, *, since_epoch: float) -> bool:
    if not runtime_log.is_file():
        return False
    try:
        text = runtime_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    recent = [
        line
        for line in text.splitlines()
        if "heartbeat |" in line
    ]
    if not recent:
        return False
    # File mtime advancing plus a heartbeat line is enough for this narrow check.
    return runtime_log.stat().st_mtime >= since_epoch - 5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-ip", default=BACKUP_IP_DEFAULT)
    parser.add_argument("--block-seconds", type=int, default=120)
    parser.add_argument("--compacted", type=Path, default=Path("/data/compacted"))
    parser.add_argument(
        "--runtime-log",
        type=Path,
        default=Path("/var/log/spread/runtime.log"),
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=Path("/data/experiments/backup_outage_check"),
    )
    parser.add_argument(
        "--drain-timeout-sec",
        type=int,
        default=900,
        help="Max wait for backlog to drain after restore",
    )
    parser.add_argument(
        "--skip-transfer-during-block",
        action="store_true",
        help="Do not intentionally start transfer while blocked",
    )
    parser.add_argument(
        "--require-collector-pid",
        type=int,
        default=0,
        help="If set, fail unless this PID is alive for the whole check",
    )
    return parser.parse_args()


def iptables_drop(backup_ip: str) -> list[str]:
    return [
        "iptables",
        "-I",
        "OUTPUT",
        "1",
        "-d",
        backup_ip,
        "-j",
        "DROP",
    ]


def iptables_undrop(backup_ip: str) -> list[str]:
    return [
        "iptables",
        "-D",
        "OUTPUT",
        "-d",
        backup_ip,
        "-j",
        "DROP",
    ]


def main() -> int:
    args = parse_args()
    evidence = args.evidence_dir
    evidence.mkdir(parents=True, exist_ok=True)
    log_path = evidence / "outage-check.jsonl"
    cleanup_cmd = iptables_undrop(args.backup_ip)
    (evidence / "cleanup.sh").write_text(
        "#!/bin/bash\nset -e\n" + " ".join(cleanup_cmd) + "\n",
        encoding="utf-8",
    )
    os.chmod(evidence / "cleanup.sh", 0o755)
    emit(log_path, "cleanup_prearmed", cleanup=cleanup_cmd)

    blocked = False
    started = time.time()
    before_files, before_bytes = pending_backlog(args.compacted)
    emit(
        log_path,
        "baseline",
        backlog_files=before_files,
        backlog_bytes=before_bytes,
        collector_pid=args.require_collector_pid or None,
    )

    def _cleanup(_signum: int | None = None, _frame: Any = None) -> None:
        nonlocal blocked
        if blocked:
            result = run(cleanup_cmd, check=False)
            emit(
                log_path,
                "cleanup_executed",
                returncode=result.returncode,
                stdout=result.stdout[-500:],
                stderr=result.stderr[-500:],
            )
            blocked = False

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    try:
        run(iptables_drop(args.backup_ip), check=True)
        blocked = True
        emit(log_path, "backup_blocked", backup_ip=args.backup_ip)

        if not args.skip_transfer_during_block:
            env = {
                **os.environ,
                "BACKUP_COMPACTED_DIR": str(args.compacted),
                "BACKUP_RCLONE_BINARY": "/opt/rclone-1.74.4/rclone",
                "BACKUP_RCLONE_REMOTE": "backup1tb",
                "BACKUP_RCLONE_PATH": os.environ.get(
                    "BACKUP_RCLONE_PATH", "spread-compacted"
                ),
                "BACKUP_SFTP_KEY_PATH": "/root/.ssh/id_ed25519_uploader",
                "BACKUP_RCLONE_SFTP_CONCURRENCY": "8",
                "BACKUP_RCLONE_SFTP_CHUNK_SIZE": "128k",
                "BACKUP_TRANSFER_LOCK_PATH": "/run/spread-backup.lock",
            }
            transfer = subprocess.Popen(
                [PYTHON, "-m", "app.storage.backup_transfer"],
                cwd=str(CODE_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            emit(log_path, "transfer_started_during_block", pid=transfer.pid)
            try:
                out, _ = transfer.communicate(timeout=args.block_seconds + 30)
            except subprocess.TimeoutExpired:
                transfer.kill()
                out, _ = transfer.communicate()
            emit(
                log_path,
                "transfer_finished_during_block",
                returncode=transfer.returncode,
                output_tail=(out or "")[-1000:],
            )

        deadline = started + args.block_seconds
        while time.time() < deadline:
            if args.require_collector_pid:
                try:
                    os.kill(args.require_collector_pid, 0)
                except OSError:
                    emit(
                        log_path,
                        "collector_died_during_block",
                        pid=args.require_collector_pid,
                    )
                    return 2
            time.sleep(5)

        mid_files, mid_bytes = pending_backlog(args.compacted)
        writer_ok = heartbeat_alive(args.runtime_log, since_epoch=started)
        emit(
            log_path,
            "block_window_complete",
            backlog_files=mid_files,
            backlog_bytes=mid_bytes,
            writer_heartbeat_ok=writer_ok,
        )
        if not writer_ok:
            emit(log_path, "fail", reason="writer_heartbeat_missing")
            return 3
    finally:
        _cleanup()

    # Drain after restore.
    drain_deadline = time.time() + args.drain_timeout_sec
    env = {
        **os.environ,
        "BACKUP_COMPACTED_DIR": str(args.compacted),
        "BACKUP_RCLONE_BINARY": "/opt/rclone-1.74.4/rclone",
        "BACKUP_RCLONE_REMOTE": "backup1tb",
        "BACKUP_RCLONE_PATH": os.environ.get(
            "BACKUP_RCLONE_PATH", "spread-compacted"
        ),
        "BACKUP_SFTP_KEY_PATH": "/root/.ssh/id_ed25519_uploader",
        "BACKUP_RCLONE_SFTP_CONCURRENCY": "8",
        "BACKUP_RCLONE_SFTP_CHUNK_SIZE": "128k",
        "BACKUP_TRANSFER_LOCK_PATH": "/run/spread-backup.lock",
    }
    while time.time() < drain_deadline:
        files, nbytes = pending_backlog(args.compacted)
        emit(log_path, "drain_progress", backlog_files=files, backlog_bytes=nbytes)
        if files == 0:
            emit(log_path, "pass", backlog_files=0, backlog_bytes=0)
            return 0
        result = subprocess.run(
            [PYTHON, "-m", "app.storage.backup_transfer"],
            cwd=str(CODE_ROOT),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        emit(
            log_path,
            "transfer_drain_attempt",
            returncode=result.returncode,
            output_tail=(result.stdout + result.stderr)[-800:],
        )
        time.sleep(5)

    files, nbytes = pending_backlog(args.compacted)
    emit(
        log_path,
        "fail",
        reason="backlog_not_drained",
        backlog_files=files,
        backlog_bytes=nbytes,
    )
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
