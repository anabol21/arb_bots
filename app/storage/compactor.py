"""One-shot, crash-safe compaction of completed parquet mtime windows."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pyarrow as pa
import pyarrow.parquet as pq


DEFAULT_LIVE_ROOT = Path("/data/live")
DEFAULT_COMPACTED_ROOT = Path("/data/compacted")
DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_RETENTION_HOURS = 24.0
# Keep batch size modest so Arrow RSS stays bounded on a ~2 GiB VPS.
DEFAULT_ITER_BATCH_ROWS = 16_384


def _release_memory() -> None:
    """Return unused Arrow slabs + run GC (jemalloc often keeps RSS otherwise)."""
    try:
        pa.default_memory_pool().release_unused()
    except Exception:
        pass
    gc.collect()


@dataclass(frozen=True)
class CompactorConfig:
    live_root: Path = DEFAULT_LIVE_ROOT
    compacted_root: Path = DEFAULT_COMPACTED_ROOT
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    archive_root: Path | None = None
    retention_hours: float = DEFAULT_RETENTION_HOURS
    max_windows: int | None = None
    iter_batch_rows: int = DEFAULT_ITER_BATCH_ROWS

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        if self.retention_hours < 0:
            raise ValueError("retention_hours must be >= 0")
        if self.max_windows is not None and self.max_windows < 1:
            raise ValueError("max_windows must be >= 1 when set")
        if self.iter_batch_rows < 1:
            raise ValueError("iter_batch_rows must be >= 1")

    @property
    def resolved_archive_root(self) -> Path:
        return self.archive_root or self.live_root / "archived"


class JsonLogger:
    """Small JSON-lines logger suitable for cron output."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger

    def emit(self, level: int, event: str, **fields: Any) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": logging.getLevelName(level).lower(),
            "event": event,
            **fields,
        }
        message = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if self._logger is None:
            print(message, file=sys.stderr if level >= logging.ERROR else sys.stdout)
        else:
            self._logger.log(level, message)


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _compact_timestamp(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _window_name(start: int, end: int, generation: int = 0) -> str:
    suffix = "" if generation == 0 else f"_g{generation:06d}"
    return (
        f"spread_{_compact_timestamp(start)}_{_compact_timestamp(end)}"
        f"{suffix}.parquet"
    )


def _next_window_name(
    window_start: int,
    interval_seconds: int,
    used_outputs: set[str],
) -> str:
    generation = 0
    while True:
        candidate = _window_name(
            window_start,
            window_start + interval_seconds,
            generation,
        )
        if candidate not in used_outputs:
            return candidate
        generation += 1


def _source_metadata(path: Path, relative_path: str) -> dict[str, Any]:
    parquet = pq.ParquetFile(path)
    return {
        "path": relative_path,
        "rows": int(parquet.metadata.num_rows),
        "size": int(path.stat().st_size),
    }


def _is_candidate(path: Path, config: CompactorConfig, open_window_start: int) -> bool:
    if not path.is_file() or path.suffix != ".parquet":
        return False
    if ".tmp" in path.name or ".inprogress" in path.name:
        return False
    try:
        path.relative_to(config.resolved_archive_root)
        return False
    except ValueError:
        pass
    return int(path.stat().st_mtime) < open_window_start


def _discover_windows(
    config: CompactorConfig,
    now_epoch: float,
    claimed_paths: set[str],
    *,
    max_windows: int | None = None,
) -> dict[int, list[Path]]:
    """Group eligible live files by mtime window.

    Uses a two-pass scan when ``max_windows`` is set so path lists for later
    windows are never retained (critical with hundreds of thousands of live files).
    """
    open_window_start = int(now_epoch // config.interval_seconds) * config.interval_seconds
    windows: dict[int, list[Path]] = {}
    if not config.live_root.exists():
        return windows

    selected: set[int] | None = None
    if max_windows is not None:
        starts: set[int] = set()
        for path in config.live_root.rglob("*.parquet"):
            if not _is_candidate(path, config, open_window_start):
                continue
            relative = path.relative_to(config.live_root).as_posix()
            if relative in claimed_paths:
                continue
            starts.add(
                int(path.stat().st_mtime // config.interval_seconds)
                * config.interval_seconds
            )
        selected = set(sorted(starts)[:max_windows])
        if not selected:
            return windows

    for path in config.live_root.rglob("*.parquet"):
        if not _is_candidate(path, config, open_window_start):
            continue
        relative = path.relative_to(config.live_root).as_posix()
        if relative in claimed_paths:
            continue
        window_start = (
            int(path.stat().st_mtime // config.interval_seconds)
            * config.interval_seconds
        )
        if selected is not None and window_start not in selected:
            continue
        windows.setdefault(window_start, []).append(path)
    for paths in windows.values():
        paths.sort(key=lambda item: item.relative_to(config.live_root).as_posix())
    return windows


def _new_manifest(
    config: CompactorConfig,
    window_start: int,
    source_paths: Iterable[Path],
    output_name: str,
) -> dict[str, Any]:
    sources = [
        _source_metadata(path, path.relative_to(config.live_root).as_posix())
        for path in source_paths
    ]
    window_end = window_start + config.interval_seconds
    return {
        "version": 1,
        "window_start": window_start,
        "window_end": window_end,
        "output": output_name,
        "status": "planned",
        "sources": sources,
        "source_files_count": len(sources),
        "total_rows": sum(item["rows"] for item in sources),
        "input_bytes": sum(item["size"] for item in sources),
    }


def _load_manifests(state_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    manifests: list[tuple[Path, dict[str, Any]]] = []
    if not state_root.exists():
        return manifests
    for path in sorted(state_root.glob("spread_*.json")):
        with path.open("r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        if not isinstance(manifest, dict) or not isinstance(manifest.get("sources"), list):
            raise ValueError(f"invalid compactor manifest: {path}")
        manifests.append((path, manifest))
    return manifests


def _source_path(config: CompactorConfig, relative: str) -> Path:
    candidate = config.live_root / relative
    if candidate.resolve(strict=False).is_relative_to(config.live_root.resolve()):
        return candidate
    raise ValueError(f"source path escapes live root: {relative}")


def _validate_final(
    path: Path,
    expected_rows: int,
    expected_sha256: str | None = None,
) -> int:
    parquet = pq.ParquetFile(path)
    actual_rows = int(parquet.metadata.num_rows)
    if actual_rows != expected_rows:
        raise ValueError(
            f"compacted row mismatch: expected={expected_rows} actual={actual_rows} path={path}"
        )
    if expected_sha256 is not None:
        actual_sha256 = _sha256_file(path)
        if actual_sha256 != expected_sha256:
            raise ValueError(
                f"compacted checksum mismatch: expected={expected_sha256} "
                f"actual={actual_sha256} path={path}"
            )
    return actual_rows


def _artifact_candidates(config: CompactorConfig, output_name: str) -> list[tuple[str, Path]]:
    """Authoritative locations for a compacted output across its lifecycle."""
    return [
        ("compacted", config.compacted_root / output_name),
        ("sent", config.compacted_root / "sent" / output_name),
    ]


def _resolve_artifact(
    config: CompactorConfig,
    output_name: str,
    expected_rows: int,
    expected_sha256: str,
) -> tuple[str, Path]:
    """Validate output at compacted/ or sent/; prefer an existing valid artifact."""
    missing: list[str] = []
    last_error: Exception | None = None
    for location, path in _artifact_candidates(config, output_name):
        if not path.is_file():
            missing.append(str(path))
            continue
        try:
            _validate_final(path, expected_rows, expected_sha256)
            return location, path
        except Exception as exc:  # keep searching alternate lifecycle path
            last_error = exc
    if last_error is not None:
        raise last_error
    raise FileNotFoundError(
        "compacted output missing from lifecycle paths: " + ", ".join(missing)
    )


def _write_final(
    config: CompactorConfig,
    manifest: dict[str, Any],
    final_path: Path,
    inprogress_path: Path,
) -> tuple[int, str]:
    """Stream sources into one parquet without loading full tables in RAM.

    Uses ``ParquetFile.iter_batches`` so Arrow RSS does not accumulate across
    many sources in one process (``read()`` per source still OOMs on thick windows).
    """
    sources = manifest["sources"]
    if not sources:
        raise ValueError("refusing to compact an empty source set")

    expected_rows = int(manifest["total_rows"])
    inprogress_path.parent.mkdir(parents=True, exist_ok=True)
    inprogress_path.unlink(missing_ok=True)

    writer: pq.ParquetWriter | None = None
    written_rows = 0
    try:
        for source in sources:
            source_path = _source_path(config, source["path"])
            if not source_path.is_file():
                raise FileNotFoundError(
                    f"manifest source missing without valid final: {source_path}"
                )
            current = _source_metadata(source_path, source["path"])
            if current["rows"] != source["rows"] or current["size"] != source["size"]:
                raise ValueError(f"manifest source changed: {source_path}")

            parquet = pq.ParquetFile(source_path)
            try:
                for batch in parquet.iter_batches(batch_size=config.iter_batch_rows):
                    if writer is None:
                        writer = pq.ParquetWriter(
                            where=str(inprogress_path),
                            schema=batch.schema,
                            compression="zstd",
                        )
                    elif batch.schema != writer.schema:
                        raise ValueError(
                            f"schema mismatch while compacting {source_path}: "
                            f"{batch.schema} != {writer.schema}"
                        )
                    writer.write_batch(batch)
                    written_rows += batch.num_rows
                    del batch
            finally:
                del parquet
                _release_memory()
        if writer is None:
            raise ValueError("refusing to compact an empty source set")
        if written_rows != expected_rows:
            raise ValueError(
                f"source row mismatch: expected={expected_rows} actual={written_rows}"
            )
    except Exception:
        if writer is not None:
            writer.close()
            writer = None
        inprogress_path.unlink(missing_ok=True)
        raise
    else:
        writer.close()
        writer = None

    _fsync_file(inprogress_path)
    _validate_final(inprogress_path, expected_rows)
    os.replace(inprogress_path, final_path)
    _fsync_dir(final_path.parent)
    rows = _validate_final(final_path, expected_rows)
    _release_memory()
    return rows, _sha256_file(final_path)


def _archive_sources(config: CompactorConfig, manifest: dict[str, Any]) -> None:
    archive_root = config.resolved_archive_root
    for source in manifest["sources"]:
        relative = source["path"]
        source_path = _source_path(config, relative)
        archive_path = archive_root / relative
        if archive_path.exists():
            if source_path.exists():
                raise FileExistsError(
                    f"source and archive both exist for manifest entry: {relative}"
                )
            os.utime(archive_path, None)
            _fsync_file(archive_path)
            _fsync_dir(archive_path.parent)
            continue
        if not source_path.exists():
            # A valid final has already represented this source; absence is safe here.
            continue
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source_path, archive_path)
        os.utime(archive_path, None)
        _fsync_file(archive_path)
        _fsync_dir(source_path.parent)
        _fsync_dir(archive_path.parent)


def _process_manifest(
    config: CompactorConfig,
    state_path: Path,
    manifest: dict[str, Any],
    log: JsonLogger,
) -> None:
    started = time.monotonic()
    final_path = config.compacted_root / manifest["output"]
    inprogress_path = config.compacted_root / ".tmp" / (
        manifest["output"] + ".inprogress"
    )
    expected_rows = int(manifest["total_rows"])

    valid_final = False
    artifact_path = final_path
    expected_sha256 = manifest.get("output_sha256")
    if manifest.get("status") in {"published", "complete"}:
        if not isinstance(expected_sha256, str) or not expected_sha256:
            raise ValueError(
                f"published manifest lacks output checksum: {state_path}"
            )
        location, artifact_path = _resolve_artifact(
            config,
            str(manifest["output"]),
            expected_rows,
            expected_sha256,
        )
        manifest["artifact_location"] = location
        valid_final = True

    if not valid_final:
        _, output_sha256 = _write_final(
            config,
            manifest,
            final_path,
            inprogress_path,
        )
        manifest["output_sha256"] = output_sha256
        manifest["artifact_location"] = "compacted"
        manifest["status"] = "published"
        artifact_path = final_path
        _atomic_write_json(state_path, manifest)
        _release_memory()

    output_bytes = artifact_path.stat().st_size
    _archive_sources(config, manifest)
    manifest["status"] = "complete"
    if "artifact_location" not in manifest:
        manifest["artifact_location"] = "compacted"
    _atomic_write_json(state_path, manifest)
    _release_memory()
    duration_ms = round((time.monotonic() - started) * 1000.0, 3)
    input_bytes = int(manifest["input_bytes"])
    compaction_ratio = (
        round(input_bytes / output_bytes, 6) if output_bytes else None
    )
    compression_pct = (
        round((1.0 - output_bytes / input_bytes) * 100.0, 6)
        if input_bytes
        else None
    )
    log.emit(
        logging.INFO,
        "compaction_complete",
        window_start=manifest["window_start"],
        window_end=manifest["window_end"],
        output=manifest["output"],
        compaction_ratio=compaction_ratio,
        compression_pct=compression_pct,
        compaction_duration_ms=duration_ms,
        row_count_match=True,
        input_bytes=input_bytes,
        output_bytes=output_bytes,
        source_files_count=manifest["source_files_count"],
        rows=expected_rows,
    )


def _apply_archive_retention(
    config: CompactorConfig,
    now_epoch: float,
    archived_relatives: Iterable[str],
) -> int:
    archive_root = config.resolved_archive_root
    if not archive_root.exists():
        return 0
    cutoff = now_epoch - config.retention_hours * 3600.0
    removed = 0
    for relative in archived_relatives:
        path = archive_root / relative
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    return removed


def _prune_manifest_archives(
    config: CompactorConfig,
    now_epoch: float,
    manifest: dict[str, Any],
) -> int:
    """Prune archived sources for one complete manifest (no giant path set)."""
    return _apply_archive_retention(
        config,
        now_epoch,
        (
            source["path"]
            for source in manifest["sources"]
            if isinstance(source.get("path"), str)
        ),
    )


def _archives_gone(config: CompactorConfig, manifest: dict[str, Any]) -> bool:
    """True when no archived source path from the manifest still exists on disk."""
    archive_root = config.resolved_archive_root
    for source in manifest.get("sources") or []:
        relative = source.get("path")
        if not isinstance(relative, str):
            continue
        try:
            if (archive_root / relative).is_file():
                return False
        except OSError:
            return False
    return True


def _prune_expired_complete_states(
    config: CompactorConfig,
    now_epoch: float,
    log: JsonLogger,
) -> dict[str, int]:
    """Drop complete window manifests older than retention, one file at a time.

    Sent parquet is already pruned by backup_transfer after ``sent_retention_hours``.
    Compactor ``.state/spread_*.json`` used to live forever and eventually OOM'd
    ``MemoryMax`` when every oneshot reloaded millions of source paths. Align
    state lifetime with archive retention:

    1. only ``status=complete``;
    2. ``window_end`` older than ``retention_hours``;
    3. never while the output still sits in ``compacted/`` pending transfer;
    4. prune archives first; delete JSON only when archives are gone.

    Incomplete/published manifests are untouched. ``backup_manifest.sqlite3`` is
    not matched by ``spread_*.json``.
    """
    state_root = config.compacted_root / ".state"
    stats = {
        "examined": 0,
        "pruned_manifests": 0,
        "removed_archive_files": 0,
        "deferred_pending_compacted": 0,
        "deferred_archives_remain": 0,
        "skipped_young": 0,
        "skipped_other": 0,
        "errors": 0,
    }
    if not state_root.is_dir():
        return stats
    cutoff = now_epoch - config.retention_hours * 3600.0
    for state_path in sorted(state_root.glob("spread_*.json")):
        stats["examined"] += 1
        try:
            with state_path.open("r", encoding="utf-8") as handle:
                manifest = json.load(handle)
            if not isinstance(manifest, dict):
                stats["skipped_other"] += 1
                continue
            if manifest.get("status") != "complete":
                stats["skipped_other"] += 1
                continue
            window_end = manifest.get("window_end")
            if not isinstance(window_end, (int, float)):
                stats["skipped_other"] += 1
                continue
            if float(window_end) > cutoff:
                stats["skipped_young"] += 1
                continue
            output_name = manifest.get("output")
            if not isinstance(output_name, str) or not output_name:
                stats["skipped_other"] += 1
                continue
            pending = config.compacted_root / output_name
            if pending.is_file():
                stats["deferred_pending_compacted"] += 1
                continue
            removed = _prune_manifest_archives(config, now_epoch, manifest)
            stats["removed_archive_files"] += removed
            if not _archives_gone(config, manifest):
                stats["deferred_archives_remain"] += 1
                continue
            state_path.unlink(missing_ok=True)
            stats["pruned_manifests"] += 1
            log.emit(
                logging.INFO,
                "compaction_state_pruned",
                output=output_name,
                window_end=window_end,
                removed_archive_files=removed,
                source_files_count=manifest.get("source_files_count"),
            )
        except Exception as exc:
            stats["errors"] += 1
            log.emit(
                logging.ERROR,
                "compaction_state_prune_alert",
                path=str(state_path),
                error=repr(exc),
            )
        finally:
            if stats["examined"] % 64 == 0:
                _release_memory()
    log.emit(logging.INFO, "compaction_state_prune_complete", **stats)
    return stats


def _collect_retention_eligible(
    config: CompactorConfig,
    manifests: list[tuple[Path, dict[str, Any]]],
    log: JsonLogger,
) -> tuple[int, int]:
    """Validate complete manifests for retention-only; return (eligible_count, failures)."""
    failures = 0
    eligible_count = 0
    for state_path, manifest in manifests:
        if manifest.get("status") != "complete":
            continue
        try:
            output_sha256 = manifest.get("output_sha256")
            if not isinstance(output_sha256, str) or not output_sha256:
                raise ValueError(
                    f"complete manifest lacks output checksum: {state_path}"
                )
            try:
                location, _artifact = _resolve_artifact(
                    config,
                    str(manifest["output"]),
                    int(manifest["total_rows"]),
                    output_sha256,
                )
            except FileNotFoundError as missing_exc:
                if manifest.get("artifact_location") != "offloaded":
                    manifest["artifact_location"] = "offloaded"
                    _atomic_write_json(state_path, manifest)
                log.emit(
                    logging.INFO,
                    "compaction_artifact_offloaded",
                    output=manifest.get("output"),
                    error=repr(missing_exc),
                    rows=manifest.get("total_rows"),
                    source_files_count=manifest.get("source_files_count"),
                )
                eligible_count += len(manifest["sources"])
                continue
            if manifest.get("artifact_location") != location:
                manifest["artifact_location"] = location
                _atomic_write_json(state_path, manifest)
            eligible_count += len(manifest["sources"])
        except Exception as exc:
            failures += 1
            log.emit(
                logging.ERROR,
                "compaction_alert",
                output=manifest.get("output"),
                error=repr(exc),
                row_count_match=False,
                source_files_count=manifest.get("source_files_count"),
                rows=manifest.get("total_rows"),
            )
    return eligible_count, failures


def run_archive_retention_only(
    config: CompactorConfig,
    *,
    now_epoch: float | None = None,
    logger: logging.Logger | None = None,
) -> int:
    """Prune eligible archived sources without attempting new compaction.

    Decouples disk reclaim from successful ``compact_once`` so an OOM mid-window
    cannot block archive retention indefinitely.
    """
    now = time.time() if now_epoch is None else now_epoch
    log = JsonLogger(logger)
    state_root = config.compacted_root / ".state"
    # Stream-drop expired complete manifests before loading the remainder into RAM.
    _prune_expired_complete_states(config, now, log)
    try:
        manifests = _load_manifests(state_root)
    except Exception as exc:
        log.emit(logging.ERROR, "compaction_alert", error=repr(exc), row_count_match=False)
        return 1
    eligible_count, failures = _collect_retention_eligible(config, manifests, log)
    try:
        removed = 0
        for _state_path, manifest in manifests:
            if manifest.get("status") != "complete":
                continue
            # Only prune after artifact validation inside _collect_retention_eligible.
            # Re-resolve cheaply: skip manifests that still fail validation.
            output_sha256 = manifest.get("output_sha256")
            if not isinstance(output_sha256, str) or not output_sha256:
                continue
            try:
                _resolve_artifact(
                    config,
                    str(manifest["output"]),
                    int(manifest["total_rows"]),
                    output_sha256,
                )
            except FileNotFoundError:
                # Offloaded: still eligible (already durable remotely).
                pass
            except Exception:
                continue
            removed += _prune_manifest_archives(config, now, manifest)
        log.emit(
            logging.INFO,
            "archive_retention_complete",
            removed_files=removed,
            mode="retention_only",
            eligible_paths=eligible_count,
        )
    except Exception as exc:
        failures += 1
        log.emit(logging.ERROR, "archive_retention_alert", error=repr(exc))
    return 1 if failures else 0


def compact_once(
    config: CompactorConfig,
    *,
    now_epoch: float | None = None,
    logger: logging.Logger | None = None,
    retention_only: bool = False,
) -> int:
    """Compact completed windows once; return a process-style status code.

    When ``config.max_windows`` is set, at most that many incomplete/new windows
    are compacted in this process (for salvage and MemoryMax-bounded oneshots).
    """
    if retention_only:
        return run_archive_retention_only(
            config, now_epoch=now_epoch, logger=logger
        )

    now = time.time() if now_epoch is None else now_epoch
    log = JsonLogger(logger)
    config.compacted_root.mkdir(parents=True, exist_ok=True)
    state_root = config.compacted_root / ".state"
    state_root.mkdir(parents=True, exist_ok=True)
    (config.compacted_root / ".tmp").mkdir(parents=True, exist_ok=True)
    failures = 0
    windows_budget = config.max_windows

    # Drop expired complete state JSON one-by-one before holding the rest in RAM.
    _prune_expired_complete_states(config, now, log)

    try:
        manifests = _load_manifests(state_root)
    except Exception as exc:
        log.emit(logging.ERROR, "compaction_alert", error=repr(exc), row_count_match=False)
        return 1

    claimed = {
        source["path"]
        for _, manifest in manifests
        if manifest.get("status") != "complete"
        for source in manifest["sources"]
    }
    retention_ok: list[dict[str, Any]] = []
    used_outputs = {str(manifest["output"]) for _, manifest in manifests}
    for state_path, manifest in manifests:
        if manifest.get("status") == "complete":
            try:
                output_sha256 = manifest.get("output_sha256")
                if not isinstance(output_sha256, str) or not output_sha256:
                    raise ValueError(
                        f"complete manifest lacks output checksum: {state_path}"
                    )
                try:
                    location, _artifact = _resolve_artifact(
                        config,
                        str(manifest["output"]),
                        int(manifest["total_rows"]),
                        output_sha256,
                    )
                except FileNotFoundError as missing_exc:
                    # Complete + checksum means compaction already succeeded.
                    # After backup transfer + sent/ retention both local lifecycle
                    # paths may be gone; that must not block archive retention or
                    # spam compaction_alert (canary FNF storm).
                    if manifest.get("artifact_location") != "offloaded":
                        manifest["artifact_location"] = "offloaded"
                        _atomic_write_json(state_path, manifest)
                    log.emit(
                        logging.INFO,
                        "compaction_artifact_offloaded",
                        output=manifest.get("output"),
                        error=repr(missing_exc),
                        rows=manifest.get("total_rows"),
                        source_files_count=manifest.get("source_files_count"),
                    )
                    retention_ok.append(manifest)
                    continue
                if manifest.get("artifact_location") != location:
                    manifest["artifact_location"] = location
                    _atomic_write_json(state_path, manifest)
                retention_ok.append(manifest)
            except Exception as exc:
                failures += 1
                log.emit(
                    logging.ERROR,
                    "compaction_alert",
                    output=manifest.get("output"),
                    error=repr(exc),
                    row_count_match=False,
                    source_files_count=manifest.get("source_files_count"),
                    rows=manifest.get("total_rows"),
                )
            continue
        if windows_budget is not None and windows_budget <= 0:
            continue
        try:
            _process_manifest(config, state_path, manifest, log)
            retention_ok.append(manifest)
            if windows_budget is not None:
                windows_budget -= 1
            _release_memory()
        except Exception as exc:
            failures += 1
            if windows_budget is not None:
                windows_budget -= 1
            log.emit(
                logging.ERROR,
                "compaction_alert",
                output=manifest.get("output"),
                error=repr(exc),
                row_count_match=False,
                source_files_count=manifest.get("source_files_count"),
                rows=manifest.get("total_rows"),
            )
            _release_memory()

    if windows_budget is not None and windows_budget <= 0:
        log.emit(
            logging.INFO,
            "compaction_window_budget_exhausted",
            max_windows=config.max_windows,
            remaining_discovered=None,
            skipped_discover=True,
        )
        windows = {}
    else:
        windows = _discover_windows(
            config,
            now,
            claimed,
            max_windows=windows_budget,
        )
    for window_start in sorted(windows):
        if windows_budget is not None and windows_budget <= 0:
            log.emit(
                logging.INFO,
                "compaction_window_budget_exhausted",
                max_windows=config.max_windows,
                remaining_discovered=sum(
                    1
                    for start in windows
                    if start >= window_start
                ),
            )
            break
        output_name = _next_window_name(
            window_start,
            config.interval_seconds,
            used_outputs,
        )
        manifest = _new_manifest(
            config,
            window_start,
            windows[window_start],
            output_name,
        )
        state_path = state_root / f"{Path(manifest['output']).stem}.json"
        try:
            _atomic_write_json(state_path, manifest)
            _process_manifest(config, state_path, manifest, log)
            claimed.update(source["path"] for source in manifest["sources"])
            used_outputs.add(output_name)
            retention_ok.append(manifest)
            if windows_budget is not None:
                windows_budget -= 1
            _release_memory()
        except Exception as exc:
            claimed.update(source["path"] for source in manifest["sources"])
            used_outputs.add(output_name)
            failures += 1
            if windows_budget is not None:
                windows_budget -= 1
            log.emit(
                logging.ERROR,
                "compaction_alert",
                output=manifest["output"],
                error=repr(exc),
                row_count_match=False,
                input_bytes=manifest["input_bytes"],
                source_files_count=manifest["source_files_count"],
                rows=manifest["total_rows"],
            )
            _release_memory()

    try:
        removed = 0
        for manifest in retention_ok:
            removed += _prune_manifest_archives(config, now, manifest)
        log.emit(logging.INFO, "archive_retention_complete", removed_files=removed)
    except Exception as exc:
        failures += 1
        log.emit(logging.ERROR, "archive_retention_alert", error=repr(exc))
    return 1 if failures else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", type=Path, default=DEFAULT_LIVE_ROOT)
    parser.add_argument("--compacted", type=Path, default=DEFAULT_COMPACTED_ROOT)
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--archive", type=Path, default=None)
    parser.add_argument(
        "--retention-hours",
        type=float,
        default=DEFAULT_RETENTION_HOURS,
    )
    parser.add_argument(
        "--retention-only",
        action="store_true",
        help="Only prune eligible archived sources; do not compact new windows.",
    )
    parser.add_argument(
        "--max-windows",
        type=int,
        default=None,
        help=(
            "Limit incomplete/new windows compacted in this oneshot "
            "(salvage / MemoryMax safety)."
        ),
    )
    parser.add_argument(
        "--iter-batch-rows",
        type=int,
        default=DEFAULT_ITER_BATCH_ROWS,
        help="ParquetFile.iter_batches size (default %(default)s).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = CompactorConfig(
            live_root=args.live,
            compacted_root=args.compacted,
            interval_seconds=args.interval,
            archive_root=args.archive,
            retention_hours=args.retention_hours,
            max_windows=args.max_windows,
            iter_batch_rows=args.iter_batch_rows,
        )
        return compact_once(config, retention_only=args.retention_only)
    except Exception as exc:
        JsonLogger().emit(logging.ERROR, "compactor_fatal", error=repr(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
