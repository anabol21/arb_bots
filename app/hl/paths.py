"""Absolute paths for the Hyperliquid L1 contour.

These names are not ``SPREAD_PARQUET_ROOT`` / ``SPREAD_SPOOL_ROOT``.
The collector's live hive and spools stay untouched.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_HL_PARQUET_ROOT = Path("/data/live-hl")
DEFAULT_HL_SPOOL_ROOT = Path("/data/spool-hl")
DEFAULT_HL_RUNTIME_LOG = Path("/var/log/spread/hl-l1.log")
DEFAULT_HL_FAILED_BATCHES_LOG = Path("/var/log/spread/hl-l1-failed-batches.log")

# Path components under /data that this contour must not use.
# Compared as components so ``/data/live-hl`` is not treated as ``/data/live``.
_FORBIDDEN_DATA_DIRS: frozenset[str] = frozenset(
    {
        "live",
        "spool",
        "spool-next",
        "bars",
        "bars-next",
        "compacted",
        "gaps",
        "gaps-next",
        "bbot",
    }
)


class HlPathError(ValueError):
    """HL path would collide with another contour's tree."""


def assert_hl_storage_path(path: Path, *, role: str, source: str) -> Path:
    """Require an absolute path outside collector, spool, and bot trees."""
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise HlPathError(f"HL {role} from {source} must be absolute, got: {path}")
    resolved = expanded.resolve(strict=False)
    parts = resolved.parts
    if len(parts) >= 3 and parts[1] == "data":
        data_dir = parts[2]
        forbidden = data_dir in _FORBIDDEN_DATA_DIRS or data_dir.startswith("bbot-")
        if forbidden:
            raise HlPathError(
                f"HL {role} from {source} must not use /data/{data_dir}: {resolved}"
            )
    return resolved


def assert_distinct_hl_roots(parquet_root: Path, spool_root: Path) -> None:
    if (
        parquet_root == spool_root
        or parquet_root in spool_root.parents
        or spool_root in parquet_root.parents
    ):
        raise HlPathError(
            "HL parquet root and spool root must be separate directories: "
            f"parquet={parquet_root} spool={spool_root}"
        )


def resolve_hl_parquet_root(explicit: str | Path | None = None) -> Path:
    if explicit is not None and str(explicit).strip():
        return assert_hl_storage_path(
            Path(explicit),
            role="parquet root",
            source="--parquet-root",
        )
    env = os.environ.get("HL_PARQUET_ROOT")
    if env:
        return assert_hl_storage_path(
            Path(env),
            role="parquet root",
            source="HL_PARQUET_ROOT",
        )
    return assert_hl_storage_path(
        DEFAULT_HL_PARQUET_ROOT,
        role="parquet root",
        source="default",
    )


def resolve_hl_spool_root(explicit: str | Path | None = None) -> Path:
    if explicit is not None and str(explicit).strip():
        return assert_hl_storage_path(
            Path(explicit),
            role="spool root",
            source="--spool-root",
        )
    env = os.environ.get("HL_SPOOL_ROOT")
    if env:
        return assert_hl_storage_path(
            Path(env),
            role="spool root",
            source="HL_SPOOL_ROOT",
        )
    return assert_hl_storage_path(
        DEFAULT_HL_SPOOL_ROOT,
        role="spool root",
        source="default",
    )


def resolve_hl_log_path(
    explicit: str | Path | None,
    *,
    env_name: str,
    default: Path | None,
) -> Path | None:
    """Log file path. ``None`` means stderr only (no default file)."""
    if explicit is not None and str(explicit).strip():
        return assert_hl_storage_path(Path(explicit), role="log", source="flag")
    env = os.environ.get(env_name)
    if env:
        return assert_hl_storage_path(Path(env), role="log", source=env_name)
    if default is None:
        return None
    return assert_hl_storage_path(default, role="log", source="default")
