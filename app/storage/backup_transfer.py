"""One-shot, cron-friendly transfer of compacted files to an SFTP rclone remote."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import json
import logging
import os
import re
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Sequence


DEFAULT_COMPACTED_DIR = Path("/data/compacted")
DEFAULT_SENT_RETENTION_HOURS = 12.0
DEFAULT_MANIFEST_NAME = "backup_manifest.sqlite3"
DEFAULT_LOCK_PATH = Path("/run/spread-backup.lock")
# flat: compacted tick artifacts (spread_*.parquet at source root).
# hive: recursive bar batches under bar_5m/... (relative path is the identity).
LAYOUT_FLAT = "flat"
LAYOUT_HIVE = "hive"
VALID_LAYOUTS = frozenset({LAYOUT_FLAT, LAYOUT_HIVE})
HIVE_SKIP_DIR_NAMES = frozenset({"sent", ".state", ".tmp"})
# Calibrated from VPS SFTP measurements with --sftp-concurrency 8 --sftp-chunk-size 128k
# (rclone 1.74.4): mean ~1.43 MiB/s, p10 ~0.60 MiB/s on small files. Floor 0.5 MiB/s
# keeps headroom under p10 so watchdog timeouts stay conservative.
# Formula: timeout = min(3600, max(120, size/floor*4 + 60)).
# Do NOT use support's 32/512k — that pair caused EOF on this endpoint.
TRANSFER_RATE_FLOOR_BYTES_S = 0.5 * 1024 * 1024
DEFAULT_SFTP_CONCURRENCY = 8
DEFAULT_SFTP_CHUNK_SIZE = "128k"
DEFAULT_HIVE_BATCH_SIZE = 32
RCLONE_FLAGS = (
    "--timeout",
    "60s",
    "--contimeout",
    "15s",
    "--retries",
    "3",
    "--stats",
    "5s",
    "--stats-one-line",
    "--stats-unit",
    "bytes",
)
# Upload/copyto/moveto may use SFTP concurrency+chunk tuning. download_verify
# deliberately omits them: concurrency on the verify download path produced a
# size-mismatch failure in retest, so SHA-256 verify stays on the conservative path.
_TRANSFERRED_RE = re.compile(
    r"Transferred:\s+([0-9.]+)\s*([kMGT]?i?Bytes)\s*/",
    re.IGNORECASE,
)


class ConfigurationError(ValueError):
    """Raised when required transfer configuration is missing or invalid."""


class TransferError(RuntimeError):
    """Raised when an rclone operation cannot be confirmed."""


class RcloneRcError(TransferError):
    """Raised when a persistent rclone RC operation fails."""


@dataclass(frozen=True)
class Config:
    compacted_dir: Path
    remote: str
    remote_path: str
    key_path: Path
    rclone: str = "rclone"
    sent_retention_hours: float = DEFAULT_SENT_RETENTION_HOURS
    sftp_concurrency: int = DEFAULT_SFTP_CONCURRENCY
    sftp_chunk_size: str = DEFAULT_SFTP_CHUNK_SIZE
    layout: str = LAYOUT_FLAT
    max_files: int | None = None
    # When set, files smaller than this skip SHA download_verify (size match only).
    # Intended for hive/tiny bar batches where full re-download dominates runtime.
    skip_sha_verify_below_bytes: int | None = None
    # Optional host-local lock acquired around each individual transfer. This is
    # distinct from the whole-invocation lock that prevents overlapping backup
    # services. It lets a waiting compactor run between bar files.
    shared_lock_path: Path | None = None
    # Number of hive files that may share one persistent rclone/SFTP session.
    # A shared-heavy-storage lock, when configured, spans the same micro-batch;
    # it is optional and does not determine whether session reuse is enabled.
    hive_batch_size: int | None = None

    @property
    def sent_dir(self) -> Path:
        return self.compacted_dir / "sent"

    @property
    def manifest_path(self) -> Path:
        return self.compacted_dir / ".state" / DEFAULT_MANIFEST_NAME

    def validate(self) -> None:
        if not self.compacted_dir.is_absolute():
            raise ConfigurationError("compacted directory must be an absolute path")
        if not self.remote.strip():
            raise ConfigurationError(
                "rclone SFTP remote is required (--remote or BACKUP_RCLONE_REMOTE)"
            )
        remote_name = self.remote.rstrip(":")
        if "/" in remote_name or ":" in remote_name:
            raise ConfigurationError("remote must be an rclone remote name, not a path")
        if not self.remote_path.strip():
            raise ConfigurationError(
                "remote path is required (--remote-path or BACKUP_RCLONE_PATH)"
            )
        if not self.key_path.is_absolute():
            raise ConfigurationError("SFTP key path must be absolute")
        if not self.key_path.is_file():
            raise ConfigurationError(
                f"SFTP key path does not exist or is not a file: {self.key_path}"
            )
        if self.sent_retention_hours < 0:
            raise ConfigurationError("sent retention hours cannot be negative")
        if self.sftp_concurrency < 0:
            raise ConfigurationError("SFTP concurrency cannot be negative")
        if self.sftp_concurrency > 0 and not self.sftp_chunk_size.strip():
            raise ConfigurationError(
                "SFTP chunk size is required when concurrency is enabled"
            )
        if self.layout not in VALID_LAYOUTS:
            raise ConfigurationError(
                f"layout must be one of {sorted(VALID_LAYOUTS)}, got {self.layout!r}"
            )
        if self.max_files is not None and self.max_files < 1:
            raise ConfigurationError("max_files must be >= 1 when set")
        if (
            self.skip_sha_verify_below_bytes is not None
            and self.skip_sha_verify_below_bytes < 0
        ):
            raise ConfigurationError("skip_sha_verify_below_bytes cannot be negative")
        if self.shared_lock_path is not None and not self.shared_lock_path.is_absolute():
            raise ConfigurationError("shared lock path must be absolute")
        if self.hive_batch_size is not None and self.hive_batch_size < 1:
            raise ConfigurationError("hive batch size must be >= 1 when set")

    @property
    def effective_hive_batch_size(self) -> int:
        if self.hive_batch_size is not None:
            return self.hive_batch_size
        if self.layout == LAYOUT_HIVE and self.shared_lock_path is not None:
            return DEFAULT_HIVE_BATCH_SIZE
        return 1


class Manifest:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS transfers (
                filename TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                state TEXT NOT NULL,
                remote_path TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                confirmed_at REAL,
                sent_at REAL,
                last_error TEXT
            )
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def get(self, filename: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM transfers WHERE filename = ?", (filename,)
        ).fetchone()

    def record_attempt(self, filename: str, size: int, remote_path: str) -> int:
        now = time.time()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO transfers (
                    filename, size, state, remote_path, attempts,
                    created_at, updated_at, last_error
                ) VALUES (?, ?, 'pending', ?, 1, ?, ?, NULL)
                ON CONFLICT(filename) DO UPDATE SET
                    size = excluded.size,
                    state = 'pending',
                    remote_path = excluded.remote_path,
                    attempts = transfers.attempts + 1,
                    updated_at = excluded.updated_at,
                    last_error = NULL
                """,
                (filename, size, remote_path, now, now),
            )
        row = self.get(filename)
        assert row is not None
        return int(row["attempts"])

    def mark_failed(self, filename: str, error: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE transfers
                SET state = 'failed', updated_at = ?, last_error = ?
                WHERE filename = ?
                """,
                (time.time(), error, filename),
            )

    def mark_conflict(self, filename: str, error: str) -> None:
        """Terminal local/remote identity conflict; never retry automatically."""
        with self.connection:
            self.connection.execute(
                """
                UPDATE transfers
                SET state = 'conflict', updated_at = ?, last_error = ?
                WHERE filename = ?
                """,
                (time.time(), error, filename),
            )

    def mark_confirmed(self, filename: str) -> None:
        now = time.time()
        with self.connection:
            self.connection.execute(
                """
                UPDATE transfers
                SET state = 'confirmed', updated_at = ?, confirmed_at = ?,
                    last_error = NULL
                WHERE filename = ?
                """,
                (now, now, filename),
            )

    def mark_sent(self, filename: str) -> None:
        now = time.time()
        with self.connection:
            self.connection.execute(
                """
                UPDATE transfers
                SET state = 'sent', updated_at = ?, sent_at = ?, last_error = NULL
                WHERE filename = ?
                """,
                (now, now, filename),
            )

    def confirmed(self) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM transfers WHERE state = 'confirmed'"
            ).fetchall()
        )

    def conflicts(self) -> set[str]:
        return {
            str(row[0])
            for row in self.connection.execute(
                "SELECT filename FROM transfers WHERE state = 'conflict'"
            )
        }

    def expired_sent(self, cutoff: float) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT * FROM transfers
                WHERE state = 'sent' AND sent_at IS NOT NULL AND sent_at < ?
                """,
                (cutoff,),
            ).fetchall()
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _transfer_progress(stderr: str) -> tuple[str | None, int | None]:
    lines = [line.strip() for line in stderr.replace("\r", "\n").splitlines()]
    progress_line = next(
        (line for line in reversed(lines) if "Transferred:" in line),
        None,
    )
    if progress_line is None:
        return None, None
    match = _TRANSFERRED_RE.search(progress_line)
    if match is None:
        return progress_line, None
    value = float(match.group(1))
    unit = match.group(2).lower()
    multipliers = {
        "bytes": 1,
        "kbytes": 1000,
        "kibytes": 1024,
        "mbytes": 1000**2,
        "mibytes": 1024**2,
        "gbytes": 1000**3,
        "gibytes": 1024**3,
        "tbytes": 1000**4,
        "tibytes": 1024**4,
    }
    multiplier = multipliers.get(unit)
    if multiplier is None:
        return progress_line, None
    return progress_line, int(value * multiplier)


