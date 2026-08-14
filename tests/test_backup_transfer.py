from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from app.storage.backup_transfer import (
    BackupTransfer,
    Config,
    LAYOUT_HIVE,
    Manifest,
    TRANSFER_RATE_FLOOR_BYTES_S,
    _transfer_progress,
)


FAKE_RCLONE = r"""#!/usr/bin/env python3
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

root = pathlib.Path(os.environ["FAKE_REMOTE_ROOT"])
mode = os.environ.get("FAKE_RCLONE_MODE", "normal")
command = sys.argv[1]

def remote_path(value):
    relative = value.split(":", 1)[1]
    return root / relative

if command == "size":
    path = remote_path(sys.argv[3])
    if not path.exists():
        sys.exit(3)
    print(json.dumps({"count": 1, "bytes": path.stat().st_size}))
elif command == "copyto":
    source_arg = sys.argv[2]
    destination_arg = sys.argv[3]
    source = (
        remote_path(source_arg)
        if ":" in source_arg
        else pathlib.Path(source_arg)
    )
    destination = (
        remote_path(destination_arg)
        if ":" in destination_arg
        else pathlib.Path(destination_arg)
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if mode == "hang":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        pathlib.Path(os.environ["FAKE_CHILD_PID_FILE"]).write_text(str(child.pid))
        time.sleep(60)
    data = source.read_bytes()
    if mode == "mismatch":
        data = data[:-1]
    destination.write_bytes(data)
elif command == "moveto":
    source = remote_path(sys.argv[2])
    destination = remote_path(sys.argv[3])
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)
else:
    print("unsupported command", file=sys.stderr)
    sys.exit(2)
"""


class BackupTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.compacted = self.root / "compacted"
        self.compacted.mkdir()
        self.remote = self.root / "remote"
        self.remote.mkdir()
        self.key = self.root / "sftp-key"
        self.key.write_text("test-only-key", encoding="utf-8")
        self.rclone = self.root / "fake-rclone"
        self.rclone.write_text(textwrap.dedent(FAKE_RCLONE), encoding="utf-8")
        self.rclone.chmod(self.rclone.stat().st_mode | stat.S_IXUSR)
        self.child_pid_file = self.root / "child.pid"
        self.environment = mock.patch.dict(
            os.environ,
            {
                "FAKE_REMOTE_ROOT": str(self.remote),
                "FAKE_RCLONE_MODE": "normal",
                "FAKE_CHILD_PID_FILE": str(self.child_pid_file),
            },
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def config(self, retention: float = 12.0) -> Config:
        return Config(
            compacted_dir=self.compacted,
            remote="fake",
            remote_path="archive",
            key_path=self.key,
            rclone=str(self.rclone),
            sent_retention_hours=retention,
        )

    def test_transfer_progress_extracts_last_partial_byte_count(self) -> None:
        line, transferred = _transfer_progress(
            "Transferred: 1.000 MiBytes / 20 MiBytes, 5%\r"
            "Transferred: 7.500 MiBytes / 20 MiBytes, 37%"
        )

        self.assertEqual(
            line,
            "Transferred: 7.500 MiBytes / 20 MiBytes, 37%",
        )
        self.assertEqual(transferred, int(7.5 * 1024 * 1024))

    def test_watchdog_floor_is_p10_grounded(self) -> None:
        # Recalibrated from old 2.3 MiB/s to ~0.5 MiB/s (measured p10 ~0.60 with headroom).
        self.assertAlmostEqual(TRANSFER_RATE_FLOOR_BYTES_S, 0.5 * 1024 * 1024)
        timeout = BackupTransfer.watchdog_timeout(20 * 1024 * 1024)
        self.assertGreaterEqual(timeout, 120.0)
        self.assertLessEqual(timeout, 3600.0)

    def _capture_rclone_commands(self, transfer: BackupTransfer) -> list[dict]:
        recorded: list[dict] = []
        logger = logging.getLogger(f"backup_transfer_test_{id(self)}")
        logger.handlers.clear()
        logger.setLevel(logging.INFO)
        logger.propagate = False

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                payload = json.loads(record.getMessage())
                if payload.get("event") == "rclone_command":
                    recorded.append(payload)

        handler = Capture()
        logger.addHandler(handler)
        transfer.logger = logger
        return recorded

    def test_copyto_and_moveto_include_sftp_tuning_flags(self) -> None:
        source = self.compacted / "spread_tuned.parquet"
        source.write_bytes(b"tuned-upload")
        transfer = BackupTransfer(self.config())
        commands = self._capture_rclone_commands(transfer)

        self.assertEqual(transfer.run(), 0)

        copyto = next(c for c in commands if c["operation"] == "copyto")
        moveto = next(c for c in commands if c["operation"] == "moveto")
        for payload in (copyto, moveto):
            argv = payload["command"]
            self.assertIn("--sftp-concurrency", argv)
            self.assertEqual(argv[argv.index("--sftp-concurrency") + 1], "8")
            self.assertIn("--sftp-chunk-size", argv)
            self.assertEqual(argv[argv.index("--sftp-chunk-size") + 1], "128k")

    def test_download_verify_omits_sftp_tuning_flags(self) -> None:
        source = self.compacted / "spread_verify.parquet"
        source.write_bytes(b"verify-safe")
        transfer = BackupTransfer(self.config())
        commands = self._capture_rclone_commands(transfer)

        self.assertEqual(transfer.run(), 0)

        verify = next(c for c in commands if c["operation"] == "download_verify")
        argv = verify["command"]
        self.assertNotIn("--sftp-concurrency", argv)
        self.assertNotIn("--sftp-chunk-size", argv)
        # Size/stat ops also stay untuned.
        for payload in commands:
            if payload["operation"].startswith("stat_") or payload[
                "operation"
            ].startswith("verify_"):
                if payload["operation"] == "download_verify":
                    continue
                self.assertNotIn("--sftp-concurrency", payload["command"])

    def test_sftp_tuning_disabled_when_concurrency_zero(self) -> None:
        source = self.compacted / "spread_untuned.parquet"
        source.write_bytes(b"no-flags")
        cfg = self.config()
        cfg = Config(
            compacted_dir=cfg.compacted_dir,
            remote=cfg.remote,
            remote_path=cfg.remote_path,
            key_path=cfg.key_path,
            rclone=cfg.rclone,
            sent_retention_hours=cfg.sent_retention_hours,
            sftp_concurrency=0,
            sftp_chunk_size="128k",
        )
        transfer = BackupTransfer(cfg)
        commands = self._capture_rclone_commands(transfer)

        self.assertEqual(transfer.run(), 0)
        copyto = next(c for c in commands if c["operation"] == "copyto")
        self.assertNotIn("--sftp-concurrency", copyto["command"])
        self.assertNotIn("--sftp-chunk-size", copyto["command"])

    def manifest_row(self, filename: str) -> sqlite3.Row:
        connection = sqlite3.connect(
            str(self.compacted / ".state" / "backup_manifest.sqlite3")
        )
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT * FROM transfers WHERE filename = ?", (filename,)
            ).fetchone()
            self.assertIsNotNone(row)
            return row
        finally:
            connection.close()

    def test_success_copies_verifies_and_moves_source(self) -> None:
        source = self.compacted / "spread_batch.parquet"
        source.write_bytes(b"compacted-data")

        result = BackupTransfer(self.config()).run()

        self.assertEqual(result, 0)
        self.assertEqual(
            (self.remote / "archive" / source.name).read_bytes(), b"compacted-data"
        )
        self.assertFalse(source.exists())
        self.assertEqual(
            (self.compacted / "sent" / source.name).read_bytes(), b"compacted-data"
        )
        self.assertEqual(self.manifest_row(source.name)["state"], "sent")

    def test_existing_same_size_different_content_is_rejected(self) -> None:
        source = self.compacted / "spread_already.parquet"
        source.write_bytes(b"same-data")
        final = self.remote / "archive" / source.name
        final.parent.mkdir(parents=True)
        final.write_bytes(b"123456789")

        result = BackupTransfer(self.config()).run()

        self.assertEqual(result, 1)
        self.assertTrue(source.exists())
        self.assertFalse((self.compacted / "sent" / source.name).exists())
        self.assertEqual(self.manifest_row(source.name)["state"], "conflict")

    def test_existing_same_content_remote_is_idempotently_reconciled(self) -> None:
        source = self.compacted / "spread_already.parquet"
        source.write_bytes(b"same-data")
        final = self.remote / "archive" / source.name
        final.parent.mkdir(parents=True)
        final.write_bytes(b"same-data")

        result = BackupTransfer(self.config()).run()

        self.assertEqual(result, 0)
        self.assertFalse(source.exists())
        self.assertTrue((self.compacted / "sent" / source.name).exists())
        self.assertEqual(self.manifest_row(source.name)["attempts"], 1)

    def test_remote_size_mismatch_never_moves_source(self) -> None:
        source = self.compacted / "spread_mismatch.parquet"
        source.write_bytes(b"source")
        final = self.remote / "archive" / source.name
        final.parent.mkdir(parents=True)
        final.write_bytes(b"wrong-size")

        result = BackupTransfer(self.config()).run()

        self.assertEqual(result, 1)
        self.assertTrue(source.exists())
        self.assertFalse((self.compacted / "sent" / source.name).exists())
        self.assertEqual(self.manifest_row(source.name)["state"], "conflict")

    def test_identity_conflict_is_not_retried(self) -> None:
        source = self.compacted / "spread_conflict.parquet"
        source.write_bytes(b"source")
        final = self.remote / "archive" / source.name
        final.parent.mkdir(parents=True)
        final.write_bytes(b"wrong-size")

        self.assertEqual(BackupTransfer(self.config()).run(), 1)
        first = self.manifest_row(source.name)
        self.assertEqual(first["state"], "conflict")
        self.assertEqual(first["attempts"], 1)

        self.assertEqual(BackupTransfer(self.config()).run(), 0)
        second = self.manifest_row(source.name)
        self.assertEqual(second["attempts"], 1)
        self.assertTrue(source.exists())

    def test_watchdog_kills_process_group_and_preserves_backlog(self) -> None:
        source = self.compacted / "spread_hung.parquet"
        source.write_bytes(b"never-move")
        os.environ["FAKE_RCLONE_MODE"] = "hang"
        transfer = BackupTransfer(self.config())

        with (
            mock.patch.object(transfer, "_remote_size", return_value=None),
            mock.patch.object(transfer, "watchdog_timeout", return_value=1.0),
        ):
            result = transfer.run()

        self.assertEqual(result, 1)
        self.assertEqual(transfer.transfer_watchdog_kills, 1)
        self.assertTrue(source.exists())
        self.assertEqual(transfer.backlog(), (1, len(b"never-move")))
        child_pid = int(self.child_pid_file.read_text(encoding="utf-8"))
        child_is_gone = False
        for _ in range(20):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                child_is_gone = True
                break
            time.sleep(0.05)
        self.assertTrue(child_is_gone, "watchdog left rclone child process alive")

    def test_failed_transfer_leaves_source_and_backlog_grows(self) -> None:
        first = self.compacted / "spread_first.parquet"
        first.write_bytes(b"1111")
        os.environ["FAKE_RCLONE_MODE"] = "mismatch"
        transfer = BackupTransfer(self.config())

        self.assertEqual(transfer.run(), 1)
        second = self.compacted / "spread_second.parquet"
        second.write_bytes(b"22")

        self.assertEqual(transfer.backlog(), (2, 6))
        self.assertTrue(first.exists())
        self.assertFalse((self.compacted / "sent" / first.name).exists())

    def test_confirmed_manifest_reconciles_remaining_source(self) -> None:
        source = self.compacted / "spread_confirmed.parquet"
        source.write_bytes(b"confirmed")
        manifest = Manifest(
            self.compacted / ".state" / "backup_manifest.sqlite3"
        )
        manifest.record_attempt(
            source.name,
            source.stat().st_size,
            "fake:archive/spread_confirmed.parquet",
        )
        manifest.mark_confirmed(source.name)
        manifest.close()

        result = BackupTransfer(self.config()).run()

        self.assertEqual(result, 0)
        self.assertFalse(source.exists())
        self.assertTrue((self.compacted / "sent" / source.name).exists())
        self.assertEqual(self.manifest_row(source.name)["state"], "sent")

    def test_confirmed_manifest_missing_local_copy_returns_failure(self) -> None:
        filename = "spread_missing.parquet"
        manifest = Manifest(
            self.compacted / ".state" / "backup_manifest.sqlite3"
        )
        manifest.record_attempt(
            filename,
            9,
            f"fake:archive/{filename}",
        )
        manifest.mark_confirmed(filename)
        manifest.close()

        result = BackupTransfer(self.config()).run()

        self.assertEqual(result, 1)
        self.assertEqual(self.manifest_row(filename)["state"], "confirmed")

    def test_retention_only_removes_expired_sent_files(self) -> None:
        sent = self.compacted / "sent"
        sent.mkdir()
        expired = sent / "expired.parquet"
        recent = sent / "recent.parquet"
        source = self.compacted / "spread_pending.parquet"
        for path in (expired, recent, source):
            path.write_bytes(path.name.encode())
        old = time.time() - 7200
        os.utime(expired, (old, old))
        os.utime(source, (old, old))
        manifest = Manifest(
            self.compacted / ".state" / "backup_manifest.sqlite3"
        )
        for path in (expired, recent):
            manifest.record_attempt(path.name, path.stat().st_size, f"fake:{path.name}")
            manifest.mark_sent(path.name)
        with manifest.connection:
            manifest.connection.execute(
                "UPDATE transfers SET sent_at = ? WHERE filename = ?",
                (old, expired.name),
            )
        manifest.close()
        final = self.remote / "archive" / source.name
        final.parent.mkdir(parents=True)
        final.write_bytes(b"x")

        BackupTransfer(self.config(retention=1.0)).run()

        self.assertFalse(expired.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(source.exists())

    def test_old_backlog_file_is_retained_for_hours_after_confirmation(self) -> None:
        source = self.compacted / "spread_old.parquet"
        source.write_bytes(b"old-backlog")
        old = time.time() - 48 * 3600
        os.utime(source, (old, old))

        self.assertEqual(BackupTransfer(self.config(retention=12.0)).run(), 0)

        retained = self.compacted / "sent" / source.name
        self.assertTrue(retained.exists())
        self.assertEqual(self.manifest_row(source.name)["state"], "sent")

    def test_only_consolidated_parquet_files_enter_backlog(self) -> None:
        consolidated = self.compacted / "spread_window.parquet"
        consolidated.write_bytes(b"ready")
        (self.compacted / "notes.txt").write_text("ignore", encoding="utf-8")
        (self.compacted / "spread_partial.inprogress").write_bytes(b"ignore")
        (self.compacted / "other.parquet").write_bytes(b"ignore")

        transfer = BackupTransfer(self.config())

        self.assertEqual(transfer.backlog(), (1, len(b"ready")))

    def test_main_skips_when_lock_held(self) -> None:
        import fcntl

        from app.storage import backup_transfer

        lock_path = self.root / "backup.lock"
        lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with mock.patch.dict(
                os.environ,
                {
                    "BACKUP_TRANSFER_LOCK_PATH": str(lock_path),
                    "BACKUP_RCLONE_REMOTE": "fake",
                    "BACKUP_RCLONE_PATH": "archive",
                    "BACKUP_SFTP_KEY_PATH": str(self.key),
                    "BACKUP_COMPACTED_DIR": str(self.compacted),
                    "BACKUP_RCLONE_BINARY": str(self.rclone),
                },
            ):
                self.assertEqual(backup_transfer.main([]), 0)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def test_hive_layout_preserves_relative_path_and_skips_sent(self) -> None:
        bars = self.root / "bars"
        relative = Path("bar_5m/base_coin=BTC/event_date=2026-08-05")
        source = bars / relative / "batch_0001.parquet"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"bar-bytes")
        ignored = bars / "sent" / relative / "already_sent.parquet"
        ignored.parent.mkdir(parents=True)
        ignored.write_bytes(b"ignore-me")
        cfg = Config(
            compacted_dir=bars,
            remote="fake",
            remote_path="spread-bars",
            key_path=self.key,
            rclone=str(self.rclone),
            layout=LAYOUT_HIVE,
        )

        result = BackupTransfer(cfg).run()

        self.assertEqual(result, 0)
        remote_path = self.remote / "spread-bars" / relative / "batch_0001.parquet"
        self.assertEqual(remote_path.read_bytes(), b"bar-bytes")
        self.assertFalse(source.exists())
        sent = bars / "sent" / relative / "batch_0001.parquet"
        self.assertEqual(sent.read_bytes(), b"bar-bytes")
        self.assertTrue(ignored.exists())
        connection = sqlite3.connect(str(bars / ".state" / "backup_manifest.sqlite3"))
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT * FROM transfers WHERE filename = ?",
                ("bar_5m/base_coin=BTC/event_date=2026-08-05/batch_0001.parquet",),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["state"], "sent")
        finally:
            connection.close()

    def test_hive_max_files_limits_transfer_but_not_backlog(self) -> None:
        bars = self.root / "bars_cap"
        for name in ("a.parquet", "b.parquet"):
            path = bars / "bar_5m" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
        cfg = Config(
            compacted_dir=bars,
            remote="fake",
            remote_path="spread-bars",
            key_path=self.key,
            rclone=str(self.rclone),
            layout=LAYOUT_HIVE,
            max_files=1,
        )
        transfer = BackupTransfer(cfg)

        self.assertEqual(transfer.run(), 0)
        self.assertEqual(transfer.run_attempts, 1)
        self.assertEqual(transfer.backlog(), (1, len(b"b.parquet")))

    def test_shared_lock_defers_without_attempting_or_moving_source(self) -> None:
        source = self.compacted / "spread_deferred.parquet"
        source.write_bytes(b"stay-pending")
        shared_lock = self.root / "heavy-storage.lock"
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import fcntl, os, sys, time; "
                    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600); "
                    "fcntl.flock(fd, fcntl.LOCK_EX); print('locked', flush=True); "
                    "time.sleep(30)"
                ),
                str(shared_lock),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        events: list[dict[str, object]] = []
        logger = logging.getLogger(f"backup_transfer_shared_lock_{id(self)}")
        logger.handlers.clear()
        logger.setLevel(logging.INFO)
        logger.propagate = False

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                events.append(json.loads(record.getMessage()))

        logger.addHandler(Capture())
        cfg = Config(
            compacted_dir=self.compacted,
            remote="fake",
            remote_path="archive",
            key_path=self.key,
            rclone=str(self.rclone),
            shared_lock_path=shared_lock,
        )
        try:
            transfer = BackupTransfer(cfg, logger=logger)
            self.assertEqual(transfer.run(), 0)
        finally:
            holder.terminate()
            holder.wait(timeout=5)

        self.assertEqual(transfer.run_attempts, 0)
        self.assertEqual(transfer.run_shared_lock_deferrals, 1)
        self.assertTrue(source.exists())
        self.assertFalse((self.compacted / "sent" / source.name).exists())
        self.assertIn(
            "transfer_deferred_shared_lock_busy",
            [event["event"] for event in events],
        )

    def test_hive_microbatch_uses_one_persistent_session_per_bounded_batch(self) -> None:
        bars = self.root / "bars_microbatch"
        for name in ("a.parquet", "b.parquet", "c.parquet"):
            path = bars / "bar_5m" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
        cfg = Config(
            compacted_dir=bars,
            remote="fake",
            remote_path="spread-bars",
            key_path=self.key,
            rclone=str(self.rclone),
            layout=LAYOUT_HIVE,
            shared_lock_path=self.root / "heavy-storage.lock",
            hive_batch_size=2,
        )
        transfer = BackupTransfer(cfg)
        outcomes = iter((True, False, True))

        def transfer_in_session(source: Path, session: object) -> bool:
            outcome = next(outcomes)
            transfer.run_attempts += 1
            if outcome:
                transfer.run_successes += 1
            return outcome

        with (
            mock.patch("app.storage.backup_transfer.RcloneRcSession") as session,
            mock.patch.object(
                transfer,
                "_transfer_file_in_rc_session",
                side_effect=transfer_in_session,
            ) as per_file,
        ):
            self.assertEqual(transfer.run(), 1)

        self.assertEqual(session.call_count, 2)
        self.assertEqual(session.return_value.start.call_count, 2)
        self.assertEqual(session.return_value.close.call_count, 2)
        self.assertEqual(per_file.call_count, 3)

    def test_hive_microbatch_reuses_session_without_shared_lock(self) -> None:
        bars = self.root / "bars_unlocked_microbatch"
        for name in ("a.parquet", "b.parquet"):
            path = bars / "bar_5m" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
        cfg = Config(
            compacted_dir=bars,
            remote="fake",
            remote_path="spread-bars",
            key_path=self.key,
            rclone=str(self.rclone),
            layout=LAYOUT_HIVE,
            hive_batch_size=32,
        )
        transfer = BackupTransfer(cfg)

        with (
            mock.patch("app.storage.backup_transfer.RcloneRcSession") as session,
            mock.patch.object(
                transfer, "_transfer_file_in_rc_session", return_value=True
            ) as per_file,
        ):
            self.assertEqual(transfer.run(), 0)

        self.assertEqual(session.call_count, 1)
        self.assertEqual(session.return_value.start.call_count, 1)
        self.assertEqual(session.return_value.close.call_count, 1)
        self.assertEqual(per_file.call_count, 2)

    def test_hive_microbatch_failure_never_moves_unconfirmed_source(self) -> None:
        bars = self.root / "bars_rc_failure"
        source = bars / "bar_5m" / "batch.parquet"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"bar")
        cfg = Config(
            compacted_dir=bars,
            remote="fake",
            remote_path="spread-bars",
            key_path=self.key,
            rclone=str(self.rclone),
            layout=LAYOUT_HIVE,
            shared_lock_path=self.root / "heavy-storage.lock",
        )
        transfer = BackupTransfer(cfg)
        transfer.manifest = Manifest(transfer.config.manifest_path)
        session = mock.Mock()
        try:
            with (
                mock.patch.object(
                    transfer,
                    "_rc_remote_size",
                    side_effect=[None, len(b"bar"), len(b"bar") - 1],
                ),
                mock.patch.object(transfer, "_verify_remote_content"),
            ):
                self.assertFalse(transfer._transfer_file_in_rc_session(source, session))
        finally:
            transfer.manifest.close()
            transfer.manifest = None

        self.assertTrue(source.exists())
        self.assertFalse((bars / "sent" / "bar_5m" / "batch.parquet").exists())
        connection = sqlite3.connect(str(bars / ".state" / "backup_manifest.sqlite3"))
        try:
            state = connection.execute(
                "SELECT state FROM transfers WHERE filename = ?",
                ("bar_5m/batch.parquet",),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(state, ("failed",))

    def test_rc_missing_final_is_a_valid_pre_copy_state(self) -> None:
        transfer = BackupTransfer(self.config())
        session = mock.Mock()
        session.request.return_value = {"item": None}

        result = transfer._rc_remote_size(
            session,
            "bar_5m/batch.parquet",
            3,
            "stat_final_before_copy",
            missing_allowed=True,
        )

        self.assertIsNone(result)

    def test_hive_busy_lock_defers_each_microbatch_without_aborting_run(self) -> None:
        import fcntl

        bars = self.root / "bars_busy_batches"
        for name in ("a.parquet", "b.parquet", "c.parquet"):
            path = bars / "bar_5m" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
        shared_lock = self.root / "heavy-storage.lock"
        lock_fd = os.open(str(shared_lock), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cfg = Config(
            compacted_dir=bars,
            remote="fake",
            remote_path="spread-bars",
            key_path=self.key,
            rclone=str(self.rclone),
            layout=LAYOUT_HIVE,
            shared_lock_path=shared_lock,
            hive_batch_size=2,
        )
        try:
            with mock.patch("app.storage.backup_transfer.time.sleep"):
                transfer = BackupTransfer(cfg)
                self.assertEqual(transfer.run(), 0)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

        self.assertEqual(transfer.run_attempts, 0)
        self.assertEqual(transfer.run_shared_lock_deferrals, 6)
        self.assertTrue((bars / "bar_5m" / "a.parquet").exists())
        self.assertTrue((bars / "bar_5m" / "c.parquet").exists())


if __name__ == "__main__":
    unittest.main()
