"""Absolute storage paths for Design A direct-to-mount writes.

Defaults target the VPS + mounted storage layout. Overrides must be absolute
paths (no cwd-relative values).
"""

from __future__ import annotations

import errno
import os
import time
from pathlib import Path

DEFAULT_STORAGE_MOUNT = Path("/mnt/storage")
DEFAULT_PARQUET_ROOT = Path("/mnt/storage/spreads_parquet_by_coins")
DEFAULT_RUNTIME_LOG = Path("/root/runtime.log")
DEFAULT_FAILED_BATCHES_LOG = Path("/root/failed_batches.log")

_ENV_PARQUET_ROOT = "SPREAD_PARQUET_ROOT"
_ENV_RUNTIME_LOG = "SPREAD_RUNTIME_LOG"
_ENV_FAILED_BATCHES_LOG = "SPREAD_FAILED_BATCHES_LOG"

_MOUNT_FAILURE_ERRNOS = {
    errno.EACCES,
    errno.EHOSTDOWN,
    errno.EHOSTUNREACH,
    errno.EIO,
    errno.ENODEV,
    errno.ENOENT,
    errno.ENOTCONN,
    errno.ENXIO,
    errno.EPERM,
    errno.EROFS,
    errno.ESTALE,
    errno.ETIMEDOUT,
}
_MOUNT_FAILURE_TEXT = (
    "input/output error",
    "no such device",
    "read-only file system",
    "stale file handle",
    "timed out",
    "transport endpoint",
)


class StorageMountError(RuntimeError):
    """The configured storage mount is absent, unhealthy, or not writable."""


def _require_absolute(path: Path, source: str) -> Path:
    if not path.is_absolute():
        raise ValueError(f"{source} must be an absolute path, got: {path}")
    return path


def assert_storage_mount_writable(
    mount_root: Path = DEFAULT_STORAGE_MOUNT,
    *,
    probe_write: bool = True,
) -> None:
    """Fail fast unless the configured storage mount is present and writable."""
    mount_root = _require_absolute(mount_root, "mount_root")
    if not mount_root.exists():
        raise StorageMountError(
            f"storage mount is missing: {mount_root}; refusing to create a local fallback"
        )
    if not mount_root.is_dir():
        raise StorageMountError(f"storage mount is not a directory: {mount_root}")
    if not mount_root.is_mount():
        raise StorageMountError(
            f"storage mount is not mounted: {mount_root}; refusing to write to the local mountpoint"
        )
    if not os.access(mount_root, os.W_OK | os.X_OK):
        raise StorageMountError(f"storage mount is not writable: {mount_root}")
    if not probe_write:
        return

    probe_path = mount_root / f".spread_mount_probe_{os.getpid()}_{time.time_ns()}"
    fd: int | None = None
    try:
        fd = os.open(probe_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, b"spread-mount-probe\n")
        os.fsync(fd)
    except OSError as exc:
        raise StorageMountError(
            f"storage mount write probe failed: {mount_root}: {exc}"
        ) from exc
    finally:
        if fd is not None:
            os.close(fd)
        try:
            probe_path.unlink(missing_ok=True)
        except OSError as exc:
            raise StorageMountError(
                f"storage mount probe cleanup failed: {probe_path}: {exc}"
            ) from exc


def is_mount_failure_error(exc: BaseException) -> bool:
    """Classify mount-loss I/O separately from schema/data corruption."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, StorageMountError):
            return True
        if isinstance(current, OSError) and current.errno in _MOUNT_FAILURE_ERRNOS:
            return True
        message = str(current).lower()
        if any(marker in message for marker in _MOUNT_FAILURE_TEXT):
            return True
        current = current.__cause__ or current.__context__
    return False


def resolve_parquet_root() -> Path:
    raw = os.environ.get(_ENV_PARQUET_ROOT)
    if raw:
        return _require_absolute(Path(raw).expanduser(), _ENV_PARQUET_ROOT)
    return DEFAULT_PARQUET_ROOT


def resolve_runtime_log_path() -> Path:
    raw = os.environ.get(_ENV_RUNTIME_LOG)
    if raw:
        return _require_absolute(Path(raw).expanduser(), _ENV_RUNTIME_LOG)
    return DEFAULT_RUNTIME_LOG


def resolve_failed_batches_log_path() -> Path:
    raw = os.environ.get(_ENV_FAILED_BATCHES_LOG)
    if raw:
        return _require_absolute(Path(raw).expanduser(), _ENV_FAILED_BATCHES_LOG)
    return DEFAULT_FAILED_BATCHES_LOG


def tmp_dir(parquet_root: Path | None = None) -> Path:
    root = parquet_root if parquet_root is not None else resolve_parquet_root()
    return root / ".tmp"


def partition_dir(parquet_root: Path, base_coin: str, event_date: str) -> Path:
    return parquet_root / f"base_coin={base_coin}" / f"event_date={event_date}"


def ensure_storage_dirs(parquet_root: Path | None = None) -> Path:
    """Create storage directories only beneath a verified mounted filesystem."""
    root = parquet_root if parquet_root is not None else resolve_parquet_root()
    root = _require_absolute(root, "parquet_root")
    try:
        root.relative_to(DEFAULT_STORAGE_MOUNT)
    except ValueError as exc:
        raise RuntimeError(
            f"parquet_root must be beneath {DEFAULT_STORAGE_MOUNT}; "
            "no fallback/staging mode is enabled"
        ) from exc

    assert_storage_mount_writable()
    staging = tmp_dir(root)
    root.mkdir(exist_ok=True)
    staging.mkdir(parents=True, exist_ok=True)
    return root