class RcloneRcSession:
    """A bounded local rclone daemon that reuses its SFTP backend per batch.

    The daemon listens only on a private Unix socket. Each RC operation still
    returns before the next lifecycle transition, so an individual source is
    never marked confirmed or moved locally until its own final remote size has
    been observed.
    """

    def __init__(self, transfer: "BackupTransfer", file_size: int) -> None:
        self.transfer = transfer
        self.file_size = file_size
        self.socket_dir = tempfile.TemporaryDirectory(
            prefix="spread-rclone-rc-", dir="/run"
        )
        self.socket_path = Path(self.socket_dir.name) / "rc.sock"
        self.process: subprocess.Popen[str] | None = None

    def start(self) -> None:
        command = [
            self.transfer.config.rclone,
            "rcd",
            "--rc-addr",
            f"unix://{self.socket_path}",
            "--rc-no-auth",
            "--rc-server-read-timeout",
            f"{int(self.transfer.watchdog_timeout(self.file_size))}s",
            "--rc-server-write-timeout",
            f"{int(self.transfer.watchdog_timeout(self.file_size))}s",
            *RCLONE_FLAGS,
            *self.transfer._sftp_tuning_flags(),
            "--sftp-key-file",
            str(self.transfer.config.key_path),
        ]
        self.transfer._emit(
            "rclone_hive_batch_command",
            command=command,
            hive_batch_size=self.transfer.config.effective_hive_batch_size,
        )
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                stderr = self.process.stderr.read() if self.process.stderr else ""
                raise RcloneRcError(
                    f"rclone rcd exited during startup: {stderr.strip()}"
                )
            if self.socket_path.exists():
                try:
                    self.request("core/pid", {}, operation="rc_ready")
                    return
                except (OSError, RcloneRcError):
                    pass
            time.sleep(0.05)
        raise RcloneRcError("rclone rcd did not become ready within 10s")

    def request(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        operation: str,
    ) -> dict[str, Any]:
        timeout = self.transfer.watchdog_timeout(self.file_size)
        started = time.monotonic()
        started_epoch = time.time()
        body = json.dumps(payload).encode("utf-8")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(timeout)
                client.connect(str(self.socket_path))
                client.sendall(
                    b"POST /"
                    + endpoint.encode("ascii")
                    + b" HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
                    + b"Connection: close\r\nContent-Length: "
                    + str(len(body)).encode("ascii")
                    + b"\r\n\r\n"
                    + body
                )
                response = http.client.HTTPResponse(client)
                response.begin()
                raw = response.read()
                status = response.status
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            self.transfer._emit(
                "rclone_rc_operation_result",
                operation=operation,
                operation_success=False,
                operation_duration_s=round(time.monotonic() - started, 6),
                operation_start_epoch=started_epoch,
                operation_end_epoch=time.time(),
                error=str(exc),
            )
            raise RcloneRcError(f"{operation} RC request failed: {exc}") from exc
        try:
            result = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError as exc:
            raise RcloneRcError(f"{operation} RC response was not JSON") from exc
        success = 200 <= status < 300
        self.transfer._emit(
            "rclone_rc_operation_result",
            operation=operation,
            operation_success=success,
            operation_duration_s=round(time.monotonic() - started, 6),
            operation_start_epoch=started_epoch,
            operation_end_epoch=time.time(),
            status=status,
        )
        if not success:
            raise RcloneRcError(f"{operation} RC status {status}: {result}")
        if not isinstance(result, dict):
            raise RcloneRcError(f"{operation} RC response was not an object")
        return result

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=5)
        self.socket_dir.cleanup()


