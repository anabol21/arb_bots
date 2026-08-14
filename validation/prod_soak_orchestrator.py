"""Isolated one-hour VPS production-readiness soak orchestrator."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import signal
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq


INTERVAL_SECONDS = 300
SNAPSHOT_SECONDS = 30
BACKUP_IP = "5.45.77.77"
RCLONE = "/opt/rclone-1.74.4/rclone"
KEY = "/root/.ssh/id_ed25519_uploader"
PYTHON = "/root/venv/bin/python"
CODE_ROOT = Path("/root/spread_staging")
LOCK_PATH = "/run/spread-backup.lock"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--remote-path", required=True)
    parser.add_argument("--duration-seconds", type=int, default=3600)
    return parser.parse_args()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_stats(root: Path, *, suffix: str | None = None) -> tuple[int, int]:
    if not root.exists():
        return 0, 0
    count = 0
    size = 0
    for path in root.rglob("*"):
        try:
            if not path.is_file() or (suffix is not None and path.suffix != suffix):
                continue
            size += path.stat().st_size
            count += 1
        except FileNotFoundError:
            # Atomic writers legitimately rename tmp files during an inventory walk.
            continue
    return count, size


def paths_stats(paths: list[Path]) -> tuple[int, int]:
    count = 0
    size = 0
    for path in paths:
        try:
            size += path.stat().st_size
            count += 1
        except FileNotFoundError:
            continue
    return count, size


class Soak:
    def __init__(self, args: argparse.Namespace) -> None:
        self.root = args.root.resolve()
        self.duration = args.duration_seconds
        self.remote_path = args.remote_path.strip("/")
        self.live = self.root / "live"
        self.archive = self.root / "archive"
        self.compacted = self.root / "compacted"
        self.sent = self.compacted / "sent"
        self.logs = self.root / "logs"
        self.spool = self.root / "spool"
        self.runtime_log = self.logs / "collector-runtime.log"
        self.failed_log = self.logs / "collector-failed-batches.log"
        self.event_log = self.logs / "orchestrator.jsonl"
        self.snapshot_log = self.logs / "snapshots.jsonl"
        self.compactor_log = self.logs / "compactor.jsonl"
        self.transfer_log = self.logs / "transfer.jsonl"
        self.collector_console = self.logs / "collector-console.log"
        self.collector: subprocess.Popen[bytes] | None = None
        self.collector_handle: Any = None
        self.job: dict[str, Any] | None = None
        self.next_snapshot = 0.0
        self.started_epoch = 0.0
        self.started_mono = 0.0
        self.writer_stopped_epoch: float | None = None
        self.forced_kill = False
        self.maintenance_failures = 0

    def emit(self, event: str, **fields: Any) -> None:
        payload = {"timestamp": time.time(), "event": event, **fields}
        with self.event_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    def write_state(self, state: str, **fields: Any) -> None:
        payload = {"timestamp": time.time(), "state": state, **fields}
        temporary = self.root / ".state.json.tmp"
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.root / "state.json")

    def setup(self) -> None:
        for path in (
            self.live,
            self.archive,
            self.compacted,
            self.logs,
            self.spool,
        ):
            path.mkdir(parents=True, exist_ok=False)
        config = {
            "collector_command": f"{PYTHON} app/screaner_b_o.py",
            "code_root": str(CODE_ROOT),
            "duration_seconds": self.duration,
            "snapshot_seconds": SNAPSHOT_SECONDS,
            "compaction_interval_seconds": INTERVAL_SECONDS,
            "transfer_offset_seconds": 70,
            "live_path": str(self.live),
            "archive_path": str(self.archive),
            "compacted_path": str(self.compacted),
            "sent_path": str(self.sent),
            "spool_path": str(self.spool),
            "runtime_log": str(self.runtime_log),
            "remote": "backup1tb",
            "remote_path": self.remote_path,
            "rclone": RCLONE,
            "sftp_key": KEY,
            "sftp_concurrency": 8,
            "sftp_chunk_size": "128k",
            "lock_path": LOCK_PATH,
            "file_states": [
                "live/*.parquet",
                "archive/<live-relative>",
                "compacted/spread_*.parquet",
                "compacted/sent/spread_*.parquet",
                f"backup1tb:{self.remote_path}/spread_*.parquet",
            ],
        }
        (self.root / "experiment-config.json").write_text(
            json.dumps(config, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (self.root / "orchestrator.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
        self.write_state("starting")

    def collector_env(self) -> dict[str, str]:
        return {
            **os.environ,
            "SPREAD_PARQUET_ROOT": str(self.live),
            "SPREAD_RUNTIME_LOG": str(self.runtime_log),
            "SPREAD_FAILED_BATCHES_LOG": str(self.failed_log),
            "SPREAD_SPOOL_ROOT": str(self.spool),
        }

    def transfer_env(self) -> dict[str, str]:
        return {
            **os.environ,
            "BACKUP_COMPACTED_DIR": str(self.compacted),
            "BACKUP_RCLONE_REMOTE": "backup1tb",
            "BACKUP_RCLONE_PATH": self.remote_path,
            "BACKUP_SFTP_KEY_PATH": KEY,
            "BACKUP_RCLONE_BINARY": RCLONE,
            "BACKUP_RCLONE_SFTP_CONCURRENCY": "8",
            "BACKUP_RCLONE_SFTP_CHUNK_SIZE": "128k",
            "BACKUP_TRANSFER_LOCK_PATH": LOCK_PATH,
            "BACKUP_SENT_RETENTION_HOURS": "12",
        }

    def start_collector(self) -> None:
        self.collector_handle = self.collector_console.open("ab", buffering=0)
        self.collector = subprocess.Popen(
            [PYTHON, "app/screaner_b_o.py"],
            cwd=CODE_ROOT,
            env=self.collector_env(),
            stdout=self.collector_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.started_epoch = time.time()
        self.started_mono = time.monotonic()
        self.next_snapshot = self.started_mono
        self.write_state(
            "running",
            collector_pid=self.collector.pid,
            started_epoch=self.started_epoch,
        )
        self.emit(
            "collector_started",
            pid=self.collector.pid,
            pgid=os.getpgid(self.collector.pid),
            command=[PYTHON, "app/screaner_b_o.py"],
        )

    def compactor_command(self) -> list[str]:
        return [
            PYTHON,
            "-m",
            "app.storage.compactor",
            "--live",
            str(self.live),
            "--compacted",
            str(self.compacted),
            "--archive",
            str(self.archive),
            "--interval",
            str(INTERVAL_SECONDS),
            "--retention-hours",
            "24",
        ]

    def transfer_command(self) -> list[str]:
        return [PYTHON, "-m", "app.storage.backup_transfer"]

    def start_job(self, kind: str) -> None:
        if self.job is not None:
            raise RuntimeError(f"maintenance overlap refused: active={self.job['kind']}")
        command = self.compactor_command() if kind == "compactor" else self.transfer_command()
        log_path = self.compactor_log if kind == "compactor" else self.transfer_log
        handle = log_path.open("ab", buffering=0)
        process = subprocess.Popen(
            command,
            cwd=CODE_ROOT,
            env=os.environ if kind == "compactor" else self.transfer_env(),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.job = {
            "kind": kind,
            "process": process,
            "handle": handle,
            "started_mono": time.monotonic(),
            "started_epoch": time.time(),
        }
        self.emit(
            "maintenance_started",
            kind=kind,
            pid=process.pid,
            pgid=os.getpgid(process.pid),
            command=command,
        )

    def check_job(self) -> bool:
        if self.job is None:
            return True
        process = self.job["process"]
        returncode = process.poll()
        if returncode is None:
            return False
        self.job["handle"].close()
        duration = time.monotonic() - self.job["started_mono"]
        self.emit(
            "maintenance_finished",
            kind=self.job["kind"],
            returncode=returncode,
            duration_s=round(duration, 6),
        )
        if returncode != 0:
            self.maintenance_failures += 1
        self.job = None
        return True

    def terminate_job(self, reason: str) -> None:
        if self.job is None:
            return
        process = self.job["process"]
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
        self.emit(
            "maintenance_forced_stop",
            kind=self.job["kind"],
            reason=reason,
            returncode=process.returncode,
        )
        self.job["handle"].close()
        self.job = None
        self.maintenance_failures += 1

    def ping(self) -> dict[str, Any]:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "2", BACKUP_IP],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        match = re.search(r"time=([0-9.]+) ms", result.stdout)
        return {
            "ping_success": result.returncode == 0,
            "ping_rtt_ms": float(match.group(1)) if match else None,
        }

    def snapshot(self, phase: str) -> None:
        live_count, live_bytes = tree_stats(self.live, suffix=".parquet")
        archive_count, archive_bytes = tree_stats(self.archive, suffix=".parquet")
        pending = list(self.compacted.glob("spread_*.parquet"))
        sent = list(self.sent.glob("spread_*.parquet")) if self.sent.exists() else []
        pending_count, pending_bytes = paths_stats(pending)
        sent_count, sent_bytes = paths_stats(sent)
        spool_count, spool_bytes = tree_stats(self.spool)
        disk = os.statvfs(self.root)
        payload = {
            "timestamp": time.time(),
            "elapsed_s": round(time.monotonic() - self.started_mono, 3),
            "phase": phase,
            "collector_alive": self.collector is not None
            and self.collector.poll() is None,
            "maintenance": None if self.job is None else self.job["kind"],
            "live_files": live_count,
            "live_bytes": live_bytes,
            "archive_files": archive_count,
            "archive_bytes": archive_bytes,
            "pending_files": pending_count,
            "pending_bytes": pending_bytes,
            "sent_files": sent_count,
            "sent_bytes": sent_bytes,
            "spool_files": spool_count,
            "spool_bytes": spool_bytes,
            "disk_free_bytes": disk.f_bavail * disk.f_frsize,
            "experiment_bytes": tree_stats(self.root)[1],
            **self.ping(),
        }
        with self.snapshot_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    def maybe_snapshot(self, phase: str, *, force: bool = False) -> None:
        now = time.monotonic()
        if force or now >= self.next_snapshot:
            self.snapshot(phase)
            self.next_snapshot = now + SNAPSHOT_SECONDS

    def wait_job(self, deadline: float, phase: str) -> bool:
        while not self.check_job():
            self.maybe_snapshot(phase)
            if time.monotonic() >= deadline:
                self.terminate_job(f"{phase}_deadline")
                return False
            time.sleep(1)
        return True

    def stop_collector(self) -> None:
        assert self.collector is not None
        self.emit("collector_stop_requested", elapsed_s=time.monotonic() - self.started_mono)
        if self.collector.poll() is None:
            os.killpg(self.collector.pid, signal.SIGTERM)
        deadline = time.monotonic() + 120
        while self.collector.poll() is None and time.monotonic() < deadline:
            self.check_job()
            self.maybe_snapshot("collector_stopping")
            time.sleep(1)
        if self.collector.poll() is None:
            self.forced_kill = True
            os.killpg(self.collector.pid, signal.SIGKILL)
            self.collector.wait(timeout=15)
        self.writer_stopped_epoch = time.time()
        self.collector_handle.close()
        self.emit(
            "collector_stopped",
            returncode=self.collector.returncode,
            forced_kill=self.forced_kill,
            duration_s=self.writer_stopped_epoch - self.started_epoch,
        )

    def backlog(self) -> tuple[int, int]:
        return paths_stats(list(self.compacted.glob("spread_*.parquet")))

    def run(self) -> int:
        self.setup()
        self.start_collector()
        next_boundary = math.floor(self.started_epoch / INTERVAL_SECONDS + 1) * INTERVAL_SECONDS
        next_compaction = next_boundary + 10
        next_transfer = next_boundary + 70
        collector_deadline = self.started_mono + self.duration

        while time.monotonic() < collector_deadline:
            self.check_job()
            now = time.time()
            if self.job is None and now >= next_compaction:
                self.start_job("compactor")
                next_compaction += INTERVAL_SECONDS
            elif self.job is None and now >= next_transfer:
                self.start_job("transfer")
                next_transfer += INTERVAL_SECONDS
            self.maybe_snapshot("collecting")
            if self.collector is not None and self.collector.poll() is not None:
                self.emit("collector_early_exit", returncode=self.collector.returncode)
                break
            time.sleep(1)

        self.stop_collector()
        if self.job is not None:
            self.wait_job(time.monotonic() + 1800, "finish_active_maintenance")

        close_epoch = math.floor(time.time() / INTERVAL_SECONDS + 1) * INTERVAL_SECONDS + 5
        self.emit("waiting_for_final_window_close", close_epoch=close_epoch)
        while time.time() < close_epoch:
            self.maybe_snapshot("waiting_window_close")
            time.sleep(1)

        self.start_job("compactor")
        self.wait_job(time.monotonic() + 900, "final_compaction")

        _, pending_bytes = self.backlog()
        drain_budget = max(
            300.0,
            min(1800.0, pending_bytes / (0.5 * 1024 * 1024) * 6 + 180),
        )
        drain_deadline = time.monotonic() + drain_budget
        self.emit(
            "drain_started",
            pending_bytes=pending_bytes,
            drain_budget_s=round(drain_budget, 3),
        )
        while self.backlog()[0] > 0 and time.monotonic() < drain_deadline:
            self.start_job("transfer")
            if not self.wait_job(drain_deadline, "draining"):
                break
            if self.backlog()[0] > 0:
                time.sleep(5)

        self.maybe_snapshot("complete", force=True)
        summary = self.account()
        summary.update(
            {
                "experiment_root": str(self.root),
                "remote_path": self.remote_path,
                "collector_duration_s": (
                    None
                    if self.writer_stopped_epoch is None
                    else self.writer_stopped_epoch - self.started_epoch
                ),
                "collector_forced_kill": self.forced_kill,
                "maintenance_failures": self.maintenance_failures,
                "final_backlog_files": self.backlog()[0],
                "final_backlog_bytes": self.backlog()[1],
            }
        )
        (self.root / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.write_state("complete", summary_path=str(self.root / "summary.json"))
        self.emit("experiment_complete", summary_path=str(self.root / "summary.json"))
        return 0

    def account(self) -> dict[str, Any]:
        state = self.compacted / ".state"
        manifest_paths = sorted(state.glob("spread_*.json"))
        complete = 0
        noncomplete = 0
        expected_rows = 0
        output_rows = 0
        archive_rows = 0
        checksum_failures: list[str] = []
        missing_outputs: list[str] = []
        missing_archives: list[str] = []
        seen_sources: set[str] = set()

        for manifest_path in manifest_paths:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("status") != "complete":
                noncomplete += 1
                continue
            complete += 1
            expected_rows += int(manifest["total_rows"])
            output_name = str(manifest["output"])
            candidates = [self.compacted / output_name, self.sent / output_name]
            output = next((path for path in candidates if path.is_file()), None)
            if output is None:
                missing_outputs.append(output_name)
            else:
                output_rows += int(pq.ParquetFile(output).metadata.num_rows)
                if sha256(output) != manifest.get("output_sha256"):
                    checksum_failures.append(output_name)
            for source in manifest["sources"]:
                relative = str(source["path"])
                if relative in seen_sources:
                    checksum_failures.append(f"duplicate-source:{relative}")
                    continue
                seen_sources.add(relative)
                archived = self.archive / relative
                if not archived.is_file():
                    missing_archives.append(relative)
                else:
                    archive_rows += int(pq.ParquetFile(archived).metadata.num_rows)

        transfer_states: dict[str, int] = {}
        transfer_attempts = 0
        sqlite_path = state / "backup_manifest.sqlite3"
        if sqlite_path.exists():
            connection = sqlite3.connect(str(sqlite_path))
            try:
                transfer_states = {
                    str(row[0]): int(row[1])
                    for row in connection.execute(
                        "SELECT state, COUNT(*) FROM transfers GROUP BY state"
                    )
                }
                transfer_attempts = int(
                    connection.execute(
                        "SELECT COALESCE(SUM(attempts), 0) FROM transfers"
                    ).fetchone()[0]
                )
            finally:
                connection.close()

        open_window = int(time.time() // INTERVAL_SECONDS) * INTERVAL_SECONDS
        closed_live = [
            str(path.relative_to(self.live))
            for path in self.live.rglob("*.parquet")
            if int(path.stat().st_mtime) < open_window
        ]
        orphans = [
            str(path.relative_to(self.root))
            for path in self.root.rglob("*")
            if path.is_file()
            and (".tmp" in path.name or ".inprogress" in path.name)
        ]
        return {
            "complete_manifests": complete,
            "noncomplete_manifests": noncomplete,
            "manifest_rows": expected_rows,
            "output_rows": output_rows,
            "archive_rows": archive_rows,
            "output_row_delta": output_rows - expected_rows,
            "archive_row_delta": archive_rows - expected_rows,
            "checksum_failures": checksum_failures,
            "missing_outputs": missing_outputs,
            "missing_archives": missing_archives,
            "closed_live_unclaimed": closed_live,
            "tmp_or_inprogress_orphans": orphans,
            "transfer_states": transfer_states,
            "transfer_attempts_total": transfer_attempts,
        }


def main() -> int:
    args = parse_args()
    soak = Soak(args)

    def stop_handler(signum: int, _frame: Any) -> None:
        soak.emit("orchestrator_signal", signal=signum)
        if soak.collector is not None and soak.collector.poll() is None:
            os.killpg(soak.collector.pid, signal.SIGTERM)
        soak.terminate_job("orchestrator_signal")
        soak.write_state("aborted", signal=signum)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    return soak.run()


if __name__ == "__main__":
    raise SystemExit(main())
