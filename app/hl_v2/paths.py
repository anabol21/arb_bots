"""Absolute paths for the HL v2 canary contour.

Defaults use ``/data/live_hl_v2`` siblings. ``/data/live`` is forbidden.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_HL_V2_PARQUET_ROOT = Path("/data/live_hl_v2")
DEFAULT_HL_V2_SPOOL_ROOT = Path("/data/spool_hl_v2")
DEFAULT_HL_V2_GAPS_ROOT = Path("/data/gaps_hl_v2")
DEFAULT_HL_V2_RUNTIME_LOG = Path("/var/log/spread/hl-v2-runtime.log")
DEFAULT_HL_V2_FAILED_BATCHES_LOG = Path("/var/log/spread/hl-v2-failed-batches.log")

# Path components under /data that this contour must not use.
# Compared as components so ``/data/live_hl_v2`` is not treated as ``/data/live``.
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
        "live-hl",
        "spool-hl",
        "live-hotadd-canary",
        "spool-hotadd-canary",
        "gaps-hotadd-canary",
    }
)


class HlV2PathError(ValueError):
    """HL v2 path would collide with another contour's tree."""


def assert_hl_v2_storage_path(path: Path, *, role: str, source: str) -> Path:
    """Require an absolute path outside collector, spool, and bot trees."""
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise HlV2PathError(f"HL v2 {role} from {source} must be absolute, got: {path}")
    resolved = expanded.resolve(strict=False)
    parts = resolved.parts
    if len(parts) >= 3 and parts[1] == "data":
        data_dir = parts[2]
        forbidden = data_dir in _FORBIDDEN_DATA_DIRS or data_dir.startswith("bbot-")
        if forbidden:
            raise HlV2PathError(
                f"HL v2 {role} from {source} must not use /data/{data_dir}: {resolved}"
            )
    return resolved


def assert_distinct_roots(parquet_root: Path, spool_root: Path) -> None:
    if (
        parquet_root == spool_root
        or parquet_root in spool_root.parents
        or spool_root in parquet_root.parents
    ):
        raise HlV2PathError(
            "HL v2 parquet root and spool root must be separate directories: "
            f"parquet={parquet_root} spool={spool_root}"
        )


def _resolve(
    explicit: str | Path | None,
    *,
    env_name: str,
    default: Path,
    role: str,
) -> Path:
    if explicit is not None and str(explicit).strip():
        return assert_hl_v2_storage_path(
            Path(explicit),
            role=role,
            source="flag",
        )
    env = os.environ.get(env_name)
    if env:
        return assert_hl_v2_storage_path(
            Path(env),
            role=role,
            source=env_name,
        )
    # SPREAD_PARQUET_ROOT is accepted for experiment wiring but still guarded.
    if role == "parquet root":
        spread = os.environ.get("SPREAD_PARQUET_ROOT")
        if spread:
            return assert_hl_v2_storage_path(
                Path(spread),
                role=role,
                source="SPREAD_PARQUET_ROOT",
            )
    if role == "spool root":
        spread = os.environ.get("SPREAD_SPOOL_ROOT")
        if spread:
            return assert_hl_v2_storage_path(
                Path(spread),
                role=role,
                source="SPREAD_SPOOL_ROOT",
            )
    if role == "gaps root":
        spread = os.environ.get("SPREAD_GAPS_ROOT")
        if spread:
            return assert_hl_v2_storage_path(
                Path(spread),
                role=role,
                source="SPREAD_GAPS_ROOT",
            )
    return assert_hl_v2_storage_path(default, role=role, source="default")


def resolve_parquet_root(explicit: str | Path | None = None) -> Path:
    return _resolve(
        explicit,
        env_name="HL_V2_PARQUET_ROOT",
        default=DEFAULT_HL_V2_PARQUET_ROOT,
        role="parquet root",
    )


def resolve_spool_root(explicit: str | Path | None = None) -> Path:
    return _resolve(
        explicit,
        env_name="HL_V2_SPOOL_ROOT",
        default=DEFAULT_HL_V2_SPOOL_ROOT,
        role="spool root",
    )


def resolve_gaps_root(explicit: str | Path | None = None) -> Path:
    return _resolve(
        explicit,
        env_name="HL_V2_GAPS_ROOT",
        default=DEFAULT_HL_V2_GAPS_ROOT,
        role="gaps root",
    )


def resolve_log_path(
    explicit: str | Path | None,
    *,
    env_name: str,
    default: Path | None,
) -> Path | None:
    if explicit is not None and str(explicit).strip():
        return assert_hl_v2_storage_path(Path(explicit), role="log", source="flag")
    env = os.environ.get(env_name)
    if env:
        return assert_hl_v2_storage_path(Path(env), role="log", source=env_name)
    if default is None:
        return None
    return assert_hl_v2_storage_path(default, role="log", source="default")
