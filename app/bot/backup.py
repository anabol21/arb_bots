"""One-shot rclone copy of B-bot journal. Never import app.storage.backup_transfer."""

from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
from pathlib import Path

from app.bot.paths import (
    resolve_backup_lock,
    resolve_data_root,
    resolve_rclone_bin,
    resolve_rclone_path,
    resolve_rclone_remote,
)

# D collector remote prefixes — refuse anywhere in the remote path, any remote name.
_D_REMOTE_MARKERS = ("spread-compacted", "spread-bars")
_ALLOWED_REMOTE_PREFIXES = ("spread-bbot", "spread-bbot-gear2")
_D_BACKUP_LOCK = Path("/run/spread-backup.lock")


class BackupError(RuntimeError):
    pass


def assert_bbot_backup_dest(*, remote: str, remote_path: str, lock_path: Path) -> None:
    """Fail-closed guard: only ``spread-bbot`` destinations; never D paths or D lock.

    Callable without rclone for unit tests.
    """
    rem = (remote or "").rstrip(":")
    rpath = (remote_path or "").strip().strip("/")
    lock = Path(lock_path)

    # Refuse D collector flock even if BBOT_BACKUP_LOCK / --lock overrides.
    if lock == _D_BACKUP_LOCK or lock.name == "spread-backup.lock":
        raise BackupError(f"refusing D backup lock: {lock}")

    if not rpath:
        raise BackupError("refusing empty remote path (expected spread-bbot prefix)")

    # Containment check anywhere in rpath (covers backup1tb:foo/spread-compacted).
    for marker in _D_REMOTE_MARKERS:
        if marker in rpath:
            raise BackupError(f"refusing D remote path marker {marker!r}: {rpath}")

    # Intended prefixes only: spread-bbot, spread-bbot-gear2, or nested under either.
    allowed = False
    for prefix in _ALLOWED_REMOTE_PREFIXES:
        if rpath == prefix or rpath.startswith(prefix + "/"):
            allowed = True
            break
    if not allowed:
        raise BackupError(
            f"refusing remote path (allowed prefixes {_ALLOWED_REMOTE_PREFIXES}): {rpath}"
        )

    if not rem:
        raise BackupError("refusing empty rclone remote name")


def run_backup(
    *,
    data_root: Path | None = None,
    rclone_bin: str | None = None,
    remote: str | None = None,
    remote_path: str | None = None,
    lock_path: Path | None = None,
) -> int:
    """Copy ``{data_root}/journal`` -> ``{remote}:{remote_path}`` under exclusive flock."""
    root = Path(data_root) if data_root is not None else resolve_data_root()
    journal = root / "journal"
    bin_path = rclone_bin or resolve_rclone_bin()
    rem = (remote or resolve_rclone_remote()).rstrip(":")
    rpath = (remote_path or resolve_rclone_path()).strip().strip("/")
    lock = Path(lock_path) if lock_path is not None else resolve_backup_lock()

    assert_bbot_backup_dest(remote=rem, remote_path=rpath, lock_path=lock)

    if not Path(bin_path).is_file() and not _which(bin_path):
        print(
            f"bbot_backup_error | rclone_missing | path={bin_path}",
            file=sys.stderr,
        )
        return 2

    if not journal.is_dir():
        print(
            f"bbot_backup_error | journal_missing | path={journal}",
            file=sys.stderr,
        )
        return 3

    dest = f"{rem}:{rpath}"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(
                f"bbot_backup_deferred | lock_busy | lock={lock}",
                file=sys.stderr,
            )
            return 4

        cmd = [
            bin_path,
            "copy",
            str(journal),
            dest,
            "--create-empty-src-dirs",
        ]
        print(
            f"bbot_backup_start | src={journal} | dest={dest} | lock={lock}",
            flush=True,
        )
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            print(
                f"bbot_backup_error | rclone_failed | code={proc.returncode} | err={err}",
                file=sys.stderr,
            )
            return proc.returncode or 1
        print(f"bbot_backup_ok | dest={dest}", flush=True)
        return 0
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)


def _which(name: str) -> bool:
    from shutil import which

    return which(name) is not None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="B-bot journal rclone backup (oneshot)")
    parser.add_argument("--data-root", default=None)
    parser.add_argument("--rclone", default=None)
    parser.add_argument("--remote", default=None)
    parser.add_argument("--remote-path", default=None)
    parser.add_argument("--lock", default=None)
    args = parser.parse_args(argv)
    try:
        return run_backup(
            data_root=Path(args.data_root) if args.data_root else None,
            rclone_bin=args.rclone,
            remote=args.remote,
            remote_path=args.remote_path,
            lock_path=Path(args.lock) if args.lock else None,
        )
    except BackupError as exc:
        print(f"bbot_backup_error | {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        # paths.resolve_backup_lock may also refuse D lock before we get here.
        print(f"bbot_backup_error | {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    # Guard-only self-check when invoked as module without rclone args that need I/O.
    if len(sys.argv) == 1:
        ok_lock = Path("/run/spread-bbot-backup.lock")
        cases = [
            ("backup1tb", "spread-bbot", ok_lock, True),
            ("backup1tb", "spread-bbot/journal", ok_lock, True),
            ("backup1tb", "foo/spread-compacted", ok_lock, False),
            ("backup1tb", "spread-compacted", ok_lock, False),
            ("other", "x/spread-bars/y", ok_lock, False),
            ("backup1tb", "spread-bars", ok_lock, False),
            ("backup1tb", "", ok_lock, False),
            ("backup1tb", "spread-bbot", _D_BACKUP_LOCK, False),
            ("backup1tb", "other-prefix", ok_lock, False),
        ]
        for rem, rpath, lock, expect_ok in cases:
            try:
                assert_bbot_backup_dest(remote=rem, remote_path=rpath, lock_path=lock)
                if not expect_ok:
                    print(f"FAIL expected refuse: {rem}:{rpath} lock={lock}", file=sys.stderr)
                    raise SystemExit(1)
            except BackupError as exc:
                if expect_ok:
                    print(f"FAIL expected allow: {rem}:{rpath} ({exc})", file=sys.stderr)
                    raise SystemExit(1)
        print("bbot_backup_guard_ok", flush=True)
        raise SystemExit(0)
    raise SystemExit(main())