class BackupTransfer:
    def __init__(self, config: Config, logger: logging.Logger | None = None) -> None:
        config.validate()
        self.config = config
        self.logger = logger or logging.getLogger("backup_transfer")
        self.manifest: Manifest | None = None
        self.transfer_watchdog_kills = 0
        self.run_attempts = 0
        self.run_retries = 0
        self.run_successes = 0
        self.reconciliation_failures = 0
        self.run_shared_lock_deferrals = 0

    def _emit(self, event: str, **fields: Any) -> None:
        payload = {"event": event, "timestamp": time.time(), **fields}
        self.logger.info(json.dumps(payload, sort_keys=True))

    def _remote(self, filename: str, inprogress: bool = False) -> str:
        name = f"{filename}.inprogress" if inprogress else filename
        path = str(PurePosixPath(self.config.remote_path.strip("/")) / name)
        return f"{self.config.remote.rstrip(':')}:{path}"

    @staticmethod
    def watchdog_timeout(file_size: int) -> float:
        # size/floor*4 + 60, clamped to [120, 3600]; floor is p10-grounded (see constant).
        estimate = file_size / TRANSFER_RATE_FLOOR_BYTES_S * 4 + 60
        return min(3600.0, max(120.0, estimate))

    def _sftp_tuning_flags(self) -> tuple[str, ...]:
        """Return upload-path SFTP tuning flags, or empty if concurrency disabled (0)."""
        if self.config.sftp_concurrency <= 0:
            return ()
        return (
            "--sftp-concurrency",
            str(self.config.sftp_concurrency),
            "--sftp-chunk-size",
            self.config.sftp_chunk_size,
        )

    def _run_rclone(
        self,
        arguments: Sequence[str],
        file_size: int,
        operation: str,
        allowed_returncodes: tuple[int, ...] = (0,),
        *,
        sftp_tuned: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        # sftp_tuned=True only for upload/copyto/moveto. download_verify stays untuned
        # because concurrency on that download path failed size checks in retest.
        command = [
            self.config.rclone,
            *arguments,
            *RCLONE_FLAGS,
            *(self._sftp_tuning_flags() if sftp_tuned else ()),
            "--sftp-key-file",
            str(self.config.key_path),
        ]
        timeout = self.watchdog_timeout(file_size)
        started_epoch = time.time()
        started = time.monotonic()
        self._emit(
            "rclone_command",
            operation=operation,
            command=command,
            watchdog_timeout_s=timeout,
        )
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.transfer_watchdog_kills += 1
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
            duration = time.monotonic() - started
            progress_line, transferred_bytes = _transfer_progress(stderr)
            self._emit(
                "rclone_operation_result",
                operation=operation,
                operation_start_epoch=started_epoch,
                operation_end_epoch=time.time(),
                operation_duration_s=round(duration, 6),
                operation_success=False,
                watchdog_timeout_s=timeout,
                watchdog_killed=True,
                expected_bytes=file_size,
                transferred_bytes_estimate=transferred_bytes,
                progress_line=progress_line,
            )
            raise TransferError(
                f"{operation} exceeded watchdog timeout {timeout:.1f}s"
            )
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        duration = time.monotonic() - started
        progress_line, transferred_bytes = _transfer_progress(stderr)
        self._emit(
            "rclone_operation_result",
            operation=operation,
            operation_start_epoch=started_epoch,
            operation_end_epoch=time.time(),
            operation_duration_s=round(duration, 6),
            operation_success=result.returncode in allowed_returncodes,
            watchdog_timeout_s=timeout,
            watchdog_killed=False,
            expected_bytes=file_size,
            transferred_bytes_estimate=transferred_bytes,
            progress_line=progress_line,
            returncode=result.returncode,
        )
        if result.returncode not in allowed_returncodes:
            raise TransferError(
                f"{operation} failed with exit {result.returncode}: "
                f"{result.stderr.strip()}"
            )
        return result

    def _remote_size(
        self, remote: str, expected_size: int, operation: str
    ) -> int | None:
        result = self._run_rclone(
            ["size", "--json", remote],
            expected_size,
            operation,
            allowed_returncodes=(0, 3),
        )
        if result.returncode == 3:
            return None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise TransferError(f"{operation} returned invalid JSON") from exc
        if isinstance(payload, dict):
            size = payload.get("bytes")
            count = payload.get("count")
            if isinstance(size, int) and count == 1:
                return size
        return None

    def _verify_remote_content(
        self,
        source: Path,
        remote: str,
        expected_size: int,
    ) -> None:
        threshold = self.config.skip_sha_verify_below_bytes
        if threshold is not None and expected_size < threshold:
            # Size already confirmed via rclone size on final remote; skip
            # expensive download+SHA for tiny hive bar files.
            self._emit(
                "download_verify_skipped_size_only",
                filename=self._relative_key(source),
                size=expected_size,
                skip_sha_verify_below_bytes=threshold,
            )
            return
        verify_dir = self.config.compacted_dir / ".state" / "verify"
        verify_dir.mkdir(parents=True, exist_ok=True)
        downloaded = verify_dir / f".{source.name}.{os.getpid()}.verify"
        downloaded.unlink(missing_ok=True)
        try:
            self._run_rclone(
                ["copyto", remote, str(downloaded)],
                expected_size,
                "download_verify",
            )
            if not downloaded.is_file() or downloaded.stat().st_size != expected_size:
                raise TransferError(
                    f"downloaded remote size mismatch for {source.name}"
                )
            if _sha256_file(downloaded) != _sha256_file(source):
                raise TransferError(
                    f"remote content checksum mismatch for {source.name}"
                )
        finally:
            downloaded.unlink(missing_ok=True)

    def _move_confirmed_source(self, relative_key: str, expected_size: int) -> None:
        source = self.config.compacted_dir / relative_key
        destination = self.config.sent_dir / relative_key
        if source.exists():
            if source.stat().st_size != expected_size:
                raise TransferError(
                    f"confirmed source size changed for {relative_key}; "
                    "refusing local move"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            source_directory_fd = os.open(source.parent, os.O_RDONLY)
            try:
                os.fsync(source_directory_fd)
            finally:
                os.close(source_directory_fd)
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        if not destination.exists():
            raise TransferError(
                f"confirmed remote has no retained local source for {relative_key}"
            )
        if destination.stat().st_size != expected_size:
            raise TransferError(
                f"retained local source size mismatch for {relative_key}"
            )
        assert self.manifest is not None
        self.manifest.mark_sent(relative_key)

    def reconcile_confirmed(self) -> None:
        assert self.manifest is not None
        for row in self.manifest.confirmed():
            try:
                self._move_confirmed_source(str(row["filename"]), int(row["size"]))
            except (OSError, TransferError) as exc:
                self.reconciliation_failures += 1
                self._emit(
                    "confirmed_reconciliation_failed",
                    filename=row["filename"],
                    error=str(exc),
                )

    def _relative_key(self, source: Path) -> str:
        if self.config.layout == LAYOUT_HIVE:
            return source.relative_to(self.config.compacted_dir).as_posix()
        return source.name

    def transfer_file(self, source: Path) -> bool:
        assert self.manifest is not None
        started = time.monotonic()
        started_epoch = time.time()
        relative_key = self._relative_key(source)
        size = source.stat().st_size
        final_remote = self._remote(relative_key)
        temporary_remote = self._remote(relative_key, inprogress=True)
        attempt = self.manifest.record_attempt(relative_key, size, final_remote)
        self.run_attempts += 1
        self.run_retries += max(0, attempt - 1)
        success = False
        remotely_confirmed = False
        preexisting_final = False
        error = ""
        try:
            final_size = self._remote_size(final_remote, size, "stat_final_before_copy")
            if final_size is not None:
                preexisting_final = True
                if final_size != size:
                    raise TransferError(
                        f"remote final size mismatch for {relative_key}: "
                        f"local={size} remote={final_size}"
                    )
            else:
                self._run_rclone(
                    ["copyto", str(source), temporary_remote],
                    size,
                    "copyto",
                    sftp_tuned=True,
                )
                temporary_size = self._remote_size(
                    temporary_remote, size, "verify_temporary"
                )
                if temporary_size != size:
                    raise TransferError(
                        f"remote temporary size mismatch for {relative_key}: "
                        f"local={size} remote={temporary_size}"
                    )
                self._run_rclone(
                    ["moveto", temporary_remote, final_remote],
                    size,
                    "moveto",
                    sftp_tuned=True,
                )
                final_size = self._remote_size(final_remote, size, "verify_final")
                if final_size != size:
                    raise TransferError(
                        f"remote final size mismatch for {relative_key}: "
                        f"local={size} remote={final_size}"
                    )
            self._verify_remote_content(source, final_remote, size)
            self.manifest.mark_confirmed(relative_key)
            remotely_confirmed = True
            self._move_confirmed_source(relative_key, size)
            success = True
            self.run_successes += 1
            return True
        except (OSError, TransferError) as exc:
            error = str(exc)
            if not remotely_confirmed:
                conflict = (
                    preexisting_final
                    and (
                        "remote final size mismatch" in error
                        or "remote content checksum mismatch" in error
                    )
                )
                if conflict:
                    self.manifest.mark_conflict(relative_key, error)
                    self._emit(
                        "transfer_identity_conflict",
                        filename=relative_key,
                        size=size,
                        error=error,
                        remediation="quarantined; allocate a new output identity only after operator review",
                    )
                else:
                    self.manifest.mark_failed(relative_key, error)
            enospc = (
                isinstance(exc, OSError) and getattr(exc, "errno", None) == 28
            ) or ("No space left" in error) or ("ENOSPC" in error.upper())
            if enospc:
                self._emit(
                    "transfer_enospc_alert",
                    filename=relative_key,
                    size=size,
                    transfer_attempt=attempt,
                    error=error,
                    layout=self.config.layout,
                )
            self._emit(
                "transfer_failure",
                filename=relative_key,
                size=size,
                transfer_attempt=attempt,
                error=error,
            )
            return False
        finally:
            self._emit(
                "transfer_result",
                filename=relative_key,
                size=size,
                transfer_duration_s=round(time.monotonic() - started, 6),
                transfer_start_epoch=started_epoch,
                transfer_end_epoch=time.time(),
                watchdog_timeout_config_s=self.watchdog_timeout(size),
                transfer_success=success,
                transfer_retry_count=max(0, attempt - 1),
                transfer_attempt=attempt,
                transfer_watchdog_kills=self.transfer_watchdog_kills,
                error=error or None,
            )

    def transfer_file_with_shared_lock(self, source: Path) -> bool | None:
        """Transfer one file, or defer the remaining batch when compaction waits.

        ``None`` means no transfer was attempted because the shared lock was
        busy. The caller stops this one-shot run so the timer retries later,
        preserving the source and avoiding a busy loop while the compactor owns
        the heavy-storage section.
        """
        lock_path = self.config.shared_lock_path
        if lock_path is None:
            return self.transfer_file(source)

        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.run_shared_lock_deferrals += 1
                self._emit(
                    "transfer_deferred_shared_lock_busy",
                    filename=self._relative_key(source),
                    lock_path=str(lock_path),
                    layout=self.config.layout,
                )
                return None
            return self.transfer_file(source)
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(lock_fd)

    def _rc_remote_size(
        self,
        session: RcloneRcSession,
        relative_key: str,
        expected_size: int,
        operation: str,
        *,
        missing_allowed: bool = False,
    ) -> int | None:
        try:
            result = session.request(
                "operations/stat",
                {
                    "fs": self._remote_fs(),
                    "remote": relative_key,
                },
                operation=operation,
            )
        except RcloneRcError as exc:
            if missing_allowed and "not found" in str(exc).lower():
                return None
            raise
        item = result.get("item")
        if isinstance(item, dict) and isinstance(item.get("Size"), int):
            return int(item["Size"])
        if item is None and missing_allowed:
            return None
        raise TransferError(f"{operation} did not return an item size")

    def _remote_fs(self) -> str:
        return f"{self.config.remote.rstrip(':')}:{self.config.remote_path.strip('/')}"

    def _transfer_file_in_rc_session(
        self, source: Path, session: RcloneRcSession
    ) -> bool:
        """Transfer one hive source through the shared persistent rclone daemon."""
        assert self.manifest is not None
        started = time.monotonic()
        started_epoch = time.time()
        relative_key = self._relative_key(source)
        size = source.stat().st_size
        final_remote = self._remote(relative_key)
        attempt = self.manifest.record_attempt(relative_key, size, final_remote)
        self.run_attempts += 1
        self.run_retries += max(0, attempt - 1)
        success = False
        remotely_confirmed = False
        preexisting_final = False
        error = ""
        temporary_key = f"{relative_key}.inprogress"
        try:
            final_size = self._rc_remote_size(
                session,
                relative_key,
                size,
                "stat_final_before_copy",
                missing_allowed=True,
            )
            if final_size is not None:
                preexisting_final = True
                if final_size != size:
                    raise TransferError(
                        f"remote final size mismatch for {relative_key}: "
                        f"local={size} remote={final_size}"
                    )
            else:
                session.request(
                    "operations/copyfile",
                    {
                        "srcFs": str(self.config.compacted_dir),
                        "srcRemote": relative_key,
                        "dstFs": self._remote_fs(),
                        "dstRemote": temporary_key,
                    },
                    operation="copyto_temporary",
                )
                temporary_size = self._rc_remote_size(
                    session, temporary_key, size, "verify_temporary"
                )
                if temporary_size != size:
                    raise TransferError(
                        f"remote temporary size mismatch for {relative_key}: "
                        f"local={size} remote={temporary_size}"
                    )
                session.request(
                    "operations/movefile",
                    {
                        "srcFs": self._remote_fs(),
                        "srcRemote": temporary_key,
                        "dstFs": self._remote_fs(),
                        "dstRemote": relative_key,
                    },
                    operation="moveto_final",
                )
                final_size = self._rc_remote_size(
                    session, relative_key, size, "verify_final"
                )
                if final_size != size:
                    raise TransferError(
                        f"remote final size mismatch for {relative_key}: "
                        f"local={size} remote={final_size}"
                    )
            self._verify_remote_content(source, final_remote, size)
            self.manifest.mark_confirmed(relative_key)
            remotely_confirmed = True
            self._move_confirmed_source(relative_key, size)
            success = True
            self.run_successes += 1
            return True
        except (OSError, TransferError) as exc:
            error = str(exc)
            if not remotely_confirmed:
                conflict = (
                    preexisting_final
                    and (
                        "remote final size mismatch" in error
                        or "remote content checksum mismatch" in error
                    )
                )
                if conflict:
                    self.manifest.mark_conflict(relative_key, error)
                    self._emit(
                        "transfer_identity_conflict",
                        filename=relative_key,
                        size=size,
                        error=error,
                        remediation="quarantined; allocate a new output identity only after operator review",
                        persistent_hive_batch=True,
                    )
                else:
                    self.manifest.mark_failed(relative_key, error)
            self._emit(
                "transfer_failure",
                filename=relative_key,
                size=size,
                transfer_attempt=attempt,
                error=error,
                persistent_hive_batch=True,
            )
            return False
        finally:
            self._emit(
                "transfer_result",
                filename=relative_key,
                size=size,
                transfer_duration_s=round(time.monotonic() - started, 6),
                transfer_start_epoch=started_epoch,
                transfer_end_epoch=time.time(),
                watchdog_timeout_config_s=self.watchdog_timeout(size),
                transfer_success=success,
                transfer_retry_count=max(0, attempt - 1),
                transfer_attempt=attempt,
                transfer_watchdog_kills=self.transfer_watchdog_kills,
                persistent_hive_batch=True,
                error=error or None,
            )

    def transfer_hive_microbatch(self, sources: Sequence[Path]) -> int:
        """Run one bounded hive batch, optionally under the shared heavy lock."""
        if not sources:
            return 0
        lock_path = self.config.shared_lock_path
        acquired = False
        lock_fd: int | None = None
        try:
            if lock_path is not None:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
                # Compactor has priority when this optional lock is in use.
                for retry in range(3):
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                        break
                    except BlockingIOError:
                        self.run_shared_lock_deferrals += 1
                        self._emit(
                            "transfer_deferred_shared_lock_busy",
                            filename=self._relative_key(sources[0]),
                            lock_path=str(lock_path),
                            layout=self.config.layout,
                            deferred_files=len(sources),
                            retry=retry + 1,
                            mechanism="hive_microbatch",
                        )
                        time.sleep(0.5)
                if not acquired:
                    self._emit(
                        "hive_microbatch_deferred",
                        deferred_files=len(sources),
                        lock_path=str(lock_path),
                        retries=3,
                    )
                    return 0
            session = RcloneRcSession(
                self, max(source.stat().st_size for source in sources)
            )
            try:
                session.start()
                successes = sum(
                    self._transfer_file_in_rc_session(source, session)
                    for source in sources
                )
                self._emit(
                    "hive_microbatch_result",
                    files=len(sources),
                    successes=successes,
                    persistent_rclone_session=True,
                )
                return successes
            finally:
                session.close()
        finally:
            if acquired and lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            if lock_fd is not None:
                os.close(lock_fd)

    def _flat_candidate_files(self) -> list[Path]:
        conflicts = self.manifest.conflicts() if self.manifest is not None else set()
        return sorted(
            path
            for path in self.config.compacted_dir.iterdir()
            if (
                path.is_file()
                and path.name.startswith("spread_")
                and path.suffix == ".parquet"
                and path.name not in conflicts
            )
        )

    def _hive_candidate_files(self) -> list[Path]:
        root = self.config.compacted_dir
        if not root.exists():
            return []
        conflicts = self.manifest.conflicts() if self.manifest is not None else set()
        candidates: list[Path] = []
        for path in root.rglob("*.parquet"):
            if not path.is_file():
                continue
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            if any(part in HIVE_SKIP_DIR_NAMES for part in relative.parts):
                continue
            if relative.as_posix() in conflicts:
                continue
            candidates.append(path)
        return sorted(candidates)

    def _candidate_files(self) -> list[Path]:
        if self.config.layout == LAYOUT_HIVE:
            files = self._hive_candidate_files()
        else:
            files = self._flat_candidate_files()
        if self.config.max_files is not None:
            return files[: self.config.max_files]
        return files

    def remove_expired_sent_files(self) -> int:
        assert self.manifest is not None
        cutoff = time.time() - self.config.sent_retention_hours * 3600
        removed = 0
        if not self.config.sent_dir.exists():
            return removed
        for row in self.manifest.expired_sent(cutoff):
            path = self.config.sent_dir / str(row["filename"])
            if path.is_file():
                path.unlink()
                removed += 1
        return removed

    def backlog(self) -> tuple[int, int]:
        # Backlog ignores max_files so ops see the full pending set.
        if self.config.layout == LAYOUT_HIVE:
            files = self._hive_candidate_files()
        else:
            files = self._flat_candidate_files()
        return len(files), sum(path.stat().st_size for path in files)

    def run(self) -> int:
        started = time.monotonic()
        self.config.compacted_dir.mkdir(parents=True, exist_ok=True)
        self.config.sent_dir.mkdir(parents=True, exist_ok=True)
        self.manifest = Manifest(self.config.manifest_path)
        try:
            self.reconcile_confirmed()
            self.remove_expired_sent_files()
            candidates = self._candidate_files()
            if (
                self.config.layout == LAYOUT_HIVE
                and self.config.effective_hive_batch_size > 1
            ):
                batch_size = self.config.effective_hive_batch_size
                for index in range(0, len(candidates), batch_size):
                    self.transfer_hive_microbatch(
                        candidates[index : index + batch_size]
                    )
            else:
                for source in candidates:
                    result = self.transfer_file_with_shared_lock(source)
                    if result is None:
                        break
            backlog_count, backlog_bytes = self.backlog()
            self._emit(
                "backup_summary",
                layout=self.config.layout,
                transfer_success=self.run_attempts == self.run_successes,
                transfer_retry_count=self.run_retries,
                transfer_attempt=self.run_attempts,
                transfer_duration_s=round(time.monotonic() - started, 6),
                backlog_files_count=backlog_count,
                backlog_size_mb=round(backlog_bytes / (1024 * 1024), 6),
                transfer_watchdog_kills=self.transfer_watchdog_kills,
                reconciliation_failures=self.reconciliation_failures,
                shared_lock_deferrals=self.run_shared_lock_deferrals,
                hive_batch_size=(
                    self.config.effective_hive_batch_size
                    if self.config.layout == LAYOUT_HIVE
                    else None
                ),
            )
            return (
                0
                if (
                    self.run_attempts == self.run_successes
                    and self.reconciliation_failures == 0
                )
                else 1
            )
        finally:
            self.manifest.close()
            self.manifest = None


def parse_args(argv: Sequence[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(
        description="One-shot compacted-file backup; schedule externally (for example every 5m)."
    )
    parser.add_argument(
        "--compacted-dir",
        default=os.environ.get("BACKUP_COMPACTED_DIR", str(DEFAULT_COMPACTED_DIR)),
    )
    parser.add_argument("--remote", default=os.environ.get("BACKUP_RCLONE_REMOTE"))
    parser.add_argument(
        "--remote-path", default=os.environ.get("BACKUP_RCLONE_PATH")
    )
    parser.add_argument(
        "--key-path", default=os.environ.get("BACKUP_SFTP_KEY_PATH")
    )
    parser.add_argument(
        "--rclone", default=os.environ.get("BACKUP_RCLONE_BINARY", "rclone")
    )
    parser.add_argument(
        "--sent-retention-hours",
        type=float,
        default=float(
            os.environ.get(
                "BACKUP_SENT_RETENTION_HOURS",
                str(DEFAULT_SENT_RETENTION_HOURS),
            )
        ),
    )
    parser.add_argument(
        "--layout",
        default=os.environ.get("BACKUP_LAYOUT", LAYOUT_FLAT),
        choices=sorted(VALID_LAYOUTS),
        help=(
            f"{LAYOUT_FLAT}: spread_*.parquet at source root (ticks); "
            f"{LAYOUT_HIVE}: recursive *.parquet under hive partitions (bars)."
        ),
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=(
            int(os.environ["BACKUP_MAX_FILES"])
            if os.environ.get("BACKUP_MAX_FILES")
            else None
        ),
        help="Optional cap on files transferred this run (smoke / catch-up throttle).",
    )
    parser.add_argument(
        "--skip-sha-verify-below-bytes",
        type=int,
        default=(
            int(os.environ["BACKUP_SKIP_SHA_VERIFY_BELOW_BYTES"])
            if os.environ.get("BACKUP_SKIP_SHA_VERIFY_BELOW_BYTES")
            else None
        ),
        help=(
            "Skip SHA download_verify for files smaller than this size "
            "(remote size match still required). Useful for hive/tiny bars."
        ),
    )
    parser.add_argument(
        "--shared-lock-path",
        default=os.environ.get("BACKUP_SHARED_LOCK_PATH"),
        help=(
            "Optional absolute host-local lock acquired and released for each "
            "file transfer; use for bars/compactor serialization."
        ),
    )
    parser.add_argument(
        "--hive-batch-size",
        type=int,
        default=(
            int(os.environ["BACKUP_HIVE_BATCH_SIZE"])
            if os.environ.get("BACKUP_HIVE_BATCH_SIZE")
            else None
        ),
        help=(
            "Maximum hive files per persistent rclone/SFTP micro-batch. "
            "Defaults to 32 for hive layout when a shared lock is configured."
        ),
    )
    parser.add_argument(
        "--sftp-concurrency",
        type=int,
        default=int(
            os.environ.get(
                "BACKUP_RCLONE_SFTP_CONCURRENCY",
                str(DEFAULT_SFTP_CONCURRENCY),
            )
        ),
        help=(
            "SFTP concurrency for upload/copyto/moveto "
            f"(default {DEFAULT_SFTP_CONCURRENCY}; 0 disables tuning). "
            "Not applied to download_verify."
        ),
    )
    parser.add_argument(
        "--sftp-chunk-size",
        default=os.environ.get(
            "BACKUP_RCLONE_SFTP_CHUNK_SIZE",
            DEFAULT_SFTP_CHUNK_SIZE,
        ),
        help=(
            "SFTP chunk size for upload/copyto/moveto "
            f"(default {DEFAULT_SFTP_CHUNK_SIZE}). Not applied to download_verify."
        ),
    )
    args = parser.parse_args(argv)
    if not args.remote:
        parser.error("--remote or BACKUP_RCLONE_REMOTE is required")
    if not args.remote_path:
        parser.error("--remote-path or BACKUP_RCLONE_PATH is required")
    if not args.key_path:
        parser.error("--key-path or BACKUP_SFTP_KEY_PATH is required")
    config = Config(
        compacted_dir=Path(args.compacted_dir),
        remote=args.remote,
        remote_path=args.remote_path,
        key_path=Path(args.key_path),
        rclone=args.rclone,
        sent_retention_hours=args.sent_retention_hours,
        sftp_concurrency=args.sftp_concurrency,
        sftp_chunk_size=args.sftp_chunk_size,
        layout=args.layout,
        max_files=args.max_files,
        skip_sha_verify_below_bytes=args.skip_sha_verify_below_bytes,
        shared_lock_path=(
            Path(args.shared_lock_path) if args.shared_lock_path else None
        ),
        hive_batch_size=args.hive_batch_size,
    )
    try:
        config.validate()
    except ConfigurationError as exc:
        parser.error(str(exc))
    return config


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    lock_path = Path(
        os.environ.get("BACKUP_TRANSFER_LOCK_PATH", str(DEFAULT_LOCK_PATH))
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logging.getLogger("backup_transfer").info(
                json.dumps(
                    {
                        "event": "transfer_skipped_lock_held",
                        "timestamp": time.time(),
                        "lock_path": str(lock_path),
                    },
                    sort_keys=True,
                )
            )
            return 0
        return BackupTransfer(parse_args(argv)).run()
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
